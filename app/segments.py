"""受众分群表达式引擎（纯函数，易测）。

分群用可嵌套的 ``all`` / ``any`` / ``not`` 布尔节点组合属性比较条件
（``condition`` 叶子），并允许通过 ``segment`` 节点引用其他分群：

.. code-block:: json

    {
      "type": "all",
      "children": [
        {"type": "condition", "attribute": "region", "operator": "eq", "values": ["cn"]},
        {"type": "not", "child": {"type": "condition", "attribute": "banned", "operator": "eq", "values": [true]}},
        {"type": "any", "children": [
          {"type": "segment", "key": "vip_users"},
          {"type": "condition", "attribute": "age", "operator": "gte", "values": [60]}
        ]}
      ]
    }

判定语义：

- 引用其他分群时**递归解析**；被引用分群缺失（已删除 / 配置版本早于分群
  建立）或引用链成环（理论上保存时已拦截，此处防御性兜底）一律按**不命中**
  处理并在 trace 中注明原因；
- 条件所需属性缺失 / 值为 ``null`` / 类型不兼容时该条件按**不命中**处理；
- ``all`` 遇首个假子表达式短路，``any`` 遇首个真子表达式短路，短路结果
  与每一层分群进入 / 离开都记录在 trace 中，供开关判定解释链使用。

保存期校验（:func:`validate_segment_definition`）负责拦截：引用不存在的
分群、直接 / 间接循环引用（422）、过深嵌套与过大表达式。
"""

from typing import Any, Callable, Protocol

# 保存期约束
MAX_EXPRESSION_NODES = 500
# 分群完全内联展开后的最大深度（含被引用分群链）
MAX_SEGMENT_DEPTH = 32
# 单次判定递归解析的最大分群层数与节点预算（DAG 式重复引用也逐节点计费），
# 超出按不命中处理，防止恶意 / 病态引用链拖垮判定或撑爆 Python 调用栈
MAX_EVAL_LAYERS = 64
MAX_EVAL_NODES = 5000

