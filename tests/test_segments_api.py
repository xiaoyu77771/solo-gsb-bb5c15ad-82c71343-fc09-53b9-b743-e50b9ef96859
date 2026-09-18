"""受众分群 API 集成测试：CRUD、422 循环引用、409 引用保护与开关 segment 规则判定。"""


def _condition(attr, operator, *values):
    return {"type": "condition", "attribute": attr, "operator": operator, "values": list(values)}


def _segment_ref(key):
    return {"type": "segment", "key": key}


def _all(*children):
    return {"type": "all", "children": list(children)}


def _any(*children):
    return {"type": "any", "children": list(children)}


def _not(child):
    return {"type": "not", "child": child}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_get_list_segment(client):
    resp = client.post(
        "/api/v1/segments",
        json={
            "segment_key": "vip_users",
            "description": "VIP 用户",
            "expression": _condition("tier", "eq", "vip"),
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["segment_key"] == "vip_users"
    assert body["description"] == "VIP 用户"
    assert body["expression"]["type"] == "condition"
    assert body["created_at"] and body["updated_at"]

    resp = client.get("/api/v1/segments/vip_users")
    assert resp.status_code == 200
    assert resp.json()["segment_key"] == "vip_users"

    resp = client.get("/api/v1/segments")
    assert resp.status_code == 200
    assert [s["segment_key"] for s in resp.json()] == ["vip_users"]


def test_get_missing_segment_returns_404(client):
    assert client.get("/api/v1/segments/nope").status_code == 404
    assert client.put(
        "/api/v1/segments/nope", json={"expression": _condition("a", "eq", 1)}
    ).status_code == 404
    assert client.delete("/api/v1/segments/nope").status_code == 404


def test_create_duplicate_segment_returns_409(client):
    payload = {"segment_key": "s1", "expression": _condition("a", "eq", 1)}
    assert client.post("/api/v1/segments", json=payload).status_code == 201
    resp = client.post("/api/v1/segments", json=payload)
    assert resp.status_code == 409


def test_update_unreferenced_segment(client):
    client.post("/api/v1/segments", json={"segment_key": "s1", "expression": _condition("a", "eq", 1)})
    resp = client.put(
        "/api/v1/segments/s1",
        json={"description": "改后", "expression": _condition("b", "eq", 2)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["description"] == "改后"
    assert body["expression"]["attribute"] == "b"


def test_delete_unreferenced_segment(client):
    client.post("/api/v1/segments", json={"segment_key": "s1", "expression": _condition("a", "eq", 1)})
    assert client.delete("/api/v1/segments/s1").status_code == 204
    assert client.get("/api/v1/segments/s1").status_code == 404


# ---------------------------------------------------------------------------
# 保存期 422：表达式结构、引用缺失、循环、深度 / 大小
# ---------------------------------------------------------------------------


def test_create_segment_bad_expression_structure_returns_422(client):
    # 未知类型
    resp = client.post(
        "/api/v1/segments", json={"segment_key": "bad", "expression": {"type": "xor"}}
    )
    assert resp.status_code == 422

    # all 必须至少一个 child
    resp = client.post(
        "/api/v1/segments", json={"segment_key": "bad2", "expression": {"type": "all", "children": []}}
    )
    assert resp.status_code == 422

    # not 缺少 child
    resp = client.post(
        "/api/v1/segments", json={"segment_key": "bad4", "expression": {"type": "not"}}
    )
    assert resp.status_code == 422

    # 条件 values 不能为空
    resp = client.post(
        "/api/v1/segments",
        json={
            "segment_key": "bad5",
            "expression": {"type": "condition", "attribute": "a", "operator": "eq", "values": []},
        },
    )
    assert resp.status_code == 422

    # 条件不允许使用 segment 操作符
    resp = client.post(
        "/api/v1/segments",
        json={
            "segment_key": "bad6",
            "expression": {"type": "condition", "attribute": "a", "operator": "segment", "values": ["x"]},
        },
    )
    assert resp.status_code == 422


def test_create_segment_missing_reference_returns_422(client):
    resp = client.post(
        "/api/v1/segments", json={"segment_key": "s1", "expression": _segment_ref("ghost")}
    )
    assert resp.status_code == 422
    assert "不存在" in resp.json()["detail"]


def test_direct_self_cycle_returns_422(client):
    resp = client.post(
        "/api/v1/segments", json={"segment_key": "self", "expression": _segment_ref("self")}
    )
    assert resp.status_code == 422
    assert "循环引用" in resp.json()["detail"]


def test_indirect_cycle_is_blocked_via_reference_protection(client):
    """间接环无法通过 API 形成：环上的每条回边都要改写一个“正被引用”的分群。

    leaf <- mid（mid 引用 leaf）。想把 leaf 改成引用 mid 形成 leaf->mid->leaf
    的环时，leaf 被 mid 引用，更新返回 409——引用保护先于环检测生效，
    既有判定不可能因分群定义变更而静默变化。环检测本身的直接单测见
    tests/test_segments_engine.py。
    """
    client.post(
        "/api/v1/segments", json={"segment_key": "leaf", "expression": _condition("x", "eq", 1)}
    )
    client.post(
        "/api/v1/segments", json={"segment_key": "mid", "expression": _segment_ref("leaf")}
    )
    resp = client.put("/api/v1/segments/leaf", json={"expression": _segment_ref("mid")})
    assert resp.status_code == 409
    assert "mid" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 409：被引用的分群禁止更新 / 删除
# ---------------------------------------------------------------------------


def test_segment_referenced_by_other_segment_cannot_change(client):
    client.post("/api/v1/segments", json={"segment_key": "base", "expression": _condition("x", "eq", 1)})
    client.post("/api/v1/segments", json={"segment_key": "combo", "expression": _all(_segment_ref("base"))})

    resp = client.put("/api/v1/segments/base", json={"expression": _condition("y", "eq", 2)})
    assert resp.status_code == 409
    assert "combo" in resp.json()["detail"]

    resp = client.delete("/api/v1/segments/base")
    assert resp.status_code == 409
    # combo 本身未被引用，可以删除；删除后 base 即可修改 / 删除
    assert client.delete("/api/v1/segments/combo").status_code == 204
    assert client.delete("/api/v1/segments/base").status_code == 204


def test_segment_referenced_by_flag_rule_cannot_change(client):
    client.post(
        "/api/v1/segments",
        json={"segment_key": "employees", "expression": _condition("employee", "eq", True)},
    )
    client.post(
        "/api/v1/flags",
        json={
            "flag_key": "feat_x",
            "variants": [{"key": "on", "weight": 100}, {"key": "off", "weight": 0}],
            "rules": [
                {"priority": 1, "operator": "segment", "segment_key": "employees", "variant_key": "on"}
            ],
        },
    )

    resp = client.put("/api/v1/segments/employees", json={"expression": _condition("x", "eq", 1)})
    assert resp.status_code == 409
    assert "feat_x" in resp.json()["detail"]
    assert client.delete("/api/v1/segments/employees").status_code == 409


def test_segment_referenced_only_by_history_version_blocks_change(client):
    """规则已从当前配置移除，但历史版本仍引用分群：更新 / 删除依旧 409。"""
    client.post(
        "/api/v1/segments", json={"segment_key": "seg1", "expression": _condition("a", "eq", 1)}
    )
    # v1：segment 规则引用 seg1
    client.post(
        "/api/v1/flags",
        json={
            "flag_key": "feat_h",
            "variants": [{"key": "on", "weight": 100}, {"key": "off", "weight": 0}],
            "rules": [
                {"priority": 1, "operator": "segment", "segment_key": "seg1", "variant_key": "on"}
            ],
        },
    )
    # v2：规则清空（当前配置不再引用）
    client.post(
        "/api/v1/flags",
        json={
            "flag_key": "feat_h",
            "variants": [{"key": "on", "weight": 100}, {"key": "off", "weight": 0}],
            "rules": [],
        },
    )
    resp = client.delete("/api/v1/segments/seg1")
    assert resp.status_code == 409
    assert "feat_h@v1" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 开关规则：segment 操作符
# ---------------------------------------------------------------------------


def _flag_with_segment_rule(segment_key="beta_users", weights=(100, 0)):
    return {
        "flag_key": "new_feat",
        "variants": [
            {"key": "on", "weight": weights[0]},
            {"key": "off", "weight": weights[1]},
        ],
        "rules": [
            {"priority": 1, "operator": "segment", "segment_key": segment_key, "variant_key": "on"}
        ],
    }


def test_create_flag_segment_rule_missing_segment_returns_422(client):
    resp = client.post("/api/v1/flags", json=_flag_with_segment_rule("ghost"))
    assert resp.status_code == 422
    assert "ghost" in resp.json()["detail"]


def test_segment_rule_shape_validation_returns_422(client):
    base = {
        "flag_key": "bad_shape",
        "variants": [{"key": "on", "weight": 100}, {"key": "off", "weight": 0}],
    }
    # segment 操作符缺 segment_key
    resp = client.post(
        "/api/v1/flags", json={**base, "rules": [{"priority": 1, "operator": "segment", "variant_key": "on"}]}
    )
    assert resp.status_code == 422
    # segment 操作符不允许 values / attribute
    resp = client.post(
        "/api/v1/flags",
        json={
            **base,
            "rules": [
                {"priority": 1, "operator": "segment", "segment_key": "s", "values": [1], "variant_key": "on"}
            ],
        },
    )
    assert resp.status_code == 422
    # 普通规则不允许带 segment_key
    resp = client.post(
        "/api/v1/flags",
        json={
            **base,
            "rules": [
                {"priority": 1, "attribute": "a", "operator": "eq", "values": [1],
                 "segment_key": "s", "variant_key": "on"}
            ],
        },
    )
    assert resp.status_code == 422


def test_segment_rule_evaluation_hit_miss_and_explanation(client):
    client.post(
        "/api/v1/segments",
        json={
            "segment_key": "vip_users",
            "expression": _condition("tier", "eq", "vip"),
        },
    )
    client.post(
        "/api/v1/segments",
        json={
            "segment_key": "beta_users",
            "description": "beta 地区 VIP",
            "expression": _all(
                _condition("region", "in", "cn", "sg"),
                _not(_condition("banned", "eq", True)),
                _segment_ref("vip_users"),
            ),
        },
    )
    client.post("/api/v1/flags", json=_flag_with_segment_rule())

    # 命中：region 命中 + 未封禁 + vip
    resp = client.post(
        "/api/v1/flags/new_feat/evaluate",
        json={"user_id": "u1", "context": {"region": "cn", "tier": "vip"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision_path"] == "rule"
    assert body["variant_key"] == "on"
    assert body["matched_rule"]["operator"] == "segment"
    assert body["matched_rule"]["segment_key"] == "beta_users"
    steps = [e["step"] for e in body["explanation"]]
    assert steps[0] == "rules_loaded"
    assert steps[-1] == "rule_selected"
    # 每层分群都有进入 / 离开；vip_users 是第二层
    entered = [e for e in body["explanation"] if e["step"] == "segment_entered"]
    assert {e["detail"]["segment_key"] for e in entered} == {"beta_users", "vip_users"}
    root = next(e for e in entered if e["detail"]["segment_key"] == "beta_users")
    nested = next(e for e in entered if e["detail"]["segment_key"] == "vip_users")
    assert root["detail"]["depth"] == 1 and root["detail"]["root"] is True
    assert nested["detail"]["depth"] == 2 and nested["detail"]["root"] is False
    assert nested["detail"]["ref_path"] == "$.children[2]"
    # 离开记录的顺序是先内层后外层
    evaluated = [e["detail"]["segment_key"] for e in body["explanation"] if e["step"] == "segment_evaluated"]
    assert evaluated == ["vip_users", "beta_users"]
    assert all(e["detail"]["result"] == "hit" for e in body["explanation"] if e["step"] == "segment_evaluated")


def test_segment_rule_missing_attribute_is_miss_then_bucket(client):
    client.post(
        "/api/v1/segments", json={"segment_key": "seg", "expression": _condition("tier", "eq", "vip")}
    )
    client.post(
        "/api/v1/flags",
        json={
            "flag_key": "f2",
            "variants": [{"key": "on", "weight": 0}, {"key": "off", "weight": 100}],
            "rules": [
                {"priority": 1, "operator": "segment", "segment_key": "seg", "variant_key": "on"}
            ],
        },
    )
    resp = client.post("/api/v1/flags/f2/evaluate", json={"user_id": "u9", "context": {}})
    body = resp.json()
    assert body["decision_path"] == "bucket"
    assert body["variant_key"] == "off"
    rule_step = next(e for e in body["explanation"] if e["step"] == "rule_evaluated")
    assert rule_step["detail"]["result"] == "miss"
    cond = next(e for e in body["explanation"] if e["step"] == "segment_condition_evaluated")
    assert cond["detail"]["result"] == "miss"
    assert "缺少属性" in cond["detail"]["reason"]


def test_segment_rule_short_circuit_recorded(client):
    client.post(
        "/api/v1/segments",
        json={
            "segment_key": "sc",
            "expression": _all(_condition("a", "eq", 1), _condition("b", "eq", 2)),
        },
    )
    client.post(
        "/api/v1/flags",
        json={
            "flag_key": "f3",
            "variants": [{"key": "on", "weight": 0}, {"key": "off", "weight": 100}],
            "rules": [
                {"priority": 1, "operator": "segment", "segment_key": "sc", "variant_key": "on"}
            ],
        },
    )
    body = client.post(
        "/api/v1/flags/f3/evaluate", json={"user_id": "u", "context": {"a": 0, "b": 2}}
    ).json()
    sc = next(e for e in body["explanation"] if e["step"] == "segment_group_short_circuited")
    assert sc["detail"]["operator"] == "all"
    assert sc["detail"]["short_circuit_index"] == 0
    # 短路后第二个条件未解析
    conds = [e for e in body["explanation"] if e["step"] == "segment_condition_evaluated"]
    assert len(conds) == 1


def test_segment_evaluation_is_stable_and_replays(client):
    client.post(
        "/api/v1/segments", json={"segment_key": "seg", "expression": _condition("tier", "eq", "vip")}
    )
    client.post(
        "/api/v1/flags",
        json={
            "flag_key": "f4",
            "variants": [{"key": "on", "weight": 100}, {"key": "off", "weight": 0}],
            "rules": [
                {"priority": 1, "operator": "segment", "segment_key": "seg", "variant_key": "on"}
            ],
        },
    )
    payload = {"user_id": "u1", "context": {"tier": "vip"}, "request_id": "fixed-rid"}
    first = client.post("/api/v1/flags/f4/evaluate", json=payload).json()
    # 幂等重放：即使 context 改成不命中，也返回首次快照
    replay = client.post(
        "/api/v1/flags/f4/evaluate",
        json={"user_id": "u1", "context": {"tier": "free"}, "request_id": "fixed-rid"},
    ).json()
    assert replay["variant_key"] == first["variant_key"] == "on"
    assert replay["explanation"] == first["explanation"]
    # 快照可回查
    snap = client.get("/api/v1/decisions/fixed-rid").json()
    assert snap["matched_rule_index"] == 0


# ---------------------------------------------------------------------------
# 分群与配置版本 / 发布批次 / 预览的交互
# ---------------------------------------------------------------------------


def test_segment_rule_pinned_historical_version_uses_frozen_definition(client):
    """分群被引用后无法修改；即便绕过，历史版本钉住的判定与首次快照一致。"""
    client.post(
        "/api/v1/segments", json={"segment_key": "seg", "expression": _condition("tier", "eq", "vip")}
    )
    client.post(
        "/api/v1/flags",
        json={
            "flag_key": "f5",
            "variants": [{"key": "on", "weight": 100}, {"key": "off", "weight": 0}],
            "rules": [
                {"priority": 1, "operator": "segment", "segment_key": "seg", "variant_key": "on"}
            ],
        },
    )
    # v2：规则清空
    client.post(
        "/api/v1/flags",
        json={
            "flag_key": "f5",
            "variants": [{"key": "on", "weight": 100}, {"key": "off", "weight": 0}],
            "rules": [],
        },
    )
    # 钉住 v1 判定：segment 规则仍生效
    body = client.post(
        "/api/v1/flags/f5/evaluate",
        json={"user_id": "u1", "context": {"tier": "vip"}, "config_version": 1},
    ).json()
    assert body["config_version"] == 1
    assert body["decision_path"] == "rule"
    assert body["variant_key"] == "on"

    # 历史版本响应包含 segment 规则
    v1 = client.get("/api/v1/flags/f5/versions/1").json()
    assert v1["rules"][0]["operator"] == "segment"
    assert v1["rules"][0]["segment_key"] == "seg"


def test_preview_impact_validates_segment_rules(client):
    client.post(
        "/api/v1/flags",
        json={
            "flag_key": "f6",
            "variants": [{"key": "on", "weight": 100}, {"key": "off", "weight": 0}],
            "rules": [],
        },
    )
    # 候选引用不存在的分群 -> 422
    resp = client.post(
        "/api/v1/flags/f6/preview-impact",
        json={
            "variants": [{"key": "on", "weight": 100}, {"key": "off", "weight": 0}],
            "rules": [
                {"priority": 1, "operator": "segment", "segment_key": "ghost", "variant_key": "on"}
            ],
            "users": [{"user_id": "u1", "context": {}}],
        },
    )
    assert resp.status_code == 422

    client.post(
        "/api/v1/segments", json={"segment_key": "seg", "expression": _condition("tier", "eq", "vip")}
    )
    resp = client.post(
        "/api/v1/flags/f6/preview-impact",
        json={
            "variants": [{"key": "on", "weight": 100}, {"key": "off", "weight": 0}],
            "rules": [
                {"priority": 1, "operator": "segment", "segment_key": "seg", "variant_key": "on"}
            ],
            "users": [
                {"user_id": "vip1", "context": {"tier": "vip"}},
                {"user_id": "free1", "context": {"tier": "free"}},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 当前配置 on 权重 100，两侧分桶均为 on；候选新增 segment 规则后：
    # vip1 由 bucket 变 rule（变体不变），free1 路径 / 变体均不变
    changed = {c["user_id"]: c for c in body["changes"]}
    assert set(changed) == {"vip1"}
    assert changed["vip1"]["current"]["decision_path"] == "bucket"
    assert changed["vip1"]["candidate"]["decision_path"] == "rule"
    assert changed["vip1"]["candidate"]["matched_rule_index"] == 0
    # 变体未迁移，迁移计数为空
    assert body["migration_counts"] == {}


def test_release_create_validates_segment_and_commit_conflict_if_deleted(client):
    """批次创建时校验分群存在；分群无法在引用期间删除，因此该 409 为防御性路径。

    通过直接构造 pending 批次后删除分群不可行（删除会 409），因此这里验证：
    创建时引用缺失 -> 422；正常创建 + 提交成功。
    """
    client.post(
        "/api/v1/flags",
        json={
            "flag_key": "f7",
            "variants": [{"key": "on", "weight": 0}, {"key": "off", "weight": 100}],
            "rules": [],
        },
    )
    resp = client.post(
        "/api/v1/releases",
        json={
            "candidates": [
                {
                    "flag_key": "f7",
                    "expected_version": 1,
                    "variants": [{"key": "on", "weight": 100}, {"key": "off", "weight": 0}],
                    "rules": [
                        {"priority": 1, "operator": "segment", "segment_key": "ghost", "variant_key": "on"}
                    ],
                }
            ],
            "users": [{"user_id": "u1", "context": {}}],
        },
    )
    assert resp.status_code == 422

    client.post(
        "/api/v1/segments", json={"segment_key": "seg", "expression": _condition("tier", "eq", "vip")}
    )
    resp = client.post(
        "/api/v1/releases",
        json={
            "candidates": [
                {
                    "flag_key": "f7",
                    "expected_version": 1,
                    "variants": [{"key": "on", "weight": 100}, {"key": "off", "weight": 0}],
                    "rules": [
                        {"priority": 1, "operator": "segment", "segment_key": "seg", "variant_key": "on"}
                    ],
                }
            ],
            "users": [{"user_id": "u1", "context": {"tier": "vip"}}],
        },
    )
    assert resp.status_code == 201, resp.text
    release_id = resp.json()["release_id"]
    commit = client.post(f"/api/v1/releases/{release_id}/commit")
    assert commit.status_code == 200
    assert commit.json()["items"][0]["committed_version"] == 2


def test_release_commit_conflicts_if_segment_vanished(client):
    """防御性路径：批次提交事务内再核对分群存在，缺失则整批 409 且零写入。

    正常 API 流中被引用分群无法删除（删除 / 更新均 409），这里直接用 ORM
    删除模拟异常数据 / 绕过场景，验证提交闸门与原子回滚。
    """
    from app.database import SessionLocal
    from app.models import Segment

    client.post(
        "/api/v1/segments",
        json={"segment_key": "seg", "expression": _condition("a", "eq", 1)},
    )
    client.post(
        "/api/v1/flags",
        json={
            "flag_key": "f8",
            "variants": [{"key": "on", "weight": 100}, {"key": "off", "weight": 0}],
            "rules": [],
        },
    )
    resp = client.post(
        "/api/v1/releases",
        json={
            "candidates": [
                {
                    "flag_key": "f8",
                    "expected_version": 1,
                    "variants": [{"key": "on", "weight": 100}, {"key": "off", "weight": 0}],
                    "rules": [
                        {"priority": 1, "operator": "segment", "segment_key": "seg", "variant_key": "on"}
                    ],
                }
            ],
            "users": [{"user_id": "u1", "context": {"a": 1}}],
        },
    )
    release_id = resp.json()["release_id"]

    db = SessionLocal()
    try:
        db.query(Segment).filter(Segment.segment_key == "seg").delete()
        db.commit()
    finally:
        db.close()

    commit = client.post(f"/api/v1/releases/{release_id}/commit")
    assert commit.status_code == 409
    assert "seg" in commit.json()["detail"]
    # 整批回滚：开关版本仍为 1，规则仍为空
    flag = client.get("/api/v1/flags/f8").json()
    assert flag["version"] == 1
    assert flag["rules"] == []
