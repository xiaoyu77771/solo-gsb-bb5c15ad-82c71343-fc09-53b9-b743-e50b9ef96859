"""FastAPI 入口：功能开关判定服务。

路由：
- POST /api/v1/flags              创建/更新开关配置（已存在则 version+1，同事务保存不可变版本）
- GET  /api/v1/flags              列出全部开关
- GET  /api/v1/flags/{flag_key}   查看单个开关
- DELETE /api/v1/flags/{flag_key} 删除开关（版本历史一并删除）
- POST   /api/v1/segments                创建受众分群（嵌套 all/any/not 表达式，可引用其他分群）
- GET    /api/v1/segments                列出全部分群
- GET    /api/v1/segments/{segment_key}  查看单个分群
- PUT    /api/v1/segments/{segment_key}  更新分群（被引用时返回 409）
- DELETE /api/v1/segments/{segment_key}  删除分群（被引用时返回 409）
- PUT    /api/v1/flags/{flag_key}/overrides/{user_id}  写入按用户临时强制变体
- DELETE /api/v1/flags/{flag_key}/overrides/{user_id}  撤销按用户临时强制变体
- GET  /api/v1/flags/{flag_key}/versions            列出版本历史
- GET  /api/v1/flags/{flag_key}/versions/{version}  查看指定版本配置
- POST /api/v1/flags/{flag_key}/rollback  回滚：将历史版本复制为新当前版本（乐观版本检查）
- POST /api/v1/flags/{flag_key}/evaluate  判定（按路径中的 flag_key，可指定 config_version）
- POST /api/v1/flags/{flag_key}/preview-impact  发布前候选配置影响预览（只读，不落版本与快照）
- POST /api/v1/releases                创建多开关发布批次（复用校验与影响预览，不改动当前配置）
- POST /api/v1/releases/{release_id}/commit  原子提交批次（事务内再核对全部版本，幂等）
- GET  /api/v1/releases/{release_id}   查询批次状态、逐开关预览摘要与提交后版本号
- POST /api/v1/evaluate           判定（flag_key 在请求体中）
- GET  /api/v1/decisions          判定快照审计列表（不透明游标分页 + 组合过滤）
- GET  /api/v1/decisions/{request_id}     按 request_id 查询单条判定快照
- GET  /healthz                   健康检查
"""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import crud
from app import cursor as cursor_mod
from app import engine as eng
from app.database import Base, engine, get_db
from app.engine import RuleEvaluationError, decide
from app.models import FlagConfig
from app.schemas import (
    DecisionListResponse,
    EvaluateRequest,
    EvaluateResponse,
    FlagResponse,
    FlagUpsertRequest,
    FlagVersionResponse,
    OverrideResponse,
    OverrideUpsertRequest,
    PreviewChange,
    PreviewImpactRequest,
    PreviewImpactResponse,
    PreviewOutcome,
    PreviewUser,
    ReleaseCreateRequest,
    ReleaseResponse,
    RollbackRequest,
    SegmentCreateRequest,
    SegmentResponse,
    SegmentUpsertRequest,
)
from app.segments import SegmentValidationError


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    try:
        # 老库 create_all 不会追加新列 / 新索引，显式幂等补齐；
        # 列补齐必须先于任何 ORM 查询（否则老库缺列时 SELECT 直接报错）
        crud.ensure_segments_table(db)
        crud.ensure_activation_window_columns(db)
        crud.ensure_snapshot_decision_path(db)
        crud.ensure_decision_audit_indexes(db)
        crud.backfill_missing_versions(db)
    finally:
        db.close()
    _seed_demo_flags()
    yield


app = FastAPI(
    title="Feature Flag Decision API",
    version="1.0.0",
    description="有序属性规则 + 加权变体 + 稳定哈希分桶的功能开关判定服务",
    lifespan=lifespan,
)


