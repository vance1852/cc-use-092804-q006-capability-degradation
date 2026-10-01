"""能力降级编排的确定性决策引擎。

输入为冻结在计划版本中的四类事实：任务所需能力、部件健康摘要、
安全限制和可替代执行链目录；输出为可解释的四档结论
（可继续 / 受限运行 / 等待人工 / 安全停机）以及回执分类结果。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Sequence


DECISIONS = ("continue", "restricted", "wait_human", "safe_stop")
DECISION_TEXT = {
    "continue": "可继续",
    "restricted": "受限运行",
    "wait_human": "等待人工",
    "safe_stop": "安全停机",
}
MODES = ("nominal", "restricted", "waiting_human", "safe_stopped")
MODE_SEVERITY = {"nominal": 0, "restricted": 1, "waiting_human": 2, "safe_stopped": 3}
DECISION_TARGET_MODE = {
    "continue": "nominal",
    "restricted": "restricted",
    "wait_human": "waiting_human",
    "safe_stop": "safe_stopped",
}
RECEIPT_OUTCOMES = ("applied", "out_of_order", "rejected_stale", "rejected_terminal")
RESTRICTION_KEYS = ("max_speed_percent", "min_obstacle_distance_m")


def component_usability(
    component_id: str,
    health_summary: Mapping[str, Mapping[str, Any]],
    metric_limits: Mapping[str, Decimal],
) -> tuple[str, list[str]]:
    """按健康摘要与安全限制判定部件可用性，并给出原因。"""
    report = health_summary.get(component_id)
    if report is None:
        return "unavailable", [f"部件 {component_id} 缺少健康报告"]
    reasons: list[str] = []
    metrics = report.get("metrics", {})
    for metric, limit in sorted(metric_limits.items()):
        if metric in metrics and Decimal(str(metrics[metric])) > limit:
            reasons.append(
                f"部件 {component_id} 指标 {metric}={metrics[metric]} 超过安全限制 {limit}"
            )
    status = str(report.get("status", ""))
    if status == "failed":
        reasons.append(f"部件 {component_id} 健康状态为 failed")
    if reasons:
        return "unavailable", reasons
    if status == "degraded":
        return "degraded", [f"部件 {component_id} 健康状态为 degraded"]
    return "ok", []


def _chain_usability(
    chain: Mapping[str, Any],
    health_summary: Mapping[str, Mapping[str, Any]],
    metric_limits: Mapping[str, Decimal],
) -> str:
    worst = "ok"
    for component_id in chain["requires_components"]:
        usability, _ = component_usability(component_id, health_summary, metric_limits)
        if usability == "unavailable":
            return "unavailable"
        if usability == "degraded":
            worst = "degraded"
    return worst


def merge_restrictions(chains: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """合并所选执行链的运行限制：速度取最严，避障距离取最大。"""
    speed: int | None = None
    distance: Decimal | None = None
    for chain in chains:
        restrictions = chain.get("restrictions", {})
        if "max_speed_percent" in restrictions:
            value = int(restrictions["max_speed_percent"])
            speed = value if speed is None else min(speed, value)
        if "min_obstacle_distance_m" in restrictions:
            value = Decimal(str(restrictions["min_obstacle_distance_m"]))
            distance = value if distance is None else max(distance, value)
    merged: dict[str, str] = {}
    if speed is not None:
        merged["max_speed_percent"] = str(speed)
    if distance is not None:
        merged["min_obstacle_distance_m"] = format(distance, "f")
    return merged


def evaluate_degradation(
    *,
    required_capabilities: Sequence[Mapping[str, Any]],
    chains: Sequence[Mapping[str, Any]],
    health_summary: Mapping[str, Mapping[str, Any]],
    metric_limits: Mapping[str, Decimal],
) -> dict[str, Any]:
    """对冻结输入求值，返回结论与可解释明细。"""
    reasons: list[str] = []
    selected: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    manual_actions: list[str] = []

    involved = sorted({
        component
        for chain in chains
        for component in chain["requires_components"]
    })
    for component_id in involved:
        usability, component_reasons = component_usability(component_id, health_summary, metric_limits)
        if usability != "ok":
            reasons.extend(component_reasons)

    for requirement in sorted(required_capabilities, key=lambda item: item["capability"]):
        capability = requirement["capability"]
        min_level = int(requirement["min_level"])
        candidates = sorted(
            (
                chain
                for chain in chains
                if chain["capability"] == capability and int(chain["level"]) >= min_level
            ),
            key=lambda chain: (int(chain["priority"]), -int(chain["level"]), chain["chain_id"]),
        )
        immediate = [chain for chain in candidates if not chain.get("requires_manual_action")]
        manual = [chain for chain in candidates if chain.get("requires_manual_action")]
        chosen: Mapping[str, Any] | None = None
        degraded = False
        fallback: Mapping[str, Any] | None = None
        for chain in immediate:
            usability = _chain_usability(chain, health_summary, metric_limits)
            if usability == "ok":
                chosen = chain
                break
            if usability == "degraded" and fallback is None:
                fallback = chain
        if chosen is None and fallback is not None:
            chosen = fallback
            degraded = True
        if chosen is None:
            missing.append({"capability": capability, "min_level": min_level})
            reasons.append(f"能力 {capability} 需要等级 {min_level}，当前没有可用的执行链")
            for chain in manual:
                if _chain_usability(chain, health_summary, metric_limits) != "unavailable":
                    action = str(chain["requires_manual_action"])
                    manual_actions.append(action)
                    reasons.append(f"能力 {capability} 可经人工动作恢复：{action}")
                    break
            continue
        entry = {
            "capability": capability,
            "chain_id": chosen["chain_id"],
            "level": int(chosen["level"]),
            "degraded": degraded,
            "restrictions": dict(chosen.get("restrictions", {})),
            "requires_resources": [dict(item) for item in chosen.get("requires_resources", [])],
        }
        selected.append(entry)
        notes: list[str] = []
        if degraded:
            notes.append("降级部件")
        if chosen.get("restrictions"):
            notes.append("带运行限制")
        if notes:
            reasons.append(
                f"能力 {capability} 由执行链 {chosen['chain_id']} 提供（{'、'.join(notes)}）"
            )

    if missing:
        decision = "wait_human" if len(manual_actions) >= len(missing) and manual_actions else "safe_stop"
        restrictions: dict[str, str] = {}
    else:
        restrictions = merge_restrictions([entry for entry in selected])
        limited = any(entry["degraded"] or entry["restrictions"] for entry in selected)
        decision = "restricted" if limited or restrictions else "continue"

    required_resources: dict[str, int] = {}
    for entry in selected:
        for item in entry["requires_resources"]:
            resource_id = str(item["resource_id"])
            required_resources[resource_id] = required_resources.get(resource_id, 0) + int(item["units"])

    reasons.append(f"结论：{DECISION_TEXT[decision]}")
    return {
        "decision": decision,
        "decision_text": DECISION_TEXT[decision],
        "target_mode": DECISION_TARGET_MODE[decision],
        "reasons": reasons,
        "missing_capabilities": missing,
        "selected_chains": selected,
        "restrictions": restrictions,
        "manual_actions": manual_actions,
        "required_resources": [
            {"resource_id": resource_id, "units": units}
            for resource_id, units in sorted(required_resources.items())
        ],
    }


def classify_receipt(
    *,
    plan_state: str,
    plan_purpose: str,
    robot_mode: str,
    reported_mode: str,
    seq: int,
    last_applied_seq: int,
) -> str:
    """对执行回执分类：生效、乱序、计划已失效或安全终态拒绝。"""
    if plan_state not in ("confirmed", "executing"):
        return "rejected_stale"
    if seq <= last_applied_seq:
        return "out_of_order"
    if plan_purpose == "degrade" and MODE_SEVERITY[reported_mode] < MODE_SEVERITY[robot_mode]:
        return "rejected_terminal" if robot_mode == "safe_stopped" else "out_of_order"
    return "applied"
