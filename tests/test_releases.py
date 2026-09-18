"""多开关发布批次：POST /api/v1/releases、提交原子性、幂等与查询。"""

from fastapi.testclient import TestClient


def _create_flag(client, key, *, description="v1", red=50, blue=50, rule_value="cn"):
    payload = {
        "flag_key": key,
        "description": description,
        "variants": [
            {"key": "red", "weight": red},
            {"key": "blue", "weight": blue},
        ],
        "rules": [
            {
                "priority": 1,
                "attribute": "region",
                "operator": "eq",
                "values": [rule_value],
                "variant_key": "red",
            }
        ],
    }
    resp = client.post("/api/v1/flags", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _candidate(key, expected, *, red, blue, description="batch"):
    return {
        "flag_key": key,
        "description": description,
        "expected_version": expected,
        "variants": [
            {"key": "red", "weight": red},
            {"key": "blue", "weight": blue},
        ],
        "rules": [],
    }


def test_create_release_previews_without_changing_config(client: TestClient):
    f1 = _create_flag(client, "btn_color", rule_value="cn")
    f2 = _create_flag(client, "header_color", rule_value="jp")

    resp = client.post(
        "/api/v1/releases",
        json={
            "candidates": [
                _candidate("btn_color", f1["version"], red=0, blue=100),
                _candidate("header_color", f2["version"], red=0, blue=100),
            ],
            "users": [
                {"user_id": "u1", "context": {}},
                {"user_id": "u2", "context": {"region": "cn"}},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["committed_at"] is None
    assert len(body["release_id"]) == 32
    assert [it["flag_key"] for it in body["items"]] == ["btn_color", "header_color"]

    item = body["items"][0]
    assert item["expected_version"] == 1
    assert item["current_version"] == 1
    assert item["committed_version"] is None
    assert item["preview"]["total_users"] == 2
    # u1 原走分桶 red(50/50，不依赖权重) / u2 命中规则 red；候选全蓝后都迁移
    assert item["preview"]["changed_users"] >= 1
    # 候选配置体回显
    assert {v["key"]: v["weight"] for v in item["variants"]} == {"red": 0, "blue": 100}

    # 创建批次不改动任何当前配置与版本历史
    assert client.get("/api/v1/flags/btn_color").json()["version"] == 1
    versions = client.get("/api/v1/flags/btn_color/versions").json()
    assert [v["version"] for v in versions] == [1]
    versions2 = client.get("/api/v1/flags/header_color/versions").json()
    assert [v["version"] for v in versions2] == [1]


def test_get_pending_release_returns_same_body(client: TestClient):
    f1 = _create_flag(client, "btn_color")
    created = client.post(
        "/api/v1/releases",
        json={
            "candidates": [_candidate("btn_color", f1["version"], red=100, blue=0)],
            "users": [{"user_id": "u1", "context": {}}],
        },
    ).json()

    got = client.get(f"/api/v1/releases/{created['release_id']}").json()
    assert got == created


def test_get_unknown_release_returns_404(client: TestClient):
    resp = client.get("/api/v1/releases/deadbeef")
    assert resp.status_code == 404


def test_commit_publishes_all_flags_in_one_transaction(client: TestClient):
    f1 = _create_flag(client, "btn_color", rule_value="cn")
    f2 = _create_flag(client, "header_color", rule_value="jp")

    created = client.post(
        "/api/v1/releases",
        json={
            "candidates": [
                _candidate("btn_color", f1["version"], red=0, blue=100),
                _candidate("header_color", f2["version"], red=0, blue=100),
            ],
            "users": [{"user_id": "u1", "context": {}}],
        },
    ).json()
    release_id = created["release_id"]

    resp = client.post(f"/api/v1/releases/{release_id}/commit")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "committed"
    assert body["committed_at"] is not None
    assert [it["committed_version"] for it in body["items"]] == [2, 2]
    # 预览摘要随提交结果一并返回
    assert body["items"][0]["preview"]["total_users"] == 1

    # 两个开关当前配置都更新为候选内容、版本 +1
    cur1 = client.get("/api/v1/flags/btn_color").json()
    cur2 = client.get("/api/v1/flags/header_color").json()
    assert cur1["version"] == 2
    assert cur2["version"] == 2
    assert {v["key"]: v["weight"] for v in cur1["variants"]} == {"red": 0, "blue": 100}
    assert cur1["rules"] == []
    assert {v["key"]: v["weight"] for v in cur2["variants"]} == {"red": 0, "blue": 100}

    # 不可变版本历史各追加一行
    assert [v["version"] for v in client.get("/api/v1/flags/btn_color/versions").json()] == [1, 2]
    assert [v["version"] for v in client.get("/api/v1/flags/header_color/versions").json()] == [1, 2]
    v2 = client.get("/api/v1/flags/btn_color/versions/2").json()
    assert {v["key"]: v["weight"] for v in v2["variants"]} == {"red": 0, "blue": 100}

    # GET 也能看到提交后的版本号与状态
    got = client.get(f"/api/v1/releases/{release_id}").json()
    assert got["status"] == "committed"
    assert [it["committed_version"] for it in got["items"]] == [2, 2]


def test_repeated_commit_is_idempotent(client: TestClient):
    f1 = _create_flag(client, "btn_color")
    created = client.post(
        "/api/v1/releases",
        json={
            "candidates": [_candidate("btn_color", f1["version"], red=0, blue=100)],
            "users": [{"user_id": "u1", "context": {}}],
        },
    ).json()
    release_id = created["release_id"]

    first = client.post(f"/api/v1/releases/{release_id}/commit")
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["items"][0]["committed_version"] == 2
    committed_at = first_body["committed_at"]

    # 重复提交：返回首次提交结果，不再次追加版本
    second = client.post(f"/api/v1/releases/{release_id}/commit")
    assert second.status_code == 200
    second_body = second.json()
    assert second_body == first_body
    assert second_body["committed_at"] == committed_at

    assert client.get("/api/v1/flags/btn_color").json()["version"] == 2
    versions = client.get("/api/v1/flags/btn_color/versions").json()
    assert [v["version"] for v in versions] == [1, 2]


def test_commit_conflict_returns_409_and_writes_nothing(client: TestClient):
    f1 = _create_flag(client, "btn_color", rule_value="cn")
    f2 = _create_flag(client, "header_color", rule_value="jp")

    created = client.post(
        "/api/v1/releases",
        json={
            "candidates": [
                _candidate("btn_color", f1["version"], red=0, blue=100),
                _candidate("header_color", f2["version"], red=0, blue=100),
            ],
            "users": [{"user_id": "u1", "context": {}}],
        },
    ).json()
    release_id = created["release_id"]

    # 批次创建后、提交前，另一个发布先推进了 header_color（v1 -> v2）
    client.post(
        "/api/v1/flags",
        json={
            "flag_key": "header_color",
            "description": "concurrent",
            "variants": [
                {"key": "red", "weight": 100},
                {"key": "blue", "weight": 0},
            ],
            "rules": [],
        },
    )

    resp = client.post(f"/api/v1/releases/{release_id}/commit")
    assert resp.status_code == 409
    assert "header_color" in resp.json()["detail"]
    assert "2" in resp.json()["detail"]

    # 没有任何部分写入：btn_color 仍为 v1 旧配置，header_color 保持并发更新后的 v2
    btn = client.get("/api/v1/flags/btn_color").json()
    assert btn["version"] == 1
    assert {v["key"]: v["weight"] for v in btn["variants"]} == {"red": 50, "blue": 50}
    assert [v["version"] for v in client.get("/api/v1/flags/btn_color/versions").json()] == [1]
    header = client.get("/api/v1/flags/header_color").json()
    assert header["version"] == 2
    assert header["description"] == "concurrent"

    # 批次仍为 pending，预览与期望版本原样保留
    got = client.get(f"/api/v1/releases/{release_id}").json()
    assert got["status"] == "pending"
    assert all(it["committed_version"] is None for it in got["items"])

    # 冲突不会让批次失效成“假已提交”：更新另一开关后再次提交依旧 409（期望仍过期）
    again = client.post(f"/api/v1/releases/{release_id}/commit")
    assert again.status_code == 409


def test_commit_conflict_when_flag_deleted(client: TestClient):
    f1 = _create_flag(client, "btn_color")
    f2 = _create_flag(client, "header_color")
    created = client.post(
        "/api/v1/releases",
        json={
            "candidates": [
                _candidate("btn_color", f1["version"], red=0, blue=100),
                _candidate("header_color", f2["version"], red=0, blue=100),
            ],
            "users": [{"user_id": "u1", "context": {}}],
        },
    ).json()

    assert client.delete("/api/v1/flags/header_color").status_code == 204
    resp = client.post(f"/api/v1/releases/{created['release_id']}/commit")
    assert resp.status_code == 409
    assert "header_color" in resp.json()["detail"]

    # btn_color 不被部分写入
    assert client.get("/api/v1/flags/btn_color").json()["version"] == 1
    assert [v["version"] for v in client.get("/api/v1/flags/btn_color/versions").json()] == [1]


def test_create_release_stale_expected_version_returns_409(client: TestClient):
    _create_flag(client, "btn_color")
    # 推进到 v2
    client.post(
        "/api/v1/flags",
        json={
            "flag_key": "btn_color",
            "description": "v2",
            "variants": [{"key": "red", "weight": 0}, {"key": "blue", "weight": 100}],
            "rules": [],
        },
    )
    resp = client.post(
        "/api/v1/releases",
        json={
            "candidates": [_candidate("btn_color", 1, red=100, blue=0)],
            "users": [{"user_id": "u1", "context": {}}],
        },
    )
    assert resp.status_code == 409
    assert "2" in resp.json()["detail"]
    # 未生成批次
    assert client.get("/api/v1/flags/btn_color").json()["version"] == 2


def test_create_release_unknown_flag_returns_404(client: TestClient):
    resp = client.post(
        "/api/v1/releases",
        json={
            "candidates": [_candidate("nope", 1, red=50, blue=50)],
            "users": [{"user_id": "u1", "context": {}}],
        },
    )
    assert resp.status_code == 404


def test_commit_unknown_release_returns_404(client: TestClient):
    resp = client.post("/api/v1/releases/nope/commit")
    assert resp.status_code == 404


def test_create_release_reuses_config_validation(client: TestClient):
    f1 = _create_flag(client, "btn_color")

    # 权重和不为 100 -> 422
    resp = client.post(
        "/api/v1/releases",
        json={
            "candidates": [_candidate("btn_color", f1["version"], red=10, blue=10)],
            "users": [{"user_id": "u1", "context": {}}],
        },
    )
    assert resp.status_code == 422

    # 规则引用不存在的变体 -> 422
    bad = _candidate("btn_color", f1["version"], red=50, blue=50)
    bad["rules"] = [
        {"priority": 1, "attribute": "region", "operator": "eq", "values": ["cn"], "variant_key": "green"}
    ]
    resp = client.post(
        "/api/v1/releases",
        json={"candidates": [bad], "users": [{"user_id": "u1", "context": {}}]},
    )
    assert resp.status_code == 422


def test_create_release_rejects_duplicate_flags_and_users(client: TestClient):
    f1 = _create_flag(client, "btn_color")
    resp = client.post(
        "/api/v1/releases",
        json={
            "candidates": [
                _candidate("btn_color", f1["version"], red=50, blue=50),
                _candidate("btn_color", f1["version"], red=0, blue=100),
            ],
            "users": [{"user_id": "u1", "context": {}}],
        },
    )
    assert resp.status_code == 422

    resp = client.post(
        "/api/v1/releases",
        json={
            "candidates": [_candidate("btn_color", f1["version"], red=50, blue=50)],
            "users": [
                {"user_id": "u1", "context": {}},
                {"user_id": "u1", "context": {"region": "cn"}},
            ],
        },
    )
    assert resp.status_code == 422


def test_create_release_preview_matches_single_flag_preview(client: TestClient):
    f1 = _create_flag(client, "btn_color", rule_value="cn")
    users = [
        {"user_id": "u1", "context": {}},
        {"user_id": "u2", "context": {"region": "cn"}},
        {"user_id": "u3", "context": {"region": "us"}},
    ]
    candidate_body = {
        "variants": [{"key": "red", "weight": 0}, {"key": "blue", "weight": 100}],
        "rules": [],
    }

    single = client.post(
        "/api/v1/flags/btn_color/preview-impact",
        json={**candidate_body, "users": users},
    ).json()

    batch = client.post(
        "/api/v1/releases",
        json={
            "candidates": [
                {
                    "flag_key": "btn_color",
                    "description": "batch",
                    "expected_version": f1["version"],
                    **candidate_body,
                }
            ],
            "users": users,
        },
    ).json()
    preview = batch["items"][0]["preview"]
    assert preview["changed_users"] == single["changed_users"]
    assert preview["migration_counts"] == single["migration_counts"]
    assert preview["changes"] == single["changes"]
    assert preview["total_users"] == single["total_users"]


def test_release_does_not_write_decision_snapshots(client: TestClient):
    f1 = _create_flag(client, "btn_color")
    before = client.get("/healthz").json()["snapshots"]
    created = client.post(
        "/api/v1/releases",
        json={
            "candidates": [_candidate("btn_color", f1["version"], red=0, blue=100)],
            "users": [
                {"user_id": "u1", "context": {}},
                {"user_id": "u2", "context": {"region": "cn"}},
            ],
        },
    ).json()
    client.post(f"/api/v1/releases/{created['release_id']}/commit")
    after = client.get("/healthz").json()["snapshots"]
    assert after == before


def test_commit_persists_activation_window(client: TestClient):
    f1 = _create_flag(client, "btn_color")
    candidate = _candidate("btn_color", f1["version"], red=0, blue=100)
    candidate["activation_window"] = {
        "active_from": "2026-11-11T00:00:00Z",
        "active_until": "2026-11-12T00:00:00Z",
        "inactive_variant_key": "red",
    }
    created = client.post(
        "/api/v1/releases",
        json={
            "candidates": [candidate],
            "users": [{"user_id": "u1", "context": {}}],
        },
    ).json()
    assert created["items"][0]["activation_window"]["inactive_variant_key"] == "red"

    committed = client.post(
        f"/api/v1/releases/{created['release_id']}/commit"
    ).json()
    assert committed["items"][0]["committed_version"] == 2

    current = client.get("/api/v1/flags/btn_color").json()
    assert current["activation_window"]["inactive_variant_key"] == "red"
    v2 = client.get("/api/v1/flags/btn_color/versions/2").json()
    assert v2["activation_window"]["active_from"].startswith("2026-11-11T00:00:00")


def test_empty_candidates_or_users_returns_422(client: TestClient):
    f1 = _create_flag(client, "btn_color")
    resp = client.post(
        "/api/v1/releases",
        json={"candidates": [], "users": [{"user_id": "u1", "context": {}}]},
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/v1/releases",
        json={
            "candidates": [_candidate("btn_color", f1["version"], red=50, blue=50)],
            "users": [],
        },
    )
    assert resp.status_code == 422


def test_expected_version_must_be_positive(client: TestClient):
    _create_flag(client, "btn_color")
    resp = client.post(
        "/api/v1/releases",
        json={
            "candidates": [_candidate("btn_color", 0, red=50, blue=50)],
            "users": [{"user_id": "u1", "context": {}}],
        },
    )
    assert resp.status_code == 422
