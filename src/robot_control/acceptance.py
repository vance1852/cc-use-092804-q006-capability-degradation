"""贯通控制算力单价、实时控制总线、控制算力库存、提名、情景分析和能力降级编排的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .degradation_service import DegradationService
from .errors import InvalidState
from .service import SupplyService


def _degradation_walkthrough(connection: sqlite3.Connection, clock: FrozenClock) -> dict[str, object]:
    """示范运行：视觉 AI 单元温度异常后的降级、停机、回执边界与恢复。"""
    service = DegradationService(connection, clock)

    COMPONENT_ROBOT = {
        "vision-ai-1": "transporter-01", "lidar-1": "transporter-01",
        "drive-ctrl-1": "transporter-01", "compute-t": "transporter-01",
        "vision-ai-2": "assembler-01", "arm-ctrl-1": "assembler-01", "compute-a": "assembler-01",
    }

    def health(component: str, status: str, temp: str, key: str, observed: str) -> dict[str, object]:
        return service.report_health("dispatch", COMPONENT_ROBOT[component], {
            "component_id": component, "status": status,
            "metrics": {"temperature_c": temp}, "observed_at": observed, "idempotency_key": key,
        })

    service.register_resource("plan", {"resource_id": "gpu-pool-1", "kind": "inference-compute", "capacity": 2})
    service.register_resource("plan", {"resource_id": "rt-bus-1", "kind": "realtime-bus-slot", "capacity": 4})
    service.register_robot("plan", {"robot_id": "transporter-01", "model": "搬运机器人 T2", "robot_kind": "transport"})
    service.register_robot("plan", {"robot_id": "assembler-01", "model": "精细装配机器人 A7", "robot_kind": "precision-assembly"})
    for robot_id, components in (
        ("transporter-01", (
            ("vision-ai-1", "vision-ai-unit", (("perception.vision", 3),)),
            ("lidar-1", "lidar", (("perception.lidar", 2),)),
            ("drive-ctrl-1", "drive-controller", (("control.locomotion", 3),)),
            ("compute-t", "compute-node", (("compute.inference", 3),)),
        )),
        ("assembler-01", (
            ("vision-ai-2", "vision-ai-unit", (("perception.vision", 3),)),
            ("arm-ctrl-1", "arm-controller", (("manipulation.precision", 3),)),
            ("compute-a", "compute-node", (("compute.inference", 3),)),
        )),
    ):
        for component_id, kind, provides in components:
            service.register_component("plan", robot_id, {
                "component_id": component_id, "kind": kind,
                "provides": [{"capability": cap, "level": level} for cap, level in provides],
            })
    for robot_id, chains in (
        ("transporter-01", (
            {"chain_id": "t-chain-vision", "capability": "perception", "level": 3,
             "requires_components": ["vision-ai-1", "compute-t"],
             "requires_resources": [{"resource_id": "gpu-pool-1", "units": 1}], "priority": 0},
            {"chain_id": "t-chain-lidar-slow", "capability": "perception", "level": 2,
             "requires_components": ["lidar-1", "compute-t"],
             "requires_resources": [{"resource_id": "gpu-pool-1", "units": 1}], "priority": 10,
             "restrictions": {"max_speed_percent": 40, "min_obstacle_distance_m": "2.5"}},
            {"chain_id": "t-chain-drive", "capability": "locomotion", "level": 3,
             "requires_components": ["drive-ctrl-1"],
             "requires_resources": [{"resource_id": "rt-bus-1", "units": 1}], "priority": 0},
        )),
        ("assembler-01", (
            {"chain_id": "a-chain-vision", "capability": "perception", "level": 3,
             "requires_components": ["vision-ai-2", "compute-a"],
             "requires_resources": [{"resource_id": "gpu-pool-1", "units": 1}], "priority": 0},
            {"chain_id": "a-chain-vision-backup", "capability": "perception", "level": 2,
             "requires_components": ["compute-a"], "priority": 10},
            {"chain_id": "a-chain-arm", "capability": "manipulation", "level": 3,
             "requires_components": ["arm-ctrl-1"],
             "requires_resources": [{"resource_id": "rt-bus-1", "units": 1}], "priority": 0},
        )),
    ):
        for chain in chains:
            service.register_chain("plan", robot_id, chain)
    service.set_task("plan", "transporter-01", {
        "task_id": "task-t-1",
        "required_capabilities": [
            {"capability": "perception", "min_level": 2},
            {"capability": "locomotion", "min_level": 3},
        ],
        "safety_limits": {"metric_limits": {"temperature_c": "85"}},
    })
    service.set_task("plan", "assembler-01", {
        "task_id": "task-a-1",
        "required_capabilities": [
            {"capability": "perception", "min_level": 3},
            {"capability": "manipulation", "min_level": 3},
        ],
        "safety_limits": {"metric_limits": {"temperature_c": "85"}},
    })
    baseline = (
        ("transporter-01", "vision-ai-1", "61"), ("transporter-01", "lidar-1", "44"),
        ("transporter-01", "drive-ctrl-1", "50"), ("transporter-01", "compute-t", "55"),
        ("assembler-01", "vision-ai-2", "60"), ("assembler-01", "arm-ctrl-1", "47"),
        ("assembler-01", "compute-a", "56"),
    )
    for index, (robot_id, component, temp) in enumerate(baseline):
        service.report_health("dispatch", robot_id, {
            "component_id": component, "status": "ok", "metrics": {"temperature_c": temp},
            "observed_at": "2026-09-24T08:05:00Z", "idempotency_key": f"base-{index}",
        })

    # 基线：两台机器人均可继续。
    for robot_id, plan_id in (("transporter-01", "p0-t"), ("assembler-01", "p0-a")):
        plan = service.propose_plan("dispatch", robot_id, plan_id, "degrade")
        assert plan["decision"] == "continue"
        service.confirm_plan("risk", plan_id, plan["revision"])
        service.submit_receipt("dispatch", plan_id, {
            "receipt_id": f"{plan_id}-r1", "seq": 1, "reported_mode": "nominal",
        })

    # 示范运行中两块视觉 AI 单元温度异常（安全限制 85°C）。
    clock.advance(minutes=5)
    health_t = health("vision-ai-1", "degraded", "87.5", "inc-t-1", "2026-09-24T08:10:00Z")
    health_a = health("vision-ai-2", "degraded", "88.1", "inc-a-1", "2026-09-24T08:10:00Z")

    # 搬运机器人：降级运行——限速 40%、避障距离 2.5m。
    plan_t1 = service.propose_plan("dispatch", "transporter-01", "p1-t", "degrade")
    assert plan_t1["decision"] == "restricted"
    service.confirm_plan("risk", "p1-t", plan_t1["revision"])
    receipt_t1 = service.submit_receipt("dispatch", "p1-t", {
        "receipt_id": "p1-t-r1", "seq": 1, "reported_mode": "restricted",
    })

    # 精细装配机器人：感知等级不足，必须安全停机退出任务。
    plan_a1 = service.propose_plan("dispatch", "assembler-01", "p1-a", "degrade")
    assert plan_a1["decision"] == "safe_stop"
    service.confirm_plan("risk", "p1-a", plan_a1["revision"])
    receipt_a1 = service.submit_receipt("dispatch", "p1-a", {
        "receipt_id": "p1-a-r1", "seq": 1, "reported_mode": "safe_stopped",
    })
    duplicate_a1 = service.submit_receipt("dispatch", "p1-a", {
        "receipt_id": "p1-a-r1", "seq": 1, "reported_mode": "safe_stopped",
    })
    late_a1 = service.submit_receipt("dispatch", "p1-a", {
        "receipt_id": "p1-a-r2", "seq": 2, "reported_mode": "nominal",
    })
    assembler_stopped = service.robot_status("audit", "assembler-01")

    # 搬运机器人计算单元故障：安全停机；迟到的正常回执不能恢复安全终态。
    clock.advance(minutes=5)
    health("compute-t", "failed", "91", "inc-t-2", "2026-09-24T08:15:00Z")
    plan_t2 = service.propose_plan("dispatch", "transporter-01", "p2-t", "degrade")
    assert plan_t2["decision"] == "safe_stop"
    service.confirm_plan("risk", "p2-t", plan_t2["revision"])
    service.submit_receipt("dispatch", "p2-t", {
        "receipt_id": "p2-t-r1", "seq": 1, "reported_mode": "safe_stopped",
    })
    plan_t3 = service.propose_plan("dispatch", "transporter-01", "p3-t", "degrade")
    service.confirm_plan("risk", "p3-t", plan_t3["revision"])
    terminal_guard = service.submit_receipt("dispatch", "p3-t", {
        "receipt_id": "p3-t-r1", "seq": 1, "reported_mode": "nominal",
    })
    service.submit_receipt("dispatch", "p3-t", {
        "receipt_id": "p3-t-r2", "seq": 2, "reported_mode": "safe_stopped",
    })

    # 恢复前必须重新满足证据：新健康证据 + 完成人工动作。
    clock.advance(minutes=25)
    blocked = None
    try:
        service.propose_plan("dispatch", "assembler-01", "p2-a", "recover")
    except InvalidState as exc:
        blocked = str(exc)
    health("vision-ai-2", "ok", "58", "rec-a-1", "2026-09-24T08:40:00Z")
    health("compute-t", "ok", "52", "rec-t-1", "2026-09-24T08:41:00Z")
    health("vision-ai-1", "ok", "63", "rec-t-2", "2026-09-24T08:42:00Z")
    for robot_id in ("transporter-01", "assembler-01"):
        for action in service.robot_status("audit", robot_id)["pending_manual_actions"]:
            service.complete_manual_action("dispatch", action["action_id"], "现场已确认")
    plan_t4 = service.propose_plan("dispatch", "transporter-01", "p4-t", "recover")
    assert plan_t4["decision"] == "continue"
    service.confirm_plan("risk", "p4-t", plan_t4["revision"])
    service.submit_receipt("dispatch", "p4-t", {
        "receipt_id": "p4-t-r1", "seq": 1, "reported_mode": "nominal",
    })
    plan_a2 = service.propose_plan("dispatch", "assembler-01", "p3-a", "recover")
    service.confirm_plan("risk", "p3-a", plan_a2["revision"])
    service.submit_receipt("dispatch", "p3-a", {
        "receipt_id": "p3-a-r1", "seq": 1, "reported_mode": "nominal",
    })
    board = service.fleet_board("audit")
    final_states = {robot["robot_id"]: robot["state"] for robot in board["robots"]}
    return {
        "incident": {"transporter_health_changed": health_t["summary_changed"], "assembler_health_changed": health_a["summary_changed"]},
        "transporter": {
            "degraded_decision": plan_t1["decision"],
            "restrictions": plan_t1["explanation"]["restrictions"],
            "adopted_chains": [c["chain_id"] for c in plan_t1["explanation"]["selected_chains"]],
            "receipt": receipt_t1["outcome"],
            "terminal_guard": terminal_guard["outcome"],
            "final_state": final_states["transporter-01"],
        },
        "assembler": {
            "stop_decision": plan_a1["decision"],
            "missing_capabilities": plan_a1["explanation"]["missing_capabilities"],
            "receipt": receipt_a1["outcome"],
            "duplicate_receipt_replayed": duplicate_a1 == receipt_a1,
            "late_normal_receipt": late_a1["outcome"],
            "stopped_snapshot": {
                "state": assembler_stopped["state"],
                "pending_manual_actions": len(assembler_stopped["pending_manual_actions"]),
                "outstanding_evidence": len(assembler_stopped["outstanding_evidence"]),
            },
            "recovery_blocked_until_evidence": blocked is not None,
            "final_state": final_states["assembler-01"],
        },
        "board_final": [
            {
                "robot_id": robot["robot_id"],
                "state": robot["state"],
                "pending_manual_actions": len(robot["pending_manual_actions"]),
                "outstanding_evidence": len(robot["outstanding_evidence"]),
            }
            for robot in board["robots"]
        ],
    }


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
    service = SupplyService(connection, clock)
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
    degradation = _degradation_walkthrough(connection, clock)
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "degradation": degradation, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行机器人控制平台调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
