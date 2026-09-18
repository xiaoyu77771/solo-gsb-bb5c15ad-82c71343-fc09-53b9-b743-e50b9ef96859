"""受众分群（segments）：CRUD、循环检测、引用保护与判定集成测试。"""

from app.engine import MAX_SEGMENT_DEPTH, Rule, evaluate_rule

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _create_segment(client, key, expression, description=""):
    return client.post(
        "/api/v1/segments",
        json={"segment_key": key, "description": description, "expression": expression},
    )


def _update_segment(client, key, expression, description=""):
    return client.put(
        f"/api/v1/segments/{key}",
        json={"description": description, "expression": expression},
    )


def _create_flag_with_segment_rule(client, segment_key, flag_key="seg_flag"):
    """创建一个带 segment 规则的开关：命中分群 -> on，否则 10/90 分桶。"""
    payload = {
        "flag_key": flag_key,
        "description": "分群规则开关",
        "variants": [
            {"key": "on", "weight": 10},
            {"key": "off", "weight": 90},
        ],
        "rules": [
            {
                "priority": 1,
                "operator": "segment",
                "values": [segment_key],
                "variant_key": "on",
            }
        ],
    }
    return client.post("/api/v1/flags", json=payload)


CN_ADULT = {
    "all": [
        {"attribute": "region", "operator": "eq", "values": ["cn"]},
        {"attribute": "age", "operator": "gte", "values": [18]},
    ]
}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_and_get_segment_roundtrip(client):
    expr = {
        "all": [
            {"attribute": "age", "operator": "gte", "values": [18]},
            {"not": {"attribute": "region", "operator": "eq", "values": ["us"]}},
            {"any": [
                {"attribute": "employee", "values": [True]},
                {"attribute": "tier", "operator": "in", "values": ["vip", "svip"]},
            ]},
        ]
    }
    resp = _create_segment(client, "beta_users", expr, description="灰度用户")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["segment_key"] == "beta_users"
    assert body["description"] == "灰度用户"
    # 表达式回显为规范化形态（缺省 operator 补为 "eq"，not 以 "not" 键序列化）
    expected = {
        "all": [
            {"attribute": "age", "operator": "gte", "values": [18]},
            {"not": {"attribute": "region", "operator": "eq", "values": ["us"]}},
            {"any": [
                {"attribute": "employee", "operator": "eq", "values": [True]},
                {"attribute": "tier", "operator": "in", "values": ["vip", "svip"]},
            ]},
        ]
    }
    assert body["expression"] == expected
    assert "created_at" in body and "updated_at" in body

    got = client.get("/api/v1/segments/beta_users")
    assert got.status_code == 200
    assert got.json()["expression"] == expected


def test_list_segments_sorted_by_key(client):
    _create_segment(client, "b_seg", CN_ADULT)
    _create_segment(client, "a_seg", {"segment": "b_seg"})
    keys = [s["segment_key"] for s in client.get("/api/v1/segments").json()]
    assert keys == ["a_seg", "b_seg"]


def test_create_duplicate_key_returns_409(client):
    assert _create_segment(client, "dup", CN_ADULT).status_code == 201
    resp = _create_segment(client, "dup", CN_ADULT)
    assert resp.status_code == 409


def test_get_missing_segment_404(client):
    assert client.get("/api/v1/segments/nope").status_code == 404


def test_update_segment_replaces_expression(client):
    _create_segment(client, "mutable", CN_ADULT, description="v1")
    new_expr = {"any": [
        {"attribute": "region", "operator": "eq", "values": ["cn"]},
        {"attribute": "region", "operator": "eq", "values": ["sg"]},
    ]}
    resp = _update_segment(client, "mutable", new_expr, description="v2")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["description"] == "v2"
    assert body["expression"] == new_expr
    assert client.get("/api/v1/segments/mutable").json()["expression"] == new_expr


def test_update_missing_segment_404(client):
    assert _update_segment(client, "nope", CN_ADULT).status_code == 404


def test_delete_segment(client):
    _create_segment(client, "temp", CN_ADULT)
    assert client.delete("/api/v1/segments/temp").status_code == 204
    assert client.get("/api/v1/segments/temp").status_code == 404
    assert client.delete("/api/v1/segments/temp").status_code == 404


# ---------------------------------------------------------------------------
# 表达式校验（422）
# ---------------------------------------------------------------------------


