"""按用户临时强制变体（override）接口与判定行为测试。"""

from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import FlagOverride

OV_PATH = "/api/v1/flags/btn_color/overrides/forced-user"


def _put(client, user_id="forced-user", **body):
    return client.put(f"/api/v1/flags/btn_color/overrides/{user_id}", json=body)


def _evaluate(client, user_id="forced-user", **body):
    body.setdefault("context", {})
    return client.post(
        f"/api/v1/flags/btn_color/evaluate", json={"user_id": user_id, **body}
    )


# ---------------------------------------------------------------------------
# PUT / DELETE 基础行为
# ---------------------------------------------------------------------------


def test_put_override_creates_then_returns_it(client, simple_flag):
    resp = _put(client, variant_key="blue", reason="人工止损：强制蓝色")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "flag_key": "btn_color",
        "user_id": "forced-user",
        "variant_key": "blue",
        "reason": "人工止损：强制蓝色",
        "expires_at": None,
        "created_at": body["created_at"],
        "updated_at": body["updated_at"],
    }
    assert body["created_at"] and body["updated_at"]
    assert body["created_at"].endswith("Z") or "+00:00" in body["created_at"]


def test_put_override_unknown_flag_404(client):
    resp = client.put(
        "/api/v1/flags/nope/overrides/u1",
        json={"variant_key": "red", "reason": "x"},
    )
    assert resp.status_code == 404


def test_put_override_unknown_variant_404(client, simple_flag):
    resp = _put(client, variant_key="ghost", reason="x")
    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]


def test_put_override_rejects_past_expires_at(client, simple_flag):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    resp = _put(client, variant_key="blue", reason="x", expires_at=past)
    assert resp.status_code == 422
    assert "未来" in resp.text


def test_put_override_rejects_naive_expires_at(client, simple_flag):
    future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None)
    resp = _put(
        client, variant_key="blue", reason="x", expires_at=future.isoformat()
    )
    assert resp.status_code == 422
    assert "UTC" in resp.text


def test_put_override_rejects_now_expires_at(client, simple_flag):
    # 恰好等于当前时刻也不允许（必须严格晚于当前）
    resp = _put(
        client,
        variant_key="blue",
        reason="x",
        expires_at=datetime.now(timezone.utc).isoformat(),
    )
    assert resp.status_code == 422


def test_put_override_accepts_future_offset_time_and_normalizes_to_utc(client, simple_flag):
    future = (datetime.now(timezone.utc) + timedelta(days=1)).astimezone(
        timezone(timedelta(hours=8))
    )
    resp = _put(
        client, variant_key="blue", reason="x", expires_at=future.isoformat()
    )
    assert resp.status_code == 200, resp.text
    expires = resp.json()["expires_at"]
    # 输出统一为 UTC
    parsed = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    assert parsed.utcoffset() == timedelta(0)


def test_put_override_replaces_previous_record(client, simple_flag):
    assert _put(client, variant_key="blue", reason="第一次").status_code == 200
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    resp = _put(client, variant_key="red", reason="第二次", expires_at=future)
    assert resp.status_code == 200
    body = resp.json()
    assert body["variant_key"] == "red"
    assert body["reason"] == "第二次"
    assert body["expires_at"] is not None

    # 库里每个 flag+user 仍只有一条
    db = SessionLocal()
    try:
        rows = db.query(FlagOverride).filter_by(
            flag_key="btn_color", user_id="forced-user"
        ).all()
        assert len(rows) == 1
    finally:
        db.close()


def test_delete_override(client, simple_flag):
    _put(client, variant_key="blue", reason="x")
    resp = client.delete(OV_PATH)
    assert resp.status_code == 204
    # 再次删除：override 已不存在 -> 404
    assert client.delete(OV_PATH).status_code == 404


def test_delete_override_unknown_flag_404(client):
    resp = client.delete("/api/v1/flags/nope/overrides/u1")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 判定行为
# ---------------------------------------------------------------------------


