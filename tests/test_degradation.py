from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from robot_control.acceptance import run as acceptance_run
from robot_control.api import JsonApplication
from robot_control.clock import FrozenClock
from robot_control.degradation import classify_receipt, evaluate_degradation, merge_restrictions
from robot_control.degradation_service import DegradationService
from robot_control.errors import Conflict, Forbidden, InvalidState
from robot_control.service import SupplyService
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _chains() -> list[dict[str, object]]:
    return [
        {"chain_id": "vision-primary", "capability": "perception", "level": 3,
         "requires_components": ["vision-ai", "compute"], "requires_resources": [],
         "restrictions": {}, "requires_manual_action": None, "priority": 0},
        {"chain_id": "lidar-slow", "capability": "perception", "level": 2,
         "requires_components": ["lidar", "compute"], "requires_resources": [],
         "restrictions": {"max_speed_percent": 40, "min_obstacle_distance_m": "2.5"},
         "requires_manual_action": None, "priority": 10},
        {"chain_id": "manual-backup", "capability": "perception", "level": 3,
         "requires_components": ["compute"], "requires_resources": [],
         "restrictions": {}, "requires_manual_action": "人工切换备用计算单元", "priority": 20},
        {"chain_id": "drive", "capability": "locomotion", "level": 3,
         "requires_components": ["drive-ctrl"], "requires_resources": [],
         "restrictions": {}, "requires_manual_action": None, "priority": 0},
    ]


def _healthy() -> dict[str, dict[str, object]]:
    return {
        component: {"status": "ok", "metrics": {"temperature_c": "60"}, "observed_at": "2026-09-24T08:00:00Z"}
        for component in ("vision-ai", "lidar", "compute", "drive-ctrl")
    }