def _seed_demo_flags() -> None:
    """首次启动时写入一个演示开关，已存在则不覆盖。"""
    db = next(get_db())
    try:
        if crud.get_flag_or_none(db, "new_checkout") is not None:
            return
        demo = FlagUpsertRequest(
            flag_key="new_checkout",
            description="新版结账页灰度：内部员工与白名单地区走规则，其余用户按 90/10 分桶",
            variants=[
                {"key": "on", "weight": 10, "description": "新版结账页"},
                {"key": "off", "weight": 90, "description": "旧版结账页"},
            ],
            rules=[
                {
                    "priority": 1,
                    "attribute": "employee",
                    "operator": "eq",
                    "values": [True],
                    "variant_key": "on",
                },
                {
                    "priority": 2,
                    "attribute": "region",
                    "operator": "in",
                    "values": ["cn-beta", "sg-beta"],
                    "variant_key": "on",
                },
            ],
        )
        crud.upsert_flag(db, demo)
    finally:
        db.close()


def _snapshot_to_response(snap, *, request_context: dict | None = None) -> dict:
    context = request_context if request_context is not None else json.loads(
        snap.request_context_json
    )
    return {
        "request_id": snap.request_id,
        "flag_key": snap.flag_key,
        "user_id": snap.user_id,
        "config_version": snap.config_version,
        "matched_rule": None,
        "bucket_value": snap.bucket_value,
        "variant_key": snap.variant_key,
        # 新快照固化判定路径（含 override）；老数据无该列时按是否命中规则推导
        "decision_path": snap.decision_path
        or ("rule" if snap.matched_rule_index is not None else "bucket"),
        "explanation": json.loads(snap.explanation_json),
        # SnapshotResponse 扩展字段
        "request_context": context,
        "matched_rule_index": snap.matched_rule_index,
        "decided_at": snap.decided_at,
    }


def _replay_response(
    db: Session, snap, flag: FlagConfig, req: EvaluateRequest
) -> EvaluateResponse:
    """从快照重建判定响应（幂等重试场景）。

    matched_rule 详情优先取快照记录的 config_version 对应的历史版本配置，
    版本记录缺失（如开关被删除重建）时回退到当前配置。
    """
    body = _snapshot_to_response(snap, request_context=req.context)
    matched_rule = None
    if snap.matched_rule_index is not None:
        ver = crud.get_flag_version(db, snap.flag_key, snap.config_version)
        rules = crud.parse_rules(ver.rules_json if ver is not None else flag.rules_json)
        if 0 <= snap.matched_rule_index < len(rules):
            r = rules[snap.matched_rule_index]
            matched_rule = {
                "rule_index": snap.matched_rule_index,
                "priority": r.priority,
                "attribute": r.attribute,
                "operator": r.operator,
                "values": r.values,
                "variant_key": r.variant_key,
                "segment_key": r.segment_key,
            }
    body["matched_rule"] = matched_rule
    allowed = EvaluateResponse.model_fields
    return EvaluateResponse(**{k: v for k, v in body.items() if k in allowed})


def _decision_response(
    request_id: str, flag: FlagConfig, req: EvaluateRequest, decision, config_version: int
) -> EvaluateResponse:
    matched_rule = None
    if decision.matched_rule is not None:
        r = decision.matched_rule
        matched_rule = {
            "rule_index": decision.matched_rule_index,
            "priority": r.priority,
            "attribute": r.attribute,
            "operator": r.operator,
            "values": r.values,
            "variant_key": r.variant_key,
            "segment_key": r.segment_key,
        }
    return EvaluateResponse(
        request_id=request_id,
        flag_key=flag.flag_key,
        user_id=req.user_id,
        config_version=config_version,
        matched_rule=matched_rule,
        bucket_value=decision.bucket_value,
        variant_key=decision.variant_key,
        decision_path=decision.decision_path,
        explanation=decision.explanation,
    )


@app.exception_handler(RuleEvaluationError)
async def rule_error_handler(request: Request, exc: RuleEvaluationError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)})


@app.exception_handler(SegmentValidationError)
async def segment_validation_error_handler(
    request: Request, exc: SegmentValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)) -> dict:
    return {"status": "ok", "snapshots": crud.count_snapshots(db)}


