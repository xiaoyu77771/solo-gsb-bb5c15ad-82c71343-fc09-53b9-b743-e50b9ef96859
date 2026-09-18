"""数据读写与配置校验。"""

import json
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import delete, func, inspect, select, text, update
from sqlalchemy.orm import Session

from app import engine as eng
from app.models import (
    DecisionSnapshot,
    FlagConfig,
    FlagConfigVersion,
    FlagOverride,
    ReleaseBatch,
    _utcnow,
)
from app.schemas import (
    ActivationWindowIn,
    FlagConfigBody,
    FlagUpsertRequest,
    OverrideUpsertRequest,
    RuleIn,
    VariantIn,
)


class NotFound(Exception):
    """开关不存在。"""


class VersionNotFound(Exception):
    """指定的历史版本不存在。"""


class VersionConflict(Exception):
    """expected_current_version 与实际当前版本不一致（期望版本已过期）。"""


def serialize_config_body(payload: FlagConfigBody) -> tuple[str, str, str | None]:
    """把候选配置体（variants/rules/activation_window）序列化为存储 JSON。

    创建开关、发布批次提交共用同一份序列化，保证落库形态一致；
    rules 按 priority 升序存储。
    """
    rules_json = json.dumps(
        [r.model_dump() for r in sorted(payload.rules, key=lambda x: x.priority)],
        ensure_ascii=False,
    )
    variants_json = json.dumps([v.model_dump() for v in payload.variants], ensure_ascii=False)
    window_json = serialize_activation_window(payload.activation_window)
    return rules_json, variants_json, window_json


def parse_rules(raw: str) -> list[eng.Rule]:
    return [eng.Rule(**item) for item in json.loads(raw or "[]")]


def parse_variants(raw: str) -> list[eng.Variant]:
    return [eng.Variant(**item) for item in json.loads(raw or "[]")]


def serialize_activation_window(window: ActivationWindowIn | None) -> str | None:
    """把生效窗口序列化为 JSON 文本（时间已在校验层转为 UTC）；无窗口返回 None。"""
    if window is None:
        return None
    return json.dumps(
        {
            "active_from": window.active_from.isoformat() if window.active_from else None,
            "active_until": window.active_until.isoformat() if window.active_until else None,
            "inactive_variant_key": window.inactive_variant_key,
        },
        ensure_ascii=False,
    )


def parse_activation_window(raw: str | None) -> eng.ActivationWindow | None:
    """从 JSON 文本还原生效窗口；NULL / 空串（老数据）一律视为无窗口。"""
    if not raw:
        return None
    item = json.loads(raw)
    return eng.ActivationWindow(
        inactive_variant_key=item["inactive_variant_key"],
        active_from=(
            datetime.fromisoformat(item["active_from"]) if item.get("active_from") else None
        ),
        active_until=(
            datetime.fromisoformat(item["active_until"]) if item.get("active_until") else None
        ),
    )


def _activation_window_to_response(raw: str | None) -> dict | None:
    """把库存 JSON 还原为响应结构（带时区的 UTC 时间，序列化输出带 Z）。"""
    window = parse_activation_window(raw)
    if window is None:
        return None
    return {
        "active_from": window.active_from,
        "active_until": window.active_until,
        "inactive_variant_key": window.inactive_variant_key,
    }


def upsert_flag(db: Session, payload: FlagUpsertRequest) -> FlagConfig:
    """创建开关；已存在时版本号 +1 并整体覆盖规则/变体。

    同一事务中向 flag_config_versions 追加一份完整不可变快照。
    """
    rules_json, variants_json, window_json = serialize_config_body(payload)

    flag = db.scalar(select(FlagConfig).where(FlagConfig.flag_key == payload.flag_key))
    if flag is None:
        flag = FlagConfig(
            flag_key=payload.flag_key,
            description=payload.description,
            rules_json=rules_json,
            variants_json=variants_json,
            activation_window_json=window_json,
            version=1,
        )
        db.add(flag)
    else:
        flag.description = payload.description
        flag.rules_json = rules_json
        flag.variants_json = variants_json
        flag.activation_window_json = window_json
        flag.version += 1
    db.add(
        FlagConfigVersion(
            flag_key=payload.flag_key,
            version=flag.version,
            description=payload.description,
            rules_json=rules_json,
            variants_json=variants_json,
            activation_window_json=window_json,
        )
    )
    db.commit()
    db.refresh(flag)
    return flag


