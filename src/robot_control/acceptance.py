"""贯通控制算力单价、实时控制总线、控制算力库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{index}", "close_cny": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "cluster-a", "name": "北部机器人控制平台", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_control_slots": "500000"})
    service.create_facility("plan", {"facility_id": "pool-b", "name": "东部推理池", "kind": "inference-pool", "timezone": "Asia/Shanghai", "capacity_control_slots": "800000"})
    service.create_route("plan", {"route_id": "fabric-a-b", "origin_id": "cluster-a", "destination_id": "pool-b", "product": "gpu-h100", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "cluster-a", "product": "gpu-h100", "grade": "PEAK_VALLEY", "quantity_control_slots": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "fabric-a-b", "shipper_id": "tenant-east", "service_date": "2026-09-25", "requested_control_slots": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "fabric-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "fabric-recovery", "name": "关键机组检修恢复与需求回落", "market_index_drop_percent": "9", "route_capacity_changes": {"fabric-a-b": "20"}, "demand_changes": {"cluster-a:gpu-h100": "-5"}})
    service.approve_scenario("risk", "fabric-recovery", 1)
    scenario = service.run_scenario("plan", "fabric-recovery", "2026-09-23")
    degradation = run_degradation(service)
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "degradation": degradation, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def run_degradation(service: SupplyService) -> dict[str, object]:
    """视觉 AI 单元温度异常下的搬运降级与装配安全停机。"""

    service.create_user("operator", "operator", "operator")

    def health(robot: str, vision: str) -> dict[str, object]:
        return {"robot_id": robot, "components": [
            {"component_id": "ctrl-main", "status": "nominal",
             "provides": ["motion_control", "arm_control"], "recovery_evidence": ["ctrl-selfcheck-ok"]},
            {"component_id": "vision-ai-1", "status": vision, "provides": ["vision"],
             "health_reason": "视觉 AI 单元温度异常" if vision != "nominal" else None,
             "recovery_evidence": ["vision-temp-normal", "vision-selfcheck-pass"]},
            {"component_id": "lidar-1", "status": "nominal", "provides": ["obstacle_detection"]},
            {"component_id": "operator-eyes", "status": "nominal", "provides": ["obstacle_detection"]},
        ]}

    chains = [
        {"chain_id": "primary", "rank": 1, "nodes": [
            {"node_id": "ctrl-main", "provides": ["motion_control"]},
            {"node_id": "vision-ai-1", "provides": ["vision"]},
            {"node_id": "lidar-1", "provides": ["obstacle_detection"]}]},
        {"chain_id": "slowed-lidar", "rank": 2, "allows_partial": True, "nodes": [
            {"node_id": "ctrl-main", "provides": ["motion_control"]},
            {"node_id": "lidar-1", "provides": ["obstacle_detection"]}]},
    ]
    carry_limits = [
        {"applies_to": "vision", "on_missing": "alternative", "on_impaired": "restrict",
         "restrictions": {"max_speed_mps": "0.5", "min_obstacle_distance_m": "2.0"}},
        {"applies_to": "obstacle_detection", "on_missing": "safe_stop", "on_impaired": "restrict",
         "restrictions": {}},
    ]

    # 搬运机器人：视觉降级 -> 激光替代链受限运行，原子锁定控制资源
    service.record_health("operator", health("carry-01", "degraded"))
    carry_plan = service.freeze_plan("operator", {
        "plan_id": "deg-carry-01", "robot_id": "carry-01", "task_id": "dock-transport-7",
        "required_capabilities": ["vision", "motion_control", "obstacle_detection"],
        "safety_limits": carry_limits, "execution_chains": chains,
    })
    carry_confirmed = service.confirm_plan("dispatch", "deg-carry-01", carry_plan["version"])
    service.register_receipt("operator", "deg-carry-01", {
        "receipt_id": "carry-rcp-2", "reported_state": "degraded", "observed_at": "2026-09-24T08:20:00Z"})
    # 迟到、重复回执
    stale = service.register_receipt("operator", "deg-carry-01", {
        "receipt_id": "carry-rcp-1", "reported_state": "degraded", "observed_at": "2026-09-24T08:10:00Z"})
    replay = service.register_receipt("operator", "deg-carry-01", {
        "receipt_id": "carry-rcp-2", "reported_state": "degraded", "observed_at": "2026-09-24T08:20:00Z"})

    # 精细装配机器人：视觉失效且安全限制禁止降级 -> 安全停机，正常回执不能恢复
    service.record_health("operator", health("assemble-09", "failed"))
    service.freeze_plan("operator", {
        "plan_id": "deg-assemble-09", "robot_id": "assemble-09", "task_id": "precision-fit-3",
        "required_capabilities": ["vision", "motion_control", "obstacle_detection", "arm_control"],
        "safety_limits": [
            {"applies_to": "vision", "on_missing": "safe_stop", "on_impaired": "safe_stop", "restrictions": {}},
            {"applies_to": "obstacle_detection", "on_missing": "wait_human", "on_impaired": "restrict", "restrictions": {}},
        ],
        "execution_chains": [{"chain_id": "primary", "rank": 1, "nodes": [
            {"node_id": "ctrl-main", "provides": ["arm_control", "motion_control"]},
            {"node_id": "vision-ai-1", "provides": ["vision"]},
            {"node_id": "lidar-1", "provides": ["obstacle_detection"]}]}],
    })
    assemble_confirmed = service.confirm_plan("dispatch", "deg-assemble-09", 1)
    late_normal = service.register_receipt("operator", "deg-assemble-09", {
        "receipt_id": "assemble-normal-late", "reported_state": "normal",
        "observed_at": "2026-09-24T09:00:00Z"})

    # 健康摘要版本变化使未终态计划失效并释放控制资源
    changed = service.record_health("operator", health("carry-01", "critical"))

    board = service.degradation_board("dispatch")
    return {
        "carry": {
            "version": carry_plan["version"], "conclusion": carry_plan["conclusion"],
            "selected_chain": carry_plan["selected_chain_id"],
            "restrictions": carry_plan["restrictions"],
            "locked_resources": sorted(item["resource_id"] for item in carry_confirmed["locked_resources"]),
            "stale_receipt_ignored": stale["ignored_reason"],
            "replayed_receipt": replay["replayed"],
            "state_after_health_change": service.get_plan("deg-carry-01")["state"],
            "invalidated_by_revision": changed["revision"],
        },
        "assemble": {
            "conclusion": assemble_confirmed["conclusion"], "state": assemble_confirmed["state"],
            "late_normal_receipt": late_normal["ignored_reason"],
        },
        "board_robots": [item["robot_id"] for item in board["robots"]],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行机器人控制平台调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