def test_override_skips_rules_and_buckets(client, simple_flag):
    # simple_flag: region=cn 命中规则 -> red；覆盖为 blue
    _put(client, variant_key="blue", reason="客服补偿")
    body = _evaluate(client, context={"region": "cn"}).json()
    assert body["decision_path"] == "override"
    assert body["variant_key"] == "blue"
    assert body["bucket_value"] is None
    assert body["matched_rule"] is None
    steps = [e["step"] for e in body["explanation"]]
    assert steps == ["override_loaded", "override_selected"]
    selected = body["explanation"][-1]["detail"]
    assert selected["variant_key"] == "blue"
    assert selected["reason"] == "客服补偿"


def test_override_without_rule_context_forces_too(client, simple_flag):
    # 无规则命中本该走分桶；override 同样短路
    _put(client, user_id="bucket-user-999", variant_key="red", reason="白名单")
    body = _evaluate(client, user_id="bucket-user-999", context={}).json()
    assert body["decision_path"] == "override"
    assert body["variant_key"] == "red"
    assert body["bucket_value"] is None


def test_override_other_user_unaffected(client, simple_flag):
    _put(client, user_id="forced-user", variant_key="blue", reason="x")
    # 另一个用户不受影响：region=cn 仍走规则 -> red
    body = _evaluate(client, user_id="someone-else", context={"region": "cn"}).json()
    assert body["decision_path"] == "rule"
    assert body["variant_key"] == "red"


def test_config_version_pinned_ignores_override(client, simple_flag):
    _put(client, variant_key="blue", reason="x")
    body = _evaluate(
        client, context={"region": "cn"}, config_version=1
    ).json()
    assert body["decision_path"] == "rule"
    assert body["variant_key"] == "red"


def test_expired_override_does_not_apply(client, simple_flag):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    _put(client, variant_key="blue", reason="临时", expires_at=future)
    assert _evaluate(client, context={"region": "cn"}).json()["decision_path"] == "override"

    # 直接把库里的到期时间改成过去（不经过接口校验），模拟已到期
    db = SessionLocal()
    try:
        ov = db.query(FlagOverride).filter_by(
            flag_key="btn_color", user_id="forced-user"
        ).one()
        ov.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        if ov.expires_at.tzinfo is not None:
            ov.expires_at = ov.expires_at.replace(tzinfo=None)
        db.commit()
    finally:
        db.close()

    body = _evaluate(client, context={"region": "cn"}).json()
    assert body["decision_path"] == "rule"
    assert body["variant_key"] == "red"


def test_override_variant_removed_by_config_update_does_not_apply(client, simple_flag):
    _put(client, variant_key="red", reason="钉在 red")
    assert _evaluate(client).json()["variant_key"] == "red"

    # 更新配置：移除 red（只剩 blue/green），override 记录仍在但不生效
    update = {
        "flag_key": "btn_color",
        "variants": [
            {"key": "blue", "weight": 50},
            {"key": "green", "weight": 50},
        ],
        "rules": [],
    }
    assert client.post("/api/v1/flags", json=update).status_code == 201
    body = _evaluate(client).json()
    assert body["decision_path"] == "bucket"
    assert body["variant_key"] in ("blue", "green")

    # 变体重新加回当前配置后，记录自动重新生效
    update["variants"] = [
        {"key": "red", "weight": 50},
        {"key": "blue", "weight": 50},
    ]
    assert client.post("/api/v1/flags", json=update).status_code == 201
    body = _evaluate(client).json()
    assert body["decision_path"] == "override"
    assert body["variant_key"] == "red"


def test_delete_flag_removes_its_overrides(client, simple_flag):
    _put(client, variant_key="blue", reason="x")
    assert client.delete("/api/v1/flags/btn_color").status_code == 204
    db = SessionLocal()
    try:
        assert db.query(FlagOverride).filter_by(flag_key="btn_color").count() == 0
    finally:
        db.close()
    # 同名重建后，旧 override 不存在：PUT 可重新写，DELETE 返回 404
    payload = {
        "flag_key": "btn_color",
        "variants": [{"key": "red", "weight": 50}, {"key": "blue", "weight": 50}],
        "rules": [],
    }
    client.post("/api/v1/flags", json=payload)
    assert client.delete(OV_PATH).status_code == 404


# ---------------------------------------------------------------------------
# 快照固化与幂等重放
# ---------------------------------------------------------------------------


