"""受众分群表达式引擎单元测试：比较、嵌套组合、递归引用、trace 与保存期校验。"""

import pytest

from app import segments as seg
from app.engine import Rule, Variant, decide


def _resolver(mapping):
    return lambda key: mapping.get(key)


# ---------------------------------------------------------------------------
# 条件比较（属性缺失按不命中）
# ---------------------------------------------------------------------------


def test_condition_missing_attribute_is_miss():
    node = {"type": "condition", "attribute": "region", "operator": "eq", "values": ["cn"]}
    hit, reason = seg.evaluate_condition(node, {})
    assert hit is False
    assert "缺少属性" in reason
    hit, _ = seg.evaluate_condition(node, {"region": None})
    assert hit is False


def test_condition_operators():
    eq = {"type": "condition", "attribute": "region", "operator": "eq", "values": ["cn"]}
    assert seg.evaluate_condition(eq, {"region": "cn"})[0] is True
    assert seg.evaluate_condition(eq, {"region": "us"})[0] is False

    num = {"type": "condition", "attribute": "age", "operator": "gte", "values": [18]}
    assert seg.evaluate_condition(num, {"age": 20})[0] is True
    # 类型不兼容（bool / 非数字）按不命中
    assert seg.evaluate_condition(num, {"age": "x"})[0] is False
    assert seg.evaluate_condition(num, {"age": True})[0] is False

    sfx = {"type": "condition", "attribute": "email", "operator": "suffix", "values": ["@c.com"]}
    assert seg.evaluate_condition(sfx, {"email": "a@c.com"})[0] is True
    assert seg.evaluate_condition(sfx, {"email": 1})[0] is False


# ---------------------------------------------------------------------------
# 嵌套 all / any / not 与短路
# ---------------------------------------------------------------------------


def expr_all_any():
    return {
        "type": "all",
        "children": [
            {"type": "condition", "attribute": "region", "operator": "in", "values": ["cn", "sg"]},
            {
                "type": "any",
                "children": [
                    {"type": "condition", "attribute": "tier", "operator": "eq", "values": ["vip"]},
                    {"type": "condition", "attribute": "age", "operator": "gte", "values": [60]},
                ],
            },
            {
                "type": "not",
                "child": {"type": "condition", "attribute": "banned", "operator": "eq", "values": [True]},
            },
        ],
    }


def test_all_any_not_combination_hit():
    hit, reason, trace = _eval(expr_all_any(), {"region": "cn", "age": 65})
    assert hit is True
    steps = [t["step"] for t in trace]
    assert "segment_group_entered" in steps
    assert "segment_group_evaluated" in steps


def _eval(expr, context):
    trace = []
    hit, reason = seg._evaluate_node(
        expr, context, _resolver({}), trace, active=(), path="$", layer=1, budget=[seg.MAX_EVAL_NODES]
    )
    return hit, reason, trace


def test_not_requires_absent_attribute_to_be_true():
    # not(banned == True)：banned 缺失 -> 条件不命中 -> not 命中
    expr = {
        "type": "not",
        "child": {"type": "condition", "attribute": "banned", "operator": "eq", "values": [True]},
    }
    hit, _, _ = _eval(expr, {})
    assert hit is True
    hit, _, _ = _eval(expr, {"banned": True})
    assert hit is False


def test_all_short_circuits_on_first_false():
    expr = {
        "type": "all",
        "children": [
            {"type": "condition", "attribute": "a", "operator": "eq", "values": [1]},
            {"type": "condition", "attribute": "b", "operator": "eq", "values": [2]},
        ],
    }
    hit, reason, trace = _eval(expr, {"a": 0, "b": 2})
    assert hit is False
    sc = next(t for t in trace if t["step"] == "segment_group_short_circuited")
    assert sc["detail"]["short_circuit_index"] == 0
    # 第二个子表达式不应被解析（短路）
    conds = [t for t in trace if t["step"] == "segment_condition_evaluated"]
    assert len(conds) == 1


def test_any_short_circuits_on_first_true():
    expr = {
        "type": "any",
        "children": [
            {"type": "condition", "attribute": "a", "operator": "eq", "values": [1]},
            {"type": "condition", "attribute": "b", "operator": "eq", "values": [2]},
        ],
    }
    hit, _, trace = _eval(expr, {"a": 1, "b": 2})
    assert hit is True
    sc = next(t for t in trace if t["step"] == "segment_group_short_circuited")
    assert sc["detail"]["short_circuit_index"] == 0
    conds = [t for t in trace if t["step"] == "segment_condition_evaluated"]
    assert len(conds) == 1