@app.post("/api/v1/flags", response_model=FlagResponse, status_code=status.HTTP_201_CREATED)
def create_or_update_flag(payload: FlagUpsertRequest, db: Session = Depends(get_db)):
    # segment 操作符规则引用的分群必须存在（Pydantic 只校验结构，
    # 引用存在性需查库）；不存在返回 422。
    try:
        crud.validate_flag_rule_segments(db, payload.rules)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    try:
        flag = crud.upsert_flag(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return crud.flag_to_response(flag)


@app.get("/api/v1/flags", response_model=list[FlagResponse])
def list_flags(db: Session = Depends(get_db)):
    return [crud.flag_to_response(f) for f in crud.list_flags(db)]


@app.get("/api/v1/flags/{flag_key}", response_model=FlagResponse)
def get_flag(flag_key: str, db: Session = Depends(get_db)):
    flag = crud.get_flag_or_none(db, flag_key)
    if flag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")
    return crud.flag_to_response(flag)


@app.delete("/api/v1/flags/{flag_key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_flag(flag_key: str, db: Session = Depends(get_db)) -> None:
    if not crud.delete_flag(db, flag_key):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")


# ---------------------------------------------------------------------------
# 受众分群
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/segments",
    response_model=SegmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_segment(payload: SegmentCreateRequest, db: Session = Depends(get_db)):
    """创建受众分群。

    - 表达式为可嵌套的 all/any/not 布尔组合 + condition 属性条件，
      并允许通过 segment 节点引用其他分群（判定时递归解析）；
    - 引用不存在的分群、直接 / 间接循环引用、展开深度或节点数超上限
      一律返回 ``422``（循环引用在 detail 中给出环路径）；
    - segment_key 已存在返回 ``409``。
    """
    try:
        segment = crud.create_segment(db, payload.segment_key, payload)
    except crud.SegmentKeyConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return crud.segment_to_response(segment)


@app.get("/api/v1/segments", response_model=list[SegmentResponse])
def list_segments(db: Session = Depends(get_db)):
    return [crud.segment_to_response(s) for s in crud.list_segments(db)]


@app.get("/api/v1/segments/{segment_key}", response_model=SegmentResponse)
def get_segment(segment_key: str, db: Session = Depends(get_db)):
    segment = crud.get_segment(db, segment_key)
    if segment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"分群 {segment_key!r} 不存在",
        )
    return crud.segment_to_response(segment)


@app.put("/api/v1/segments/{segment_key}", response_model=SegmentResponse)
def update_segment(
    segment_key: str, payload: SegmentUpsertRequest, db: Session = Depends(get_db)
):
    """整体覆盖分群表达式。

    - 分群不存在返回 ``404``；
    - 分群仍被其他分群或开关规则（含历史版本）引用时返回 ``409``，
      detail 列出引用方，避免既有判定（含钉住 config_version 的重放）
      静默变化——需要变更时请新建分群并迁移引用；
    - 表达式非法（引用缺失 / 循环引用 / 过深过大）返回 ``422``。
    """
    try:
        segment = crud.update_segment(db, segment_key, payload)
    except crud.SegmentNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"分群 {segment_key!r} 不存在",
        )
    except crud.SegmentInUse as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return crud.segment_to_response(segment)


@app.delete("/api/v1/segments/{segment_key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_segment(segment_key: str, db: Session = Depends(get_db)) -> None:
    """删除分群；不存在返回 ``404``，仍被引用返回 ``409``。"""
    try:
        if not crud.delete_segment(db, segment_key):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"分群 {segment_key!r} 不存在",
            )
    except crud.SegmentInUse as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@app.put(
    "/api/v1/flags/{flag_key}/overrides/{user_id}",
    response_model=OverrideResponse,
)
def put_override(
    flag_key: str, user_id: str, req: OverrideUpsertRequest, db: Session = Depends(get_db)
):
    """写入（或覆盖）按用户的临时强制变体。

    - 开关不存在返回 ``404``；variant_key 不在开关当前配置的变体中同样返回 ``404``；
    - expires_at 缺省表示不过期；提供时必须是带时区的未来 UTC 时间，否则 ``422``
      （由 Pydantic 校验）；
    - 同一 flag_key + user_id 重复 PUT 为整体覆盖（variant_key / reason / expires_at）。
    """
    flag = crud.get_flag_or_none(db, flag_key)
    if flag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")
    current_variant_keys = {v.key for v in crud.parse_variants(flag.variants_json)}
    if req.variant_key not in current_variant_keys:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"开关 {flag_key!r} 的当前配置中不存在变体 {req.variant_key!r}",
        )
    ov = crud.upsert_override(db, flag_key, user_id, req)
    return crud.override_to_response(ov)


