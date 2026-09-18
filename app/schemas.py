"""请求 / 响应数据模型（Pydantic v2）。"""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Operator = Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "prefix", "suffix", "contains"]


class RuleIn(BaseModel):
    """有序属性规则。priority 越小优先级越高。"""

    priority: int = Field(ge=1, description="优先级，数字越小越先判定")
    attribute: str = Field(min_length=1, description="要检查的用户属性名，如 region / age")
    operator: Operator = "eq"
    values: list[Any] = Field(default_factory=list, description="比较值；eq/ne/gt 等取第一个")
    variant_key: str = Field(min_length=1, description="命中后返回的变体 key")

    @field_validator("values")
    @classmethod
    def _values_not_empty(cls, v: list[Any]) -> list[Any]:
        if not v:
            raise ValueError("规则必须提供至少一个比较值 values")
        return v


class VariantIn(BaseModel):
    """加权变体。所有权重之和必须等于 100。"""

    key: str = Field(min_length=1)
    weight: int = Field(ge=0, le=100, description="桶权重，全部变体之和必须为 100")
    description: str = ""


class ActivationWindowIn(BaseModel):
    """版本化生效窗口：判定按 ``[active_from, active_until)`` 生效。

    至少提供 ``active_from`` / ``active_until`` 之一；时间必须带时区，
    服务统一转为 UTC 存储；两个边界同时提供时前者必须早于后者。
    """

    active_from: datetime | None = Field(
        default=None, description="生效起点（含），ISO 8601 且带时区，统一转 UTC"
    )
    active_until: datetime | None = Field(
        default=None, description="生效终点（不含），ISO 8601 且带时区，统一转 UTC"
    )
    inactive_variant_key: str = Field(
        min_length=1, max_length=128, description="窗口外返回的兜底变体 key"
    )

    @field_validator("active_from", "active_until")
    @classmethod
    def _must_be_tz_aware_utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("时间必须带时区，例如 2026-09-20T08:00:00Z")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _window_consistent(self) -> "ActivationWindowIn":
        if self.active_from is None and self.active_until is None:
            raise ValueError("activation_window 至少需要 active_from 或 active_until 之一")
        if (
            self.active_from is not None
            and self.active_until is not None
            and self.active_from >= self.active_until
        ):
            raise ValueError("active_from 必须早于 active_until")
        return self


class FlagConfigBody(BaseModel):
    """开关配置体（variants + rules 及配套校验）。

    创建 / 更新接口与发布前影响预览共用同一套候选配置校验。
    """

    variants: list[VariantIn] = Field(min_length=2, description="至少配置两个加权变体")
    rules: list[RuleIn] = Field(default_factory=list)
    activation_window: ActivationWindowIn | None = Field(
        default=None,
        description="可选生效窗口；缺省（null）表示不设置，窗口随配置版本一同保存",
    )

    @field_validator("variants")
    @classmethod
    def _weights_sum_to_100(cls, v: list[VariantIn]) -> list[VariantIn]:
        total = sum(item.weight for item in v)
        if total != 100:
            raise ValueError(f"变体权重之和必须为 100，当前为 {total}")
        keys = [item.key for item in v]
        if len(set(keys)) != len(keys):
            raise ValueError("变体 key 不允许重复")
        return v

    @model_validator(mode="after")
    def _rules_consistent(self) -> "FlagConfigBody":
        priorities = [r.priority for r in self.rules]
        if len(set(priorities)) != len(priorities):
            raise ValueError("规则 priority 不允许重复")
        variant_keys = {v.key for v in self.variants}
        for r in self.rules:
            if r.variant_key not in variant_keys:
                raise ValueError(f"规则(priority={r.priority})引用了不存在的变体 {r.variant_key!r}")
        if (
            self.activation_window is not None
            and self.activation_window.inactive_variant_key not in variant_keys
        ):
            raise ValueError(
                f"activation_window 的兜底变体 "
                f"{self.activation_window.inactive_variant_key!r} 不存在于 variants 中"
            )
        return self


class FlagUpsertRequest(FlagConfigBody):
    flag_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    description: str = ""


class FlagResponse(BaseModel):
    flag_key: str
    description: str
    version: int
    variants: list[VariantIn]
    rules: list[RuleIn]
    activation_window: ActivationWindowIn | None = Field(
        description="当前配置的生效窗口；null 表示无窗口"
    )
    created_at: Any
    updated_at: Any