def test_trace_records_every_layer_entered_and_evaluated():
    mapping = {
        "vip": {"type": "condition", "attribute": "tier", "operator": "eq", "values": ["vip"]},
        "root": {
            "type": "all",
            "children": [
                {"type": "condition", "attribute": "region", "operator": "eq", "values": ["cn"]},
                {"type": "segment", "key": "vip"},
            ],
        },
    }
    hit, _, trace = seg.evaluate_segment_rule(
        "root", {"region": "cn", "tier": "vip"}, _resolver(mapping)
    )
    assert hit is True
    entered = [t["detail"].get("segment_key") for t in trace if t["step"] == "segment_entered"]
    evaluated = [t["detail"].get("segment_key") for t in trace if t["step"] == "segment_evaluated"]
    assert entered == ["root", "vip"]
    assert evaluated == ["vip", "root"]
    # 根层 depth=1，被引用层 depth=2
    root_entry = next(t for t in trace if t["step"] == "segment_entered" and t["detail"]["segment_key"] == "root")
    vip_entry = next(t for t in trace if t["step"] == "segment_entered" and t["detail"]["segment_key"] == "vip")
    assert root_entry["detail"]["depth"] == 1
    assert vip_entry["detail"]["depth"] == 2
    assert vip_entry["detail"]["ref_path"] == "$.children[1]"


def test_missing_referenced_segment_is_miss_with_trace():
    mapping = {"root": {"type": "segment", "key": "ghost"}}
    hit, reason, trace = seg.evaluate_segment_rule("root", {}, _resolver(mapping))
    assert hit is False
    assert "不存在" in reason
    assert trace[0]["step"] == "segment_entered"
    assert any(t["step"] == "segment_reference_missing" for t in trace)


def test_resolver_none_is_miss():
    hit, reason, trace = seg.evaluate_segment_rule("root", {}, None)
    assert hit is False
    assert "无法解析" in reason
    assert trace == []


# ---------------------------------------------------------------------------
# 保存期校验：引用缺失 / 循环 / 深度 / 大小
# ---------------------------------------------------------------------------


def test_validate_missing_reference():
    with pytest.raises(seg.SegmentValidationError, match="不存在"):
        seg.validate_segment_definition(
            "a", {"type": "segment", "key": "missing"}, existing={}
        )


def test_validate_direct_self_cycle():
    with pytest.raises(seg.SegmentValidationError, match="循环引用"):
        seg.validate_segment_definition(
            "a", {"type": "segment", "key": "a"}, existing={}
        )


def test_validate_indirect_cycle_on_update():
    existing = {
        "a": {"type": "segment", "key": "b"},
        "b": {"type": "segment", "key": "c"},
        "c": {"type": "condition", "attribute": "x", "operator": "eq", "values": [1]},
    }
    # 把 c 更新为引用 a -> a -> b -> c -> a 成环
    candidate = {"type": "segment", "key": "a"}
    with pytest.raises(seg.SegmentValidationError) as exc:
        seg.validate_segment_definition("c", candidate, {k: v for k, v in existing.items() if k != "c"})
    assert "循环引用" in str(exc.value)


def test_validate_no_cycle_accepts_dag():
    existing = {"base": {"type": "condition", "attribute": "x", "operator": "eq", "values": [1]}}
    seg.validate_segment_definition(
        "combo",
        {"type": "all", "children": [
            {"type": "segment", "key": "base"},
            {"type": "condition", "attribute": "y", "operator": "eq", "values": [2]},
        ]},
        existing,
    )


def test_validate_too_deep():
    # 构造超过 MAX_SEGMENT_DEPTH 的 not 嵌套链
    node = {"type": "condition", "attribute": "a", "operator": "eq", "values": [1]}
    for _ in range(seg.MAX_SEGMENT_DEPTH):
        node = {"type": "not", "child": node}
    with pytest.raises(seg.SegmentValidationError, match="深度"):
        seg.validate_segment_definition("deep", node, existing={})


