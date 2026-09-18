"""版本化生效窗口（activation_window）测试。

覆盖：
- 创建 / 更新时的窗口校验（422 场景）与 UTC 归一化；
- 窗口随当前配置与每个不可变历史版本保存、回滚复制；
- 判定：窗口外跳过 override / 规则 / 分桶，返回兜底变体与 decision_path="inactive"；
- 边界语义 [active_from, active_until)（引擎级单测）；
- request_id 幂等重放不按当前时间重算；
- 老库的幂等列迁移（老数据视为无窗口）。
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import crud
from app import engine as eng

UTC = timezone.utc


def _window_payload(*, inactive_variant_key="off", active_from=None, active_until=None):
    window = {"inactive_variant_key": inactive_variant_key}
    if active_from is not None:
        window["active_from"] = active_from
    if active_until is not None:
        window["active_until"] = active_until
    return window


def _flag_payload(*, flag_key="windowed", window=None):
    payload = {
        "flag_key": flag_key,
        "description": "带生效窗口的开关",
        "variants": [
            {"key": "on", "weight": 50},
            {"key": "off", "weight": 50},
        ],
        "rules": [
            {
                "priority": 1,
                "attribute": "region",
                "operator": "eq",
                "values": ["cn"],
                "variant_key": "on",
            }
        ],
    }
    if window is not None:
        payload["activation_window"] = window
    return payload


def _create(client, *, flag_key="windowed", window=None):
    resp = client.post("/api/v1/flags", json=_flag_payload(flag_key=flag_key, window=window))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _future_window(**kwargs):
    """窗口尚未开始：active_from 在明天。"""
    kwargs.setdefault("active_from", (datetime.now(UTC) + timedelta(days=1)).isoformat())
    return _window_payload(**kwargs)


def _past_window(**kwargs):
    """窗口已经结束：active_until 在昨天。"""
    kwargs.setdefault("active_until", (datetime.now(UTC) - timedelta(days=1)).isoformat())
    return _window_payload(**kwargs)


def _open_window(**kwargs):
    """当前正处于窗口内。"""
    kwargs.setdefault("active_from", (datetime.now(UTC) - timedelta(days=1)).isoformat())
    kwargs.setdefault("active_until", (datetime.now(UTC) + timedelta(days=1)).isoformat())
    return _window_payload(**kwargs)


def _evaluate(client, flag_key="windowed", **body):
    body.setdefault("user_id", "u1")
    body.setdefault("context", {})
    return client.post(f"/api/v1/flags/{flag_key}/evaluate", json=body)


# ---------------------------------------------------------------------------
# 创建 / 更新校验
# ---------------------------------------------------------------------------


def test_window_requires_at_least_one_boundary(client):
    payload = _flag_payload(window={"inactive_variant_key": "off"})
    resp = client.post("/api/v1/flags", json=payload)
    assert resp.status_code == 422
    assert "active_from" in resp.text or "active_until" in resp.text


def test_window_rejects_naive_timestamps(client):
    naive_from = (datetime.now(UTC) + timedelta(days=1)).replace(tzinfo=None).isoformat()
    resp = client.post(
        "/api/v1/flags",
        json=_flag_payload(window=_window_payload(active_from=naive_from)),
    )
    assert resp.status_code == 422
    assert "时区" in resp.text

    naive_until = (datetime.now(UTC) + timedelta(days=2)).replace(tzinfo=None).isoformat()
    resp = client.post(
        "/api/v1/flags",
        json=_flag_payload(window=_window_payload(active_until=naive_until)),
    )
    assert resp.status_code == 422
    assert "时区" in resp.text


def test_window_rejects_inverted_boundaries(client):
    now = datetime.now(UTC)
    window = _window_payload(
        active_from=(now + timedelta(days=2)).isoformat(),
        active_until=(now + timedelta(days=1)).isoformat(),
    )
    resp = client.post("/api/v1/flags", json=_flag_payload(window=window))
    assert resp.status_code == 422
    assert "早于" in resp.text

    # 相等也不允许（必须严格早于）
    same = (now + timedelta(days=1)).isoformat()
    window = _window_payload(active_from=same, active_until=same)
    resp = client.post("/api/v1/flags", json=_flag_payload(window=window))
    assert resp.status_code == 422


def test_window_rejects_unknown_inactive_variant(client):
    resp = client.post(
        "/api/v1/flags",
        json=_flag_payload(window=_future_window(inactive_variant_key="ghost")),
    )
    assert resp.status_code == 422
    assert "ghost" in resp.text


def test_window_requires_inactive_variant_key(client):
    window = {"active_from": (datetime.now(UTC) + timedelta(days=1)).isoformat()}
    resp = client.post("/api/v1/flags", json=_flag_payload(window=window))
    assert resp.status_code == 422


def test_window_single_boundary_accepted(client):
    only_from = _create(client, flag_key="w_from", window=_future_window())
    assert only_from["activation_window"]["active_from"] is not None
    assert only_from["activation_window"]["active_until"] is None

    only_until = _create(client, flag_key="w_until", window=_past_window())
    assert only_until["activation_window"]["active_from"] is None
    assert only_until["activation_window"]["active_until"] is not None


def test_window_times_normalized_to_utc(client):
    offset = timezone(timedelta(hours=8))
    active_from = (datetime.now(UTC) + timedelta(days=1)).astimezone(offset)
    body = _create(client, window=_window_payload(active_from=active_from.isoformat()))
    window = body["activation_window"]
    parsed = datetime.fromisoformat(window["active_from"].replace("Z", "+00:00"))
    assert parsed.utcoffset() == timedelta(0)
    assert parsed == active_from.astimezone(UTC)
    assert window["inactive_variant_key"] == "off"


def test_flag_without_window_returns_null(client):
    body = _create(client)
    assert body["activation_window"] is None
    assert client.get("/api/v1/flags/windowed").json()["activation_window"] is None


# ---------------------------------------------------------------------------
# 窗口随版本保存 / 更新清除 / 回滚复制
# ---------------------------------------------------------------------------


def test_window_saved_per_immutable_version(client):
    _create(client, window=_future_window())  # v1 带窗口
    # v2 不提供窗口 -> 整体覆盖语义下窗口被清除
    resp = client.post("/api/v1/flags", json=_flag_payload())
    assert resp.status_code == 201
    assert resp.json()["version"] == 2
    assert resp.json()["activation_window"] is None

    versions = client.get("/api/v1/flags/windowed/versions").json()
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["activation_window"]["inactive_variant_key"] == "off"
    assert versions[0]["activation_window"]["active_from"] is not None
    assert versions[1]["activation_window"] is None

    # 单个历史版本查询同样返回窗口
    v1 = client.get("/api/v1/flags/windowed/versions/1").json()
    assert v1["activation_window"]["inactive_variant_key"] == "off"


def test_window_updated_with_config(client):
    _create(client, window=_future_window())  # v1：未来窗口
    resp = client.post("/api/v1/flags", json=_flag_payload(window=_open_window()))
    assert resp.json()["version"] == 2
    current = client.get("/api/v1/flags/windowed").json()
    assert current["activation_window"]["active_until"] is not None
    # v1 的窗口不受后续更新影响
    v1 = client.get("/api/v1/flags/windowed/versions/1").json()
    assert v1["activation_window"]["active_until"] is None


def test_rollback_copies_activation_window(client):
    _create(client, window=_future_window())  # v1 带窗口
    client.post("/api/v1/flags", json=_flag_payload())  # v2 无窗口

    resp = client.post(
        "/api/v1/flags/windowed/rollback",
        json={"target_version": 1, "expected_current_version": 2},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == 3
    assert body["activation_window"]["inactive_variant_key"] == "off"
    assert body["activation_window"]["active_from"] is not None

    # 回滚产生的新版本快照同样固化窗口
    v3 = client.get("/api/v1/flags/windowed/versions/3").json()
    assert v3["activation_window"]["inactive_variant_key"] == "off"


# ---------------------------------------------------------------------------
# 判定行为
# ---------------------------------------------------------------------------


def test_evaluate_before_active_from_returns_inactive(client):
    _create(client, window=_future_window())
    resp = _evaluate(client, context={"region": "cn"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision_path"] == "inactive"
    assert body["variant_key"] == "off"  # 兜底变体，而非规则命中的 on
    assert body["matched_rule"] is None
    assert body["bucket_value"] is None
    steps = [e["step"] for e in body["explanation"]]
    assert steps == ["activation_window_loaded", "activation_window_inactive"]
    inactive_step = body["explanation"][-1]["detail"]
    assert inactive_step["boundary"] == "active_from"
    assert "尚未生效" in inactive_step["reason"]


def test_evaluate_after_active_until_returns_inactive(client):
    _create(client, window=_past_window())
    body = _evaluate(client, context={"region": "cn"}).json()
    assert body["decision_path"] == "inactive"
    assert body["variant_key"] == "off"
    inactive_step = body["explanation"][-1]["detail"]
    assert inactive_step["boundary"] == "active_until"
    assert "已结束" in inactive_step["reason"]


def test_evaluate_inside_window_keeps_normal_semantics(client):
    _create(client, window=_open_window())
    # 规则命中
    body = _evaluate(client, context={"region": "cn"}).json()
    assert body["decision_path"] == "rule"
    assert body["variant_key"] == "on"
    # 无规则上下文走分桶
    body = _evaluate(client, user_id="u9", context={}).json()
    assert body["decision_path"] == "bucket"
    assert body["variant_key"] in ("on", "off")


def test_inactive_window_skips_override(client):
    _create(client, window=_future_window())
    resp = client.put(
        "/api/v1/flags/windowed/overrides/u1",
        json={"variant_key": "on", "reason": "人工强制"},
    )
    assert resp.status_code == 200
    body = _evaluate(client).json()
    # 窗口外：override 不生效，直接返回兜底变体
    assert body["decision_path"] == "inactive"
    assert body["variant_key"] == "off"


def test_open_window_still_allows_override(client):
    _create(client, window=_open_window())
    client.put(
        "/api/v1/flags/windowed/overrides/u1",
        json={"variant_key": "on", "reason": "人工强制"},
    )
    body = _evaluate(client).json()
    assert body["decision_path"] == "override"
    assert body["variant_key"] == "on"


def test_pinned_config_version_uses_that_versions_window(client):
    _create(client, window=_future_window())  # v1 带未来窗口
    client.post("/api/v1/flags", json=_flag_payload())  # v2 无窗口

    # 缺省用最新版（v2 无窗口）：正常判定
    latest = _evaluate(client, context={"region": "cn"}).json()
    assert latest["decision_path"] == "rule"
    assert latest["config_version"] == 2

    # 钉住 v1：按 v1 的窗口判定 -> inactive
    pinned = _evaluate(client, context={"region": "cn"}, config_version=1).json()
    assert pinned["decision_path"] == "inactive"
    assert pinned["variant_key"] == "off"
    assert pinned["config_version"] == 1


def test_inactive_snapshot_persisted_and_queryable(client):
    _create(client, window=_past_window())
    body = _evaluate(client, request_id="win-req-001").json()
    assert body["decision_path"] == "inactive"

    snap = client.get("/api/v1/decisions/win-req-001").json()
    assert snap["decision_path"] == "inactive"
    assert snap["variant_key"] == "off"
    assert snap["bucket_value"] is None
    assert snap["matched_rule_index"] is None
    steps = [e["step"] for e in snap["explanation"]]
    assert steps == ["activation_window_loaded", "activation_window_inactive"]

    # 审计列表同样返回 inactive 路径
    items = client.get(
        "/api/v1/decisions", params={"flag_key": "windowed"}
    ).json()["items"]
    assert any(i["request_id"] == "win-req-001" for i in items)
    hit = next(i for i in items if i["request_id"] == "win-req-001")
    assert hit["decision_path"] == "inactive"


def test_request_id_replay_not_recomputed_by_current_time(client):
    _create(client, window=_future_window())  # v1：窗口未生效
    first = _evaluate(client, request_id="win-req-002").json()
    assert first["decision_path"] == "inactive"

    # 更新为无窗口配置后，同一 request_id 仍重放首次快照，不按当前时间重算
    client.post("/api/v1/flags", json=_flag_payload())
    replay = _evaluate(client, request_id="win-req-002").json()
    assert replay["request_id"] == "win-req-002"
    assert replay["decision_path"] == "inactive"
    assert replay["variant_key"] == "off"
    assert replay["explanation"] == first["explanation"]

    # 新的 request_id 按当前配置正常判定
    fresh = _evaluate(client, context={"region": "cn"}).json()
    assert fresh["decision_path"] == "rule"
    assert fresh["variant_key"] == "on"


def test_preview_impact_applies_candidate_window(client, simple_flag):
    # simple_flag: red/blue 50/50，规则 region=cn -> red，无窗口
    payload = {
        "variants": [
            {"key": "red", "weight": 50},
            {"key": "blue", "weight": 50},
        ],
        "rules": [],
        # 候选配置带未来窗口，兜底 blue
        "activation_window": _future_window(inactive_variant_key="blue"),
        "users": [{"user_id": "u1", "context": {"region": "cn"}}],
    }
    resp = client.post("/api/v1/flags/btn_color/preview-impact", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed_users"] == 1
    change = body["changes"][0]
    assert change["current"]["decision_path"] == "rule"
    assert change["candidate"] == {
        "variant_key": "blue",
        "decision_path": "inactive",
        "matched_rule_index": None,
    }
    assert body["migration_counts"] == {"red": {"blue": 1}}


def test_preview_impact_validates_candidate_window(client, simple_flag):
    payload = {
        "variants": [
            {"key": "red", "weight": 50},
            {"key": "blue", "weight": 50},
        ],
        "rules": [],
        "activation_window": {"inactive_variant_key": "ghost"},
        "users": [{"user_id": "u1", "context": {}}],
    }
    assert client.post("/api/v1/flags/btn_color/preview-impact", json=payload).status_code == 422


# ---------------------------------------------------------------------------
# 引擎级边界语义：[active_from, active_until)
# ---------------------------------------------------------------------------


def _eng_window(active_from=None, active_until=None):
    return eng.ActivationWindow(
        inactive_variant_key="off",
        active_from=active_from,
        active_until=active_until,
    )


def test_window_boundaries_are_start_inclusive_end_exclusive():
    start = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    window = _eng_window(active_from=start, active_until=end)

    assert eng.window_is_active(window, start) is True  # 起点含
    assert eng.window_is_active(window, start - timedelta(microseconds=1)) is False
    assert eng.window_is_active(window, end) is False  # 终点不含
    assert eng.window_is_active(window, end - timedelta(microseconds=1)) is True
    assert eng.window_is_active(window, start + (end - start) / 2) is True


def test_window_single_boundary_semantics():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    only_from = _eng_window(active_from=start)
    assert eng.window_is_active(only_from, start) is True
    assert eng.window_is_active(only_from, start + timedelta(days=3650)) is True
    assert eng.window_is_active(only_from, start - timedelta(seconds=1)) is False

    end = datetime(2026, 11, 1, tzinfo=UTC)
    only_until = _eng_window(active_until=end)
    assert eng.window_is_active(only_until, end) is False
    assert eng.window_is_active(only_until, end - timedelta(seconds=1)) is True
    assert eng.window_is_active(only_until, datetime(2000, 1, 1, tzinfo=UTC)) is True


def test_inactive_decision_explains_boundary():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = datetime(2026, 11, 1, tzinfo=UTC)
    window = _eng_window(active_from=start, active_until=end)

    before = eng.inactive_decision(window, datetime(2026, 9, 30, tzinfo=UTC))
    assert before.decision_path == "inactive"
    assert before.variant_key == "off"
    assert before.explanation[-1]["detail"]["boundary"] == "active_from"

    after = eng.inactive_decision(window, datetime(2026, 11, 1, tzinfo=UTC))
    assert after.explanation[-1]["detail"]["boundary"] == "active_until"


# ---------------------------------------------------------------------------
# 老库幂等迁移
# ---------------------------------------------------------------------------


@pytest.fixture()
def old_schema_db(tmp_path):
    """模拟本功能上线前的老库：两张配置表都没有 activation_window_json 列。"""
    db_file = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{db_file}", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE flag_configs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "flag_key VARCHAR(128) NOT NULL,"
                "description VARCHAR(512) NOT NULL,"
                "rules_json TEXT NOT NULL,"
                "variants_json TEXT NOT NULL,"
                "version INTEGER NOT NULL,"
                "created_at DATETIME,"
                "updated_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE flag_config_versions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "flag_key VARCHAR(128) NOT NULL,"
                "version INTEGER NOT NULL,"
                "description VARCHAR(512) NOT NULL,"
                "rules_json TEXT NOT NULL,"
                "variants_json TEXT NOT NULL,"
                "created_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO flag_configs (flag_key, description, rules_json, variants_json,"
                " version) VALUES ('legacy', '', '[]', '[]', 1)"
            )
        )
    Session = sessionmaker(bind=engine, future=True)
    yield Session
    engine.dispose()


def test_ensure_activation_window_columns_migrates_old_db(old_schema_db):
    db = old_schema_db()
    try:
        crud.ensure_activation_window_columns(db)
        # 幂等：重复执行不报错
        crud.ensure_activation_window_columns(db)
        for table in ("flag_configs", "flag_config_versions"):
            cols = {row[1] for row in db.execute(text(f"PRAGMA table_info({table})"))}
            assert "activation_window_json" in cols
        # 老数据为 NULL -> 视为无窗口
        raw = db.execute(
            text("SELECT activation_window_json FROM flag_configs WHERE flag_key='legacy'")
        ).scalar()
        assert raw is None
        assert crud.parse_activation_window(raw) is None
    finally:
        db.close()


def test_lifespan_migration_sequence_on_old_db(old_schema_db):
    """按 lifespan 的顺序对老库执行迁移：列补齐先于 ORM 查询（backfill）。"""
    db = old_schema_db()
    try:
        crud.ensure_activation_window_columns(db)
        # 列补齐后 ORM 查询老表不再缺列；backfill 为老开关补齐当前版本快照
        added = crud.backfill_missing_versions(db)
        assert added == 1
        ver = crud.get_flag_version(db, "legacy", 1)
        assert ver is not None
        assert ver.activation_window_json is None
        # 老开关读出为无窗口
        flag = crud.get_flag_or_none(db, "legacy")
        assert crud.parse_activation_window(flag.activation_window_json) is None
        # 整个序列幂等：再跑一轮不再新增版本
        crud.ensure_activation_window_columns(db)
        assert crud.backfill_missing_versions(db) == 0
    finally:
        db.close()


def test_parse_activation_window_roundtrip():
    from app.schemas import ActivationWindowIn

    window = ActivationWindowIn(
        active_from="2026-10-01T08:00:00+08:00",
        active_until="2026-11-01T00:00:00Z",
        inactive_variant_key="off",
    )
    raw = crud.serialize_activation_window(window)
    parsed = crud.parse_activation_window(raw)
    assert parsed.inactive_variant_key == "off"
    # +08:00 输入统一归一为 UTC
    assert parsed.active_from == datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    assert parsed.active_until == datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    # 无窗口 / 老数据
    assert crud.serialize_activation_window(None) is None
    assert crud.parse_activation_window(None) is None
    assert crud.parse_activation_window("") is None