class FlagVersionResponse(BaseModel):
    """某一历史版本的完整不可变配置。"""

    flag_key: str
    version: int
    description: str
    variants: list[VariantIn]
    rules: list[RuleIn]
    activation_window: ActivationWindowIn | None = Field(
        description="该版本保存时的生效窗口；null 表示无窗口（老数据一律为 null）"
    )
    created_at: Any


class RollbackRequest(BaseModel):
    """版本回滚请求：把历史版本复制为新当前版本（乐观并发控制）。"""

    target_version: int = Field(
        ge=1, description="要复制为新当前版本的历史版本号"
    )
    expected_current_version: int = Field(
        ge=1,
        description="调用方认为的当前版本号；与实际当前版本不一致时返回 409，不改动数据",
    )


class OverrideUpsertRequest(BaseModel):
    """按用户写入 / 更新临时强制变体。

    flag_key 与 user_id 取自路径；variant_key 必须是开关当前配置中存在的
    变体（否则 404）；expires_at 可省略（表示不过期），提供时必须是
    带时区、且严格晚于当前时刻的未来 UTC 时间（否则 422）。
    """

    variant_key: str = Field(min_length=1, max_length=128, description="强制返回的变体 key")
    reason: str = Field(min_length=1, max_length=512, description="强制原因，随判定快照固化")
    expires_at: datetime | None = Field(
        default=None, description="可选过期时间（ISO 8601，须为未来的 UTC 时间）；省略表示不过期"
    )

    @field_validator("expires_at")
    @classmethod
    def _expires_must_be_future_utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("expires_at 必须是带时区的 UTC 时间，例如 2026-09-20T08:00:00Z")
        now = datetime.now(timezone.utc)
        if v.astimezone(timezone.utc) <= now:
            raise ValueError("expires_at 必须是晚于当前时刻的未来 UTC 时间")
        return v.astimezone(timezone.utc)


class OverrideResponse(BaseModel):
    """一条按用户临时强制变体的当前状态。"""

    flag_key: str
    user_id: str
    variant_key: str
    reason: str
    expires_at: datetime | None = Field(description="过期时间（UTC）；null 表示不过期")
    created_at: datetime
    updated_at: datetime


class EvaluateRequest(BaseModel):
    flag_key: str | None = Field(default=None, max_length=128, description="路径式调用可省略")
    user_id: str = Field(min_length=1, max_length=128)
    context: dict[str, Any] = Field(
        default_factory=dict, description="用户属性，规则按此匹配，如 {\"region\": \"cn\", \"age\": 20}"
    )
    request_id: str | None = Field(default=None, max_length=64, description="可选，幂等用；缺省自动生成")
    config_version: int | None = Field(
        default=None, ge=1, description="可选，指定使用的历史配置版本；缺省使用最新版"
    )


class ExplanationStep(BaseModel):
    step: str
    detail: dict[str, Any] = Field(default_factory=dict)


class EvaluateResponse(BaseModel):
    request_id: str
    flag_key: str
    user_id: str
    config_version: int
    matched_rule: dict[str, Any] | None = Field(
        description="命中的规则；无规则命中时为 null"
    )
    bucket_value: int | None = Field(description="0-99 的稳定哈希分桶值；规则命中时为 null")
    variant_key: str
    decision_path: Literal["rule", "bucket", "override", "inactive"]
    explanation: list[ExplanationStep]


class SnapshotResponse(BaseModel):
    request_id: str
    flag_key: str
    user_id: str
    request_context: dict[str, Any]
    config_version: int
    bucket_value: int | None
    matched_rule_index: int | None
    variant_key: str
    decision_path: Literal["rule", "bucket", "override", "inactive"]
    explanation: list[ExplanationStep]
    decided_at: Any


class DecisionListResponse(BaseModel):
    """判定快照审计列表：不透明键集游标分页。

    ``items`` 字段结构与 SnapshotResponse 完全一致，按
    (decided_at DESC, request_id DESC) 排序；无更多数据时 ``next_cursor`` 为 null。
    """

    items: list[SnapshotResponse]
    next_cursor: str | None = Field(description="下一页不透明游标；末页为 null")


class PreviewUser(BaseModel):
    """发布前影响预览的单个评估对象。"""

    user_id: str = Field(min_length=1, max_length=128)
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="用户属性，规则按此匹配，如 {\"region\": \"cn\", \"age\": 20}",
    )