def rollback_flag(
    db: Session, flag_key: str, target_version: int, expected_current_version: int
) -> FlagConfig:
    """把指定历史版本复制为新的当前版本（回滚发布）。

    仅当开关存在、实际当前版本等于 expected_current_version、且
    target_version 历史版本存在时，在**同一事务**中：
    1. 把目标历史版本的完整配置追加为新的不可变版本（version + 1）；
    2. 更新当前配置的 description / rules / variants。

    历史版本一律只增不改；期望版本过期抛 VersionConflict（不写入、不提交），
    目标版本不存在抛 VersionNotFound。
    """
    flag = db.scalar(select(FlagConfig).where(FlagConfig.flag_key == flag_key))
    if flag is None:
        raise NotFound(flag_key)
    if flag.version != expected_current_version:
        raise VersionConflict(
            f"开关 {flag_key!r} 当前版本为 {flag.version}，与期望版本 "
            f"{expected_current_version} 不一致"
        )
    target = get_flag_version(db, flag_key, target_version)
    if target is None:
        raise VersionNotFound(
            f"开关 {flag_key!r} 没有版本 {target_version}"
        )

    flag.description = target.description
    flag.rules_json = target.rules_json
    flag.variants_json = target.variants_json
    flag.activation_window_json = target.activation_window_json
    flag.version += 1
    db.add(
        FlagConfigVersion(
            flag_key=flag_key,
            version=flag.version,
            description=target.description,
            rules_json=target.rules_json,
            variants_json=target.variants_json,
            activation_window_json=target.activation_window_json,
        )
    )
    db.commit()
    db.refresh(flag)
    return flag


def get_flag_or_none(db: Session, flag_key: str) -> FlagConfig | None:
    return db.scalar(select(FlagConfig).where(FlagConfig.flag_key == flag_key))


def list_flags(db: Session) -> Sequence[FlagConfig]:
    return db.scalars(select(FlagConfig).order_by(FlagConfig.flag_key)).all()


def delete_flag(db: Session, flag_key: str) -> bool:
    flag = get_flag_or_none(db, flag_key)
    if flag is None:
        return False
    # 版本历史随开关一起删除，避免删除后重建同 key 开关时版本号冲突
    db.execute(delete(FlagConfigVersion).where(FlagConfigVersion.flag_key == flag_key))
    # 按用户强制变体一并删除；重建后的同名开关不带任何 override
    db.execute(delete(FlagOverride).where(FlagOverride.flag_key == flag_key))
    db.delete(flag)
    db.commit()
    return True


# ---------------------------------------------------------------------------
# 按用户临时强制变体（override）
# ---------------------------------------------------------------------------


def get_override(db: Session, flag_key: str, user_id: str) -> FlagOverride | None:
    return db.scalar(
        select(FlagOverride).where(
            FlagOverride.flag_key == flag_key,
            FlagOverride.user_id == user_id,
        )
    )


def upsert_override(
    db: Session, flag_key: str, user_id: str, payload: OverrideUpsertRequest
) -> FlagOverride:
    """写入或更新某开关下某用户的临时强制变体。

    变体存在性（404）由调用方对照当前配置校验；expires_at 统一存为 naive UTC。
    """
    expires_at = (
        payload.expires_at.astimezone(timezone.utc).replace(tzinfo=None)
        if payload.expires_at is not None
        else None
    )
    ov = get_override(db, flag_key, user_id)
    if ov is None:
        ov = FlagOverride(
            flag_key=flag_key,
            user_id=user_id,
            variant_key=payload.variant_key,
            reason=payload.reason,
            expires_at=expires_at,
        )
        db.add(ov)
    else:
        ov.variant_key = payload.variant_key
        ov.reason = payload.reason
        ov.expires_at = expires_at
    db.commit()
    db.refresh(ov)
    return ov


def delete_override(db: Session, flag_key: str, user_id: str) -> bool:
    ov = get_override(db, flag_key, user_id)
    if ov is None:
        return False
    db.delete(ov)
    db.commit()
    return True


def get_active_override(
    db: Session, flag_key: str, user_id: str, variant_keys: set[str]
) -> eng.Override | None:
    """返回当前有效的 override，没有或不生效时返回 None。

    不生效（记录保留但判定继续走规则/分桶）的两种情况：
    - expires_at 已到期（null 视为永不过期）；
    - 记录的 variant_key 已不在当前配置的变体集合中（变体被移除）。
    """
    ov = get_override(db, flag_key, user_id)
    if ov is None:
        return None
    if ov.variant_key not in variant_keys:
        return None
    if ov.expires_at is not None:
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        if ov.expires_at <= now_naive:
            return None
    return eng.Override(
        variant_key=ov.variant_key,
        reason=ov.reason,
        expires_at=ov.expires_at,
    )


