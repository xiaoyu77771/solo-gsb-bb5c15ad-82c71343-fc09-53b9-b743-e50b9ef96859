"""SQLAlchemy ORM 模型。"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

class FlagConfig(Base):
    """功能开关配置。rules / variants 以 JSON 文本存储。"""

    __tablename__ = "flag_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flag_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(512), default="")
    # [{ "priority": 1, "attribute": "region", "operator": "eq", "values": ["cn"],
    #    "variant_key": "red" }]
    rules_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # [{ "key": "red", "weight": 50, "description": "" }]
    variants_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # null 表示无生效窗口（老数据一律为 null）；否则存 JSON 文本：
    # {"active_from": <iso utc>|null, "active_until": <iso utc>|null,
    #  "inactive_variant_key": "off"}
    activation_window_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class FlagConfigVersion(Base):
    """开关配置的不可变历史版本。

    每次创建 / 更新开关时，在同一事务中追加一行完整配置快照；
    历史行只增不改，判定可按 version 精确重放当时的规则与权重。
    """

    __tablename__ = "flag_config_versions"
    __table_args__ = (
        UniqueConstraint("flag_key", "version", name="uq_flag_config_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flag_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(512), default="")
    rules_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    variants_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # 与创建该版本时的当前配置一致；老数据为 null（视为无窗口）
    activation_window_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class FlagOverride(Base):
    """按用户的临时强制变体（override）。

    仅在判定未指定 ``config_version`` 时参与：未过期且 ``variant_key``
    仍存在于当前配置的记录会跳过规则与分桶，直接返回指定变体
    （``decision_path="override"``）。到期或变体已被移除的记录保留在库中
    但不生效（变体再次加回配置时记录自动重新生效）。
    """

    __tablename__ = "flag_overrides"
    __table_args__ = (
        UniqueConstraint("flag_key", "user_id", name="uq_flag_override_flag_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flag_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    variant_key: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    # null 表示不过期；否则存 naive UTC，判定时与当前 UTC 时间比较
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ReleaseBatch(Base):
    """多开关发布批次：一次预览、一次原子提交、提交幂等。

    创建（POST /api/v1/releases）时仅落一条 ``pending`` 记录：
    候选配置与逐开关影响预览固化在 JSON 列中，不改动任何开关当前配置。
    提交时在单一事务内对每个 flag_key 做
    ``UPDATE ... WHERE flag_key=? AND version=expected_version`` 的
    条件更新并追加不可变版本；任一条件更新未命中即回滚整批（409）。
    同一批次重复提交直接返回首次提交结果（幂等）。
    """

    __tablename__ = "release_batches"

    release_id: Mapped[str] = mapped_column(
        String(64), primary_key=True
    )
    # pending：已生成预览、尚未提交；committed：已原子发布
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    # 逐开关候选/预览结果，保持请求顺序：
    # [{"flag_key","description","expected_version","current_version",
    #   "rules_json","variants_json","activation_window_json",
    #   "committed_version"(提交后补),
    #   "preview": {"total_users","changed_users","changes",
    #               "migration_counts"}}]
    items_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DecisionSnapshot(Base):
    """一次判定请求的完整快照，可按 request_id 回查。"""

    __tablename__ = "decision_snapshots"
    __table_args__ = (
        # 审计列表：(decided_at, request_id) 倒序键集分页
        Index("ix_decision_snapshots_decided_at_request_id", "decided_at", "request_id"),
        # 组合过滤 + 同一倒序键集分页
        Index("ix_decision_snapshots_flag_decided", "flag_key", "decided_at", "request_id"),
        Index("ix_decision_snapshots_user_decided", "user_id", "decided_at", "request_id"),
        Index("ix_decision_snapshots_variant_decided", "variant_key", "decided_at", "request_id"),
        Index(
            "ix_decision_snapshots_flag_user_decided",
            "flag_key",
            "user_id",
            "decided_at",
            "request_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    flag_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    request_context_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    bucket_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_rule_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    variant_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # 判定路径：rule / bucket / override。老数据由 ensure_snapshot_decision_path
    # 按 matched_rule_index 是否为空回填。
    decision_path: Mapped[str | None] = mapped_column(String(16), nullable=True)
    explanation_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