class PreviewImpactRequest(FlagConfigBody):
    """发布前影响预览请求：候选配置与 1～500 个互异 user_id。

    flag_key 取路径值，候选 variants/rules 的校验与创建接口完全一致。
    """

    users: list[PreviewUser] = Field(min_length=1, max_length=500)

    @field_validator("users")
    @classmethod
    def _user_ids_unique(cls, v: list[PreviewUser]) -> list[PreviewUser]:
        ids = [u.user_id for u in v]
        if len(set(ids)) != len(ids):
            raise ValueError("user_id 不允许重复")
        return v


class PreviewOutcome(BaseModel):
    """单个配置下的判定结果（只读判定，无快照与解释链）。"""

    variant_key: str
    decision_path: Literal["rule", "bucket", "inactive"]
    matched_rule_index: int | None = Field(
        description="排序后命中的规则下标（从 0 开始）；无规则命中时为 null"
    )


class PreviewChange(BaseModel):
    """结果发生变化的单个用户（按请求顺序返回）。"""

    user_id: str
    current: PreviewOutcome
    candidate: PreviewOutcome


class PreviewImpactResponse(BaseModel):
    flag_key: str
    current_version: int = Field(description="当前线上配置版本；候选配置不会产生新版本")
    total_users: int = Field(description="输入用户数")
    changed_users: int = Field(description="结果发生变化的用户数")
    changes: list[PreviewChange]
    # {"<当前变体>": {"<候选变体>": 迁移人数, ...}, ...}，仅含非零计数
    migration_counts: dict[str, dict[str, int]]


class ReleaseCandidate(FlagConfigBody):
    """发布批次中的单个开关候选（复用 FlagConfigBody 的全部配置校验）。

    flag_key 必须指向已存在的开关；expected_version 为调用方认为的
    当前线上版本，提交时在事务内再次核对，不一致则整批 409。
    """

    flag_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    description: str = ""
    expected_version: int = Field(
        ge=1,
        description="调用方认为的当前线上版本；提交事务内再次核对，不一致整批返回 409",
    )


class ReleaseCreateRequest(BaseModel):
    """创建多开关发布批次：多个开关候选 + 一批共享预览用户。

    仅生成待提交批次与逐开关只读影响预览，不改动任何开关当前配置。
    flag_key 在同一批次中不允许重复；预览用户 1～500 个且 user_id 互异
    （与单开关 preview-impact 一致）。
    """

    candidates: list[ReleaseCandidate] = Field(min_length=1)
    users: list[PreviewUser] = Field(min_length=1, max_length=500)

    @field_validator("candidates")
    @classmethod
    def _flag_keys_unique(cls, v: list[ReleaseCandidate]) -> list[ReleaseCandidate]:
        keys = [c.flag_key for c in v]
        if len(set(keys)) != len(keys):
            raise ValueError("同一发布批次中 flag_key 不允许重复")
        return v

    @field_validator("users")
    @classmethod
    def _user_ids_unique(cls, v: list[PreviewUser]) -> list[PreviewUser]:
        ids = [u.user_id for u in v]
        if len(set(ids)) != len(ids):
            raise ValueError("user_id 不允许重复")
        return v


class ReleasePreviewSummary(BaseModel):
    """批次内单个开关的影响预览摘要（结构与 preview-impact 一致）。"""

    total_users: int
    changed_users: int
    changes: list[PreviewChange]
    migration_counts: dict[str, dict[str, int]]


class ReleaseItemResponse(BaseModel):
    """批次内单个开关的候选配置、预览摘要与提交结果。"""

    flag_key: str
    description: str
    expected_version: int = Field(description="创建批次时记录的线上版本（提交时据此核对）")
    current_version: int = Field(description="创建批次时该开关的线上版本")
    variants: list[VariantIn]
    rules: list[RuleIn]
    activation_window: ActivationWindowIn | None = Field(
        description="候选配置的生效窗口；null 表示无窗口"
    )
    preview: ReleasePreviewSummary
    committed_version: int | None = Field(
        default=None,
        description="提交成功后该开关的新版本号；批次未提交时为 null",
    )


class ReleaseResponse(BaseModel):
    """发布批次状态与逐开关预览/提交结果（创建、查询、提交共用）。"""

    release_id: str
    status: Literal["pending", "committed"]
    items: list[ReleaseItemResponse]
    created_at: Any
    committed_at: Any = Field(description="提交完成时间（UTC）；批次未提交时为 null")
