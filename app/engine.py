"""功能开关判定引擎。

判定顺序：
0. 配置带生效窗口（activation_window）且当前时刻落在窗口
   ``[active_from, active_until)`` 之外时，跳过 override、规则与分桶，
   直接返回兜底变体 inactive_variant_key，decision_path="inactive"。
1. 未指定 config_version 时，先查该 flag+user 是否存在“未过期且变体仍存在于
   当前配置”的临时强制变体（override）：命中则跳过规则与分桶，直接返回该变体，
   decision_path="override"。到期或变体已移除的记录不生效，继续正常判定。
2. 按 priority 升序逐条评估规则，返回首个命中规则的 variant_key；
   规则所需属性缺失（或类型不兼容）一律视为“未命中”并继续下一条。
   ``operator="segment"`` 的规则引用受众分群：递归解析分群表达式
   （all/any/not 组合、属性比较、嵌套分群引用），属性缺失按不命中处理，
   引用的分群不存在 / 检测到循环 / 超过嵌套上限同样按不命中处理；
   解释链记录每层分群（segment_evaluated）与组合子短路结果
   （segment_combinator_evaluated / segment_condition_evaluated）。
3. 没有规则命中时，对 ``flag_key + ":" + user_id`` 做 SHA-256 稳定哈希，
   映射到 0-99 的桶，再按变体权重顺序切桶选变体。

同一用户在配置不变时结果稳定：哈希输入只含 flag_key 与 user_id。
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# 桶空间固定为 100，与“权重总和必须为 100”对应
BUCKET_SPACE = 100

# 分群引用解析的最大嵌套层数（防御性上限；API 保存时已保证无环，
# 此上限只兜底手工构造的异常数据，超出按未命中处理并记录原因）
MAX_SEGMENT_DEPTH = 64


@dataclass(frozen=True)
class Rule:
    priority: int
    attribute: str
    operator: str
    values: list[Any]
    variant_key: str


@dataclass(frozen=True)
class Variant:
    key: str
    weight: int
    description: str = ""


@dataclass(frozen=True)
class Override:
    """按用户的临时强制变体（判定前由持久层读取）。"""

    variant_key: str
    reason: str
    expires_at: Any = None  # naive UTC datetime 或 None（不过期）


@dataclass(frozen=True)
class ActivationWindow:
    """配置的版本化生效窗口（时间均为带时区的 UTC，至少一个边界非空）。

    生效区间为 ``[active_from, active_until)``：起点含、终点不含；
    缺省的一侧表示该方向不设限。
    """

    inactive_variant_key: str
    active_from: datetime | None = None
    active_until: datetime | None = None


class RuleEvaluationError(ValueError):
    """配置本身非法（如规则指向不存在的变体）。"""


def stable_bucket(flag_key: str, user_id: str) -> int:
    """返回 0..99 的稳定分桶值。

    SHA-256("flag_key:user_id") 大端取前 8 字节对 100 取模；
    仅依赖 flag_key 与 user_id，配置不变则结果不变。
    """
    raw = f"{flag_key}:{user_id}".encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return int.from_bytes(digest[:8], "big") % BUCKET_SPACE


def _compare(actual: Any, operator: str, expected: list[Any]) -> tuple[bool, str]:
    """执行单个操作符比较，返回 (是否命中, 原因说明)。"""
    target = expected[0]

    if operator == "eq":
        return actual == target, f"{actual!r} {'==' if actual == target else '!='} {target!r}"
    if operator == "ne":
        return actual != target, f"{actual!r} {'!=' if actual != target else '=='} {target!r}"
    if operator == "in":
        return actual in expected, f"{actual!r} {'∈' if actual in expected else '∉'} {expected!r}"
    if operator == "not_in":
        return actual not in expected, f"{actual!r} {'∉' if actual not in expected else '∈'} {expected!r}"

    if operator in ("gt", "gte", "lt", "lte"):
        if isinstance(actual, bool) or isinstance(target, bool) or not _is_number(actual) or not _is_number(target):
            return False, f"数值比较要求两侧均为数字，得到 {type(actual).__name__} 与 {type(target).__name__}"
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
            return actual.startswith(target), f"{actual!r} startswith {target!r} => {actual.startswith(target)}"
        if operator == "suffix":
            return actual.endswith(target), f"{actual!r} endswith {target!r} => {actual.endswith(target)}"
        return target in actual, f"{target!r} in {actual!r} => {target in actual}"

    return False, f"未知操作符 {operator}"


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float))


def evaluate_rule(
    rule: Rule,
    context: dict[str, Any],
    segments: dict[str, dict] | None = None,
    segment_steps: list[dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    """评估单条规则。属性缺失或值为 None 一律视为未命中。

    ``operator="segment"`` 时 ``values[0]`` 为分群 key：递归解析分群
    表达式（``segments`` 为 segment_key -> 表达式字典），引用的分群
    不存在、检测到循环引用或超过嵌套上限一律视为未命中；评估轨迹
    （每层分群与短路结果）追加到 ``segment_steps``（提供时）。
    """
    if rule.operator == "segment":
        key = rule.values[0] if rule.values else ""
        steps = segment_steps if segment_steps is not None else []
        hit = _eval_segment_ref(key, context, segments or {}, 1, (), steps)
        return hit, f"分群 {key!r} 判定为{'命中' if hit else '未命中'}"
    if rule.attribute not in context or context[rule.attribute] is None:
        return False, f"缺少属性 {rule.attribute!r}，视为未命中"
    return _compare(context[rule.attribute], rule.operator, rule.values)


# ---------------------------------------------------------------------------
# 受众分群表达式求值（纯函数，表达式为 JSON 兼容的 dict 树）
# ---------------------------------------------------------------------------


def _eval_segment_ref(
    key: str,
    context: dict[str, Any],
    segments: dict[str, dict],
    depth: int,
    resolving: tuple[str, ...],
    steps: list[dict[str, Any]],
) -> bool:
    """解析一层分群引用，返回是否命中；轨迹追加到 steps。

    ``depth`` 为分群嵌套层数（规则直接引用的分群为第 1 层）；
    ``resolving`` 为当前解析路径上的分群 key，用于防御循环引用。
    """
    if key not in segments:
        steps.append(
            {
                "step": "segment_evaluated",
                "detail": {
                    "segment_key": key,
                    "depth": depth,
                    "result": "miss",
                    "reason": f"引用的分群 {key!r} 不存在，视为未命中",
                },
            }
        )
        return False
    if key in resolving:
        cycle = " -> ".join([*resolving, key])
        steps.append(
            {
                "step": "segment_evaluated",
                "detail": {
                    "segment_key": key,
                    "depth": depth,
                    "result": "miss",
                    "reason": f"检测到循环引用 {cycle}，视为未命中",
                },
            }
        )
        return False
    if depth > MAX_SEGMENT_DEPTH:
        steps.append(
            {
                "step": "segment_evaluated",
                "detail": {
                    "segment_key": key,
                    "depth": depth,
                    "result": "miss",
                    "reason": f"超过最大嵌套层数 {MAX_SEGMENT_DEPTH}，视为未命中",
                },
            }
        )
        return False
    result = _eval_segment_expr(
        segments[key], context, segments, depth, (*resolving, key), steps
    )
    steps.append(
        {
            "step": "segment_evaluated",
            "detail": {
                "segment_key": key,
                "depth": depth,
                "result": "hit" if result else "miss",
                "reason": f"分群 {key!r} 表达式判定为{'命中' if result else '未命中'}",
            },
        }
    )
    return result


def _eval_segment_expr(
    expr: dict,
    context: dict[str, Any],
    segments: dict[str, dict],
    depth: int,
    resolving: tuple[str, ...],
    steps: list[dict[str, Any]],
) -> bool:
    """评估分群表达式树节点（all/any/not/条件叶子/分群引用）。"""
    if "all" in expr:
        children = expr["all"]
        evaluated = 0
        result = True
        for child in children:
            evaluated += 1
            if not _eval_segment_expr(child, context, segments, depth, resolving, steps):
                result = False
                break  # 短路：首个未命中即终止
        short_circuited = evaluated < len(children)
        if result:
            reason = f"all：全部 {len(children)} 个子表达式命中"
        elif short_circuited:
            reason = (
                f"all：第 {evaluated}/{len(children)} 个子表达式未命中，"
                f"短路跳过后续 {len(children) - evaluated} 个"
            )
        else:
            reason = f"all：第 {evaluated}/{len(children)} 个子表达式未命中"
        steps.append(_combinator_step("all", depth, result, evaluated, len(children), reason))
        return result
    if "any" in expr:
        children = expr["any"]
        evaluated = 0
        result = False
        for child in children:
            evaluated += 1
            if _eval_segment_expr(child, context, segments, depth, resolving, steps):
                result = True
                break  # 短路：首个命中即终止
        short_circuited = evaluated < len(children)
        if not result:
            reason = f"any：全部 {len(children)} 个子表达式均未命中"
        elif short_circuited:
            reason = (
                f"any：第 {evaluated}/{len(children)} 个子表达式命中，"
                f"短路跳过后续 {len(children) - evaluated} 个"
            )
        else:
            reason = f"any：第 {evaluated}/{len(children)} 个子表达式命中"
        steps.append(_combinator_step("any", depth, result, evaluated, len(children), reason))
        return result
    if "not" in expr:
        child_hit = _eval_segment_expr(
            expr["not"], context, segments, depth, resolving, steps
        )
        result = not child_hit
        steps.append(
            _combinator_step(
                "not",
                depth,
                result,
                1,
                1,
                f"not：子表达式{'命中' if child_hit else '未命中'}，"
                f"取反为{'命中' if result else '未命中'}",
            )
        )
        return result
    if "segment" in expr:
        return _eval_segment_ref(
            expr["segment"], context, segments, depth + 1, resolving, steps
        )
    # 条件叶子：属性缺失 / 值为 None 一律视为未命中
    attribute = expr.get("attribute", "")
    operator = expr.get("operator", "eq")
    values = expr.get("values", [])
    if attribute not in context or context[attribute] is None:
        hit, reason = False, f"缺少属性 {attribute!r}，视为未命中"
    else:
        hit, reason = _compare(context[attribute], operator, values)
    steps.append(
        {
            "step": "segment_condition_evaluated",
            "detail": {
                "depth": depth,
                "attribute": attribute,
                "operator": operator,
                "values": values,
                "result": "hit" if hit else "miss",
                "reason": reason,
            },
        }
    )
    return hit


def _combinator_step(
    combinator: str,
    depth: int,
    result: bool,
    children_evaluated: int,
    children_total: int,
    reason: str,
) -> dict[str, Any]:
    return {
        "step": "segment_combinator_evaluated",
        "detail": {
            "combinator": combinator,
            "depth": depth,
            "result": "hit" if result else "miss",
            "children_total": children_total,
            "children_evaluated": children_evaluated,
            "short_circuited": children_evaluated < children_total,
            "reason": reason,
        },
    }


def pick_variant_by_weight(variants: list[Variant], bucket: int) -> tuple[str, tuple[int, int]]:
    """按 variants 顺序切 [0,100) 的桶区间，返回 (variant_key, 命中区间)。

    权重为 0 的变体不会被选中。
    """
    cursor = 0
    for v in variants:
        start, end = cursor, cursor + v.weight
        if start <= bucket < end:
            return v.key, (start, end)
        cursor = end
    # 理论不可达：权重已保证总和 100
    raise RuleEvaluationError(f"分桶 {bucket} 未落入任何变体区间，请检查权重配置")


@dataclass
class Decision:
    variant_key: str
    decision_path: str  # "rule" | "bucket" | "override" | "inactive"
    bucket_value: int | None
    matched_rule: Rule | None
    matched_rule_index: int | None  # 排序后从 0 开始
    explanation: list[dict[str, Any]]


def override_decision(ov: Override) -> Decision:
    """临时强制变体命中：跳过规则与分桶，直接产出判定结果与解释链。

    变体是否仍存在于当前配置、是否已过期由调用方在读取阶段判定，
    进入本函数的 override 一定是“当前有效”的。
    """
    detail = {
        "variant_key": ov.variant_key,
        "reason": ov.reason,
        "expires_at": ov.expires_at.isoformat() if ov.expires_at is not None else None,
    }
    explanation = [
        {
            "step": "override_loaded",
            "detail": {
                "variant_key": ov.variant_key,
                "reason": ov.reason,
                "expires_at": detail["expires_at"],
            },
        },
        {
            "step": "override_selected",
            "detail": {
                **detail,
                "selection_reason": "命中按用户的临时强制变体，跳过规则判定与哈希分桶",
            },
        },
    ]
    return Decision(
        variant_key=ov.variant_key,
        decision_path="override",
        bucket_value=None,
        matched_rule=None,
        matched_rule_index=None,
        explanation=explanation,
    )


def window_is_active(window: ActivationWindow, now: datetime) -> bool:
    """判定 now 是否落在窗口 ``[active_from, active_until)`` 内。

    起点含（now == active_from 视为生效）、终点不含（now == active_until
    视为失效）；缺省的边界不参与判断。now 与边界均为带时区时间。
    """
    if window.active_from is not None and now < window.active_from:
        return False
    if window.active_until is not None and now >= window.active_until:
        return False
    return True


def inactive_decision(window: ActivationWindow, now: datetime) -> Decision:
    """窗口外判定：跳过 override、规则与分桶，返回兜底变体及边界说明。

    进入本函数时窗口一定未生效（由 window_is_active 判定）；解释链指明
    触发的是哪一侧边界（active_from 尚未到达 / active_until 已经越过）。
    """
    if window.active_from is not None and now < window.active_from:
        boundary = "active_from"
        boundary_reason = (
            f"当前时刻 {now.isoformat()} 早于生效起点 "
            f"{window.active_from.isoformat()}，窗口尚未生效"
        )
    else:
        boundary = "active_until"
        boundary_reason = (
            f"当前时刻 {now.isoformat()} 不早于生效终点 "
            f"{window.active_until.isoformat()}，窗口已结束"
        )
    window_detail = {
        "active_from": window.active_from.isoformat() if window.active_from else None,
        "active_until": window.active_until.isoformat() if window.active_until else None,
        "inactive_variant_key": window.inactive_variant_key,
    }
    explanation = [
        {
            "step": "activation_window_loaded",
            "detail": {**window_detail, "now": now.isoformat()},
        },
        {
            "step": "activation_window_inactive",
            "detail": {
                **window_detail,
                "boundary": boundary,
                "reason": boundary_reason,
                "selection_reason": "窗口外跳过 override、规则判定与哈希分桶，返回兜底变体",
            },
        },
    ]
    return Decision(
        variant_key=window.inactive_variant_key,
        decision_path="inactive",
        bucket_value=None,
        matched_rule=None,
        matched_rule_index=None,
        explanation=explanation,
    )


def decide(
    flag_key: str,
    user_id: str,
    context: dict[str, Any],
    variants: list[Variant],
    rules: list[Rule],
    segments: dict[str, dict] | None = None,
) -> Decision:
    """完整判定流程并产出解释链。

    ``segments`` 为 segment_key -> 表达式字典（operator=segment 的规则
    按此递归解析）；缺省视为无分群，引用分群的规则一律未命中。
    """
    explanation: list[dict[str, Any]] = []
    variant_keys = {v.key for v in variants}

    ordered = sorted(rules, key=lambda r: r.priority)
    explanation.append(
        {
            "step": "rules_loaded",
            "detail": {
                "rule_count": len(ordered),
                "evaluation_order": [
                    {"rule_index": i, "priority": r.priority, "attribute": r.attribute}
                    for i, r in enumerate(ordered)
                ],
            },
        }
    )

    # 1) 有序规则判定
    for index, rule in enumerate(ordered):
        if rule.variant_key not in variant_keys:
            raise RuleEvaluationError(
                f"priority={rule.priority} 的规则引用了不存在的变体 {rule.variant_key!r}"
            )
        segment_steps: list[dict[str, Any]] = []
        hit, reason = evaluate_rule(rule, context, segments, segment_steps)
        explanation.append(
            {
                "step": "rule_evaluated",
                "detail": {
                    "rule_index": index,
                    "priority": rule.priority,
                    "attribute": rule.attribute,
                    "operator": rule.operator,
                    "values": rule.values,
                    "result": "hit" if hit else "miss",
                    "reason": reason,
                },
            }
        )
        # 分群规则的递归解析轨迹（每层分群 + 短路结果）跟在规则步骤之后
        explanation.extend(segment_steps)
        if hit:
            explanation.append(
                {
                    "step": "rule_selected",
                    "detail": {
                        "rule_index": index,
                        "priority": rule.priority,
                        "variant_key": rule.variant_key,
                        "reason": "首个命中规则，跳过哈希分桶",
                    },
                }
            )
            return Decision(
                variant_key=rule.variant_key,
                decision_path="rule",
                bucket_value=None,
                matched_rule=rule,
                matched_rule_index=index,
                explanation=explanation,
            )

    # 2) 稳定哈希分桶
    explanation.append(
        {"step": "no_rule_matched", "detail": {"reason": "所有规则均未命中，进入哈希分桶"}}
    )
    bucket = stable_bucket(flag_key, user_id)
    explanation.append(
        {
            "step": "bucket_computed",
            "detail": {
                "hash_input": f"{flag_key}:{user_id}",
                "algorithm": "sha256[:8] % 100",
                "bucket_value": bucket,
            },
        }
    )

    variant_key, (start, end) = pick_variant_by_weight(variants, bucket)
    ranges = []
    cursor = 0
    for v in variants:
        ranges.append({"variant_key": v.key, "weight": v.weight, "range": [cursor, cursor + v.weight]})
        cursor += v.weight

    explanation.append(
        {
            "step": "variant_selected",
            "detail": {
                "bucket_value": bucket,
                "selected_variant": variant_key,
                "selected_range": [start, end],
                "variant_ranges": ranges,
            },
        }
    )

    return Decision(
        variant_key=variant_key,
        decision_path="bucket",
        bucket_value=bucket,
        matched_rule=None,
        matched_rule_index=None,
        explanation=explanation,
    )