def test_validate_too_many_nodes():
    children = [
        {"type": "condition", "attribute": f"a{i}", "operator": "eq", "values": [i]}
        for i in range(seg.MAX_EXPRESSION_NODES + 1)
    ]
    with pytest.raises(seg.SegmentValidationError, match="节点数"):
        seg.validate_segment_definition("big", {"type": "all", "children": children}, existing={})


def test_expansion_depth_counts_reference_chain():
    existing = {
        "a": {"type": "not", "child": {"type": "condition", "attribute": "x", "operator": "eq", "values": [1]}},
    }
    # b 引用 a：展开深度 = 1(b segment 节点) + depth(a=2) = 3
    existing["b"] = {"type": "segment", "key": "a"}
    assert seg.expansion_depth("b", existing) == 3


# ---------------------------------------------------------------------------
# decide() 集成 segment 规则
# ---------------------------------------------------------------------------



def test_decide_segment_rule_hit_and_miss():
    mapping = {
        "vip": {"type": "condition", "attribute": "tier", "operator": "eq", "values": ["vip"]},
    }
    rules = [Rule(1, None, "segment", [], "on", "vip")]
    variants = [Variant("on", 100), Variant("off", 0)]

    d = decide("f", "u1", {"tier": "vip"}, variants, rules, segment_resolver=_resolver(mapping))
    assert d.decision_path == "rule"
    assert d.variant_key == "on"
    assert d.matched_rule.segment_key == "vip"
    steps = [e["step"] for e in d.explanation]
    assert steps[0] == "rules_loaded"
    assert "segment_entered" in steps
    assert steps[-1] == "rule_selected"
    rule_step = next(e for e in d.explanation if e["step"] == "rule_evaluated")
    assert rule_step["detail"]["segment_key"] == "vip"
    assert rule_step["detail"]["result"] == "hit"

    d2 = decide("f", "u2", {"tier": "free"}, variants, rules, segment_resolver=_resolver(mapping))
    assert d2.decision_path == "bucket"
    assert d2.matched_rule is None


def test_decide_segment_rule_without_resolver_is_miss():
    rules = [Rule(1, None, "segment", [], "on", "vip")]
    variants = [Variant("on", 50), Variant("off", 50)]
    d = decide("f", "u", {}, variants, rules)
    assert d.decision_path == "bucket"
    rule_step = next(e for e in d.explanation if e["step"] == "rule_evaluated")
    assert rule_step["detail"]["result"] == "miss"


def test_runtime_layer_guard_on_pathological_chain():
    # 手工构造绕过保存期校验的超长无环引用链：运行时按层上限兜底，不抛 RecursionError
    chain = {f"s{i}": {"type": "segment", "key": f"s{i + 1}"} for i in range(500)}
    chain["s500"] = {"type": "condition", "attribute": "x", "operator": "eq", "values": [1]}
    hit, reason, trace = seg.evaluate_segment_rule(
        "s0", {"x": 1}, _resolver(chain)
    )
    assert hit is False
    assert "层数" in reason
    assert any(t["step"] == "segment_layer_limit_exceeded" for t in trace)


def test_runtime_node_budget_on_bushy_dag():
    # 根分群用任意浅层表达式重复引用同一分群多次（DAG，无环），触发节点预算。
    # 用 any + 恒假条件保证不短路，逐节点计费直到预算耗尽。
    base = {"type": "condition", "attribute": "x", "operator": "eq", "values": [1]}
    many_refs = {
        "type": "any",
        "children": [{"type": "segment", "key": "base"} for _ in range(seg.MAX_EVAL_NODES + 10)],
    }
    mapping = {"base": base, "root": many_refs}
    hit, reason, trace = seg.evaluate_segment_rule(
        "root", {"x": 2}, _resolver(mapping)
    )
    assert hit is False
    assert "节点数" in reason
    assert any(t["step"] == "segment_eval_budget_exceeded" for t in trace)


def test_runtime_cycle_is_miss_with_trace():
    # 手工构造循环 resolver（数据库层由保存校验保证不可能出现）
    mapping = {
        "a": {"type": "segment", "key": "b"},
        "b": {"type": "segment", "key": "a"},
    }
    hit, reason, trace = seg.evaluate_segment_rule("a", {}, _resolver(mapping))
    assert hit is False
    assert "循环引用" in reason
    assert any(t["step"] == "segment_cycle_detected" for t in trace)
    entered = [t["detail"]["segment_key"] for t in trace if t["step"] == "segment_entered"]
    assert entered == ["a", "b"]
