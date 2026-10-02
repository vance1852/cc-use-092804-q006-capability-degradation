"""能力降级计划版本的纯计算：能力匹配、可替代执行链选择与解释性结论。

结论分为四档：

- ``continue``：任务所需能力全部由名义部件提供，按主执行链继续；
- ``restricted``：存在降级部件或部分能力由人工补偿，按替代链和安全限制受限运行；
- ``wait_human``：替代链需要人工动作补偿，动作证据未齐，等待人工；
- ``safe_stop``：安全限制要求的能力缺失或关键受损且无名义替代链，安全停机。

本模块只做确定性计算，不访问数据库，便于离线复核。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .errors import ValidationFailed
from .planning import canonical_json, decimal_text, digest


CONTINUE = "continue"
RESTRICTED = "restricted"
WAIT_HUMAN = "wait_human"
SAFE_STOP = "safe_stop"
CONCLUSIONS = (CONTINUE, RESTRICTED, WAIT_HUMAN, SAFE_STOP)

NOMINAL = "nominal"
DEGRADED = "degraded"
CRITICAL = "critical"
FAILED = "failed"
COMPONENT_STATUSES = (NOMINAL, DEGRADED, CRITICAL, FAILED)
IMPAIRED_STATUSES = (DEGRADED, CRITICAL)

ON_MISSING_POLICIES = ("safe_stop", "wait_human", "alternative")
ON_IMPAIRED_POLICIES = ("safe_stop", "restrict", "wait_human")


@dataclass(frozen=True, slots=True)
class HealthComponent:
    """部件健康摘要中的一个条目。"""

    component_id: str
    status: str
    provides: frozenset[str]
    health_reason: str | None
    recovery_evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SafetyLimit:
    """任务安全限制：某项能力缺失或受损时允许的处置与运行包络。"""

    applies_to: str
    on_missing: str
    on_impaired: str
    restrictions: Mapping[str, Decimal]


@dataclass(frozen=True, slots=True)
class ChainNode:
    """执行链上的一个控制资源节点，节点标识必须能对应部件摘要。"""

    node_id: str
    provides: frozenset[str]


@dataclass(frozen=True, slots=True)
class ExecutionChain:
    chain_id: str
    rank: int
    allows_partial: bool
    nodes: tuple[ChainNode, ...]


@dataclass(frozen=True, slots=True)
class ManualAction:
    action_id: str
    description: str
    compensates_capability: str | None


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationFailed(f"{path} 必须是对象")
    return value


def _sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationFailed(f"{path} 必须是数组")
    return value


def _text(value: object, path: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{path} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{path} 不能超过 {maximum} 个字符")
    return result


def _capability_set(value: object, path: str) -> frozenset[str]:
    items = _sequence(value, path)
    result: set[str] = set()
    for index, item in enumerate(items):
        name = _text(item, f"{path}[{index}]", 64)
        if name in result:
            raise ValidationFailed(f"{path} 不能重复: {name}")
        result.add(name)
    if not result:
        raise ValidationFailed(f"{path} 不能为空")
    return frozenset(result)


def _decimal(value: object, path: str) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{path} 必须是数值")
    try:
        result = Decimal(str(value))
    except (ValueError, ArithmeticError) as exc:
        raise ValidationFailed(f"{path} 必须是十进制数值") from exc
    if not result.is_finite() or result < 0:
        raise ValidationFailed(f"{path} 必须是非负有限数值")
    return result


def parse_components(raw: object) -> tuple[HealthComponent, ...]:
    components: list[HealthComponent] = []
    seen: set[str] = set()
    for index, item in enumerate(_sequence(raw, "components")):
        data = _mapping(item, f"components[{index}]")
        component_id = _text(data.get("component_id"), f"components[{index}].component_id", 64)
        if component_id in seen:
            raise ValidationFailed(f"部件编号重复: {component_id}")
        seen.add(component_id)
        status = _text(data.get("status"), f"components[{index}].status", 16)
        if status not in COMPONENT_STATUSES:
            raise ValidationFailed(f"components[{index}].status 不受支持")
        evidence = tuple(
            _text(value, f"components[{index}].recovery_evidence[{sub}]", 128)
            for sub, value in enumerate(_sequence(data.get("recovery_evidence", ()), f"components[{index}].recovery_evidence"))
        )
        reason_value = data.get("health_reason")
        components.append(
            HealthComponent(
                component_id=component_id,
                status=status,
                provides=_capability_set(data.get("provides"), f"components[{index}].provides"),
                health_reason=None if reason_value is None else _text(reason_value, f"components[{index}].health_reason", 512),
                recovery_evidence=evidence,
            )
        )
    if not components:
        raise ValidationFailed("components 不能为空")
    return tuple(components)


def parse_safety_limits(raw: object, required: frozenset[str]) -> tuple[SafetyLimit, ...]:
    limits: list[SafetyLimit] = []
    seen: set[str] = set()
    for index, item in enumerate(_sequence(raw, "safety_limits")):
        data = _mapping(item, f"safety_limits[{index}]")
        applies_to = _text(data.get("applies_to"), f"safety_limits[{index}].applies_to", 64)
        if applies_to not in required:
            raise ValidationFailed(f"safety_limits[{index}].applies_to 不是任务所需能力")
        if applies_to in seen:
            raise ValidationFailed(f"能力 {applies_to} 存在重复安全限制")
        seen.add(applies_to)
        on_missing = _text(data.get("on_missing", "wait_human"), f"safety_limits[{index}].on_missing", 16)
        on_impaired = _text(data.get("on_impaired", "restrict"), f"safety_limits[{index}].on_impaired", 16)
        if on_missing not in ON_MISSING_POLICIES:
            raise ValidationFailed(f"safety_limits[{index}].on_missing 不受支持")
        if on_impaired not in ON_IMPAIRED_POLICIES:
            raise ValidationFailed(f"safety_limits[{index}].on_impaired 不受支持")
        restrictions_raw = _mapping(data.get("restrictions", {}), f"safety_limits[{index}].restrictions")
        restrictions = {
            _text(key, f"safety_limits[{index}].restrictions 键", 64): _decimal(value, f"safety_limits[{index}].restrictions.{key}")
            for key, value in restrictions_raw.items()
        }
        limits.append(SafetyLimit(applies_to, on_missing, on_impaired, restrictions))
    return tuple(limits)


def parse_chains(raw: object, required: frozenset[str], components: Mapping[str, HealthComponent]) -> tuple[ExecutionChain, ...]:
    chains: list[ExecutionChain] = []
    seen: set[str] = set()
    for index, item in enumerate(_sequence(raw, "execution_chains")):
        data = _mapping(item, f"execution_chains[{index}]")
        chain_id = _text(data.get("chain_id"), f"execution_chains[{index}].chain_id", 64)
        if chain_id in seen:
            raise ValidationFailed(f"执行链编号重复: {chain_id}")
        seen.add(chain_id)
        rank = data.get("rank", index + 1)
        if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0:
            raise ValidationFailed(f"execution_chains[{index}].rank 必须是正整数")
        nodes: list[ChainNode] = []
        node_ids: set[str] = set()
        for sub, node_raw in enumerate(_sequence(data.get("nodes"), f"execution_chains[{index}].nodes")):
            node_data = _mapping(node_raw, f"execution_chains[{index}].nodes[{sub}]")
            node_id = _text(node_data.get("node_id"), f"execution_chains[{index}].nodes[{sub}].node_id", 64)
            component = components.get(node_id)
            if component is None:
                raise ValidationFailed(f"执行链 {chain_id} 引用了健康摘要中不存在的部件/资源: {node_id}")
            if node_id in node_ids:
                raise ValidationFailed(f"执行链 {chain_id} 节点重复: {node_id}")
            node_ids.add(node_id)
            provides = _capability_set(node_data.get("provides"), f"execution_chains[{index}].nodes[{sub}].provides")
            if not provides <= component.provides:
                raise ValidationFailed(f"节点 {node_id} 声明了部件未提供的能力: {sorted(provides - component.provides)}")
            if not provides <= required:
                raise ValidationFailed(f"执行链 {chain_id} 提供了任务不需要的能力: {sorted(provides - required)}")
            nodes.append(ChainNode(node_id, provides))
        if not nodes:
            raise ValidationFailed(f"execution_chains[{index}].nodes 不能为空")
        allows_partial = bool(data.get("allows_partial", False))
        chains.append(ExecutionChain(chain_id, rank, allows_partial, tuple(nodes)))
    if not chains:
        raise ValidationFailed("execution_chains 不能为空")
    return tuple(chains)


def parse_manual_actions(raw: object, required: frozenset[str]) -> tuple[ManualAction, ...]:
    actions: list[ManualAction] = []
    seen: set[str] = set()
    for index, item in enumerate(_sequence(raw, "manual_actions", )):
        data = _mapping(item, f"manual_actions[{index}]")
        action_id = _text(data.get("action_id"), f"manual_actions[{index}].action_id", 64)
        if action_id in seen:
            raise ValidationFailed(f"人工动作编号重复: {action_id}")
        seen.add(action_id)
        compensates = data.get("compensates_capability")
        if compensates is not None:
            compensates = _text(compensates, f"manual_actions[{index}].compensates_capability", 64)
            if compensates not in required:
                raise ValidationFailed(f"manual_actions[{index}].compensates_capability 不是任务所需能力")
        actions.append(
            ManualAction(
                action_id=action_id,
                description=_text(data.get("description"), f"manual_actions[{index}].description", 512),
                compensates_capability=compensates,
            )
        )
    return tuple(actions)


def parse_plan_content(raw: Mapping[str, Any]) -> dict[str, Any]:
    """解析并规范化冻结计划版本所需的全部输入。"""

    task_id = _text(raw.get("task_id"), "task_id", 64)
    robot_id = _text(raw.get("robot_id"), "robot_id", 64)
    required = _capability_set(raw.get("required_capabilities"), "required_capabilities")
    components = parse_components(raw.get("components"))
    component_map = {item.component_id: item for item in components}
    provided_by_components: set[str] = set()
    for component in components:
        provided_by_components.update(component.provides)
    unknown = required - provided_by_components
    if unknown:
        raise ValidationFailed(f"任务所需能力没有任何部件可提供: {sorted(unknown)}")
    limits = parse_safety_limits(raw.get("safety_limits", ()), required)
    chains = parse_chains(raw.get("execution_chains", ()), required, component_map)
    actions = parse_manual_actions(raw.get("manual_actions", ()), required)
    return {
        "task_id": task_id,
        "robot_id": robot_id,
        "required_capabilities": tuple(sorted(required)),
        "components": components,
        "safety_limits": limits,
        "execution_chains": chains,
        "manual_actions": actions,
    }


def _global_providers(required: frozenset[str], components: Sequence[HealthComponent]) -> dict[str, list[HealthComponent]]:
    providers: dict[str, list[HealthComponent]] = {cap: [] for cap in required}
    for component in components:
        for cap in component.provides & required:
            providers[cap].append(component)
    return providers


def _select_chain(
    required: frozenset[str],
    chains: Sequence[ExecutionChain],
    components: Mapping[str, HealthComponent],
) -> ExecutionChain | None:
    """在全部节点未失效的链中，优先选择全覆盖、rank 最小的一条。"""

    best: tuple[tuple[int, int, int, str], ExecutionChain, frozenset[str]] | None = None
    for chain in chains:
        if any(components[node.node_id].status == FAILED for node in chain.nodes):
            continue
        has_impaired = any(
            components[node.node_id].status in IMPAIRED_STATUSES for node in chain.nodes
        )
        covered = frozenset().union(*(node.provides for node in chain.nodes))
        if covered == required:
            score = (1 if has_impaired else 0, 0, chain.rank, chain.chain_id)
        elif covered < required and chain.allows_partial:
            score = (1 if has_impaired else 0, 1, chain.rank, chain.chain_id)
        else:
            continue
        if best is None or score < best[0]:
            best = (score, chain, covered)
    return None if best is None else best[1]


def _merge_restrictions(restrictions: Sequence[tuple[str, Mapping[str, Decimal]]]) -> dict[str, Decimal]:
    merged: dict[str, Decimal] = {}
    for _, values in restrictions:
        for key, value in values.items():
            if key not in merged:
                merged[key] = value
            elif key.startswith("min_"):
                merged[key] = max(merged[key], value)
            else:
                # max_* 以及未声明方向的包络量一律取更严格（更小）值
                merged[key] = min(merged[key], value)
    return merged


def evaluate_plan(
    parsed: Mapping[str, Any],
    *,
    completed_actions: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """依据冻结输入计算解释性降级结论。

    ``completed_actions`` 是已补齐证据的人工动作编号集合（计划版本刚冻结时为空）。
    """

    required = frozenset(parsed["required_capabilities"])
    components: Sequence[HealthComponent] = parsed["components"]
    component_map = {item.component_id: item for item in components}
    limits: Sequence[SafetyLimit] = parsed["safety_limits"]
    chains: Sequence[ExecutionChain] = parsed["execution_chains"]
    actions: Sequence[ManualAction] = parsed["manual_actions"]
    limit_by_cap = {limit.applies_to: limit for limit in limits}
    global_providers = _global_providers(required, components)

    def policy_missing(cap: str) -> str:
        limit = limit_by_cap.get(cap)
        return WAIT_HUMAN if limit is None else limit.on_missing

    def policy_impaired(cap: str) -> str:
        limit = limit_by_cap.get(cap)
        return "restrict" if limit is None else limit.on_impaired

    globally_missing = {
        cap for cap, providers in global_providers.items()
        if all(component.status == FAILED for component in providers)
    }
    globally_impaired = {
        cap for cap, providers in global_providers.items()
        if cap not in globally_missing
        and all(component.status in IMPAIRED_STATUSES or component.status == FAILED for component in providers)
        and any(component.status in IMPAIRED_STATUSES for component in providers)
    }

    selected = _select_chain(required, chains, component_map)
    reasons: list[str] = []
    for cap in sorted(required):
        for component in sorted(global_providers[cap], key=lambda item: item.component_id):
            if component.status != NOMINAL and component.health_reason:
                reasons.append(
                    f"能力 {cap} 的提供部件 {component.component_id} 状态为 {component.status}：{component.health_reason}"
                )

    if selected is None:
        stop_caps = sorted(
            cap for cap in (globally_missing | globally_impaired)
            if (policy_missing(cap) == SAFE_STOP and cap in globally_missing)
            or (policy_impaired(cap) == SAFE_STOP and cap in globally_impaired)
        )
        if stop_caps:
            conclusion = SAFE_STOP
            rule = "no_feasible_chain_safety_stop"
            reasons.append(f"无可行执行链，且安全限制要求对 {stop_caps} 安全停机")
        else:
            conclusion = WAIT_HUMAN
            rule = "no_feasible_chain_wait"
            reasons.append("无可行执行链，等待人工介入或更换部件")
        covered = frozenset()
        chain_impaired: set[str] = set()
        locked_resources: list[str] = []
    else:
        covered = frozenset().union(*(node.provides for node in selected.nodes))
        chain_impaired = set()
        for cap in sorted(covered):
            nodes_providing = [node for node in selected.nodes if cap in node.provides]
            statuses = [component_map[node.node_id].status for node in nodes_providing]
            if NOMINAL not in statuses and any(status in IMPAIRED_STATUSES for status in statuses):
                chain_impaired.add(cap)
        residual_missing = required - covered

        stop_caps = sorted(
            cap for cap in residual_missing if policy_missing(cap) == SAFE_STOP
        ) + sorted(cap for cap in chain_impaired if policy_impaired(cap) == SAFE_STOP)
        # 需要人工补偿才能放行的能力：缺失策略为 wait_human，或受损策略为 wait_human。
        wait_caps = sorted(
            cap for cap in residual_missing if policy_missing(cap) == WAIT_HUMAN
        ) + sorted(cap for cap in chain_impaired if policy_impaired(cap) == WAIT_HUMAN)
        # 允许由替代链/安全包络直接豁免的能力。
        alternative_caps = sorted(cap for cap in residual_missing if policy_missing(cap) == "alternative")
        restrict_caps = sorted(cap for cap in chain_impaired if policy_impaired(cap) == "restrict")

        pending: list[str] = []
        blockers: list[str] = []
        for cap in wait_caps:
            action = next((item for item in actions if item.compensates_capability == cap), None)
            if action is None:
                blockers.append(cap)
            elif action.action_id not in completed_actions:
                pending.append(action.action_id)

        if stop_caps:
            conclusion = SAFE_STOP
            rule = "safety_limit_stop"
            reasons.append(f"安全限制要求对缺失或受损能力 {stop_caps} 安全停机")
        elif blockers:
            conclusion = WAIT_HUMAN
            rule = "manual_compensation_undefined_wait"
            reasons.append(f"能力 {blockers} 需要人工补偿，但未定义补偿人工动作，等待人工")
        elif pending:
            conclusion = WAIT_HUMAN
            rule = "manual_compensation_pending"
            reasons.append(f"待人工动作 {pending} 完成后方可受限运行")
        elif alternative_caps or restrict_caps or wait_caps:
            conclusion = RESTRICTED
            rule = "alternative_chain_restricted" if alternative_caps else "impaired_capability_restricted"
            if alternative_caps:
                reasons.append(f"能力 {alternative_caps} 由替代执行链豁免，按安全限制受限运行")
            if restrict_caps:
                reasons.append(f"能力 {restrict_caps} 由降级部件提供，按安全限制受限运行")
            if wait_caps:
                reasons.append(f"能力 {wait_caps} 的人工补偿已完成，按替代链受限运行")
        else:
            conclusion = CONTINUE
            rule = "all_capabilities_nominal"
            reasons.append("选定执行链全部节点名义可用，任务可继续")
        locked_resources = sorted(node.node_id for node in selected.nodes) if conclusion != SAFE_STOP else []

    restricted_caps = set()
    if selected is not None and conclusion in {RESTRICTED, WAIT_HUMAN}:
        restricted_caps.update(
            cap for cap in (required - covered) if policy_missing(cap) == "alternative"
        )
        restricted_caps.update(cap for cap in chain_impaired if policy_impaired(cap) == "restrict")
        restricted_caps.update(
            cap for cap in chain_impaired if policy_impaired(cap) == WAIT_HUMAN and conclusion == RESTRICTED
        )
        restricted_caps.update(
            cap for cap in (required - covered) if policy_missing(cap) == WAIT_HUMAN and conclusion == RESTRICTED
        )
    restrictions = _merge_restrictions(
        [
            (cap, limit_by_cap[cap].restrictions)
            for cap in sorted(restricted_caps)
            if cap in limit_by_cap
        ]
    )

    recovery: list[dict[str, Any]] = []
    for cap in sorted(required):
        bad_components = [
            component for component in global_providers[cap]
            if component.status != NOMINAL
        ]
        if not bad_components:
            continue
        evidence = sorted({item for component in bad_components for item in component.recovery_evidence})
        recovery.append(
            {
                "capability": cap,
                "required_status": NOMINAL,
                "blocked_components": [
                    {"component_id": component.component_id, "status": component.status}
                    for component in sorted(bad_components, key=lambda item: item.component_id)
                ],
                "required_evidence": evidence,
            }
        )

    action_by_id = {action.action_id: action for action in actions}
    pending_actions = sorted(action.action_id for action in actions if action.action_id not in completed_actions)

    return {
        "conclusion": conclusion,
        "decision_rule": rule,
        "selected_chain_id": None if selected is None else selected.chain_id,
        "locked_resources": locked_resources,
        "missing_capabilities": sorted(required - covered),
        "impaired_capabilities": sorted(chain_impaired) if selected is not None else sorted(globally_impaired),
        "restrictions": {key: decimal_text(value) for key, value in sorted(restrictions.items())},
        "pending_manual_actions": pending_actions,
        "manual_action_map": action_by_id,
        "recovery_evidence": recovery,
        "reasons": reasons,
    }


def component_dicts(components: Sequence[HealthComponent]) -> list[dict[str, Any]]:
    return [
        {
            "component_id": item.component_id,
            "status": item.status,
            "provides": sorted(item.provides),
            "health_reason": item.health_reason,
            "recovery_evidence": list(item.recovery_evidence),
        }
        for item in components
    ]


def freeze_content(parsed: Mapping[str, Any], snapshot_revision: int) -> tuple[str, str]:
    """把规范化输入和健康摘要版本冻结成 canonical JSON 与 SHA-256。"""

    payload = {
        "task_id": parsed["task_id"],
        "robot_id": parsed["robot_id"],
        "required_capabilities": list(parsed["required_capabilities"]),
        "health_snapshot_revision": snapshot_revision,
        "components": component_dicts(parsed["components"]),
        "safety_limits": [
            {
                "applies_to": item.applies_to,
                "on_missing": item.on_missing,
                "on_impaired": item.on_impaired,
                "restrictions": {key: decimal_text(value) for key, value in sorted(item.restrictions.items())},
            }
            for item in parsed["safety_limits"]
        ],
        "execution_chains": [
            {
                "chain_id": item.chain_id,
                "rank": item.rank,
                "allows_partial": item.allows_partial,
                "nodes": [
                    {"node_id": node.node_id, "provides": sorted(node.provides)}
                    for node in item.nodes
                ],
            }
            for item in parsed["execution_chains"]
        ],
        "manual_actions": [
            {
                "action_id": item.action_id,
                "description": item.description,
                "compensates_capability": item.compensates_capability,
            }
            for item in parsed["manual_actions"]
        ],
    }
    text = canonical_json(payload)
    return text, digest(payload)