def test_expression_validation_rejects_malformed(client):
    bad_expressions = [
        {"all": []},                                             # all 至少一个子表达式
        {"any": []},                                             # any 至少一个子表达式
        {"attribute": "a", "operator": "nope", "values": [1]},   # 未知操作符
        {"attribute": "a", "operator": "eq"},                    # values 缺失
        {"attribute": "a", "values": []},                        # values 为空
        {"attribute": "", "values": [1]},                        # attribute 为空
        {"attribute": "a", "values": [1], "extra": 1},           # 多余字段
        {"segment": ""},                                         # 分群 key 为空
        {"segment": "bad key!"},                                 # 分群 key 非法字符
        {"not": {"all": []}},                                    # 嵌套的非法子表达式
        {"all": [{"attribute": "a", "values": [1]}, {"all": []}]},
        "not-a-dict",                                            # 表达式必须是对象
    ]
    for expr in bad_expressions:
        resp = _create_segment(client, "bad_seg", expr)
        assert resp.status_code == 422, f"应拒绝: {expr!r}"
    # key 本身非法
    resp = _create_segment(client, "bad key", CN_ADULT)
    assert resp.status_code == 422


def test_expression_depth_limit(client):
    expr = {"attribute": "a", "values": [1]}
    for _ in range(31):
        expr = {"not": expr}
    assert _create_segment(client, "deep32", expr).status_code == 201  # 恰好 32 层
    expr = {"not": expr}  # 33 层
    resp = _create_segment(client, "deep33", expr)
    assert resp.status_code == 422
    assert "嵌套" in resp.text