class EngineTests(unittest.TestCase):
    def test_continue_when_all_healthy(self) -> None:
        result = evaluate_degradation(
            required_capabilities=[{"capability": "perception", "min_level": 2}],
            chains=_chains(),
            health_summary=_healthy(),
            metric_limits={"temperature_c": Decimal("85")},
        )
        self.assertEqual(result["decision"], "continue")
        self.assertEqual(result["target_mode"], "nominal")
        self.assertEqual(result["missing_capabilities"], [])

    def test_restricted_when_alternative_chain_with_limits(self) -> None:
        summary = _healthy()
        summary["vision-ai"] = {"status": "degraded", "metrics": {"temperature_c": "87.5"},
                                "observed_at": "2026-09-24T08:10:00Z"}
        result = evaluate_degradation(
            required_capabilities=[{"capability": "perception", "min_level": 2}],
            chains=_chains(),
            health_summary=summary,
            metric_limits={"temperature_c": Decimal("85")},
        )
        self.assertEqual(result["decision"], "restricted")
        self.assertEqual(result["restrictions"], {"max_speed_percent": "40", "min_obstacle_distance_m": "2.5"})
        selected = {entry["capability"]: entry["chain_id"] for entry in result["selected_chains"]}
        self.assertEqual(selected["perception"], "lidar-slow")
        self.assertTrue(any("超过安全限制" in reason for reason in result["reasons"]))

    def test_wait_human_when_only_manual_chain_remains(self) -> None:
        summary = _healthy()
        summary["vision-ai"] = {"status": "failed", "metrics": {}, "observed_at": "2026-09-24T08:10:00Z"}
        summary["lidar"] = {"status": "failed", "metrics": {}, "observed_at": "2026-09-24T08:10:00Z"}
        result = evaluate_degradation(
            required_capabilities=[{"capability": "perception", "min_level": 3}],
            chains=_chains(),
            health_summary=summary,
            metric_limits={"temperature_c": Decimal("85")},
        )
        self.assertEqual(result["decision"], "wait_human")
        self.assertEqual(result["manual_actions"], ["人工切换备用计算单元"])
        self.assertEqual(result["missing_capabilities"], [{"capability": "perception", "min_level": 3}])

    def test_safe_stop_when_no_chain_and_no_manual_action(self) -> None:
        summary = _healthy()
        summary["vision-ai"] = {"status": "failed", "metrics": {}, "observed_at": "2026-09-24T08:10:00Z"}
        summary["lidar"] = {"status": "failed", "metrics": {}, "observed_at": "2026-09-24T08:10:00Z"}
        summary["compute"] = {"status": "failed", "metrics": {}, "observed_at": "2026-09-24T08:10:00Z"}
        result = evaluate_degradation(
            required_capabilities=[{"capability": "perception", "min_level": 3}],
            chains=_chains(),
            health_summary=summary,
            metric_limits={"temperature_c": Decimal("85")},
        )
        self.assertEqual(result["decision"], "safe_stop")
        self.assertEqual(result["target_mode"], "safe_stopped")

    def test_missing_health_report_makes_component_unavailable(self) -> None:
        result = evaluate_degradation(
            required_capabilities=[{"capability": "locomotion", "min_level": 3}],
            chains=_chains(),
            health_summary={},
            metric_limits={},
        )
        self.assertEqual(result["decision"], "safe_stop")
        self.assertTrue(any("缺少健康报告" in reason for reason in result["reasons"]))

    def test_merge_restrictions_takes_strictest(self) -> None:
        merged = merge_restrictions([
            {"restrictions": {"max_speed_percent": 40, "min_obstacle_distance_m": "2.5"}},
            {"restrictions": {"max_speed_percent": 60, "min_obstacle_distance_m": "1.5"}},
        ])
        self.assertEqual(merged, {"max_speed_percent": "40", "min_obstacle_distance_m": "2.5"})

    def test_receipt_classification(self) -> None:
        base = {"plan_purpose": "degrade", "robot_mode": "restricted", "reported_mode": "restricted", "seq": 2, "last_applied_seq": 1}
        self.assertEqual(classify_receipt(plan_state="confirmed", **base), "applied")
        self.assertEqual(classify_receipt(plan_state="invalidated", **base), "rejected_stale")
        self.assertEqual(classify_receipt(plan_state="executing", **{**base, "seq": 1}), "out_of_order")
        self.assertEqual(
            classify_receipt(plan_state="executing", **{**base, "robot_mode": "safe_stopped"}),
            "rejected_terminal",
        )
        self.assertEqual(
            classify_receipt(plan_state="executing", plan_purpose="recover", robot_mode="safe_stopped",
                             reported_mode="nominal", seq=1, last_applied_seq=0),
            "applied",
        )


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.supply = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.supply.create_user(user_id, user_id, role)
        self.service = DegradationService(self.connection, self.clock)
        self.service.register_resource("plan", {"resource_id": "gpu-1", "kind": "compute", "capacity": 1})
        self.service.register_robot("plan", {"robot_id": "robot-x", "model": "搬运机器人", "robot_kind": "transport"})
        for component, provides in (
            ("vision-ai", (("perception.vision", 3),)),
            ("lidar", (("perception.lidar", 2),)),
            ("compute", (("compute.inference", 3),)),
            ("drive-ctrl", (("control.locomotion", 3),)),
        ):
            self.service.register_component("plan", "robot-x", {
                "component_id": component, "kind": "unit",
                "provides": [{"capability": cap, "level": level} for cap, level in provides],
            })
        for chain in (
            {"chain_id": "vision-primary", "capability": "perception", "level": 3,
             "requires_components": ["vision-ai", "compute"],
             "requires_resources": [{"resource_id": "gpu-1", "units": 1}], "priority": 0},
            {"chain_id": "lidar-slow", "capability": "perception", "level": 2,
             "requires_components": ["lidar", "compute"],
             "requires_resources": [{"resource_id": "gpu-1", "units": 1}], "priority": 10,
             "restrictions": {"max_speed_percent": 40, "min_obstacle_distance_m": "2.5"}},
            {"chain_id": "drive", "capability": "locomotion", "level": 3,
             "requires_components": ["drive-ctrl"], "requires_resources": [], "priority": 0},
        ):
            self.service.register_chain("plan", "robot-x", chain)
        self.service.set_task("plan", "robot-x", {
            "task_id": "task-1",
            "required_capabilities": [
                {"capability": "perception", "min_level": 2},
                {"capability": "locomotion", "min_level": 3},
            ],
            "safety_limits": {"metric_limits": {"temperature_c": "85"}},
        })
        for index, component in enumerate(("vision-ai", "lidar", "compute", "drive-ctrl")):
            self.health(component, "ok", "60", f"base-{index}")

    def tearDown(self) -> None:
        self.connection.close()

    def health(self, component: str, status: str, temp: str, key: str,
               observed: str = "2026-09-24T08:05:00Z") -> dict[str, object]:
        return self.service.report_health("dispatch", "robot-x", {
            "component_id": component, "status": status, "metrics": {"temperature_c": temp},
            "observed_at": observed, "idempotency_key": key,
        })

    def propose(self, plan_id: str = "p-1", purpose: str = "degrade") -> dict[str, object]:
        return self.service.propose_plan("dispatch", "robot-x", plan_id, purpose)

    def confirm(self, plan_id: str, revision: int = 1) -> dict[str, object]:
        return self.service.confirm_plan("risk", plan_id, revision)

    def receipt(self, plan_id: str, receipt_id: str, seq: int, mode: str) -> dict[str, object]:
        return self.service.submit_receipt("dispatch", plan_id, {
            "receipt_id": receipt_id, "seq": seq, "reported_mode": mode,
        })