@app.delete(
    "/api/v1/flags/{flag_key}/overrides/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_override(flag_key: str, user_id: str, db: Session = Depends(get_db)) -> None:
    """撤销按用户的临时强制变体。开关或 override 不存在均返回 ``404``。"""
    if crud.get_flag_or_none(db, flag_key) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")
    if not crud.delete_override(db, flag_key, user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"开关 {flag_key!r} 不存在用户 {user_id!r} 的临时强制变体",
        )


@app.get("/api/v1/flags/{flag_key}/versions", response_model=list[FlagVersionResponse])
def list_flag_versions(flag_key: str, db: Session = Depends(get_db)):
    if crud.get_flag_or_none(db, flag_key) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")
    return [crud.version_to_response(v) for v in crud.list_flag_versions(db, flag_key)]


@app.get("/api/v1/flags/{flag_key}/versions/{version}", response_model=FlagVersionResponse)
def get_flag_version(flag_key: str, version: int, db: Session = Depends(get_db)):
    if crud.get_flag_or_none(db, flag_key) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")
    ver = crud.get_flag_version(db, flag_key, version)
    if ver is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"开关 {flag_key!r} 没有版本 {version}",
        )
    return crud.version_to_response(ver)


@app.post(
    "/api/v1/flags/{flag_key}/rollback",
    response_model=FlagResponse,
    status_code=status.HTTP_201_CREATED,
)
def rollback_flag(
    flag_key: str, req: RollbackRequest, db: Session = Depends(get_db)
):
    """将指定历史版本复制为新的当前版本（回滚发布）。

    - 同一事务中追加新版本（version+1，内容复制自 target_version）并更新当前配置；
    - 历史版本保持不可变，回滚不删除、不覆盖任何历史行；
    - ``expected_current_version`` 与实际当前版本不一致时返回 ``409``，不改动数据；
    - 开关不存在或目标历史版本不存在返回 ``404``。
    """
    try:
        flag = crud.rollback_flag(
            db,
            flag_key,
            target_version=req.target_version,
            expected_current_version=req.expected_current_version,
        )
    except crud.NotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在"
        )
    except crud.VersionConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except crud.VersionNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return crud.flag_to_response(flag)