def test_create_with_unknown_segment_ref_422(client):
    resp = _create_segment(client, "dangling", {"segment": "ghost"})
    assert resp.status_code == 422
    assert "ghost" in resp.json()["detail"]
    # 嵌套在组合子里的未知引用同样拒绝
    resp = _create_segment(
        client, "dangling2", {"all": [{"not": {"segment": "ghost"}}]}
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 循环引用检测（422）
# ---------------------------------------------------------------------------


def test_self_reference_on_create_422(client):
    resp = _create_segment(client, "selfish", {"segment": "selfish"})
    assert resp.status_code == 422
    assert "循环" in resp.json()["detail"]


def test_self_reference_on_update_422(client):
    _create_segment(client, "selfish2", CN_ADULT)
    resp = _update_segment(client, "selfish2", {"not": {"segment": "selfish2"}})
    assert resp.status_code == 422
    assert "循环" in resp.json()["detail"]


def test_indirect_cycle_detected_422(client):
    # b 先存在（无引用），a 引用 b；再把 b 更新为引用 a -> a -> b -> a 成环
    _create_segment(client, "seg_b", CN_ADULT)
    _create_segment(client, "seg_a", {"segment": "seg_b"})
    resp = _update_segment(client, "seg_b", {"segment": "seg_a"})
    assert resp.status_code == 422
    assert "循环" in resp.json()["detail"]
    # 更新被拒绝，b 的表达式保持原样
    assert client.get("/api/v1/segments/seg_b").json()["expression"] == CN_ADULT


def test_longer_indirect_cycle_detected_422(client):
    # c -> 无引用；b -> c；a -> b；把 c 更新为引用 a -> a -> b -> c -> a 成环
    _create_segment(client, "c_seg", CN_ADULT)
    _create_segment(client, "b_seg", {"segment": "c_seg"})
    _create_segment(client, "a_seg", {"segment": "b_seg"})
    resp = _update_segment(client, "c_seg", {"segment": "a_seg"})
    assert resp.status_code == 422
    assert "循环" in resp.json()["detail"]


def test_update_to_valid_expression_when_only_self_graph(client):
    # 未被引用的分群可以自由更新为非循环表达式
    _create_segment(client, "base", CN_ADULT)
    _create_segment(client, "wrapper", {"segment": "base"})
    resp = _update_segment(client, "wrapper", {"all": [{"segment": "base"}]})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 引用保护（409）
# ---------------------------------------------------------------------------


def test_update_delete_segment_referenced_by_segment_409(client):
    _create_segment(client, "inner", CN_ADULT)
    _create_segment(client, "outer", {"segment": "inner"})

    resp = _update_segment(client, "inner", {"any": [{"attribute": "r", "values": ["x"]}]})
    assert resp.status_code == 409
    assert "outer" in resp.json()["detail"]

    resp = client.delete("/api/v1/segments/inner")
    assert resp.status_code == 409
    assert "outer" in resp.json()["detail"]

    # 解除引用后可以删除
    assert client.delete("/api/v1/segments/outer").status_code == 204
    assert client.delete("/api/v1/segments/inner").status_code == 204


def test_update_delete_segment_referenced_by_flag_rule_409(client):
    _create_segment(client, "cn_adult", CN_ADULT)
    assert _create_flag_with_segment_rule(client, "cn_adult").status_code == 201

    resp = _update_segment(client, "cn_adult", {"segment": "cn_adult2"})
    assert resp.status_code == 422  # 引用不存在的分群优先于 409
    resp = _update_segment(client, "cn_adult", CN_ADULT, description="换个描述")
    assert resp.status_code == 409
    assert "seg_flag" in resp.json()["detail"]

    resp = client.delete("/api/v1/segments/cn_adult")
    assert resp.status_code == 409
    assert "seg_flag" in resp.json()["detail"]

    # 开关更新为不再引用该分群后，删除放行
    payload = {
        "flag_key": "seg_flag",
        "variants": [{"key": "on", "weight": 10}, {"key": "off", "weight": 90}],
        "rules": [],
    }
    assert client.post("/api/v1/flags", json=payload).status_code == 201
    assert client.delete("/api/v1/segments/cn_adult").status_code == 204


def test_delete_flag_frees_segment_reference(client):
    _create_segment(client, "sole", CN_ADULT)
    assert _create_flag_with_segment_rule(client, "sole").status_code == 201
    assert client.delete("/api/v1/flags/seg_flag").status_code == 204
    assert client.delete("/api/v1/segments/sole").status_code == 204


# ---------------------------------------------------------------------------
# 开关规则的 segment 操作符：配置校验
# ---------------------------------------------------------------------------


def test_flag_with_segment_rule_requires_existing_segment(client):
    resp = _create_flag_with_segment_rule(client, "ghost_segment")
    assert resp.status_code == 422
    assert "ghost_segment" in resp.json()["detail"]


def test_segment_rule_schema_validation(client):
    base = {
        "flag_key": "schema_flag",
        "variants": [{"key": "on", "weight": 10}, {"key": "off", "weight": 90}],
    }
    # operator=segment 不应提供 attribute
    rule = {"priority": 1, "attribute": "region", "operator": "segment",
            "values": ["s1"], "variant_key": "on"}
    assert client.post("/api/v1/flags", json={**base, "rules": [rule]}).status_code == 422
    # values 必须恰好一个分群 key
    rule = {"priority": 1, "operator": "segment", "values": ["a", "b"], "variant_key": "on"}
    assert client.post("/api/v1/flags", json={**base, "rules": [rule]}).status_code == 422
    rule = {"priority": 1, "operator": "segment", "values": [123], "variant_key": "on"}
    assert client.post("/api/v1/flags", json={**base, "rules": [rule]}).status_code == 422
    rule = {"priority": 1, "operator": "segment", "values": [], "variant_key": "on"}
    assert client.post("/api/v1/flags", json={**base, "rules": [rule]}).status_code == 422
    # 非 segment 操作符仍要求 attribute
    rule = {"priority": 1, "operator": "eq", "values": [1], "variant_key": "on"}
    assert client.post("/api/v1/flags", json={**base, "rules": [rule]}).status_code == 422


# ---------------------------------------------------------------------------
# 判定集成
# ---------------------------------------------------------------------------


def test_evaluate_segment_rule_hit_and_miss(client):
    _create_segment(client, "cn_adult", CN_ADULT)
    assert _create_flag_with_segment_rule(client, "cn_adult").status_code == 201

    hit = client.post(
        "/api/v1/flags/seg_flag/evaluate",
        json={"user_id": "u1", "context": {"region": "cn", "age": 20}},
    ).json()
    assert hit["decision_path"] == "rule"
    assert hit["variant_key"] == "on"
    assert hit["matched_rule"]["operator"] == "segment"
    assert hit["matched_rule"]["values"] == ["cn_adult"]

    miss = client.post(
        "/api/v1/flags/seg_flag/evaluate",
        json={"user_id": "u1", "context": {"region": "cn", "age": 10}},
    ).json()
    assert miss["decision_path"] == "bucket"
    assert miss["bucket_value"] is not None
    assert miss["variant_key"] in ("on", "off")


def test_evaluate_segment_missing_attribute_is_miss(client):
    _create_segment(client, "cn_adult", CN_ADULT)
    _create_flag_with_segment_rule(client, "cn_adult")
    body = client.post(
        "/api/v1/flags/seg_flag/evaluate",
        json={"user_id": "u2", "context": {}},  # region / age 均缺失
    ).json()
    assert body["decision_path"] == "bucket"
    cond_steps = [
        s for s in body["explanation"] if s["step"] == "segment_condition_evaluated"
    ]
    assert cond_steps
    assert cond_steps[0]["detail"]["result"] == "miss"
    assert "缺少属性" in cond_steps[0]["detail"]["reason"]


def test_evaluate_nested_segment_refs_and_explanation_layers(client):
    _create_segment(client, "l3_leaf", {"attribute": "employee", "values": [True]})
    _create_segment(client, "l2_mid", {"all": [{"segment": "l3_leaf"}]})
    _create_segment(
        client,
        "l1_top",
        {"any": [
            {"attribute": "region", "operator": "eq", "values": ["cn"]},
            {"segment": "l2_mid"},
        ]},
    )
    _create_flag_with_segment_rule(client, "l1_top")

    body = client.post(
        "/api/v1/flags/seg_flag/evaluate",
        json={"user_id": "u1", "context": {"region": "us", "employee": True}},
    ).json()
    assert body["decision_path"] == "rule"
    assert body["variant_key"] == "on"

    seg_steps = [s for s in body["explanation"] if s["step"] == "segment_evaluated"]
    # 每层分群都有记录：l1_top(depth=1) 与嵌套的 l2_mid(depth=2)、l3_leaf(depth=3)
    by_key = {s["detail"]["segment_key"]: s["detail"] for s in seg_steps}
    assert by_key["l1_top"]["depth"] == 1
    assert by_key["l1_top"]["result"] == "hit"
    assert by_key["l2_mid"]["depth"] == 2
    assert by_key["l3_leaf"]["depth"] == 3
    assert by_key["l3_leaf"]["result"] == "hit"


def test_evaluate_short_circuit_recorded(client):
    # all：首个子表达式未命中即短路，后续条件不求值
    _create_segment(
        client,
        "short_all",
        {"all": [
            {"attribute": "region", "operator": "eq", "values": ["cn"]},
            {"attribute": "age", "operator": "gte", "values": [18]},
            {"attribute": "employee", "values": [True]},
        ]},
    )
    _create_flag_with_segment_rule(client, "short_all")
    body = client.post(
        "/api/v1/flags/seg_flag/evaluate",
        json={"user_id": "u1", "context": {"region": "us"}},
    ).json()
    assert body["decision_path"] == "bucket"
    comb = next(
        s for s in body["explanation"] if s["step"] == "segment_combinator_evaluated"
    )
    assert comb["detail"]["combinator"] == "all"
    assert comb["detail"]["children_evaluated"] == 1
    assert comb["detail"]["children_total"] == 3
    assert comb["detail"]["short_circuited"] is True
    assert "短路" in comb["detail"]["reason"]
    # 短路后只评估了第一个条件
    cond_steps = [
        s for s in body["explanation"] if s["step"] == "segment_condition_evaluated"
    ]
    assert len(cond_steps) == 1

    # any：首个子表达式命中即短路
    _create_segment(
        client,
        "short_any",
        {"any": [
            {"attribute": "region", "operator": "eq", "values": ["cn"]},
            {"attribute": "age", "operator": "gte", "values": [18]},
        ]},
    )
    _create_flag_with_segment_rule(client, "short_any", flag_key="seg_flag2")
    body = client.post(
        "/api/v1/flags/seg_flag2/evaluate",
        json={"user_id": "u1", "context": {"region": "cn"}},
    ).json()
    assert body["decision_path"] == "rule"
    comb = next(
        s for s in body["explanation"] if s["step"] == "segment_combinator_evaluated"
    )
    assert comb["detail"]["combinator"] == "any"
    assert comb["detail"]["children_evaluated"] == 1
    assert comb["detail"]["short_circuited"] is True


def test_evaluate_last_child_miss_reason(client):
    # all 的最后一个子表达式未命中（无短路）：reason 不得误报“全部命中”
    _create_segment(
        client,
        "last_miss",
        {"all": [
            {"attribute": "region", "operator": "eq", "values": ["cn"]},
            {"attribute": "age", "operator": "gte", "values": [18]},
        ]},
    )
    _create_flag_with_segment_rule(client, "last_miss")
    body = client.post(
        "/api/v1/flags/seg_flag/evaluate",
        json={"user_id": "u1", "context": {"region": "cn", "age": 10}},
    ).json()
    comb = next(
        s for s in body["explanation"] if s["step"] == "segment_combinator_evaluated"
    )
    assert comb["detail"]["result"] == "miss"
    assert comb["detail"]["children_evaluated"] == 2
    assert comb["detail"]["short_circuited"] is False
    assert "未命中" in comb["detail"]["reason"]
    assert "全部" not in comb["detail"]["reason"]


def test_evaluate_not_combinator(client):
    _create_segment(
        client, "not_us", {"not": {"attribute": "region", "operator": "eq", "values": ["us"]}}
    )
    _create_flag_with_segment_rule(client, "not_us")
    body = client.post(
        "/api/v1/flags/seg_flag/evaluate",
        json={"user_id": "u1", "context": {"region": "cn"}},
    ).json()
    assert body["decision_path"] == "rule"
    comb = next(
        s for s in body["explanation"] if s["step"] == "segment_combinator_evaluated"
    )
    assert comb["detail"]["combinator"] == "not"
    assert comb["detail"]["result"] == "hit"


def test_segment_evaluation_steps_follow_rule_step(client):
    _create_segment(client, "cn_adult", CN_ADULT)
    _create_flag_with_segment_rule(client, "cn_adult")
    body = client.post(
        "/api/v1/flags/seg_flag/evaluate",
        json={"user_id": "u1", "context": {"region": "cn", "age": 30}},
    ).json()
    steps = [s["step"] for s in body["explanation"]]
    rule_idx = steps.index("rule_evaluated")
    first_seg_idx = min(
        i for i, s in enumerate(steps) if s.startswith("segment_")
    )
    # 分群轨迹跟在对应规则步骤之后
    assert rule_idx < first_seg_idx
    rule_step = body["explanation"][rule_idx]
    assert rule_step["detail"]["operator"] == "segment"
    assert "cn_adult" in rule_step["detail"]["reason"]


def test_segment_snapshot_and_idempotent_replay(client):
    _create_segment(client, "cn_adult", CN_ADULT)
    _create_flag_with_segment_rule(client, "cn_adult")
    payload = {
        "user_id": "u1",
        "context": {"region": "cn", "age": 20},
        "request_id": "seg-req-001",
    }
    first = client.post("/api/v1/flags/seg_flag/evaluate", json=payload).json()
    assert first["decision_path"] == "rule"
    # 快照固化分群轨迹；重放返回首次结果
    snap = client.get(f"/api/v1/decisions/{first['request_id']}").json()
    assert any(s["step"] == "segment_evaluated" for s in snap["explanation"])
    replay = client.post("/api/v1/flags/seg_flag/evaluate", json=payload).json()
    assert replay["explanation"] == first["explanation"]
    assert replay["variant_key"] == first["variant_key"]


def test_pinned_config_version_resolves_current_segments(client):
    # 分群不随开关版本固化：钉住历史版本判定时按当前分群定义解析
    _create_segment(client, "cn_adult", CN_ADULT)
    _create_flag_with_segment_rule(client, "cn_adult")  # v1
    payload = {
        "flag_key": "seg_flag",
        "variants": [{"key": "on", "weight": 10}, {"key": "off", "weight": 90}],
        "rules": [],
    }
    assert client.post("/api/v1/flags", json=payload).status_code == 201  # v2 去掉规则
    body = client.post(
        "/api/v1/flags/seg_flag/evaluate",
        json={
            "user_id": "u1",
            "context": {"region": "cn", "age": 20},
            "config_version": 1,
        },
    ).json()
    assert body["decision_path"] == "rule"
    assert body["variant_key"] == "on"


# ---------------------------------------------------------------------------
# 影响预览与发布批次集成
# ---------------------------------------------------------------------------


def test_preview_impact_with_segment_rule(client, simple_flag):
    # simple_flag: red/blue 各 50，规则 region=cn -> red；桶值 u1=8(red)、u10=85(blue)
    _create_segment(client, "employees", {"attribute": "employee", "values": [True]})
    payload = {
        "variants": [{"key": "red", "weight": 0}, {"key": "blue", "weight": 100}],
        "rules": [
            {"priority": 1, "operator": "segment", "values": ["employees"], "variant_key": "red"}
        ],
        "users": [
            # u10 当前 blue(桶)，候选命中分群规则 -> red(rule)
            {"user_id": "u10", "context": {"employee": True}},
            # u1 当前 red(桶)，候选分群未命中（缺属性）-> blue(桶)
            {"user_id": "u1", "context": {}},
        ],
    }
    resp = client.post("/api/v1/flags/btn_color/preview-impact", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed_users"] == 2
    by_user = {c["user_id"]: c for c in body["changes"]}
    assert by_user["u10"]["current"] == {
        "variant_key": "blue", "decision_path": "bucket", "matched_rule_index": None
    }
    assert by_user["u10"]["candidate"] == {
        "variant_key": "red", "decision_path": "rule", "matched_rule_index": 0
    }
    assert by_user["u1"]["candidate"]["variant_key"] == "blue"
    assert body["migration_counts"] == {"blue": {"red": 1}, "red": {"blue": 1}}


def test_preview_impact_rejects_unknown_segment(client, simple_flag):
    payload = {
        "variants": [{"key": "red", "weight": 0}, {"key": "blue", "weight": 100}],
        "rules": [
            {"priority": 1, "operator": "segment", "values": ["ghost"], "variant_key": "red"}
        ],
        "users": [{"user_id": "u1", "context": {}}],
    }
    resp = client.post("/api/v1/flags/btn_color/preview-impact", json=payload)
    assert resp.status_code == 422
    assert "ghost" in resp.json()["detail"]


def test_release_with_segment_rule_commit_then_evaluate(client, simple_flag):
    _create_segment(client, "cn_adult", CN_ADULT)
    candidate = {
        "flag_key": "btn_color",
        "expected_version": 1,
        "variants": [{"key": "red", "weight": 0}, {"key": "blue", "weight": 100}],
        "rules": [
            {"priority": 1, "operator": "segment", "values": ["cn_adult"], "variant_key": "red"}
        ],
    }
    resp = client.post(
        "/api/v1/releases",
        json={"candidates": [candidate], "users": [{"user_id": "u1", "context": {}}]},
    )
    assert resp.status_code == 201, resp.text
    release_id = resp.json()["release_id"]
    commit = client.post(f"/api/v1/releases/{release_id}/commit")
    assert commit.status_code == 200, commit.text
    assert commit.json()["items"][0]["committed_version"] == 2

    body = client.post(
        "/api/v1/flags/btn_color/evaluate",
        json={"user_id": "u1", "context": {"region": "cn", "age": 20}},
    ).json()
    assert body["decision_path"] == "rule"
    assert body["variant_key"] == "red"


def test_release_rejects_unknown_segment(client, simple_flag):
    candidate = {
        "flag_key": "btn_color",
        "expected_version": 1,
        "variants": [{"key": "red", "weight": 0}, {"key": "blue", "weight": 100}],
        "rules": [
            {"priority": 1, "operator": "segment", "values": ["ghost"], "variant_key": "red"}
        ],
    }
    resp = client.post(
        "/api/v1/releases",
        json={"candidates": [candidate], "users": [{"user_id": "u1", "context": {}}]},
    )
    assert resp.status_code == 422
    assert "ghost" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 引擎层防御（绕过 API 直接构造异常数据）
# ---------------------------------------------------------------------------


def test_engine_missing_segment_is_miss():
    steps = []
    hit, reason = evaluate_rule(Rule(1, "", "segment", ["ghost"], "on"), {}, {}, steps)
    assert hit is False
    assert "未命中" in reason
    assert steps[0]["step"] == "segment_evaluated"
    assert "不存在" in steps[0]["detail"]["reason"]


def test_engine_cycle_guard_is_miss():
    segments = {"a": {"segment": "b"}, "b": {"segment": "a"}}
    steps = []
    hit, _ = evaluate_rule(Rule(1, "", "segment", ["a"], "on"), {}, segments, steps)
    assert hit is False
    assert any("循环引用" in s["detail"]["reason"] for s in steps)


def test_engine_depth_guard_is_miss():
    # 构造超过 MAX_SEGMENT_DEPTH 的引用链（API 层不可能产生，仅防御）
    segments = {f"s{i}": {"segment": f"s{i + 1}"} for i in range(MAX_SEGMENT_DEPTH + 5)}
    segments[f"s{MAX_SEGMENT_DEPTH + 5}"] = {"attribute": "x", "values": [1]}
    steps = []
    hit, _ = evaluate_rule(
        Rule(1, "", "segment", ["s0"], "on"), {"x": 1}, segments, steps
    )
    assert hit is False
    assert any("最大嵌套" in s["detail"]["reason"] for s in steps)


def test_engine_segment_none_map_is_miss():
    hit, reason = evaluate_rule(Rule(1, "", "segment", ["any_seg"], "on"), {"x": 1})
    assert hit is False
    assert "未命中" in reason