class PlanLifecycleTests(ServiceTestBase):
    def test_restricted_plan_freezes_inputs_and_locks_resources(self) -> None:
        self.health("vision-ai", "degraded", "87.5", "inc-1", "2026-09-24T08:10:00Z")
        plan = self.propose()
        self.assertEqual(plan["decision"], "restricted")
        self.assertEqual(plan["decision_text"], "受限运行")
        self.assertEqual(len(plan["inputs_sha256"]), 64)
        frozen = self.connection.execute(
            "SELECT frozen_json FROM degradation_plans WHERE plan_id='p-1'"
        ).fetchone()
        self.assertIn("vision-ai", frozen["frozen_json"])
        confirmed = self.confirm("p-1", plan["revision"])
        self.assertEqual(confirmed["state"], "confirmed")
        locks = self.connection.execute(
            "SELECT resource_id,units,state FROM resource_locks WHERE plan_id='p-1'"
        ).fetchall()
        self.assertEqual([(row["resource_id"], row["units"], row["state"]) for row in locks],
                         [("gpu-1", 1, "active")])
        applied = self.receipt("p-1", "r-1", 1, "restricted")
        self.assertEqual(applied["outcome"], "applied")
        status = self.service.robot_status("audit", "robot-x")
        self.assertEqual(status["state"], "restricted")
        self.assertEqual(status["restrictions_in_effect"], {"max_speed_percent": "40", "min_obstacle_distance_m": "2.5"})
        adopted = {entry["chain_id"] for entry in status["adopted_chains"]}
        self.assertEqual(adopted, {"lidar-slow", "drive"})

    def test_context_change_invalidates_unexecuted_plans_and_releases_locks(self) -> None:
        self.health("vision-ai", "degraded", "87.5", "inc-1", "2026-09-24T08:10:00Z")
        plan = self.propose()
        self.confirm("p-1", plan["revision"])
        version_before = self.service.robot_status("audit", "robot-x")["context_version"]
        self.health("lidar", "degraded", "80", "inc-2", "2026-09-24T08:20:00Z")
        status = self.service.robot_status("audit", "robot-x")
        self.assertGreater(status["context_version"], version_before)
        row = self.connection.execute("SELECT state FROM degradation_plans WHERE plan_id='p-1'").fetchone()
        self.assertEqual(row["state"], "invalidated")
        locks = self.connection.execute(
            "SELECT state FROM resource_locks WHERE plan_id='p-1'"
        ).fetchall()
        self.assertEqual([lock["state"] for lock in locks], ["released"])
        with self.assertRaises(InvalidState):
            self.confirm("p-1", 2)

    def test_confirm_is_atomic_when_resource_capacity_insufficient(self) -> None:
        self.service.register_robot("plan", {"robot_id": "robot-y", "model": "装配机器人", "robot_kind": "precision-assembly"})
        self.service.register_component("plan", "robot-y", {
            "component_id": "compute-y", "kind": "unit",
            "provides": [{"capability": "perception", "level": 2}],
        })
        self.service.register_chain("plan", "robot-y", {
            "chain_id": "y-chain", "capability": "perception", "level": 2,
            "requires_components": ["compute-y"],
            "requires_resources": [{"resource_id": "gpu-1", "units": 1}], "priority": 0,
        })
        self.service.set_task("plan", "robot-y", {
            "task_id": "task-y", "required_capabilities": [{"capability": "perception", "min_level": 2}],
        })
        self.service.report_health("dispatch", "robot-y", {
            "component_id": "compute-y", "status": "ok", "metrics": {},
            "observed_at": "2026-09-24T08:05:00Z", "idempotency_key": "base-y",
        })
        plan_x = self.propose("p-x")
        self.confirm("p-x", plan_x["revision"])
        plan_y = self.service.propose_plan("dispatch", "robot-y", "p-y", "degrade")
        with self.assertRaises(Conflict):
            self.service.confirm_plan("risk", "p-y", plan_y["revision"])
        row = self.connection.execute("SELECT state FROM degradation_plans WHERE plan_id='p-y'").fetchone()
        self.assertEqual(row["state"], "proposed")
        locks = self.connection.execute("SELECT count(*) AS c FROM resource_locks WHERE plan_id='p-y'").fetchone()
        self.assertEqual(locks["c"], 0)

    def test_recover_requires_fresh_evidence_and_manual_action(self) -> None:
        self.health("vision-ai", "degraded", "87.5", "inc-1", "2026-09-24T08:10:00Z")
        plan = self.propose()
        self.confirm("p-1", plan["revision"])
        self.receipt("p-1", "r-1", 1, "restricted")
        with self.assertRaises(InvalidState):
            self.propose("p-2", "recover")
        stale_ok = self.health("vision-ai", "ok", "62", "rec-0", "2026-09-24T08:09:00Z")
        self.assertEqual(stale_ok["requirements_satisfied"], 0)
        with self.assertRaises(InvalidState):
            self.propose("p-2", "recover")
        fresh_ok = self.health("vision-ai", "ok", "62", "rec-1", "2026-09-24T08:30:00Z")
        self.assertEqual(fresh_ok["requirements_satisfied"], 1)
        recovery = self.propose("p-2", "recover")
        self.assertEqual(recovery["decision"], "continue")
        self.confirm("p-2", recovery["revision"])
        applied = self.receipt("p-2", "r-2", 1, "nominal")
        self.assertEqual(applied["robot_state"], "nominal")

    def test_safe_stop_plan_creates_manual_action_evidence(self) -> None:
        self.health("vision-ai", "failed", "91", "inc-1", "2026-09-24T08:10:00Z")
        self.health("lidar", "failed", "90", "inc-2", "2026-09-24T08:10:00Z")
        plan = self.propose()
        self.assertEqual(plan["decision"], "safe_stop")
        self.confirm("p-1", plan["revision"])
        status = self.service.robot_status("audit", "robot-x")
        self.assertEqual(len(status["pending_manual_actions"]), 1)
        kinds = {item["kind"] for item in status["outstanding_evidence"]}
        self.assertEqual(kinds, {"component_health", "manual_action"})
        action_id = status["pending_manual_actions"][0]["action_id"]
        done = self.service.complete_manual_action("dispatch", action_id, "已现场确认")
        self.assertEqual(done["state"], "completed")
        remaining = self.service.robot_status("audit", "robot-x")["outstanding_evidence"]
        self.assertEqual({item["kind"] for item in remaining}, {"component_health"})