def _evaluate(req: EvaluateRequest, db: Session, flag_key: str) -> EvaluateResponse:
    flag = crud.get_flag_or_none(db, flag_key)
    if flag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在"
        )

    request_id = req.request_id or uuid.uuid4().hex

    # 幂等：相同 request_id 直接返回首次判定快照，保证重试结果一致
    existing = crud.get_snapshot(db, request_id)
    if existing is not None:
        if existing.flag_key != flag_key or existing.user_id != req.user_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"request_id={request_id} 已用于其他判定（flag_key/user_id 不一致）",
            )
        return _replay_response(db, existing, flag, req)

    # 解析本次判定使用的配置版本：缺省用最新版，指定时必须存在
    if req.config_version is None:
        rules_json, variants_json = flag.rules_json, flag.variants_json
        window_json = flag.activation_window_json
        config_version = flag.version
    else:
        ver = crud.get_flag_version(db, flag_key, req.config_version)
        if ver is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"开关 {flag_key!r} 没有版本 {req.config_version}",
            )
        rules_json, variants_json = ver.rules_json, ver.variants_json
        window_json = ver.activation_window_json
        config_version = ver.version

    rules = crud.parse_rules(rules_json)
    variants = crud.parse_variants(variants_json)

    # 生效窗口按所选配置版本的 [active_from, active_until) 判定：
    # 窗口外跳过 override、规则与分桶，直接返回兜底变体（decision_path="inactive"）。
    decision = None
    window = crud.parse_activation_window(window_json)
    if window is not None:
        now = datetime.now(timezone.utc)
        if not eng.window_is_active(window, now):
            decision = eng.inactive_decision(window, now)

    # 仅在未钉住历史版本时允许临时强制变体：未过期且变体仍存在于当前配置才生效；
    # 到期或变体已移除的记录不参与，继续走规则与分桶。
    if decision is None and req.config_version is None:
        active_override = crud.get_active_override(
            db, flag_key, req.user_id, {v.key for v in variants}
        )
        if active_override is not None:
            decision = eng.override_decision(active_override)

    if decision is None:
        # 分群规则在判定期递归解析引用；缺失 / 成环按不命中处理（见 app.segments）
        segment_resolver = crud.DBSegmentResolver(db)
        decision = decide(
            flag.flag_key,
            req.user_id,
            req.context,
            variants,
            rules,
            segment_resolver=segment_resolver,
        )

    try:
        crud.save_snapshot(
            db,
            request_id=request_id,
            flag_key=flag.flag_key,
            config_version=config_version,
            user_id=req.user_id,
            context=req.context,
            decision=decision,
        )
    except IntegrityError:
        # 并发下 request_id 唯一键冲突：回退到已存在的快照
        db.rollback()
        existing = crud.get_snapshot(db, request_id)
        if existing is not None:
            return _replay_response(db, existing, flag, req)
        raise

    return _decision_response(request_id, flag, req, decision, config_version)


@app.post("/api/v1/evaluate", response_model=EvaluateResponse)
def evaluate_by_body(req: EvaluateRequest, db: Session = Depends(get_db)):
    if not req.flag_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="请求体必须提供 flag_key"
        )
    return _evaluate(req, db, req.flag_key)


@app.post("/api/v1/flags/{flag_key}/evaluate", response_model=EvaluateResponse)
def evaluate_by_path(flag_key: str, req: EvaluateRequest, db: Session = Depends(get_db)):
    if req.flag_key is not None and req.flag_key != flag_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="路径 flag_key 与请求体 flag_key 不一致",
        )
    return _evaluate(req, db, flag_key)


def _build_candidate_config(
    body,
) -> tuple[list[eng.Rule], list[eng.Variant], eng.ActivationWindow | None]:
    """把候选配置体（variants/rules/activation_window，已通过 Pydantic 校验）
    构建为引擎对象；候选仅存在于请求内存中。"""
    candidate_rules = [
        eng.Rule(**r.model_dump())
        for r in sorted(body.rules, key=lambda x: x.priority)
    ]
    candidate_variants = [eng.Variant(**v.model_dump()) for v in body.variants]
    candidate_window = (
        eng.ActivationWindow(
            inactive_variant_key=body.activation_window.inactive_variant_key,
            active_from=body.activation_window.active_from,
            active_until=body.activation_window.active_until,
        )
        if body.activation_window is not None
        else None
    )
    return candidate_rules, candidate_variants, candidate_window