def test_override_snapshot_persisted_and_queryable(client, simple_flag):
    _put(client, variant_key="blue", reason="客服补偿")
    decision = _evaluate(client, context={"region": "cn"}).json()
    rid = decision["request_id"]

    snap = client.get(f"/api/v1/decisions/{rid}").json()
    assert snap["decision_path"] == "override"
    assert snap["variant_key"] == "blue"
    assert snap["bucket_value"] is None
    assert snap["matched_rule_index"] is None
    steps = [e["step"] for e in snap["explanation"]]
    assert steps == ["override_loaded", "override_selected"]
    assert snap["explanation"][0]["detail"]["reason"] == "客服补偿"

    # 审计列表同样返回 override 路径
    items = client.get(
        "/api/v1/decisions", params={"flag_key": "btn_color", "user_id": "forced-user"}
    ).json()["items"]
    assert items[0]["decision_path"] == "override"


def test_replay_same_request_id_after_override_changed(client, simple_flag):
    _put(client, variant_key="blue", reason="第一次覆盖")
    payload = {"user_id": "forced-user", "context": {"region": "cn"}, "request_id": "ov-rid-1"}
    first = client.post("/api/v1/flags/btn_color/evaluate", json=payload).json()
    assert first["decision_path"] == "override"
    assert first["variant_key"] == "blue"

    # 修改 override（换变体与原因）后，用同一 request_id 仍重放首次结果
    _put(client, variant_key="red", reason="第二次覆盖")
    second = client.post("/api/v1/flags/btn_color/evaluate", json=payload).json()
    assert second["request_id"] == "ov-rid-1"
    assert second["decision_path"] == "override"
    assert second["variant_key"] == "blue"
    assert second["explanation"] == first["explanation"]


def test_replay_same_request_id_after_override_deleted(client, simple_flag):
    _put(client, variant_key="blue", reason="x")
    payload = {"user_id": "forced-user", "context": {"region": "cn"}, "request_id": "ov-rid-2"}
    first = client.post("/api/v1/flags/btn_color/evaluate", json=payload).json()
    assert first["variant_key"] == "blue"

    assert client.delete(OV_PATH).status_code == 204
    # 删除后正常判定回到规则 -> red；但同 request_id 仍重放首次 override 结果
    normal = client.post(
        "/api/v1/flags/btn_color/evaluate",
        json={"user_id": "forced-user", "context": {"region": "cn"}},
    ).json()
    assert normal["decision_path"] == "rule"
    assert normal["variant_key"] == "red"

    replay = client.post("/api/v1/flags/btn_color/evaluate", json=payload).json()
    assert replay["request_id"] == "ov-rid-2"
    assert replay["decision_path"] == "override"
    assert replay["variant_key"] == "blue"
    assert replay["explanation"] == first["explanation"]


def test_replay_same_request_id_after_override_expired(client, simple_flag):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    _put(client, variant_key="blue", reason="限期覆盖", expires_at=future)
    payload = {"user_id": "forced-user", "context": {}, "request_id": "ov-rid-3"}
    first = client.post("/api/v1/flags/btn_color/evaluate", json=payload).json()
    assert first["decision_path"] == "override"
    assert first["variant_key"] == "blue"

    db = SessionLocal()
    try:
        ov = db.query(FlagOverride).filter_by(
            flag_key="btn_color", user_id="forced-user"
        ).one()
        ov.expires_at = datetime(2000, 1, 1)
        db.commit()
    finally:
        db.close()

    # 到期后新判定走分桶；同 request_id 仍重放首次 override 结果
    normal = client.post(
        "/api/v1/flags/btn_color/evaluate",
        json={"user_id": "forced-user", "context": {}},
    ).json()
    assert normal["decision_path"] == "bucket"

    replay = client.post("/api/v1/flags/btn_color/evaluate", json=payload).json()
    assert replay["request_id"] == "ov-rid-3"
    assert replay["decision_path"] == "override"
    assert replay["variant_key"] == "blue"
    assert replay["explanation"] == first["explanation"]


def test_override_only_applies_to_latest_config_version_number(client, simple_flag):
    # 未指定 config_version 时快照记录的仍是当前版本号
    _put(client, variant_key="blue", reason="x")
    body = _evaluate(client).json()
    assert body["config_version"] == 1
    assert body["decision_path"] == "override"
