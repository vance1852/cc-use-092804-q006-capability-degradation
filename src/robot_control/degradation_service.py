"""能力降级编排的事务用例：计划版本、原子锁定、回执与恢复证据。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .degradation import (
    DECISION_TEXT,
    classify_receipt,
    component_usability,
    evaluate_degradation,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    ChainRegistration,
    ComponentRegistration,
    HealthReport,
    ReceiptRequest,
    ResourceRegistration,
    RobotRegistration,
    TaskDefinition,
)
from .planning import canonical_json, digest
from .service import ROLE_PERMISSIONS
from .storage import initialize, transaction


class DegradationService:
    """在单个 SQLite 连接上编排机器人能力降级与恢复。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def _robot(self, robot_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM degradation_robots WHERE robot_id=?", (robot_id,)
        ).fetchone()
        if row is None:
            raise NotFound("机器人不存在")
        return row

    def _plan(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM degradation_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("降级计划不存在")
        return row

    def _health_summary(self, robot_id: str) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT component_id,status,metrics_json,observed_at FROM component_health_reports "
            "WHERE robot_id=? ORDER BY observed_at,report_id",
            (robot_id,),
        ).fetchall()
        summary: dict[str, dict[str, Any]] = {}
        for row in rows:
            summary[row["component_id"]] = {
                "status": row["status"],
                "metrics": json.loads(row["metrics_json"]),
                "observed_at": row["observed_at"],
            }
        return summary

    def _chains(self, robot_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM execution_chains WHERE robot_id=? ORDER BY chain_id", (robot_id,)
        ).fetchall()
        return [
            {
                "chain_id": row["chain_id"],
                "capability": row["capability"],
                "level": int(row["level"]),
                "requires_components": json.loads(row["requires_components_json"]),
                "requires_resources": json.loads(row["requires_resources_json"]),
                "restrictions": json.loads(row["restrictions_json"]),
                "requires_manual_action": row["requires_manual_action"],
                "priority": int(row["priority"]),
            }
            for row in rows
        ]

    def _active_task(self, robot_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM robot_tasks WHERE robot_id=? AND state='active' ORDER BY created_at DESC LIMIT 1",
            (robot_id,),
        ).fetchone()

    def _outstanding_requirements(self, robot_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM recovery_requirements WHERE robot_id=? AND state='outstanding' "
            "ORDER BY requirement_id",
            (robot_id,),
        ).fetchall()

    def _bump_context(self, robot_id: str, reason: str, actor_id: str) -> None:
        """冻结输入变化：递增上下文版本并使未执行计划失效、释放其锁。"""
        now = self._now()
        self.connection.execute(
            "UPDATE degradation_robots SET context_version=context_version+1 WHERE robot_id=?",
            (robot_id,),
        )
        pending = self.connection.execute(
            "SELECT plan_id FROM degradation_plans WHERE robot_id=? AND state IN ('proposed','confirmed')",
            (robot_id,),
        ).fetchall()
        invalidated = [row["plan_id"] for row in pending]
        if invalidated:
            marks = ",".join("?" for _ in invalidated)
            self.connection.execute(
                f"UPDATE degradation_plans SET state='invalidated',revision=revision+1 "
                f"WHERE plan_id IN ({marks})",
                invalidated,
            )
            self.connection.execute(
                f"UPDATE resource_locks SET state='released',released_at=? "
                f"WHERE plan_id IN ({marks}) AND state='active'",
                [now, *invalidated],
            )
        robot = self._robot(robot_id)
        if robot["active_plan_id"] in invalidated:
            self.connection.execute(
                "UPDATE degradation_robots SET active_plan_id=NULL WHERE robot_id=?", (robot_id,)
            )
        version = self._robot(robot_id)["context_version"]
        self._audit(
            "degradation_robot",
            robot_id,
            "degradation.context_changed",
            actor_id,
            {"reason": reason, "context_version": version, "invalidated_plans": invalidated},
        )

    def register_robot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "degradation.catalog.write")
        robot = RobotRegistration.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO degradation_robots(robot_id,model,robot_kind,created_at) VALUES(?,?,?,?)",
                    (robot.robot_id, robot.model, robot.robot_kind, self._now()),
                )
                self._audit(
                    "degradation_robot", robot.robot_id, "degradation.robot_registered",
                    actor_id, {"model": robot.model, "robot_kind": robot.robot_kind},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("机器人编号已经存在") from exc
        return {"robot_id": robot.robot_id, "robot_kind": robot.robot_kind, "state": "nominal", "context_version": 1}

    def register_component(self, actor_id: str, robot_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "degradation.catalog.write")
        self._robot(robot_id)
        component = ComponentRegistration.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO robot_components(robot_id,component_id,kind,provides_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (robot_id, component.component_id, component.kind,
                     canonical_json(component.provides), self._now()),
                )
                self._bump_context(robot_id, f"登记部件 {component.component_id}", actor_id)
                self._audit(
                    "degradation_robot", robot_id, "degradation.component_registered",
                    actor_id, {"component_id": component.component_id, "kind": component.kind},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("部件编号已经存在") from exc
        return {"robot_id": robot_id, "component_id": component.component_id, "provides": component.provides}

    def register_chain(self, actor_id: str, robot_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "degradation.catalog.write")
        self._robot(robot_id)
        chain = ChainRegistration.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO execution_chains(robot_id,chain_id,capability,level,requires_components_json,"
                    "requires_resources_json,restrictions_json,requires_manual_action,priority,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        robot_id, chain.chain_id, chain.capability, chain.level,
                        canonical_json(chain.requires_components),
                        canonical_json(chain.requires_resources),
                        canonical_json(chain.restrictions),
                        chain.requires_manual_action, chain.priority, self._now(),
                    ),
                )
                self._bump_context(robot_id, f"登记执行链 {chain.chain_id}", actor_id)
                self._audit(
                    "degradation_robot", robot_id, "degradation.chain_registered",
                    actor_id, {"chain_id": chain.chain_id, "capability": chain.capability},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("执行链编号已经存在") from exc
        return {"robot_id": robot_id, "chain_id": chain.chain_id, "capability": chain.capability}

    def register_resource(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "degradation.catalog.write")
        resource = ResourceRegistration.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO control_resources(resource_id,kind,capacity,created_at) VALUES(?,?,?,?)",
                    (resource.resource_id, resource.kind, resource.capacity, self._now()),
                )
                self._audit(
                    "control_resource", resource.resource_id, "degradation.resource_registered",
                    actor_id, {"kind": resource.kind, "capacity": resource.capacity},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("控制资源编号已经存在") from exc
        return {"resource_id": resource.resource_id, "capacity": resource.capacity}

    def set_task(self, actor_id: str, robot_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "degradation.catalog.write")
        self._robot(robot_id)
        task = TaskDefinition.from_dict(raw)
        safety_limits = {"metric_limits": task.safety_limits}
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE robot_tasks SET state='exited',revision=revision+1 "
                    "WHERE robot_id=? AND state='active'",
                    (robot_id,),
                )
                self.connection.execute(
                    "INSERT INTO robot_tasks(task_id,robot_id,required_capabilities_json,safety_limits_json,"
                    "created_at) VALUES(?,?,?,?,?)",
                    (task.task_id, robot_id, canonical_json(task.required_capabilities),
                     canonical_json(safety_limits), self._now()),
                )
                self._bump_context(robot_id, "任务所需能力或安全限制更新", actor_id)
                self._audit(
                    "degradation_robot", robot_id, "degradation.task_set",
                    actor_id, {"task_id": task.task_id},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("任务编号已经存在") from exc
        return {"robot_id": robot_id, "task_id": task.task_id, "state": "active"}

    def report_health(self, actor_id: str, robot_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "degradation.health.write")
        self._robot(robot_id)
        report = HealthReport.from_dict(raw)
        component = self.connection.execute(
            "SELECT 1 FROM robot_components WHERE robot_id=? AND component_id=?",
            (robot_id, report.component_id),
        ).fetchone()
        if component is None:
            raise NotFound("部件未登记")
        request_digest = digest({"robot_id": robot_id, "payload": dict(raw)})
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency "
            "WHERE scope='degradation-health' AND idempotency_key=?",
            (report.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同健康上报内容")
            return json.loads(stored["response_json"])
        observed_at = utc_text(parse_utc(report.observed_at, "observed_at"))
        before = self._health_summary(robot_id).get(report.component_id)
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO component_health_reports(robot_id,component_id,status,metrics_json,observed_at,"
                "idempotency_key,reported_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (robot_id, report.component_id, report.status, canonical_json(report.metrics),
                 observed_at, report.idempotency_key, actor_id, now),
            )
            report_id = int(cursor.lastrowid)
            after = self._health_summary(robot_id).get(report.component_id)
            changed = (None if before is None else (before["status"], before["metrics"])) != (
                None if after is None else (after["status"], after["metrics"])
            )
            satisfied = 0
            if report.status == "ok":
                cursor = self.connection.execute(
                    "UPDATE recovery_requirements SET state='satisfied',satisfied_at=?,satisfied_by=? "
                    "WHERE robot_id=? AND kind='component_health' AND component_id=? AND state='outstanding' "
                    "AND blocked_observed_at<?",
                    (now, str(report_id), robot_id, report.component_id, observed_at),
                )
                satisfied = cursor.rowcount
            if changed:
                self._bump_context(robot_id, f"部件 {report.component_id} 健康摘要变化", actor_id)
            response = {
                "report_id": report_id,
                "robot_id": robot_id,
                "component_id": report.component_id,
                "status": report.status,
                "summary_changed": changed,
                "requirements_satisfied": satisfied,
                "context_version": self._robot(robot_id)["context_version"],
            }
            self.connection.execute(
                "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('degradation-health',?,?,?,?)",
                (report.idempotency_key, request_digest, canonical_json(response), now),
            )
            self._audit(
                "degradation_robot", robot_id, "degradation.health_reported", actor_id,
                {"report_id": report_id, "component_id": report.component_id, "status": report.status},
            )
        return response

    def _evaluate_now(self, robot_id: str) -> dict[str, Any]:
        task = self._active_task(robot_id)
        if task is None:
            raise InvalidState("机器人没有活动任务，无法编排降级计划")
        required = json.loads(task["required_capabilities_json"])
        safety_limits = json.loads(task["safety_limits_json"])
        metric_limits = {
            metric: Decimal(limit) for metric, limit in safety_limits.get("metric_limits", {}).items()
        }
        summary = self._health_summary(robot_id)
        chains = self._chains(robot_id)
        evaluation = evaluate_degradation(
            required_capabilities=required,
            chains=chains,
            health_summary=summary,
            metric_limits=metric_limits,
        )
        frozen = {
            "robot_id": robot_id,
            "task": {
                "task_id": task["task_id"],
                "required_capabilities": required,
                "safety_limits": safety_limits,
            },
            "health_summary": summary,
            "chains": chains,
        }
        return {"evaluation": evaluation, "frozen": frozen}

    def propose_plan(self, actor_id: str, robot_id: str, plan_id: str, purpose: str) -> dict[str, Any]:
        self._require(actor_id, "degradation.plan.propose")
        if purpose not in ("degrade", "recover"):
            raise ValidationFailed("purpose 必须是 degrade 或 recover")
        robot = self._robot(robot_id)
        if purpose == "recover":
            if robot["state"] == "nominal":
                raise InvalidState("机器人当前处于正常状态，无需恢复")
            outstanding = self._outstanding_requirements(robot_id)
            if outstanding:
                details = [row["detail"] for row in outstanding]
                raise InvalidState("恢复证据尚未重新满足：" + "；".join(details))
        evaluated = self._evaluate_now(robot_id)
        evaluation = evaluated["evaluation"]
        frozen = evaluated["frozen"]
        robot = self._robot(robot_id)
        frozen["context_version"] = robot["context_version"]
        frozen["purpose"] = purpose
        frozen["frozen_at"] = self._now()
        inputs_sha256 = digest({
            "robot_id": robot_id,
            "purpose": purpose,
            "task": frozen["task"],
            "health_summary": frozen["health_summary"],
            "chains": frozen["chains"],
        })
        try:
            with transaction(self.connection, immediate=True):
                stale = self.connection.execute(
                    "SELECT plan_id FROM degradation_plans WHERE robot_id=? AND state='proposed'",
                    (robot_id,),
                ).fetchall()
                if stale:
                    marks = ",".join("?" for _ in stale)
                    self.connection.execute(
                        f"UPDATE degradation_plans SET state='invalidated',revision=revision+1 "
                        f"WHERE plan_id IN ({marks})",
                        [row["plan_id"] for row in stale],
                    )
                self.connection.execute(
                    "INSERT INTO degradation_plans(plan_id,robot_id,purpose,context_version,inputs_sha256,"
                    "frozen_json,decision,target_mode,explanation_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        plan_id, robot_id, purpose, robot["context_version"], inputs_sha256,
                        canonical_json(frozen), evaluation["decision"], evaluation["target_mode"],
                        canonical_json(evaluation), actor_id, self._now(),
                    ),
                )
                self._audit(
                    "degradation_plan", plan_id, "degradation.plan_proposed", actor_id,
                    {
                        "robot_id": robot_id,
                        "purpose": purpose,
                        "decision": evaluation["decision"],
                        "inputs_sha256": inputs_sha256,
                        "replaced_proposals": [row["plan_id"] for row in stale],
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("计划编号已经存在") from exc
        return self._plan_view(self._plan(plan_id))

    def _plan_view(self, plan: sqlite3.Row) -> dict[str, Any]:
        return {
            "plan_id": plan["plan_id"],
            "robot_id": plan["robot_id"],
            "purpose": plan["purpose"],
            "context_version": plan["context_version"],
            "inputs_sha256": plan["inputs_sha256"],
            "decision": plan["decision"],
            "decision_text": DECISION_TEXT[plan["decision"]],
            "target_mode": plan["target_mode"],
            "state": plan["state"],
            "revision": plan["revision"],
            "explanation": json.loads(plan["explanation_json"]),
            "created_at": plan["created_at"],
            "confirmed_at": plan["confirmed_at"],
            "completed_at": plan["completed_at"],
        }

    def confirm_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "degradation.plan.confirm")
        plan = self._plan(plan_id)
        robot = self._robot(plan["robot_id"])
        robot_id = robot["robot_id"]
        if plan["state"] != "proposed" or plan["revision"] != expected_revision:
            raise InvalidState("计划不是当前待确认版本")
        if plan["context_version"] != robot["context_version"]:
            raise InvalidState("计划版本已失效，请重新评估")
        explanation = json.loads(plan["explanation_json"])
        required_resources = explanation["required_resources"]
        for item in required_resources:
            resource = self.connection.execute(
                "SELECT 1 FROM control_resources WHERE resource_id=?", (item["resource_id"],)
            ).fetchone()
            if resource is None:
                raise NotFound(f"控制资源 {item['resource_id']} 不存在")
        if plan["purpose"] == "recover":
            outstanding = self._outstanding_requirements(robot_id)
            if outstanding:
                details = [row["detail"] for row in outstanding]
                raise InvalidState("恢复证据尚未重新满足：" + "；".join(details))
        now = self._now()
        with transaction(self.connection, immediate=True):
            superseding = self.connection.execute(
                "SELECT plan_id FROM degradation_plans WHERE robot_id=? AND state IN ('confirmed','executing')",
                (robot_id,),
            ).fetchall()
            if superseding:
                marks = ",".join("?" for _ in superseding)
                self.connection.execute(
                    f"UPDATE degradation_plans SET state='superseded',revision=revision+1 "
                    f"WHERE plan_id IN ({marks})",
                    [row["plan_id"] for row in superseding],
                )
            self.connection.execute(
                "UPDATE resource_locks SET state='released',released_at=? "
                "WHERE robot_id=? AND state='active'",
                (now, robot_id),
            )
            for item in required_resources:
                used = self.connection.execute(
                    "SELECT COALESCE(SUM(units),0) AS used FROM resource_locks "
                    "WHERE resource_id=? AND state='active'",
                    (item["resource_id"],),
                ).fetchone()["used"]
                capacity = self.connection.execute(
                    "SELECT capacity FROM control_resources WHERE resource_id=?",
                    (item["resource_id"],),
                ).fetchone()["capacity"]
                if used + item["units"] > capacity:
                    raise Conflict(f"控制资源 {item['resource_id']} 容量不足，无法原子锁定")
            for item in required_resources:
                self.connection.execute(
                    "INSERT INTO resource_locks(plan_id,robot_id,resource_id,units,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (plan_id, robot_id, item["resource_id"], item["units"], now),
                )
            self.connection.execute(
                "UPDATE degradation_plans SET state='confirmed',confirmed_at=?,revision=revision+1 "
                "WHERE plan_id=?",
                (now, plan_id),
            )
            self.connection.execute(
                "UPDATE degradation_robots SET active_plan_id=? WHERE robot_id=?",
                (plan_id, robot_id),
            )
            manual_action_ids: list[str] = []
            requirement_ids: list[int] = []
            if plan["purpose"] == "degrade":
                descriptions = list(explanation["manual_actions"])
                if plan["decision"] == "safe_stop":
                    descriptions.append(f"现场确认机器人 {robot_id} 已安全停机并退出任务")
                for description in dict.fromkeys(descriptions):
                    existing = self.connection.execute(
                        "SELECT action_id FROM manual_actions WHERE robot_id=? AND description=? "
                        "AND state='pending'",
                        (robot_id, description),
                    ).fetchone()
                    if existing is not None:
                        continue
                    action_id = f"{plan_id}-ma-{len(manual_action_ids) + 1}"
                    self.connection.execute(
                        "INSERT INTO manual_actions(action_id,robot_id,plan_id,description,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (action_id, robot_id, plan_id, description, now),
                    )
                    manual_action_ids.append(action_id)
                    cursor = self.connection.execute(
                        "INSERT INTO recovery_requirements(robot_id,kind,action_id,detail,raised_at) "
                        "VALUES(?,?,?,?,?)",
                        (robot_id, "manual_action", action_id, f"完成人工动作：{description}", now),
                    )
                    requirement_ids.append(int(cursor.lastrowid))
                frozen = json.loads(plan["frozen_json"])
                summary = frozen["health_summary"]
                limits = {
                    metric: Decimal(limit)
                    for metric, limit in frozen["task"]["safety_limits"].get("metric_limits", {}).items()
                }
                for component_id in sorted(summary):
                    usability, reasons = component_usability(component_id, summary, limits)
                    if usability == "ok":
                        continue
                    existing = self.connection.execute(
                        "SELECT 1 FROM recovery_requirements WHERE robot_id=? AND kind='component_health' "
                        "AND component_id=? AND state='outstanding'",
                        (robot_id, component_id),
                    ).fetchone()
                    if existing is not None:
                        continue
                    cursor = self.connection.execute(
                        "INSERT INTO recovery_requirements(robot_id,kind,component_id,detail,"
                        "blocked_observed_at,raised_at) VALUES(?,?,?,?,?,?)",
                        (
                            robot_id, "component_health", component_id,
                            f"部件 {component_id} 需要新的健康证据（{';'.join(reasons)}）",
                            summary[component_id]["observed_at"], now,
                        ),
                    )
                    requirement_ids.append(int(cursor.lastrowid))
            self._audit(
                "degradation_plan", plan_id, "degradation.plan_confirmed", actor_id,
                {
                    "robot_id": robot_id,
                    "decision": plan["decision"],
                    "locked_resources": required_resources,
                    "superseded_plans": [row["plan_id"] for row in superseding],
                    "manual_actions": manual_action_ids,
                    "recovery_requirements": requirement_ids,
                },
            )
        return self._plan_view(self._plan(plan_id))

    def submit_receipt(self, actor_id: str, plan_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "degradation.receipt.write")
        plan = self._plan(plan_id)
        receipt = ReceiptRequest.from_dict(raw)
        stored = self.connection.execute(
            "SELECT plan_id,response_json FROM execution_receipts WHERE receipt_id=?",
            (receipt.receipt_id,),
        ).fetchone()
        if stored is not None:
            if stored["plan_id"] != plan_id:
                raise Conflict("回执编号已被其他计划使用")
            return json.loads(stored["response_json"])
        robot = self._robot(plan["robot_id"])
        outcome = classify_receipt(
            plan_state=plan["state"],
            plan_purpose=plan["purpose"],
            robot_mode=robot["state"],
            reported_mode=receipt.reported_mode,
            seq=receipt.seq,
            last_applied_seq=plan["last_receipt_seq"],
        )
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                if outcome == "applied":
                    self.connection.execute(
                        "UPDATE degradation_robots SET state=? WHERE robot_id=?",
                        (receipt.reported_mode, robot["robot_id"]),
                    )
                    completed = receipt.reported_mode == plan["target_mode"]
                    new_state = "completed" if completed else "executing"
                    self.connection.execute(
                        "UPDATE degradation_plans SET state=?,last_receipt_seq=?,revision=revision+1,"
                        "completed_at=CASE WHEN ? THEN ? ELSE completed_at END WHERE plan_id=?",
                        (new_state, receipt.seq, completed, now, plan_id),
                    )
                    if completed and plan["target_mode"] == "safe_stopped":
                        self.connection.execute(
                            "UPDATE resource_locks SET state='released',released_at=? "
                            "WHERE plan_id=? AND state='active'",
                            (now, plan_id),
                        )
                fresh_plan = self._plan(plan_id)
                fresh_robot = self._robot(robot["robot_id"])
                response = {
                    "receipt_id": receipt.receipt_id,
                    "plan_id": plan_id,
                    "robot_id": robot["robot_id"],
                    "outcome": outcome,
                    "robot_state": fresh_robot["state"],
                    "plan_state": fresh_plan["state"],
                }
                self.connection.execute(
                    "INSERT INTO execution_receipts(receipt_id,plan_id,robot_id,seq,reported_mode,outcome,"
                    "note,response_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (receipt.receipt_id, plan_id, robot["robot_id"], receipt.seq, receipt.reported_mode,
                     outcome, receipt.note, canonical_json(response), now),
                )
                self._audit(
                    "degradation_plan", plan_id, "degradation.receipt_recorded", actor_id,
                    {"receipt_id": receipt.receipt_id, "outcome": outcome, "reported_mode": receipt.reported_mode},
                )
        except sqlite3.IntegrityError:
            stored = self.connection.execute(
                "SELECT response_json FROM execution_receipts WHERE receipt_id=?",
                (receipt.receipt_id,),
            ).fetchone()
            if stored is None:
                raise
            return json.loads(stored["response_json"])
        return response

    def complete_manual_action(self, actor_id: str, action_id: str, note: str = "") -> dict[str, Any]:
        self._require(actor_id, "degradation.action.write")
        action = self.connection.execute(
            "SELECT * FROM manual_actions WHERE action_id=?", (action_id,)
        ).fetchone()
        if action is None:
            raise NotFound("人工动作不存在")
        if action["state"] != "pending":
            raise InvalidState("人工动作已处理")
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE manual_actions SET state='completed',completed_by=?,completed_at=? WHERE action_id=?",
                (actor_id, now, action_id),
            )
            self.connection.execute(
                "UPDATE recovery_requirements SET state='satisfied',satisfied_at=?,satisfied_by=? "
                "WHERE kind='manual_action' AND action_id=? AND state='outstanding'",
                (now, actor_id, action_id),
            )
            self._bump_context(action["robot_id"], f"人工动作 {action_id} 已完成", actor_id)
            self._audit(
                "degradation_robot", action["robot_id"], "degradation.manual_action_completed",
                actor_id, {"action_id": action_id, "note": note},
            )
        return {"action_id": action_id, "state": "completed", "completed_at": now}

    def robot_status(self, actor_id: str, robot_id: str) -> dict[str, Any]:
        self._require(actor_id, "degradation.board.read")
        robot = self._robot(robot_id)
        summary = self._health_summary(robot_id)
        active_plan = None
        adopted_chains: list[dict[str, Any]] = []
        restrictions: dict[str, str] = {}
        if robot["active_plan_id"]:
            plan = self._plan(robot["active_plan_id"])
            explanation = json.loads(plan["explanation_json"])
            active_plan = {
                "plan_id": plan["plan_id"],
                "purpose": plan["purpose"],
                "decision": plan["decision"],
                "decision_text": DECISION_TEXT[plan["decision"]],
                "state": plan["state"],
                "confirmed_at": plan["confirmed_at"],
            }
            adopted_chains = explanation["selected_chains"]
            restrictions = explanation["restrictions"]
        missing: list[dict[str, Any]] = []
        live_decision = None
        if self._active_task(robot_id) is not None:
            evaluated = self._evaluate_now(robot_id)["evaluation"]
            missing = evaluated["missing_capabilities"]
            live_decision = {
                "decision": evaluated["decision"],
                "decision_text": evaluated["decision_text"],
            }
        pending_actions = self.connection.execute(
            "SELECT action_id,plan_id,description,created_at FROM manual_actions "
            "WHERE robot_id=? AND state='pending' ORDER BY created_at,action_id",
            (robot_id,),
        ).fetchall()
        outstanding = self._outstanding_requirements(robot_id)
        return {
            "robot_id": robot_id,
            "model": robot["model"],
            "robot_kind": robot["robot_kind"],
            "state": robot["state"],
            "context_version": robot["context_version"],
            "active_plan": active_plan,
            "missing_capabilities": missing,
            "live_decision": live_decision,
            "adopted_chains": adopted_chains,
            "restrictions_in_effect": restrictions,
            "pending_manual_actions": [dict(row) for row in pending_actions],
            "outstanding_evidence": [
                {
                    "requirement_id": row["requirement_id"],
                    "kind": row["kind"],
                    "component_id": row["component_id"],
                    "action_id": row["action_id"],
                    "detail": row["detail"],
                    "raised_at": row["raised_at"],
                }
                for row in outstanding
            ],
            "health_summary": summary,
        }

    def fleet_board(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "degradation.board.read")
        robots = self.connection.execute(
            "SELECT robot_id FROM degradation_robots ORDER BY robot_id"
        ).fetchall()
        return {
            "generated_at": self._now(),
            "robots": [self.robot_status(actor_id, row["robot_id"]) for row in robots],
        }