def _impact_preview(
    flag: FlagConfig,
    candidate_rules: list[eng.Rule],
    candidate_variants: list[eng.Variant],
    candidate_window: eng.ActivationWindow | None,
    users: list[PreviewUser],
    now: datetime,
    segment_resolver: crud.DBSegmentResolver,
) -> tuple[list[PreviewChange], dict[str, dict[str, int]]]:
    """对每个用户分别用当前线上配置与候选配置做只读判定并对比。

    返回 (发生变化的用户列表, 当前变体->候选变体迁移计数)；不写版本、
    不写快照。两侧判定共用同一判定时刻，保证窗口判定可比。
    """
    current_rules = crud.parse_rules(flag.rules_json)
    current_variants = crud.parse_variants(flag.variants_json)
    current_window = crud.parse_activation_window(flag.activation_window_json)

    def _decide_with_window(variants, rules, window, user: PreviewUser) -> eng.Decision:
        if window is not None and not eng.window_is_active(window, now):
            return eng.inactive_decision(window, now)
        return decide(
            flag.flag_key,
            user.user_id,
            user.context,
            variants,
            rules,
            segment_resolver=segment_resolver,
        )

    changes: list[PreviewChange] = []
    migration: dict[str, dict[str, int]] = {}
    for user in users:
        current_decision = _decide_with_window(
            current_variants, current_rules, current_window, user
        )
        candidate_decision = _decide_with_window(
            candidate_variants, candidate_rules, candidate_window, user
        )

        current_outcome = PreviewOutcome(
            variant_key=current_decision.variant_key,
            decision_path=current_decision.decision_path,
            matched_rule_index=current_decision.matched_rule_index,
        )
        candidate_outcome = PreviewOutcome(
            variant_key=candidate_decision.variant_key,
            decision_path=candidate_decision.decision_path,
            matched_rule_index=candidate_decision.matched_rule_index,
        )

        if (
            current_outcome.variant_key != candidate_outcome.variant_key
            or current_outcome.decision_path != candidate_outcome.decision_path
            or current_outcome.matched_rule_index != candidate_outcome.matched_rule_index
        ):
            changes.append(
                PreviewChange(
                    user_id=user.user_id,
                    current=current_outcome,
                    candidate=candidate_outcome,
                )
            )
            # 仅变体迁移计入汇总；变体不变（仅判定路径 / 命中规则变化）不算迁移
            if current_outcome.variant_key != candidate_outcome.variant_key:
                migration.setdefault(current_outcome.variant_key, {})
                bucket = migration[current_outcome.variant_key]
                bucket[candidate_outcome.variant_key] = (
                    bucket.get(candidate_outcome.variant_key, 0) + 1
                )
    return changes, migration


@app.post(
    "/api/v1/flags/{flag_key}/preview-impact",
    response_model=PreviewImpactResponse,
)
def preview_impact(
    flag_key: str, req: PreviewImpactRequest, db: Session = Depends(get_db)
) -> PreviewImpactResponse:
    """发布前评估候选配置：当前版本与候选配置分别做只读判定并对比。

    - 候选 variants/rules/activation_window 沿用创建接口的全部校验（Pydantic 层返回 422）；
    - 不写入配置版本，也不写入 DecisionSnapshot；
    - 两侧判定都应用各自配置的生效窗口（同一请求共用同一判定时刻）；
    - 仅返回 variant_key / decision_path / matched_rule_index 任一发生变化的用户，
      顺序与请求一致；汇总输入数、变化数及当前变体 -> 候选变体的迁移计数。
    """
    flag = crud.get_flag_or_none(db, flag_key)
    if flag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在"
        )

    # segment 规则引用的分群必须存在（候选仅在内存中，需查库校验）
    try:
        crud.validate_flag_rule_segments(db, req.rules)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    candidate_rules, candidate_variants, candidate_window = _build_candidate_config(req)

    # 整个请求使用同一时刻判定窗口，保证各用户结果可比
    now = datetime.now(timezone.utc)
    segment_resolver = crud.DBSegmentResolver(db)
    changes, migration = _impact_preview(
        flag,
        candidate_rules,
        candidate_variants,
        candidate_window,
        req.users,
        now,
        segment_resolver,
    )

    return PreviewImpactResponse(
        flag_key=flag_key,
        current_version=flag.version,
        total_users=len(req.users),
        changed_users=len(changes),
        changes=changes,
        migration_counts=migration,
    )