# 属性比较操作符（与开关规则的比较语义一致；"segment" 是规则专用操作符，
# 不是分群条件操作符）
CONDITION_OPERATORS = frozenset(
    {"eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "prefix", "suffix", "contains"}
)


class SegmentValidationError(ValueError):
    """分群表达式保存期校验失败（引用缺失、循环引用、过深 / 过大）。"""


class SegmentResolver(Protocol):
    """按 key 解析被引用分群表达式；缺失返回 None。"""

    def __call__(self, key: str) -> dict[str, Any] | None: ...


# ---------------------------------------------------------------------------
# 属性比较（与 app.engine 的规则比较同语义，放在本模块避免循环导入）
# ---------------------------------------------------------------------------


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def compare(actual: Any, operator: str, expected: list[Any]) -> tuple[bool, str]:
    """执行单个操作符比较，返回 (是否命中, 原因说明)。"""
    target = expected[0]

    if operator == "eq":
        return actual == target, f"{actual!r} {'==' if actual == target else '!='} {target!r}"
    if operator == "ne":
        return actual != target, f"{actual!r} {'!=' if actual != target else '=='} {target!r}"
    if operator == "in":
        hit = actual in expected
        return hit, f"{actual!r} {'∈' if hit else '∉'} {expected!r}"
    if operator == "not_in":
        hit = actual not in expected
        return hit, f"{actual!r} {'∉' if hit else '∈'} {expected!r}"

    if operator in ("gt", "gte", "lt", "lte"):
        if not _is_number(actual) or not _is_number(target):
            return (
                False,
                f"数值比较要求两侧均为数字，得到 {type(actual).__name__} 与 {type(target).__name__}",
            )
        if operator == "gt":
            return actual > target, f"{actual} > {target} => {actual > target}"
        if operator == "gte":
            return actual >= target, f"{actual} >= {target} => {actual >= target}"
        if operator == "lt":
            return actual < target, f"{actual} < {target} => {actual < target}"
        return actual <= target, f"{actual} <= {target} => {actual <= target}"

    if operator in ("prefix", "suffix", "contains"):
        if not isinstance(actual, str) or not isinstance(target, str):
            return False, f"{operator} 要求两侧均为字符串"
        if operator == "prefix":
            hit = actual.startswith(target)
            return hit, f"{actual!r} startswith {target!r} => {hit}"
        if operator == "suffix":
            hit = actual.endswith(target)
            return hit, f"{actual!r} endswith {target!r} => {hit}"
        hit = target in actual
        return hit, f"{target!r} in {actual!r} => {hit}"

    return False, f"未知操作符 {operator}"


def evaluate_condition(node: dict[str, Any], context: dict[str, Any]) -> tuple[bool, str]:
    """评估单个条件叶子。属性缺失或值为 None 一律视为不命中。"""
    attribute = node["attribute"]
    if attribute not in context or context[attribute] is None:
        return False, f"缺少属性 {attribute!r}，视为未命中"
    return compare(context[attribute], node["operator"], list(node["values"]))


# ---------------------------------------------------------------------------
# 表达式遍历 / 保存期校验（全部迭代实现，避免深层嵌套触发递归限制）
# ---------------------------------------------------------------------------


def iter_nodes(expr: dict[str, Any]):
    """深度优先迭代遍历表达式节点（先父后子）。"""
    stack = [expr]
    while stack:
        node = stack.pop()
        yield node
        t = node.get("type")
        if t in ("all", "any"):
            stack.extend(reversed(node.get("children", [])))
        elif t == "not":
            stack.append(node.get("child"))
        # condition / segment 为叶子


def count_nodes(expr: dict[str, Any]) -> int:
    return sum(1 for _ in iter_nodes(expr))


def collect_segment_refs(expr: dict[str, Any]) -> set[str]:
    """收集表达式内全部 ``segment`` 引用的分群 key（含自身引用）。"""
    return {
        node["key"]
        for node in iter_nodes(expr)
        if node.get("type") == "segment" and node.get("key")
    }


def find_cycle(
    key: str,
    expressions: dict[str, dict[str, Any]],
) -> list[str] | None:
    """在引用图中检测从 ``key`` 出发能否回到 ``key``（直接 / 间接自引用）。

    ``expressions`` 为 key -> 表达式的映射（通常含被保存分群自身的候选
    表达式）。检测到环时返回闭环路径（首尾为同一分群，如
    ``["a", "b", "a"]``），无环返回 None。图中引用缺失不视为环。
    """
    stack: list[tuple[str, list[str]]] = [(key, [])]
    while stack:
        current, path = stack.pop()
        if current in path:
            cycle = path[path.index(current) :] + [current]
            return cycle
        expr = expressions.get(current)
        if expr is None:
            continue
        for ref in collect_segment_refs(expr):
            stack.append((ref, path + [current]))
    return None


def expansion_depth(
    key: str,
    expressions: dict[str, dict[str, Any]],
) -> int:
    """分群完全内联展开后的深度（引用链深度 + 各分群自身嵌套深度）。

    深度按展开后的节点树计算：``condition`` 叶子深 1；``all`` / ``any``
    为 1 + 最深子树；``not`` 为 1 + 子树；``segment`` 引用为
    1 + 被引用分群的展开深度。调用前应先用 :func:`find_cycle` 排除环。
    引用缺失按被引用方深度 0 处理。
    """
    memo: dict[str, int] = {}
    # 迭代后序 DFS：(segment_key, state)；state 0 = 进入, 1 = 子项已展开
    stack: list[tuple[str, int]] = [(key, 0)]
    while stack:
        current, state = stack.pop()
        if state == 1:
            expr = expressions.get(current)
            if expr is None or current in memo:
                memo.setdefault(current, 0)
                continue
            memo[current] = _node_depth(expr, expressions, memo)
            continue
        if current in memo:
            continue
        expr = expressions.get(current)
        if expr is None:
            memo[current] = 0
            continue
        stack.append((current, 1))
        for ref in collect_segment_refs(expr):
            if ref not in memo:
                stack.append((ref, 0))
    return memo.get(key, 0)


def _node_depth(
    node: dict[str, Any],
    expressions: dict[str, dict[str, Any]],
    memo: dict[str, int],
) -> int:
    t = node.get("type")
    if t == "condition":
        return 1
    if t == "segment":
        return 1 + memo.get(node["key"], 0)
    if t == "not":
        return 1 + _node_depth(node["child"], expressions, memo)
    if t in ("all", "any"):
        children = node.get("children") or []
        if not children:
            return 1
        return 1 + max(_node_depth(c, expressions, memo) for c in children)
    return 1


def validate_segment_definition(
    key: str,
    expr: dict[str, Any],
    existing: dict[str, dict[str, Any]],
) -> None:
    """保存分群前的全部引用约束校验，失败抛 :class:`SegmentValidationError`。

    ``existing`` 为当前已保存分群 key -> 表达式的映射；校验 ``key`` 自身的
    候选表达式时，引用图中 ``key`` 以候选表达式为准（更新场景）。
    """
    node_count = count_nodes(expr)
    if node_count > MAX_EXPRESSION_NODES:
        raise SegmentValidationError(
            f"分群表达式节点数为 {node_count}，超过上限 {MAX_EXPRESSION_NODES}"
        )

    refs = collect_segment_refs(expr)
    missing = sorted(r for r in refs if r != key and r not in existing)
    if missing:
        raise SegmentValidationError(
            f"表达式引用了不存在的分群：{', '.join(missing)}"
        )

    graph = dict(existing)
    graph[key] = expr
    cycle = find_cycle(key, graph)
    if cycle is not None:
        raise SegmentValidationError(
            "检测到分群循环引用：" + " -> ".join(cycle)
        )

    depth = expansion_depth(key, graph)
    if depth > MAX_SEGMENT_DEPTH:
        raise SegmentValidationError(
            f"分群引用展开深度为 {depth}，超过上限 {MAX_SEGMENT_DEPTH}"
        )


# ---------------------------------------------------------------------------
# 判定期递归解析 + 分层 trace
# ---------------------------------------------------------------------------


def evaluate_segment_rule(
    segment_key: str,
    context: dict[str, Any],
    resolver: Callable[[str], dict[str, Any] | None] | None,
) -> tuple[bool, str, list[dict[str, Any]]]:
    """评估操作符为 ``segment`` 的开关规则。

    返回 (是否命中, 原因, trace)。resolver 缺失（未接入持久层的纯引擎
    调用）、被引用分群不存在或引用链成环均按不命中处理。trace 记录
    每一层分群的进入 / 离开以及短路结果，可直接并入开关判定解释链。
    """
    trace: list[dict[str, Any]] = []
    if resolver is None:
        return False, f"分群 {segment_key!r} 无法解析（未提供分群读取器），视为未命中", trace
    hit, reason = _enter_segment(
        segment_key,
        context,
        resolver,
        trace,
        active=(),
        ref_path=None,
        depth=1,
        budget=[MAX_EVAL_NODES],
        root=True,
    )
    return hit, reason, trace


def _enter_segment(
    key: str,
    context: dict[str, Any],
    resolver: Callable[[str], dict[str, Any] | None],
    trace: list[dict[str, Any]],
    *,
    active: tuple[str, ...],
    ref_path: str | None,
    depth: int,
    budget: list[int],
    root: bool,
) -> tuple[bool, str]:
    """进入一层分群：记录进入 / 离开，递归解析其表达式。"""
    entry_detail = {
        "segment_key": key,
        "depth": depth,
        "ref_path": ref_path,
        "root": root,
    }
    if depth > MAX_EVAL_LAYERS:
        reason = f"分群引用层数超过上限 {MAX_EVAL_LAYERS}，按不命中处理"
        trace.append(
            {
                "step": "segment_layer_limit_exceeded",
                "detail": {**entry_detail, "reason": reason},
            }
        )
        return False, reason
    if key in active:
        chain = " -> ".join([*active, key])
        reason = f"检测到分群循环引用 {chain}，按不命中处理"
        trace.append({"step": "segment_cycle_detected", "detail": {**entry_detail, "reason": reason}})
        return False, reason

    expr = resolver(key)
    if expr is None:
        reason = f"被引用分群 {key!r} 不存在，按不命中处理"
        trace.append(
            {"step": "segment_reference_missing", "detail": {**entry_detail, "reason": reason}}
        )
        return False, reason

    trace.append({"step": "segment_entered", "detail": entry_detail})
    hit, reason = _evaluate_node(
        expr,
        context,
        resolver,
        trace,
        active=active + (key,),
        path="$",
        layer=depth,
        budget=budget,
    )
    trace.append(
        {
            "step": "segment_evaluated",
            "detail": {
                **entry_detail,
                "result": "hit" if hit else "miss",
                "reason": reason,
            },
        }
    )
    return hit, reason


def _evaluate_node(
    node: dict[str, Any],
    context: dict[str, Any],
    resolver: Callable[[str], dict[str, Any] | None],
    trace: list[dict[str, Any]],
    *,
    active: tuple[str, ...],
    path: str,
    layer: int,
    budget: list[int],
) -> tuple[bool, str]:
    """递归评估表达式节点，逐层追加 trace（迭代预算内）。

    ``layer`` 为当前分群层（根分群为 1，每跨过一个 segment 引用 +1），
    同一分群表达式内部的 all/any/not/condition 嵌套不增加层号。
    """
    if budget[0] <= 0:
        reason = f"分群判定节点数超过上限 {MAX_EVAL_NODES}，按不命中处理"
        trace.append(
            {
                "step": "segment_eval_budget_exceeded",
                "detail": {"path": path, "layer": layer, "reason": reason},
            }
        )
        return False, reason
    budget[0] -= 1

    t = node.get("type")

    if t == "condition":
        hit, reason = evaluate_condition(node, context)
        trace.append(
            {
                "step": "segment_condition_evaluated",
                "detail": {
                    "path": path,
                    "layer": layer,
                    "attribute": node["attribute"],
                    "operator": node["operator"],
                    "values": list(node["values"]),
                    "result": "hit" if hit else "miss",
                    "reason": reason,
                },
            }
        )
        return hit, reason

    if t == "segment":
        return _enter_segment(
            node["key"],
            context,
            resolver,
            trace,
            active=active,
            ref_path=path,
            depth=layer + 1,
            budget=budget,
            root=False,
        )

    if t == "not":
        child_hit, child_reason = _evaluate_node(
            node["child"],
            context,
            resolver,
            trace,
            active=active,
            path=f"{path}.child",
            layer=layer,
            budget=budget,
        )
        hit = not child_hit
        reason = f"not({child_hit}) => {hit}"
        trace.append(
            {
                "step": "segment_group_evaluated",
                "detail": {
                    "path": path,
                    "layer": layer,
                    "operator": "not",
                    "result": "hit" if hit else "miss",
                    "reason": reason,
                },
            }
        )
        return hit, reason

    if t in ("all", "any"):
        children = node.get("children") or []
        want = t == "all"
        trace.append(
            {
                "step": "segment_group_entered",
                "detail": {
                    "path": path,
                    "layer": layer,
                    "operator": t,
                    "child_count": len(children),
                },
            }
        )
        results: list[bool] = []
        aborted = False
        for i, child in enumerate(children):
            child_hit, child_reason = _evaluate_node(
                child,
                context,
                resolver,
                trace,
                active=active,
                path=f"{path}.children[{i}]",
                layer=layer,
                budget=budget,
            )
            results.append(child_hit)
            if child_hit is not want:
                # all 遇假 / any 遇真：记录短路，后续子表达式不再解析
                reason = (
                    f"{t} 在第 {i} 个子表达式处短路"
                    f"（结果 {'假' if want else '真'}），最终{'命中' if not want else '不命中'}"
                )
                trace.append(
                    {
                        "step": "segment_group_short_circuited",
                        "detail": {
                            "path": path,
                            "layer": layer,
                            "operator": t,
                            "short_circuit_index": i,
                            "evaluated_results": results,
                            "reason": reason,
                        },
                    }
                )
                trace.append(
                    {
                        "step": "segment_group_evaluated",
                        "detail": {
                            "path": path,
                            "layer": layer,
                            "operator": t,
                            "result": "miss" if want else "hit",
                            "reason": reason,
                        },
                    }
                )
                return not want, reason
            if budget[0] <= 0:
                # 预算在子表达式判定中耗尽（或层数越界）：终止本组合，
                # 沿用子表达式给出的失败原因，避免被“全部为假”覆盖。
                aborted = True
                reason = child_reason
                break
        if aborted:
            trace.append(
                {
                    "step": "segment_group_evaluated",
                    "detail": {
                        "path": path,
                        "layer": layer,
                        "operator": t,
                        "result": "miss",
                        "reason": reason,
                    },
                }
            )
            return False, reason
        hit = want  # all: 全真为真；any: 全假为假（空 children 时 all=True/any=False）
        reason = (
            f"全部 {len(results)} 个子表达式均为{'真' if want else '假'}，"
            f"{'命中' if hit else '不命中'}"
        )
        trace.append(
            {
                "step": "segment_group_evaluated",
                "detail": {
                    "path": path,
                    "layer": layer,
                    "operator": t,
                    "result": "hit" if hit else "miss",
                    "reason": reason,
                },
            }
        )
        return hit, reason

    reason = f"未知表达式类型 {t!r}，按不命中处理"
    trace.append(
        {
            "step": "segment_condition_evaluated",
            "detail": {"path": path, "layer": layer, "result": "miss", "reason": reason},
        }
    )
    return False, reason
