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
3. 没有规则命中时，对 ``flag_key + ":" + user_id`` 做 SHA-256 稳定哈希，
   映射到 0-99 的桶，再按变体权重顺序切桶选变体。

同一用户在配置不变时结果稳定：哈希输入只含 flag_key 与 user_id。
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app import segments as seg

# 桶空间固定为 100，与“权重总和必须为 100”对应
BUCKET_SPACE = 100


@dataclass(frozen=True)
class Rule:
    priority: int
    attribute: str | None
    operator: str
    values: list[Any]
    variant_key: str
    # operator == "segment" 时引用的受众分群 key；其余操作符为 None
    segment_key: str | None = None


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


def evaluate_rule(rule: Rule, context: dict[str, Any]) -> tuple[bool, str]:
    """评估单条规则。属性缺失或值为 None 一律视为未命中。"""
    if rule.attribute not in context or context[rule.attribute] is None:
        return False, f"缺少属性 {rule.attribute!r}，视为未命中"
    return _compare(context[rule.attribute], rule.operator, rule.values)


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
    segment_resolver: Callable[[str], dict[str, Any] | None] | None = None,
) -> Decision:
    """完整判定流程并产出解释链。

    ``operator="segment"`` 的规则通过 ``segment_resolver`` 递归解析受众
    分群（表达式可嵌套 all/any/not、属性条件并引用其他分群）；resolver
    缺失、被引用分群不存在或引用链成环均按未命中处理，分群每层的进入 /
    离开与短路结果以 ``segment_*`` 步骤并入解释链。
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
                    {
                        "rule_index": i,
                        "priority": r.priority,
                        "attribute": r.attribute,
                        "operator": r.operator,
                        "segment_key": r.segment_key,
                    }
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

        if rule.operator == "segment":
            hit, reason, trace = seg.evaluate_segment_rule(
                rule.segment_key, context, segment_resolver
            )
            # 分群内部每层判定（进入 / 条件 / 短路 / 离开）先并入解释链，
            # 再追加规则级步骤，保持“先因后果”的阅读顺序。
            explanation.extend(trace)
        else:
            hit, reason = evaluate_rule(rule, context)

        explanation.append(
            {
                "step": "rule_evaluated",
                "detail": {
                    "rule_index": index,
                    "priority": rule.priority,
                    "attribute": rule.attribute,
                    "operator": rule.operator,
                    "values": rule.values,
                    "segment_key": rule.segment_key,
                    "result": "hit" if hit else "miss",
                    "reason": reason,
                },
            }
        )
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
