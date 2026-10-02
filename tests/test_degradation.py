"""能力降级编排的领域计算与服务用例。"""

from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from datetime import datetime, timezone

from robot_control.api import JsonApplication
from robot_control.clock import FrozenClock
from robot_control.degradation import (
    CONTINUE,
    RESTRICTED,
    SAFE_STOP,
    WAIT_HUMAN,
    parse_plan_content,
    evaluate_plan,
)
from robot_control.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from robot_control.service import SupplyService


def health(robot: str, vision: str = "nominal") -> dict:
    return {
        "robot_id": robot,
        "components": [
            {"component_id": "ctrl-main", "status": "nominal",
             "provides": ["motion_control", "arm_control"], "recovery_evidence": ["ctrl-selfcheck-ok"]},
            {"component_id": "vision-ai-1", "status": vision, "provides": ["vision"],
             "health_reason": "视觉 AI 单元温度异常" if vision != "nominal" else None,
             "recovery_evidence": ["vision-selfcheck-pass", "vision-temp-normal"]},
            {"component_id": "lidar-1", "status": "nominal", "provides": ["obstacle_detection"],
             "recovery_evidence": ["lidar-selfcheck-ok"]},
            {"component_id": "operator-eyes", "status": "nominal", "provides": ["obstacle_detection"]},
        ],
    }


CARRY_CHAINS = [
    {"chain_id": "primary", "rank": 1, "nodes": [
        {"node_id": "ctrl-main", "provides": ["motion_control"]},
        {"node_id": "vision-ai-1", "provides": ["vision"]},
        {"node_id": "lidar-1", "provides": ["obstacle_detection"]}]},
    {"chain_id": "slowed-lidar", "rank": 2, "allows_partial": True, "nodes": [
        {"node_id": "ctrl-main", "provides": ["motion_control"]},
        {"node_id": "lidar-1", "provides": ["obstacle_detection"]}]},
]

CARRY_LIMITS = [
    {"applies_to": "vision", "on_missing": "alternative", "on_impaired": "restrict",
     "restrictions": {"max_speed_mps": "0.5", "min_obstacle_distance_m": "2.0"}},
    {"applies_to": "obstacle_detection", "on_missing": "safe_stop", "on_impaired": "restrict",
     "restrictions": {"min_obstacle_distance_m": "3.0"}},
]


def carry_plan(plan_id: str, robot: str = "carry") -> dict:
    return {
        "plan_id": plan_id, "robot_id": robot, "task_id": "move-1",
        "required_capabilities": ["vision", "motion_control", "obstacle_detection"],
        "safety_limits": copy.deepcopy(CARRY_LIMITS),
        "execution_chains": copy.deepcopy(CARRY_CHAINS),
    }