def override_to_response(ov: FlagOverride) -> dict:
    """序列化为 OverrideResponse；naive UTC 统一补回 tzinfo（输出带 Z）。"""
    def _aware(dt: datetime) -> datetime:
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

    return {
        "flag_key": ov.flag_key,
        "user_id": ov.user_id,
        "variant_key": ov.variant_key,
        "reason": ov.reason,
        "expires_at": _aware(ov.expires_at) if ov.expires_at is not None else None,
        "created_at": _aware(ov.created_at),
        "updated_at": _aware(ov.updated_at),
    }


def list_flag_versions(db: Session, flag_key: str) -> Sequence[FlagConfigVersion]:
    return db.scalars(
        select(FlagConfigVersion)
        .where(FlagConfigVersion.flag_key == flag_key)
        .order_by(FlagConfigVersion.version)
    ).all()


def get_flag_version(db: Session, flag_key: str, version: int) -> FlagConfigVersion | None:
    return db.scalar(
        select(FlagConfigVersion).where(
            FlagConfigVersion.flag_key == flag_key,
            FlagConfigVersion.version == version,
        )
    )


def backfill_missing_versions(db: Session) -> int:
    """为缺少版本记录的开关补齐当前版本快照（兼容本功能上线前已有的数据）。"""
    added = 0
    for flag in list_flags(db):
        if get_flag_version(db, flag.flag_key, flag.version) is None:
            db.add(
                FlagConfigVersion(
                    flag_key=flag.flag_key,
                    version=flag.version,
                    description=flag.description,
                    rules_json=flag.rules_json,
                    variants_json=flag.variants_json,
                    activation_window_json=flag.activation_window_json,
                    created_at=flag.updated_at,
                )
            )
            added += 1
    if added:
        db.commit()
    return added


def flag_to_response(flag: FlagConfig) -> dict:
    rules = [RuleIn(**item) for item in json.loads(flag.rules_json)]
    variants = [VariantIn(**item) for item in json.loads(flag.variants_json)]
    return {
        "flag_key": flag.flag_key,
        "description": flag.description,
        "version": flag.version,
        "variants": variants,
        "rules": rules,
        "activation_window": _activation_window_to_response(flag.activation_window_json),
        "created_at": flag.created_at,
        "updated_at": flag.updated_at,
    }


def version_to_response(ver: FlagConfigVersion) -> dict:
    rules = [RuleIn(**item) for item in json.loads(ver.rules_json)]
    variants = [VariantIn(**item) for item in json.loads(ver.variants_json)]
    return {
        "flag_key": ver.flag_key,
        "version": ver.version,
        "description": ver.description,
        "variants": variants,
        "rules": rules,
        "activation_window": _activation_window_to_response(ver.activation_window_json),
        "created_at": ver.created_at,
    }