class ReceiptTests(ServiceTestBase):
    def _confirmed_restricted_plan(self) -> None:
        self.health("vision-ai", "degraded", "87.5", "inc-1", "2026-09-24T08:10:00Z")
        plan = self.propose()
        self.confirm("p-1", plan["revision"])

    def test_duplicate_receipt_is_replayed_idempotently(self) -> None:
        self._confirmed_restricted_plan()
        first = self.receipt("p-1", "r-1", 1, "restricted")
        second = self.receipt("p-1", "r-1", 1, "restricted")
        self.assertEqual(first, second)
        count = self.connection.execute("SELECT count(*) AS c FROM execution_receipts").fetchone()
        self.assertEqual(count["c"], 1)

    def test_out_of_order_receipt_does_not_move_state(self) -> None:
        self.health("vision-ai", "failed", "91", "inc-1", "2026-09-24T08:10:00Z")
        self.health("lidar", "failed", "90", "inc-2", "2026-09-24T08:10:00Z")
        plan = self.propose()
        self.assertEqual(plan["decision"], "safe_stop")
        self.confirm("p-1", plan["revision"])
        applied = self.receipt("p-1", "r-2", 2, "restricted")
        self.assertEqual(applied["outcome"], "applied")
        late = self.receipt("p-1", "r-1", 1, "restricted")
        self.assertEqual(late["outcome"], "out_of_order")
        status = self.service.robot_status("audit", "robot-x")
        self.assertEqual(status["state"], "restricted")

    def test_late_normal_receipt_cannot_revive_safe_terminal_state(self) -> None:
        self._confirmed_restricted_plan()
        emergency = self.receipt("p-1", "r-1", 1, "safe_stopped")
        self.assertEqual(emergency["outcome"], "applied")
        self.assertEqual(emergency["robot_state"], "safe_stopped")
        late = self.receipt("p-1", "r-2", 2, "restricted")
        self.assertEqual(late["outcome"], "rejected_terminal")
        status = self.service.robot_status("audit", "robot-x")
        self.assertEqual(status["state"], "safe_stopped")

    def test_receipt_on_stale_plan_is_rejected(self) -> None:
        self._confirmed_restricted_plan()
        self.receipt("p-1", "r-1", 1, "restricted")
        self.health("lidar", "degraded", "80", "inc-2", "2026-09-24T08:20:00Z")
        late = self.receipt("p-1", "r-9", 9, "restricted")
        self.assertEqual(late["outcome"], "rejected_stale")

    def test_recovery_plan_can_leave_terminal_state(self) -> None:
        self._confirmed_restricted_plan()
        self.receipt("p-1", "r-1", 1, "safe_stopped")
        self.health("vision-ai", "ok", "60", "rec-1", "2026-09-24T08:30:00Z")
        recovery = self.propose("p-2", "recover")
        self.confirm("p-2", recovery["revision"])
        applied = self.receipt("p-2", "r-2", 1, "nominal")
        self.assertEqual(applied["outcome"], "applied")
        self.assertEqual(applied["robot_state"], "nominal")