@app.post(
    "/api/v1/releases",
    response_model=ReleaseResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_release(req: ReleaseCreateRequest, db: Session = Depends(get_db)):
    """创建多开关发布批次：候选配置仅校验与预览，不改动任何当前配置。

    - 每个候选的 variants/rules/activation_window 与创建接口同结构、同校验
      （Pydantic 层返回 422）；同一批次内 flag_key 不可重复；
    - 涉及开关不存在返回 404；expected_version 与创建时刻的线上版本不一致
      立即返回 409（提交时事务内还会再次核对）；
    - 逐开关复用单开关影响预览逻辑，仅固化预览摘要到批次，不写配置版本、
      不写 DecisionSnapshot。
    """
    flags: dict[str, FlagConfig] = {}
    for cand in req.candidates:
        flag = crud.get_flag_or_none(db, cand.flag_key)
        if flag is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"开关 {cand.flag_key!r} 不存在",
            )
        if flag.version != cand.expected_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"开关 {cand.flag_key!r} 当前版本为 {flag.version}，与期望版本 "
                    f"{cand.expected_version} 不一致"
                ),
            )
        # 候选中的 segment 规则必须引用已存在的分群
        try:
            crud.validate_flag_rule_segments(db, cand.rules)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            )
        flags[cand.flag_key] = flag

    # 整个批次共用同一判定时刻：与单开关预览一致，各候选结果可比
    now = datetime.now(timezone.utc)
    segment_resolver = crud.DBSegmentResolver(db)
    items: list[dict] = []
    for cand in req.candidates:
        flag = flags[cand.flag_key]
        rules_json, variants_json, window_json = crud.serialize_config_body(cand)
        candidate_rules, candidate_variants, candidate_window = _build_candidate_config(cand)
        changes, migration = _impact_preview(
            flag,
            candidate_rules,
            candidate_variants,
            candidate_window,
            req.users,
            now,
            segment_resolver,
        )
        items.append(
            {
                "flag_key": cand.flag_key,
                "description": cand.description,
                "expected_version": cand.expected_version,
                "current_version": flag.version,
                "rules_json": rules_json,
                "variants_json": variants_json,
                "activation_window_json": window_json,
                "committed_version": None,
                "preview": {
                    "total_users": len(req.users),
                    "changed_users": len(changes),
                    "changes": [c.model_dump() for c in changes],
                    "migration_counts": migration,
                },
            }
        )

    release_id = uuid.uuid4().hex
    batch = crud.create_release(db, release_id=release_id, items=items)
    return crud.release_to_response(batch)


@app.post(
    "/api/v1/releases/{release_id}/commit",
    response_model=ReleaseResponse,
)
def commit_release(release_id: str, db: Session = Depends(get_db)):
    """提交批次：单一事务内再核对全部当前版本，全部一致才原子发布。

    - pending 批次：事务内对每个开关执行带版本谓词的条件更新，任一冲突
      （开关已删除或版本已被推进）整体回滚并返回 409，不存在部分写入；
    - committed 批次：幂等返回首次提交结果（不会再次核对、再次追加版本）；
    - 批次不存在返回 404。
    """
    batch = crud.get_release(db, release_id)
    if batch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"发布批次 {release_id!r} 不存在",
        )
    if batch.status == "committed":
        # 幂等：重复提交直接返回首次提交结果
        return crud.release_to_response(batch)
    try:
        batch = crud.commit_release(db, batch)
    except crud.VersionConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return crud.release_to_response(batch)


@app.get(
    "/api/v1/releases/{release_id}",
    response_model=ReleaseResponse,
)
def get_release(release_id: str, db: Session = Depends(get_db)):
    """返回批次状态、逐开关预览摘要；提交后含各开关的新版本号。"""
    batch = crud.get_release(db, release_id)
    if batch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"发布批次 {release_id!r} 不存在",
        )
    return crud.release_to_response(batch)