class EvaluationTests(unittest.TestCase):
    def evaluate(self, raw: dict, completed=frozenset()):
        return evaluate_plan(parse_plan_content(raw), completed_actions=completed)

    def test_all_nominal_selects_primary_and_continues(self) -> None:
        raw = health("r") | carry_plan("p")
        outcome = self.evaluate(raw)
        self.assertEqual(outcome["conclusion"], CONTINUE)
        self.assertEqual(outcome["selected_chain_id"], "primary")
        self.assertEqual(outcome["missing_capabilities"], [])
        self.assertEqual(outcome["locked_resources"], ["ctrl-main", "lidar-1", "vision-ai-1"])

    def test_degraded_vision_falls_back_to_slowed_lidar_chain(self) -> None:
        raw = health("r", "degraded") | carry_plan("p")
        outcome = self.evaluate(raw)
        self.assertEqual(outcome["conclusion"], RESTRICTED)
        self.assertEqual(outcome["decision_rule"], "alternative_chain_restricted")
        self.assertEqual(outcome["selected_chain_id"], "slowed-lidar")
        self.assertEqual(outcome["missing_capabilities"], ["vision"])
        self.assertEqual(outcome["restrictions"],
                         {"max_speed_mps": "0.5", "min_obstacle_distance_m": "2.0"})

    def test_failed_vision_safe_stop_when_policy_requires(self) -> None:
        chains = [{"chain_id": "primary", "rank": 1, "nodes": [
            {"node_id": "ctrl-main", "provides": ["arm_control", "motion_control"]},
            {"node_id": "vision-ai-1", "provides": ["vision"]},
            {"node_id": "lidar-1", "provides": ["obstacle_detection"]}]}]
        raw = health("r", "failed") | {
            "plan_id": "p", "task_id": "assemble-1",
            "required_capabilities": ["vision", "motion_control", "obstacle_detection", "arm_control"],
            "safety_limits": [
                {"applies_to": "vision", "on_missing": "safe_stop", "on_impaired": "safe_stop", "restrictions": {}},
                {"applies_to": "obstacle_detection", "on_missing": "wait_human", "on_impaired": "restrict", "restrictions": {}},
            ],
            "execution_chains": chains,
        }
        outcome = self.evaluate(raw)
        self.assertEqual(outcome["conclusion"], SAFE_STOP)
        self.assertIsNone(outcome["selected_chain_id"])
        self.assertEqual(outcome["locked_resources"], [])

    def test_wait_human_until_manual_compensation_evidence(self) -> None:
        chains = [{"chain_id": "manual-guard", "rank": 1, "allows_partial": True, "nodes": [
            {"node_id": "ctrl-main", "provides": ["motion_control"]},
            {"node_id": "operator-eyes", "provides": ["obstacle_detection"]}]}]
        raw = health("r", "degraded") | {
            "plan_id": "p", "task_id": "move-2",
            "required_capabilities": ["vision", "motion_control", "obstacle_detection"],
            "safety_limits": [
                {"applies_to": "vision", "on_missing": "wait_human", "on_impaired": "restrict",
                 "restrictions": {"max_speed_mps": "0.3", "min_obstacle_distance_m": "3.0"}},
                {"applies_to": "obstacle_detection", "on_missing": "safe_stop", "on_impaired": "restrict",
                 "restrictions": {}},
            ],
            "execution_chains": chains,
            "manual_actions": [{"action_id": "ma-clear", "description": "人工确认通道清空",
                                "compensates_capability": "vision"}],
        }
        outcome = self.evaluate(raw)
        self.assertEqual(outcome["conclusion"], WAIT_HUMAN)
        self.assertEqual(outcome["decision_rule"], "manual_compensation_pending")
        done = self.evaluate(raw, completed=frozenset({"ma-clear"}))
        self.assertEqual(done["conclusion"], RESTRICTED)
        self.assertEqual(done["restrictions"],
                         {"max_speed_mps": "0.3", "min_obstacle_distance_m": "3.0"})

    def test_wait_human_blocks_when_no_compensation_defined(self) -> None:
        chains = [{"chain_id": "manual-guard", "rank": 1, "allows_partial": True, "nodes": [
            {"node_id": "ctrl-main", "provides": ["motion_control"]},
            {"node_id": "operator-eyes", "provides": ["obstacle_detection"]}]}]
        raw = health("r", "failed") | {
            "plan_id": "p", "task_id": "move-3",
            "required_capabilities": ["vision", "motion_control", "obstacle_detection"],
            "safety_limits": [
                {"applies_to": "vision", "on_missing": "wait_human", "on_impaired": "restrict", "restrictions": {}},
                {"applies_to": "obstacle_detection", "on_missing": "safe_stop", "on_impaired": "restrict", "restrictions": {}},
            ],
            "execution_chains": chains,
        }
        self.assertEqual(self.evaluate(raw)["conclusion"], WAIT_HUMAN)

    def test_chain_cannot_reference_unknown_component_or_capability(self) -> None:
        raw = health("r", "degraded") | carry_plan("p")
        raw["execution_chains"][1]["nodes"][1]["provides"] = ["obstacle_detection", "laser_ray"]
        with self.assertRaises(ValidationFailed):
            parse_plan_content(raw)
        raw = health("r", "degraded") | carry_plan("p")
        raw["execution_chains"][1]["nodes"].append({"node_id": "ghost", "provides": ["vision"]})
        with self.assertRaises(ValidationFailed):
            parse_plan_content(raw)

    def test_required_capability_without_provider_rejected(self) -> None:
        raw = health("r") | carry_plan("p")
        raw["required_capabilities"].append("force_feedback")
        with self.assertRaises(ValidationFailed):
            parse_plan_content(raw)


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("op", "operator"), ("disp", "dispatcher"), ("au", "auditor")):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def record(self, robot: str = "carry", vision: str = "degraded") -> dict:
        return self.service.record_health("op", health(robot, vision))

    def freeze_carry(self, robot: str = "carry", plan_id: str = "plan-1") -> dict:
        return self.service.freeze_plan("op", carry_plan(plan_id, robot))

    def test_health_snapshot_is_versioned_and_deduped(self) -> None:
        first = self.record(vision="degraded")
        self.assertEqual(first["revision"], 1)
        self.assertTrue(first["changed"])
        duplicate = self.record(vision="degraded")
        self.assertEqual(duplicate["revision"], 1)
        self.assertFalse(duplicate["changed"])
        changed = self.record(vision="critical")
        self.assertEqual(changed["revision"], 2)

    def test_plan_is_frozen_with_content_sha_and_explanation(self) -> None:
        self.record()
        plan = self.freeze_carry()
        self.assertEqual(plan["version"], 1)
        self.assertEqual(plan["state"], "draft")
        self.assertEqual(plan["conclusion"], RESTRICTED)
        self.assertEqual(plan["selected_chain_id"], "slowed-lidar")
        self.assertEqual(len(plan["content_sha256"]), 64)
        self.assertTrue(plan["reasons"])

    def test_confirm_atomically_locks_chain_resources(self) -> None:
        self.record("carry")
        plan = self.freeze_carry("carry", "plan-carry")
        confirmed = self.service.confirm_plan("disp", "plan-carry", 1)
        self.assertEqual(confirmed["state"], "confirmed")
        locked = {row["resource_id"]: row["state"] for row in confirmed["locked_resources"]}
        self.assertEqual(locked, {"ctrl-main": "locked", "lidar-1": "locked"})
        # 第二台机器人不能抢到仍被锁定的资源
        self.record("carry-x")
        other = self.freeze_carry("carry-x", "plan-x")
        with self.assertRaises(Conflict):
            self.service.confirm_plan("disp", "plan-x", 1)
        # 确认失败不留下半截锁
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM plan_locked_resources WHERE plan_id='plan-x'").fetchone()[0],
            0,
        )

    def test_confirm_requires_current_version_and_current_health_revision(self) -> None:
        self.record()
        plan = self.freeze_carry()
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("disp", "plan-1", 99)
        self.record(vision="critical")
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("disp", "plan-1", 1)

    def test_safe_stop_conclusion_enters_terminal_state_without_locks(self) -> None:
        self.service.record_health("op", health("assemble", "failed"))
        raw = {
            "plan_id": "plan-asm", "robot_id": "assemble", "task_id": "assemble-1",
            "required_capabilities": ["vision", "motion_control", "obstacle_detection", "arm_control"],
            "safety_limits": [
                {"applies_to": "vision", "on_missing": "safe_stop", "on_impaired": "safe_stop", "restrictions": {}},
                {"applies_to": "obstacle_detection", "on_missing": "wait_human", "on_impaired": "restrict", "restrictions": {}},
            ],
            "execution_chains": [{"chain_id": "primary", "rank": 1, "nodes": [
                {"node_id": "ctrl-main", "provides": ["arm_control", "motion_control"]},
                {"node_id": "vision-ai-1", "provides": ["vision"]},
                {"node_id": "lidar-1", "provides": ["obstacle_detection"]}]}],
        }
        self.service.freeze_plan("op", raw)
        confirmed = self.service.confirm_plan("disp", "plan-asm", 1)
        self.assertEqual(confirmed["state"], "safe_stopped")
        self.assertEqual(confirmed["locked_resources"], [])

    def test_health_revision_invalidates_confirmed_plan_and_releases_locks(self) -> None:
        self.record()
        self.freeze_carry()
        self.service.confirm_plan("disp", "plan-1", 1)
        changed = self.record(vision="critical")
        self.assertEqual(changed["invalidated_plans"], ["plan-1"])
        plan = self.service.get_plan("plan-1")
        self.assertEqual(plan["state"], "invalidated")
        self.assertTrue(all(row["state"] == "released" for row in plan["locked_resources"]))
        # 失效计划不能确认
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("disp", "plan-1", 1)
        # 新版本可冻结，资源可重新锁定
        self.record(vision="degraded")
        version_two = self.freeze_carry("carry", "plan-2")
        self.assertEqual(version_two["version"], 2)
        confirmed = self.service.confirm_plan("disp", "plan-2", 2)
        self.assertEqual(confirmed["state"], "confirmed")

    def test_duplicate_and_out_of_order_receipts_are_idempotent(self) -> None:
        self.record()
        self.freeze_carry()
        self.service.confirm_plan("disp", "plan-1", 1)
        later = self.service.register_receipt("op", "plan-1", {
            "receipt_id": "r-2", "reported_state": "degraded", "observed_at": "2026-10-02T08:05:00Z"})
        self.assertTrue(later["applied"])
        self.assertEqual(later["state"], "executing")
        earlier = self.service.register_receipt("op", "plan-1", {
            "receipt_id": "r-1", "reported_state": "degraded", "observed_at": "2026-10-02T08:01:00Z"})
        self.assertFalse(earlier["applied"])
        self.assertEqual(earlier["ignored_reason"], "stale_out_of_order")
        replay = self.service.register_receipt("op", "plan-1", {
            "receipt_id": "r-2", "reported_state": "degraded", "observed_at": "2026-10-02T08:05:00Z"})
        self.assertTrue(replay["replayed"])

    def test_late_normal_receipt_cannot_leave_safe_terminal_state(self) -> None:
        self.service.record_health("op", health("assemble", "failed"))
        raw = {
            "plan_id": "plan-asm", "robot_id": "assemble", "task_id": "assemble-1",
            "required_capabilities": ["vision", "motion_control", "obstacle_detection", "arm_control"],
            "safety_limits": [
                {"applies_to": "vision", "on_missing": "safe_stop", "on_impaired": "safe_stop", "restrictions": {}},
                {"applies_to": "obstacle_detection", "on_missing": "wait_human", "on_impaired": "restrict", "restrictions": {}},
            ],
            "execution_chains": [{"chain_id": "primary", "rank": 1, "nodes": [
                {"node_id": "ctrl-main", "provides": ["arm_control", "motion_control"]},
                {"node_id": "vision-ai-1", "provides": ["vision"]},
                {"node_id": "lidar-1", "provides": ["obstacle_detection"]}]}],
        }
        self.service.freeze_plan("op", raw)
        self.service.confirm_plan("disp", "plan-asm", 1)
        late_normal = self.service.register_receipt("op", "plan-asm", {
            "receipt_id": "normal-late", "reported_state": "normal", "observed_at": "2026-10-02T09:00:00Z"})
        self.assertFalse(late_normal["applied"])
        self.assertEqual(late_normal["ignored_reason"], "safe_terminal_state")
        self.assertEqual(self.service.get_plan("plan-asm")["state"], "safe_stopped")

    def test_safe_stop_receipt_releases_locks_and_blocks_recovery(self) -> None:
        self.record()
        self.freeze_carry()
        self.service.confirm_plan("disp", "plan-1", 1)
        stop = self.service.register_receipt("op", "plan-1", {
            "receipt_id": "stop-1", "reported_state": "safe_stop", "observed_at": "2026-10-02T08:10:00Z"})
        self.assertEqual(stop["state"], "safe_stopped")
        plan = self.service.get_plan("plan-1")
        self.assertTrue(all(row["state"] == "released" for row in plan["locked_resources"]))
        recovery = self.service.register_receipt("op", "plan-1", {
            "receipt_id": "normal-1", "reported_state": "normal", "observed_at": "2026-10-02T08:30:00Z"})
        self.assertFalse(recovery["applied"])
        self.assertEqual(self.service.get_plan("plan-1")["state"], "safe_stopped")

    def test_manual_action_evidence_advances_wait_to_restricted(self) -> None:
        self.service.record_health("op", health("carry-2", "degraded"))
        raw = {
            "plan_id": "plan-wait", "robot_id": "carry-2", "task_id": "move-9",
            "required_capabilities": ["vision", "motion_control", "obstacle_detection"],
            "safety_limits": [
                {"applies_to": "vision", "on_missing": "wait_human", "on_impaired": "restrict",
                 "restrictions": {"max_speed_mps": "0.3", "min_obstacle_distance_m": "3.0"}},
                {"applies_to": "obstacle_detection", "on_missing": "safe_stop", "on_impaired": "restrict",
                 "restrictions": {}},
            ],
            "execution_chains": [{"chain_id": "manual-guard", "rank": 1, "allows_partial": True, "nodes": [
                {"node_id": "ctrl-main", "provides": ["motion_control"]},
                {"node_id": "operator-eyes", "provides": ["obstacle_detection"]}]}],
            "manual_actions": [{"action_id": "ma-clear", "description": "人工确认通道清空",
                                "compensates_capability": "vision"}],
        }
        self.service.freeze_plan("op", raw)
        self.assertEqual(self.service.get_plan("plan-wait")["conclusion"], WAIT_HUMAN)
        self.service.confirm_plan("disp", "plan-wait", 1)
        with self.assertRaises(ValidationFailed):
            self.service.complete_manual_action("op", "plan-wait", "ma-clear", "  ")
        updated = self.service.complete_manual_action("op", "plan-wait", "ma-clear", "现场清场照片 IMG-1")
        self.assertEqual(updated["conclusion"], RESTRICTED)
        self.assertEqual(updated["restrictions"],
                         {"max_speed_mps": "0.3", "min_obstacle_distance_m": "3.0"})
        action = next(item for item in updated["manual_actions"] if item["action_id"] == "ma-clear")
        self.assertEqual(action["state"], "completed")
        self.assertEqual(action["completed_by"], "op")
        with self.assertRaises(Conflict):
            self.service.complete_manual_action("op", "plan-wait", "ma-clear", "再次登记")

    def test_board_shows_gaps_chain_pending_actions_and_recovery_evidence(self) -> None:
        self.service.record_health("op", health("carry-2", "degraded"))
        raw = {
            "plan_id": "plan-wait", "robot_id": "carry-2", "task_id": "move-9",
            "required_capabilities": ["vision", "motion_control", "obstacle_detection"],
            "safety_limits": [
                {"applies_to": "vision", "on_missing": "wait_human", "on_impaired": "restrict",
                 "restrictions": {"max_speed_mps": "0.3", "min_obstacle_distance_m": "3.0"}},
                {"applies_to": "obstacle_detection", "on_missing": "safe_stop", "on_impaired": "restrict",
                 "restrictions": {}},
            ],
            "execution_chains": [{"chain_id": "manual-guard", "rank": 1, "allows_partial": True, "nodes": [
                {"node_id": "ctrl-main", "provides": ["motion_control"]},
                {"node_id": "operator-eyes", "provides": ["obstacle_detection"]}]}],
            "manual_actions": [{"action_id": "ma-clear", "description": "人工确认通道清空",
                                "compensates_capability": "vision"}],
        }
        self.service.freeze_plan("op", raw)
        view = self.service.robot_degradation("carry-2")
        plan = view["latest_plan"]
        self.assertEqual(plan["missing_capabilities"], ["vision"])
        self.assertEqual(plan["adopted_chain"]["chain_id"], "manual-guard")
        pending = [item["action_id"] for item in plan["manual_actions"] if item["state"] == "pending"]
        self.assertEqual(pending, ["ma-clear"])
        recovery = {item["capability"]: item for item in plan["recovery_requirements"]}
        self.assertEqual(recovery["vision"]["required_evidence"],
                         ["vision-selfcheck-pass", "vision-temp-normal"])
        self.assertEqual(recovery["vision"]["blocked_components"][0]["component_id"], "vision-ai-1")
        board = self.service.degradation_board("au")
        self.assertEqual([item["robot_id"] for item in board["robots"]], ["carry-2"])

    def test_role_permissions(self) -> None:
        self.record()
        with self.assertRaises(Forbidden):
            self.service.record_health("disp", health("carry"))
        self.freeze_carry()
        with self.assertRaises(Forbidden):
            self.service.confirm_plan("op", "plan-1", 1)
        # 调度值班可读后台，现场操作员不能
        self.assertEqual(
            [item["robot_id"] for item in self.service.degradation_board("disp")["robots"]],
            ["carry"],
        )
        with self.assertRaises(Forbidden):
            self.service.degradation_board("op")

    def test_api_routes(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        recorded = app.handle("POST", "/health/snapshots", {"X-Actor-Id": "op"},
                              json.dumps(health("carry", "degraded")).encode())
        self.assertEqual(recorded.status, 201)
        frozen = app.handle("POST", "/degradation/plans", {"X-Actor-Id": "op"},
                            json.dumps(carry_plan("plan-1")).encode())
        self.assertEqual(frozen.status, 201)
        confirmed = app.handle(
            "POST", "/degradation/plans/plan-1/confirm", {"X-Actor-Id": "disp"},
            b'{"expected_version":1}')
        self.assertEqual(confirmed.status, 200)
        board = app.handle("GET", "/degradation/board", {"X-Actor-Id": "au"})
        self.assertEqual(board.status, 200)
        robot = app.handle("GET", "/degradation/robots/carry", {"X-Actor-Id": "au"})
        self.assertEqual(robot.status, 200)
        self.assertEqual(robot.body["latest_plan"]["selected_chain_id"], "slowed-lidar")


if __name__ == "__main__":
    unittest.main()