class BoardAndPermissionTests(ServiceTestBase):
    def test_board_shows_missing_chains_actions_and_evidence(self) -> None:
        self.health("vision-ai", "failed", "91", "inc-1", "2026-09-24T08:10:00Z")
        self.health("lidar", "failed", "90", "inc-2", "2026-09-24T08:10:00Z")
        plan = self.propose()
        self.confirm("p-1", plan["revision"])
        status = self.service.robot_status("audit", "robot-x")
        self.assertEqual(status["missing_capabilities"], [{"capability": "perception", "min_level": 2}])
        self.assertEqual(status["live_decision"]["decision"], "safe_stop")
        self.assertEqual(len(status["pending_manual_actions"]), 1)
        self.assertTrue(status["outstanding_evidence"])
        board = self.service.fleet_board("audit")
        self.assertEqual([robot["robot_id"] for robot in board["robots"]], ["robot-x"])

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.propose_plan("plan", "robot-x", "p-x", "degrade")
        self.health("vision-ai", "degraded", "87.5", "inc-1", "2026-09-24T08:10:00Z")
        plan = self.propose()
        with self.assertRaises(Forbidden):
            self.service.confirm_plan("dispatch", "p-1", plan["revision"])
        with self.assertRaises(Forbidden):
            self.service.robot_status("plan", "robot-x")
        with self.assertRaises(Forbidden):
            self.service.report_health("audit", "robot-x", {
                "component_id": "vision-ai", "status": "ok", "metrics": {},
                "observed_at": "2026-09-24T08:30:00Z", "idempotency_key": "nope",
            })

    def test_health_report_idempotency(self) -> None:
        payload = {
            "component_id": "vision-ai", "status": "degraded", "metrics": {"temperature_c": "87.5"},
            "observed_at": "2026-09-24T08:10:00Z", "idempotency_key": "dup-1",
        }
        first = self.service.report_health("dispatch", "robot-x", payload)
        second = self.service.report_health("dispatch", "robot-x", payload)
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.service.report_health("dispatch", "robot-x", {**payload, "status": "failed"})

    def test_api_routes(self) -> None:
        app = JsonApplication(self.supply, self.service)
        import json as jsonlib
        created = app.handle("POST", "/degradation/robots", {"X-Actor-Id": "plan"},
                             jsonlib.dumps({"robot_id": "robot-z", "model": "巡检机器人", "robot_kind": "inspection"}).encode())
        self.assertEqual(created.status, 201)
        board = app.handle("GET", "/degradation/board", {"X-Actor-Id": "audit"})
        self.assertEqual(board.status, 200)
        self.assertEqual(len(board.body["robots"]), 2)
        missing = app.handle("GET", "/degradation/robots/robot-x/status", {"X-Actor-Id": "audit"})
        self.assertEqual(missing.status, 200)
        unknown = app.handle("POST", "/degradation/unknown", {"X-Actor-Id": "plan"}, b"{}")
        self.assertEqual(unknown.status, 404)


class AcceptanceTests(unittest.TestCase):
    def test_walkthrough(self) -> None:
        result = acceptance_run(ROOT)
        degradation = result["degradation"]
        self.assertEqual(degradation["transporter"]["degraded_decision"], "restricted")
        self.assertEqual(degradation["transporter"]["terminal_guard"], "rejected_terminal")
        self.assertEqual(degradation["assembler"]["stop_decision"], "safe_stop")
        self.assertTrue(degradation["assembler"]["duplicate_receipt_replayed"])
        self.assertEqual(degradation["assembler"]["late_normal_receipt"], "rejected_stale")
        self.assertTrue(degradation["assembler"]["recovery_blocked_until_evidence"])
        for robot in degradation["board_final"]:
            self.assertEqual(robot["state"], "nominal")
            self.assertEqual(robot["pending_manual_actions"], 0)
            self.assertEqual(robot["outstanding_evidence"], 0)
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