@app.get(
    "/api/v1/decisions",
    response_model=DecisionListResponse,
    summary="判定快照审计列表（不透明游标分页）",
)
def list_decisions(
    flag_key: str | None = Query(default=None, max_length=128),
    user_id: str | None = Query(default=None, max_length=128),
    variant_key: str | None = Query(default=None, max_length=128),
    decided_from: str | None = Query(default=None, description="判定时间下界（含），ISO 8601"),
    decided_to: str | None = Query(default=None, description="判定时间上界（含），ISO 8601"),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, description="上一页返回的不透明 next_cursor"),
    db: Session = Depends(get_db),
) -> DecisionListResponse:
    """按 (decided_at DESC, request_id DESC) 键集游标分页返回判定快照。

    - 支持 ``flag_key`` / ``user_id`` / ``variant_key`` 与
      ``decided_from`` / ``decided_to`` 时间区间的组合过滤；
    - ``limit`` 取 1～100，默认 20；
    - 游标记录排序边界并绑定本次过滤条件：翻页时过滤条件必须与首页一致，
      否则连同损坏游标、倒置时间区间一起返回 422；
    - 以首页请求发起时全表最大自增 id 为上界高水位，翻页期间新写入的
      快照不会进入后续页；
    - 无更多数据时 ``next_cursor`` 为 ``null``。
    """

    def _parse_dt(name: str, raw: str) -> datetime:
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{name} 必须是 ISO 8601 时间，例如 2026-09-17T08:00:00Z",
            ) from exc
        # 统一成 naive UTC 比较，与 SQLAlchemy 从 SQLite 读出的 decided_at 形态一致
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    dt_from = _parse_dt("decided_from", decided_from) if decided_from is not None else None
    dt_to = _parse_dt("decided_to", decided_to) if decided_to is not None else None
    if dt_from is not None and dt_to is not None and dt_from > dt_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="时间区间倒置：decided_from 不得晚于 decided_to",
        )

    filters = cursor_mod.CursorFilter(
        flag_key=flag_key,
        user_id=user_id,
        variant_key=variant_key,
        decided_from=dt_from,
        decided_to=dt_to,
    )

    boundary_ts = boundary_rid = None
    max_id = None
    if cursor is not None:
        if not cursor:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="游标损坏或已失效"
            )
        try:
            decoded = cursor_mod.decode_cursor(cursor)
        except cursor_mod.InvalidCursor as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"游标损坏或已失效：{exc}"
            ) from exc
        # 游标绑定本次过滤条件：任一字段不一致即拒绝（时间按规范化后的 UTC 比较）
        if decoded.filters != filters:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="游标与本次过滤条件不一致：翻页必须沿用首页的 flag_key/user_id/variant_key/decided_from/decided_to",
            )
        boundary_ts = decoded.boundary_ts
        boundary_rid = decoded.boundary_request_id
        max_id = decoded.watermark_id
    else:
        # 首页：在列表查询前冻结高水位，窗口固定为请求发起时已存在的行
        max_id = crud.max_snapshot_id(db)

    rows = list(
        crud.list_snapshots(
            db,
            flag_key=flag_key,
            user_id=user_id,
            variant_key=variant_key,
            decided_from=dt_from,
            decided_to=dt_to,
            limit=limit,
            boundary_ts=boundary_ts,
            boundary_request_id=boundary_rid,
            max_id=max_id,
        )
    )

    has_more = len(rows) > limit
    page = rows[:limit]
    # 首页游标携带上面冻结的 hwm；后续页沿用游标中的 hwm，窗口不漂移。
    # 翻页期间新写入快照的自增 id 必大于 hwm，被 id <= hwm 排除。

    next_cursor = None
    if has_more and page:
        # 有行返回时 hwm 必然非空（首页前已冻结、后续页由游标携带）
        assert max_id is not None
        last = page[-1]
        next_cursor = cursor_mod.encode_cursor(
            boundary_ts=last.decided_at,
            boundary_request_id=last.request_id,
            watermark_id=max_id,
            filters=filters,
        )

    return DecisionListResponse(
        items=[_snapshot_to_response(snap) for snap in page],
        next_cursor=next_cursor,
    )


@app.get("/api/v1/decisions/{request_id}")
def get_decision(request_id: str, db: Session = Depends(get_db)):
    snap = crud.get_snapshot(db, request_id)
    if snap is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"未找到 request_id={request_id} 的判定快照"
        )
    return _snapshot_to_response(snap)