def save_snapshot(
    db: Session,
    *,
    request_id: str,
    flag_key: str,
    config_version: int,
    user_id: str,
    context: dict,
    decision: eng.Decision,
) -> DecisionSnapshot:
    snapshot = DecisionSnapshot(
        request_id=request_id,
        flag_key=flag_key,
        user_id=user_id,
        request_context_json=json.dumps(context, ensure_ascii=False, sort_keys=True),
        config_version=config_version,
        bucket_value=decision.bucket_value,
        matched_rule_index=decision.matched_rule_index,
        variant_key=decision.variant_key,
        decision_path=decision.decision_path,
        explanation_json=json.dumps(decision.explanation, ensure_ascii=False),
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def get_snapshot(db: Session, request_id: str) -> DecisionSnapshot | None:
    return db.scalar(select(DecisionSnapshot).where(DecisionSnapshot.request_id == request_id))


def count_snapshots(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(DecisionSnapshot)) or 0


def max_snapshot_id(db: Session) -> int | None:
    """返回当前最大自增 id（首页发起时的高水位）；空表返回 None。"""
    return db.scalar(select(func.max(DecisionSnapshot.id)))


def ensure_snapshot_decision_path(db: Session) -> None:
    """为老库的 decision_snapshots 幂等补齐 decision_path 列并回填。

    ``Base.metadata.create_all`` 只建新表、不会向老表追加列：
    缺列时 ALTER TABLE 增加可空列，再按 matched_rule_index 是否为空把
    历史快照回填为 rule / bucket（override 为新功能，老数据不可能出现）。
    """
    columns = {c["name"] for c in inspect(db.bind).get_columns("decision_snapshots")}
    if "decision_path" in columns:
        return
    db.execute(text("ALTER TABLE decision_snapshots ADD COLUMN decision_path VARCHAR(16)"))
    db.execute(
        text(
            "UPDATE decision_snapshots "
            "SET decision_path = CASE WHEN matched_rule_index IS NOT NULL "
            "THEN 'rule' ELSE 'bucket' END "
            "WHERE decision_path IS NULL"
        )
    )
    db.commit()


def ensure_activation_window_columns(db: Session) -> None:
    """为老库幂等补齐 activation_window_json 列（flag_configs / flag_config_versions）。

    ``Base.metadata.create_all`` 只建新表、不会向老表追加列：缺列时
    ALTER TABLE 增加可空列；老数据一律为 NULL，判定视为无生效窗口。
    重复执行为空操作。
    """
    for table in ("flag_configs", "flag_config_versions"):
        columns = {c["name"] for c in inspect(db.bind).get_columns(table)}
        if "activation_window_json" in columns:
            continue
        db.execute(text(f"ALTER TABLE {table} ADD COLUMN activation_window_json TEXT"))
    db.commit()


def ensure_decision_audit_indexes(db: Session) -> None:
    """为已存在的 decision_snapshots 表幂等补齐审计列表索引。

    ``Base.metadata.create_all`` 只建新表、不会向老表追加新索引，
    因此升级启动时显式执行 CREATE INDEX IF NOT EXISTS（SQLite 与
    PostgreSQL 9.5+ 均支持），缺失才创建，已存在则为空操作。
    """
    statements = (
        "CREATE INDEX IF NOT EXISTS ix_decision_snapshots_decided_at_request_id "
        "ON decision_snapshots (decided_at, request_id)",
        "CREATE INDEX IF NOT EXISTS ix_decision_snapshots_flag_decided "
        "ON decision_snapshots (flag_key, decided_at, request_id)",
        "CREATE INDEX IF NOT EXISTS ix_decision_snapshots_user_decided "
        "ON decision_snapshots (user_id, decided_at, request_id)",
        "CREATE INDEX IF NOT EXISTS ix_decision_snapshots_variant_decided "
        "ON decision_snapshots (variant_key, decided_at, request_id)",
        "CREATE INDEX IF NOT EXISTS ix_decision_snapshots_flag_user_decided "
        "ON decision_snapshots (flag_key, user_id, decided_at, request_id)",
    )
    for ddl in statements:
        db.execute(text(ddl))
    db.commit()


def list_snapshots(
    db: Session,
    *,
    flag_key: str | None = None,
    user_id: str | None = None,
    variant_key: str | None = None,
    decided_from: datetime | None = None,
    decided_to: datetime | None = None,
    limit: int = 20,
    # 键集边界：严格位于 (decided_at < boundary_ts) 或
    # (decided_at == boundary_ts AND request_id < boundary_request_id)
    boundary_ts: datetime | None = None,
    boundary_request_id: str | None = None,
    # 上界高水位（首页请求发起时全表最大自增 id）：翻页期间新写入
    # 快照的 id 更大，一律被 id <= max_id 排除，保证翻页窗口不漂移、不重不漏。
    max_id: int | None = None,
) -> Sequence[DecisionSnapshot]:
    """按 (decided_at DESC, request_id DESC) 键集分页查询判定快照。

    多取 1 行（limit + 1）用于判断是否还有下一页；同一 decided_at 用
    request_id 做唯一并列裁决，保证同一时间戳不重不漏。
    """
    stmt = select(DecisionSnapshot)

    if flag_key is not None:
        stmt = stmt.where(DecisionSnapshot.flag_key == flag_key)
    if user_id is not None:
        stmt = stmt.where(DecisionSnapshot.user_id == user_id)
    if variant_key is not None:
        stmt = stmt.where(DecisionSnapshot.variant_key == variant_key)
    if decided_from is not None:
        stmt = stmt.where(DecisionSnapshot.decided_at >= decided_from)
    if decided_to is not None:
        stmt = stmt.where(DecisionSnapshot.decided_at <= decided_to)

    if boundary_ts is not None:
        # 必须携带成对的 request_id（游标保证）
        stmt = stmt.where(
            (DecisionSnapshot.decided_at < boundary_ts)
            | (
                (DecisionSnapshot.decided_at == boundary_ts)
                & (DecisionSnapshot.request_id < boundary_request_id)
            )
        )

    if max_id is not None:
        stmt = stmt.where(DecisionSnapshot.id <= max_id)

    stmt = stmt.order_by(
        DecisionSnapshot.decided_at.desc(), DecisionSnapshot.request_id.desc()
    ).limit(limit + 1)
    return db.scalars(stmt).all()


# ---------------------------------------------------------------------------
# 多开关发布批次
# ---------------------------------------------------------------------------


def create_release(db: Session, *, release_id: str, items: list[dict]) -> ReleaseBatch:
    """落一条 pending 批次（items 已含候选配置 JSON 与逐开关预览摘要）。

    仅写 release_batches 一张表，不触碰任何开关当前配置与版本历史。
    """
    batch = ReleaseBatch(
        release_id=release_id,
        status="pending",
        items_json=json.dumps(items, ensure_ascii=False),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def get_release(db: Session, release_id: str) -> ReleaseBatch | None:
    return db.scalar(select(ReleaseBatch).where(ReleaseBatch.release_id == release_id))


def commit_release(db: Session, batch: ReleaseBatch) -> ReleaseBatch:
    """在**单一数据库事务**内原子提交 pending 批次。

    对每个候选执行带乐观版本谓词的条件更新::

        UPDATE flag_configs
           SET description=?, rules_json=?, variants_json=?,
               activation_window_json=?, version=version+1, updated_at=?
         WHERE flag_key=? AND version=expected_version

    任一更新未命中行（开关已删除，或当前版本已被其他发布推进）即回滚
    整个事务并抛 VersionConflict——先成功的开关更新随之撤销，不存在
    部分写入。全部命中后：

    1. 为每个开关追加一条 version=expected_version+1 的不可变版本快照；
    2. 把提交后的新版本号写回批次 items，批次置为 committed。

    唯一约束 (flag_key, version) 兜底并发：两个事务不可能为同一开关
    追加同一版本号；冲突时同样整体回滚。
    """
    items = json.loads(batch.items_json)

    try:
        new_versions: dict[str, int] = {}
        for item in items:
            expected = item["expected_version"]
            result = db.execute(
                update(FlagConfig)
                .where(
                    FlagConfig.flag_key == item["flag_key"],
                    FlagConfig.version == expected,
                )
                .values(
                    description=item["description"],
                    rules_json=item["rules_json"],
                    variants_json=item["variants_json"],
                    activation_window_json=item.get("activation_window_json"),
                    version=FlagConfig.version + 1,
                    updated_at=_utcnow(),
                )
            )
            if result.rowcount != 1:
                # 不立即提交：抛出后由外层 rollback，整批零写入
                raise VersionConflict(
                    _release_conflict_message(db, item)
                )
            new_versions[item["flag_key"]] = expected + 1

        # 全部条件更新命中后，在同一事务追加不可变版本（只增不改）
        for item in items:
            db.add(
                FlagConfigVersion(
                    flag_key=item["flag_key"],
                    version=new_versions[item["flag_key"]],
                    description=item["description"],
                    rules_json=item["rules_json"],
                    variants_json=item["variants_json"],
                    activation_window_json=item.get("activation_window_json"),
                )
            )

        for item in items:
            item["committed_version"] = new_versions[item["flag_key"]]
        batch.items_json = json.dumps(items, ensure_ascii=False)
        batch.status = "committed"
        batch.committed_at = _utcnow()

        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(batch)
    return batch


def _release_conflict_message(db: Session, item: dict) -> str:
    """构造单个候选核对失败的 409 文案（区分版本推进与开关缺失）。"""
    flag = get_flag_or_none(db, item["flag_key"])
    if flag is None:
        return (
            f"开关 {item['flag_key']!r} 已不存在（创建批次时期望版本 "
            f"{item['expected_version']}）"
        )
    return (
        f"开关 {item['flag_key']!r} 当前版本为 {flag.version}，与批次期望版本 "
        f"{item['expected_version']} 不一致"
    )


def _utc_aware(dt: datetime) -> datetime:
    """naive UTC（SQLite 读出形态）补回 tzinfo，其余转 UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def release_to_response(batch: ReleaseBatch) -> dict:
    """把批次（含逐开关候选与预览摘要）序列化为响应结构。"""
    items = json.loads(batch.items_json or "[]")
    out_items = []
    for item in items:
        rules = [RuleIn(**r) for r in json.loads(item["rules_json"])]
        variants = [VariantIn(**v) for v in json.loads(item["variants_json"])]
        out_items.append(
            {
                "flag_key": item["flag_key"],
                "description": item["description"],
                "expected_version": item["expected_version"],
                "current_version": item["current_version"],
                "variants": variants,
                "rules": rules,
                "activation_window": _activation_window_to_response(
                    item.get("activation_window_json")
                ),
                "preview": item["preview"],
                "committed_version": item.get("committed_version"),
            }
        )
    return {
        "release_id": batch.release_id,
        "status": batch.status,
        "items": out_items,
        "created_at": _utc_aware(batch.created_at),
        "committed_at": _utc_aware(batch.committed_at) if batch.committed_at else None,
    }
