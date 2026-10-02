"""控制算力单价、控制算力库存、实时控制总线和提名的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .degradation import (
    CONTINUE,
    RESTRICTED,
    SAFE_STOP,
    WAIT_HUMAN,
    evaluate_plan,
    freeze_content,
    parse_components,
    parse_plan_content,
)
from .models import IndexQuote, Facility, InventoryLot, NominationRequest, Route, SupplyScenario
from .planning import (
    AllocationRequest,
    PricePoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    moving_average,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run", "degradation.read"},
    "dispatcher": {"nomination.write", "allocation.run", "transfer.write", "inventory.write", "plan.confirm", "degradation.read"},
    "risk": {"outage.write", "scenario.approve", "report.read", "degradation.read"},
    "auditor": {"report.read", "audit.read", "degradation.read"},
    "operator": {
        "health.write", "plan.write", "manual.write", "receipt.write"
    },
}

TERMINAL_PLAN_STATES = {"safe_stopped", "completed", "superseded", "invalidated"}


class SupplyService:
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

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        quote = IndexQuote.from_dict(raw)
        previous = self.connection.execute(
            "SELECT quote_id,source_revision FROM market_index_quotes WHERE market_index=? AND trade_date=? "
            "ORDER BY quote_id DESC LIMIT 1",
            (quote.market_index, quote.trade_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == quote.source_revision:
            raise Conflict("同一来源修订已登记")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO market_index_quotes(market_index,trade_date,close_cny,source_revision,observed_at,"
                    "supersedes_quote_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        quote.market_index,
                        quote.trade_date,
                        decimal_text(quote.close_cny),
                        quote.source_revision,
                        quote.observed_at,
                        None if previous is None else previous["quote_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                quote_id = int(cursor.lastrowid)
                self._audit(
                    "quote",
                    str(quote_id),
                    "quote.recorded",
                    actor_id,
                    {"market_index": quote.market_index, "trade_date": quote.trade_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("控制算力单价版本冲突") from exc
        return {"quote_id": quote_id, "market_index": quote.market_index, "trade_date": quote.trade_date}

    def price_summary(self, market_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT q.trade_date,q.close_cny FROM market_index_quotes q "
            "JOIN (SELECT trade_date,max(quote_id) quote_id FROM market_index_quotes "
            "WHERE market_index=? GROUP BY trade_date) latest ON latest.quote_id=q.quote_id "
            "ORDER BY q.trade_date DESC LIMIT ?",
            (market_index.upper(), sessions),
        ).fetchall()
        points = [PricePoint(row["trade_date"], Decimal(row["close_cny"])) for row in rows]
        if not points:
            raise NotFound("没有基准控制算力单价")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.trade_date)
        return {
            "market_index": market_index.upper(),
            "latest": {"trade_date": latest.trade_date, "close_cny": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "observations": len(points),
        }

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = Facility.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO facilities(facility_id,name,kind,timezone,capacity_control_slots,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.facility_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_control_slots),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.facility_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = Route.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO routes(route_id,origin_id,destination_id,product,daily_capacity,"
                    "loss_basis_points,transit_hours,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route.route_id,
                        route.origin_id,
                        route.destination_id,
                        route.product,
                        decimal_text(route.daily_capacity),
                        route.loss_basis_points,
                        route.transit_hours,
                        self._now(),
                    ),
                )
                self._audit("route", route.route_id, "route.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("实时控制总线编号冲突或设施不存在") from exc
        return self.route(route.route_id)

    def route(self, route_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            raise NotFound("实时控制总线不存在")
        return dict(row)

    def announce_outage(
        self,
        actor_id: str,
        route_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(route_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO route_outages(route_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (route_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            outage_id = int(cursor.lastrowid)
            self._audit("route", route_id, "outage.announced", actor_id, {"outage_id": outage_id})
        return {"outage_id": outage_id, "route_id": route_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = InventoryLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_control_slots,available_control_slots,"
                    "unit_cost_cny,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.lot_id,
                        lot.facility_id,
                        lot.product,
                        lot.grade,
                        decimal_text(lot.quantity_control_slots),
                        decimal_text(lot.quantity_control_slots),
                        decimal_text(lot.unit_cost_cny),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("国产控制器资源批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.lot_id)

    def inventory_lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound("国产控制器资源批次不存在")
        return dict(row)

    def inventory_summary(self, facility_id: str, product: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE facility_id=? AND product=? ORDER BY received_at,lot_id",
            (facility_id, product),
        ).fetchall()
        return {"facility_id": facility_id, "product": product, **weighted_inventory_cost(rows)}

    def submit_nomination(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "nomination.write")
        nomination = NominationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='nomination' AND idempotency_key=?",
            (nomination.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同提名内容")
            return json.loads(stored["response_json"])
        route = self.route(nomination.route_id)
        if route["state"] != "active":
            raise InvalidState("实时控制总线当前不可提名")
        response = {
            "nomination_id": nomination.nomination_id,
            "route_id": nomination.route_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO nominations(nomination_id,route_id,shipper_id,service_date,requested_control_slots,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        nomination.nomination_id,
                        nomination.route_id,
                        nomination.shipper_id,
                        nomination.service_date,
                        decimal_text(nomination.requested_control_slots),
                        nomination.priority,
                        nomination.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('nomination',?,?,?,?)",
                    (nomination.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("nomination", nomination.nomination_id, "nomination.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("提名编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, service_date: str) -> Decimal:
        start = service_date + "T00:00:00Z"
        end = service_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY outage_id",
            (route["route_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        return effective_capacity(Decimal(route["daily_capacity"]), percentages)

    def allocate(self, actor_id: str, route_id: str, service_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise NotFound("实时控制总线不存在")
        nominations = self.connection.execute(
            "SELECT * FROM nominations WHERE route_id=? AND service_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,nomination_id",
            (route_id, service_date),
        ).fetchall()
        if not nominations:
            raise InvalidState("没有待分配提名")
        requests = [
            AllocationRequest(
                row["nomination_id"],
                Decimal(row["requested_control_slots"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in nominations
        ]
        available = self._capacity_for_date(route, service_date)
        input_value = [dict(row) for row in nominations]
        input_sha256 = digest({"route": dict(route), "nominations": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "route_id": route_id,
            "service_date": service_date,
            "available_capacity": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(route_id,service_date,input_sha256,available_capacity,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (route_id, service_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_control_slots"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE nominations SET allocated_control_slots=?,state=?,revision=revision+1 "
                    "WHERE nomination_id=? AND state='submitted'",
                    (item["allocated_control_slots"], state, item["nomination_id"]),
                )
            allocation_id = int(cursor.lastrowid)
            self._audit("route", route_id, "allocation.completed", actor_id, {"allocation_id": allocation_id})
        return {"allocation_id": allocation_id, **result}

    def dispatch_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        nomination_id: str,
        lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        nomination = self.connection.execute(
            "SELECT n.*,r.loss_basis_points,r.transit_hours,r.origin_id FROM nominations n "
            "JOIN routes r ON r.route_id=n.route_id WHERE n.nomination_id=?",
            (nomination_id,),
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if nomination["state"] != "allocated" or nomination["revision"] != expected_revision:
            raise InvalidState("提名不是当前可交付版本")
        lot = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFound("国产控制器资源批次不存在")
        allocated = Decimal(nomination["allocated_control_slots"])
        available = Decimal(lot["available_control_slots"])
        if lot["facility_id"] != nomination["origin_id"] or lot["product"] != self.route(nomination["route_id"])["product"]:
            raise Conflict("国产控制器资源批次与实时控制总线起点或资源类型不匹配")
        if available < allocated:
            raise Conflict("控制算力库存不足以完成分配")
        expected_delivery = delivered_after_loss(allocated, int(nomination["loss_basis_points"]))
        departed_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE inventory_lots SET available_control_slots=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - allocated)), lot_id, lot["revision"]),
            )
            self.connection.execute(
                "UPDATE nominations SET state='in_transit',revision=revision+1 WHERE nomination_id=? AND revision=?",
                (nomination_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,loaded_control_slots,"
                "expected_delivered_control_slots,departed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    nomination_id,
                    lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    actor_id,
                    departed_at,
                ),
            )
            self._audit("transfer", transfer_id, "transfer.dispatched", actor_id, {"nomination_id": nomination_id})
        return {
            "transfer_id": transfer_id,
            "state": "in_transit",
            "loaded_control_slots": decimal_text(allocated),
            "expected_delivered_control_slots": decimal_text(expected_delivery),
            "expected_arrival": utc_text(parse_utc(departed_at) + timedelta(hours=int(nomination["transit_hours"]))),
        }

    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = SupplyScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE supply_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM supply_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = SupplyScenario.from_dict(json.loads(row["definition_json"]))
        price_row = self.connection.execute(
            "SELECT close_cny FROM market_index_quotes WHERE trade_date<=? ORDER BY trade_date DESC,quote_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
        if price_row is None:
            raise InvalidState("截止日期没有可用控制算力单价")
        routes = self.connection.execute("SELECT * FROM routes WHERE state='active' ORDER BY route_id").fetchall()
        inventory = self.connection.execute(
            "SELECT facility_id,product,sum(CAST(available_control_slots AS REAL)) available_control_slots "
            "FROM inventory_lots GROUP BY facility_id,product ORDER BY facility_id,product"
        ).fetchall()
        input_value = {
            "scenario_sha256": row["content_sha256"],
            "as_of_date": as_of_date,
            "price": price_row["close_cny"],
            "routes": [dict(item) for item in routes],
            "inventory": [dict(item) for item in inventory],
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
            (scenario_id, as_of_date, input_sha256),
        ).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = scenario_projection(
            current_price=Decimal(price_row["close_cny"]),
            market_index_drop_percent=scenario.market_index_drop_percent,
            routes=routes,
            inventory=inventory,
            route_capacity_changes=scenario.route_capacity_changes,
            demand_changes=scenario.demand_changes,
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM supply_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}

    # ------------------------------------------------------------------
    # 能力降级编排
    # ------------------------------------------------------------------

    def record_health(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记部件健康摘要版本；摘要变化自动使该机器人未终态计划失效。"""

        self._require(actor_id, "health.write")
        robot_id = raw.get("robot_id")
        if not isinstance(robot_id, str) or not robot_id.strip():
            raise ValidationFailed("robot_id 不能为空")
        robot_id = robot_id.strip()
        components = parse_components(raw.get("components"))
        summary = {
            "robot_id": robot_id,
            "components": [
                {
                    "component_id": item.component_id,
                    "status": item.status,
                    "provides": sorted(item.provides),
                    "health_reason": item.health_reason,
                    "recovery_evidence": list(item.recovery_evidence),
                }
                for item in components
            ],
        }
        summary_text = canonical_json(summary)
        content_sha256 = hashlib.sha256(summary_text.encode("utf-8")).hexdigest()
        with transaction(self.connection, immediate=True):
            previous = self.connection.execute(
                "SELECT revision,content_sha256 FROM health_snapshots WHERE robot_id=? "
                "ORDER BY revision DESC LIMIT 1",
                (robot_id,),
            ).fetchone()
            if previous is not None and previous["content_sha256"] == content_sha256:
                return {"robot_id": robot_id, "revision": previous["revision"], "changed": False}
            cursor = self.connection.execute(
                "INSERT INTO health_snapshots(robot_id,summary_json,content_sha256,created_by,created_at) "
                "VALUES(?,?,?,?,?)",
                (robot_id, summary_text, content_sha256, actor_id, self._now()),
            )
            revision = int(cursor.lastrowid)
            invalidated: list[str] = []
            if previous is not None:
                active = self.connection.execute(
                    "SELECT plan_id,state FROM degradation_plans WHERE robot_id=? "
                    "AND state IN ('draft','confirmed','executing')",
                    (robot_id,),
                ).fetchall()
                for row in active:
                    self.connection.execute(
                        "UPDATE degradation_plans SET state='invalidated' WHERE plan_id=? "
                        "AND state IN ('draft','confirmed','executing')",
                        (row["plan_id"],),
                    )
                    if row["state"] in {"confirmed", "executing"}:
                        self._release_locks(row["plan_id"])
                    invalidated.append(row["plan_id"])
                    self._audit(
                        "degradation_plan", row["plan_id"], "plan.invalidated", actor_id,
                        {"reason": "health_revision_changed", "health_revision": revision},
                    )
            self._audit(
                "robot_health", robot_id, "health.recorded", actor_id,
                {"revision": revision, "sha256": content_sha256, "invalidated_plans": invalidated},
            )
        return {"robot_id": robot_id, "revision": revision, "changed": True, "invalidated_plans": invalidated}

    def _latest_health(self, robot_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM health_snapshots WHERE robot_id=? ORDER BY revision DESC LIMIT 1",
            (robot_id,),
        ).fetchone()
        if row is None:
            raise NotFound("机器人还没有部件健康摘要")
        return row

    def freeze_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """把任务能力、健康摘要、安全限制和替代链冻结成一个计划版本。"""

        self._require(actor_id, "plan.write")
        robot_id = raw.get("robot_id")
        if not isinstance(robot_id, str) or not robot_id.strip():
            raise ValidationFailed("robot_id 不能为空")
        robot_id = robot_id.strip()
        snapshot = self._latest_health(robot_id)
        health_summary = json.loads(snapshot["summary_json"])
        merged = dict(raw)
        merged["robot_id"] = robot_id
        merged["components"] = health_summary["components"]
        parsed = parse_plan_content(merged)
        outcome = evaluate_plan(parsed)
        content_text, content_sha256 = freeze_content(parsed, int(snapshot["revision"]))
        active = self.connection.execute(
            "SELECT plan_id FROM degradation_plans WHERE robot_id=? AND state IN ('confirmed','executing')",
            (robot_id,),
        ).fetchone()
        if active is not None:
            raise Conflict("该机器人存在已确认且未终态的计划，请先登记新的健康摘要使其失效")
        duplicate = self.connection.execute(
            "SELECT plan_id FROM degradation_plans WHERE robot_id=? AND content_sha256=?",
            (robot_id, content_sha256),
        ).fetchone()
        if duplicate is not None:
            raise Conflict("相同健康版本与计划内容已经冻结过")
        row = self.connection.execute(
            "SELECT coalesce(max(version),0)+1 AS next_version FROM degradation_plans WHERE robot_id=?",
            (robot_id,),
        ).fetchone()
        version = int(row["next_version"])
        plan_id = raw.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise ValidationFailed("plan_id 不能为空")
        plan_id = plan_id.strip()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO degradation_plans(plan_id,robot_id,task_id,version,state,health_revision,content_json,"
                    "content_sha256,conclusion,decision_rule,selected_chain_id,restrictions_json,missing_json,impaired_json,"
                    "reasons_json,created_by,created_at) VALUES(?,?,?,?, 'draft',?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        plan_id, robot_id, parsed["task_id"], version, int(snapshot["revision"]),
                        content_text, content_sha256, outcome["conclusion"], outcome["decision_rule"],
                        outcome["selected_chain_id"], canonical_json(outcome["restrictions"]),
                        canonical_json(outcome["missing_capabilities"]),
                        canonical_json(outcome["impaired_capabilities"]),
                        canonical_json(outcome["reasons"]), actor_id, self._now(),
                    ),
                )
                for action in parsed["manual_actions"]:
                    self.connection.execute(
                        "INSERT INTO plan_manual_actions(plan_id,action_id,description,compensates_capability) "
                        "VALUES(?,?,?,?)",
                        (plan_id, action.action_id, action.description, action.compensates_capability),
                    )
                self._audit(
                    "degradation_plan", plan_id, "plan.frozen", actor_id,
                    {"robot_id": robot_id, "version": version, "conclusion": outcome["conclusion"],
                     "health_revision": snapshot["revision"], "sha256": content_sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("计划编号冲突，或该机器人存在并发的活跃计划版本") from exc
        return self.get_plan(plan_id)

    def _plan_row(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM degradation_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound("降级计划不存在")
        return row

    def _release_locks(self, plan_id: str) -> None:
        self.connection.execute(
            "UPDATE plan_locked_resources SET state='released',released_at=? WHERE plan_id=? AND state='locked'",
            (self._now(), plan_id),
        )

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        row = self._plan_row(plan_id)
        actions = self.connection.execute(
            "SELECT action_id,description,compensates_capability,state,evidence_json,completed_by,completed_at "
            "FROM plan_manual_actions WHERE plan_id=? ORDER BY action_id",
            (plan_id,),
        ).fetchall()
        locks = self.connection.execute(
            "SELECT resource_id,state,locked_at,released_at FROM plan_locked_resources WHERE plan_id=? ORDER BY resource_id",
            (plan_id,),
        ).fetchall()
        receipts = self.connection.execute(
            "SELECT receipt_id,reported_state,observed_at,applied,ignored_reason,created_at "
            "FROM plan_receipts WHERE plan_id=? ORDER BY observed_at,receipt_id",
            (plan_id,),
        ).fetchall()
        return {
            "plan_id": row["plan_id"],
            "robot_id": row["robot_id"],
            "task_id": row["task_id"],
            "version": row["version"],
            "state": row["state"],
            "health_revision": row["health_revision"],
            "content_sha256": row["content_sha256"],
            "conclusion": row["conclusion"],
            "decision_rule": row["decision_rule"],
            "selected_chain_id": row["selected_chain_id"],
            "restrictions": json.loads(row["restrictions_json"]),
            "missing_capabilities": json.loads(row["missing_json"]),
            "impaired_capabilities": json.loads(row["impaired_json"]),
            "reasons": json.loads(row["reasons_json"]),
            "reported_phase": row["reported_phase"],
            "last_receipt_at": row["last_receipt_at"],
            "locked_resources": [dict(item) for item in locks],
            "manual_actions": [dict(item) | {"evidence": json.loads(item["evidence_json"])} for item in actions],
            "receipts": [dict(item) for item in receipts],
            "created_at": row["created_at"],
            "confirmed_at": row["confirmed_at"],
        }

    def confirm_plan(self, actor_id: str, plan_id: str, expected_version: int) -> dict[str, Any]:
        """确认计划：原子锁定替代链涉及的控制资源；安全停机直接进入安全终态。"""

        self._require(actor_id, "plan.confirm")
        with transaction(self.connection, immediate=True):
            row = self._plan_row(plan_id)
            if row["state"] != "draft":
                raise InvalidState("只有草稿计划可以确认")
            if row["version"] != expected_version:
                raise InvalidState("计划版本已变化，请基于新版本重新确认")
            other = self.connection.execute(
                "SELECT plan_id FROM degradation_plans WHERE robot_id=? "
                "AND state IN ('confirmed','executing') AND plan_id<>?",
                (row["robot_id"], plan_id),
            ).fetchone()
            if other is not None:
                raise Conflict("该机器人已有活跃的降级计划")
            snapshot = self._latest_health(row["robot_id"])
            if snapshot["revision"] != row["health_revision"]:
                raise InvalidState("部件健康摘要已有新版本，该计划版本不能确认")
            now = self._now()
            if row["conclusion"] == SAFE_STOP:
                self.connection.execute(
                    "UPDATE degradation_plans SET state='safe_stopped',confirmed_at=? WHERE plan_id=?",
                    (now, plan_id),
                )
                self._audit("degradation_plan", plan_id, "plan.safe_stopped", actor_id, {"version": expected_version})
                return self.get_plan(plan_id)
            content = json.loads(row["content_json"])
            outcome = evaluate_plan(parse_plan_content(content))
            resources = outcome["locked_resources"]
            try:
                for resource_id in resources:
                    self.connection.execute(
                        "INSERT INTO plan_locked_resources(plan_id,robot_id,resource_id,locked_at) "
                        "VALUES(?,?,?,?)",
                        (plan_id, row["robot_id"], resource_id, now),
                    )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"控制资源 {resource_id} 已被其他计划锁定") from exc
            self.connection.execute(
                "UPDATE degradation_plans SET state='confirmed',confirmed_at=? WHERE plan_id=?",
                (now, plan_id),
            )
            self._audit(
                "degradation_plan", plan_id, "plan.confirmed", actor_id,
                {"version": expected_version, "locked_resources": resources},
            )
        return self.get_plan(plan_id)

    def complete_manual_action(
        self, actor_id: str, plan_id: str, action_id: str, evidence: str
    ) -> dict[str, Any]:
        """登记人工动作完成证据；全部补偿动作齐备时结论自动推进为受限运行。"""

        self._require(actor_id, "manual.write")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValidationFailed("人工动作必须附带完成证据")
        with transaction(self.connection, immediate=True):
            row = self._plan_row(plan_id)
            if row["state"] not in {"confirmed", "executing"}:
                raise InvalidState("只有执行中的计划可以登记人工动作")
            action = self.connection.execute(
                "SELECT * FROM plan_manual_actions WHERE plan_id=? AND action_id=?",
                (plan_id, action_id),
            ).fetchone()
            if action is None:
                raise NotFound("人工动作不存在")
            if action["state"] == "completed":
                raise Conflict("人工动作已经完成")
            items = json.loads(action["evidence_json"])
            items.append({"evidence": evidence.strip()[:512], "by": actor_id, "at": self._now()})
            self.connection.execute(
                "UPDATE plan_manual_actions SET state='completed',evidence_json=?,completed_by=?,completed_at=? "
                "WHERE plan_id=? AND action_id=? AND state='pending'",
                (canonical_json(items), actor_id, self._now(), plan_id, action_id),
            )
            completed = {
                item["action_id"]
                for item in self.connection.execute(
                    "SELECT action_id FROM plan_manual_actions WHERE plan_id=? AND state='completed'", (plan_id,)
                ).fetchall()
            }
            content = json.loads(row["content_json"])
            outcome = evaluate_plan(parse_plan_content(content), completed_actions=frozenset(completed))
            self.connection.execute(
                "UPDATE degradation_plans SET conclusion=?,decision_rule=?,restrictions_json=?,missing_json=?,"
                "impaired_json=?,reasons_json=? WHERE plan_id=?",
                (
                    outcome["conclusion"], outcome["decision_rule"], canonical_json(outcome["restrictions"]),
                    canonical_json(outcome["missing_capabilities"]), canonical_json(outcome["impaired_capabilities"]),
                    canonical_json(outcome["reasons"]), plan_id,
                ),
            )
            self._audit(
                "degradation_plan", plan_id, "manual.completed", actor_id,
                {"action_id": action_id, "conclusion": outcome["conclusion"]},
            )
        return self.get_plan(plan_id)

    def register_receipt(self, actor_id: str, plan_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记机器人执行回执；重复幂等、乱序忽略，安全终态不被任何迟到回执恢复。"""

        self._require(actor_id, "receipt.write")
        receipt_id = raw.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id.strip():
            raise ValidationFailed("receipt_id 不能为空")
        receipt_id = receipt_id.strip()
        reported_state = raw.get("reported_state")
        if reported_state not in {"normal", "degraded", "safe_stop"}:
            raise ValidationFailed("reported_state 必须是 normal、degraded 或 safe_stop")
        try:
            observed = parse_utc(raw["observed_at"], "observed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        observed_text = utc_text(observed)
        request_digest = digest({"receipt_id": receipt_id, "reported_state": reported_state, "observed_at": observed_text})
        row = self._plan_row(plan_id)
        existing = self.connection.execute(
            "SELECT applied,ignored_reason FROM plan_receipts WHERE plan_id=? AND receipt_id=?",
            (plan_id, receipt_id),
        ).fetchone()
        if existing is not None:
            return {
                "plan_id": plan_id, "receipt_id": receipt_id, "applied": bool(existing["applied"]),
                "ignored_reason": existing["ignored_reason"], "replayed": True,
            }
        ignored_reason: str | None = None
        applied = True
        if row["state"] == "safe_stopped":
            applied = False
            ignored_reason = "safe_terminal_state"
        elif row["state"] not in {"confirmed", "executing"}:
            applied = False
            ignored_reason = "plan_not_active"
        elif row["last_receipt_at"] is not None and observed_text < row["last_receipt_at"]:
            applied = False
            ignored_reason = "stale_out_of_order"
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO plan_receipts(plan_id,receipt_id,robot_id,reported_state,observed_at,"
                    "request_sha256,applied,ignored_reason,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        plan_id, receipt_id, row["robot_id"], reported_state, observed_text, request_digest,
                        1 if applied else 0, ignored_reason, actor_id, self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("回执编号并发冲突") from exc
            new_state = row["state"]
            if applied:
                if reported_state == SAFE_STOP:
                    new_state = "safe_stopped"
                    self._release_locks(plan_id)
                elif reported_state == "degraded" and row["state"] == "confirmed" and row["conclusion"] in {CONTINUE, RESTRICTED}:
                    new_state = "executing"
                self.connection.execute(
                    "UPDATE degradation_plans SET reported_phase=?,last_receipt_at=?,state=? WHERE plan_id=?",
                    (reported_state, observed_text, new_state, plan_id),
                )
            self._audit(
                "degradation_plan", plan_id, "receipt.registered", actor_id,
                {"receipt_id": receipt_id, "reported_state": reported_state, "applied": applied,
                 "ignored_reason": ignored_reason, "state": new_state},
            )
        return {
            "plan_id": plan_id, "receipt_id": receipt_id, "applied": applied,
            "ignored_reason": ignored_reason, "state": new_state, "replayed": False,
        }

    def _robot_recovery(self, content: Mapping[str, Any]) -> list[dict[str, Any]]:
        outcome = evaluate_plan(parse_plan_content(content))
        return outcome["recovery_evidence"]

    def robot_degradation(self, robot_id: str) -> dict[str, Any]:
        """单台机器人后台视图：缺失能力、替代链、待办人工动作与恢复证据。"""

        snapshot = self._latest_health(robot_id)
        plan_row = self.connection.execute(
            "SELECT * FROM degradation_plans WHERE robot_id=? ORDER BY version DESC LIMIT 1",
            (robot_id,),
        ).fetchone()
        result: dict[str, Any] = {
            "robot_id": robot_id,
            "health_revision": snapshot["revision"],
            "latest_plan": None,
        }
        if plan_row is None:
            return result
        plan = self.get_plan(plan_row["plan_id"])
        content = json.loads(plan_row["content_json"])
        chain = None
        if plan_row["selected_chain_id"]:
            chain = next(
                (item for item in content["execution_chains"] if item["chain_id"] == plan_row["selected_chain_id"]),
                None,
            )
        plan["recovery_requirements"] = self._robot_recovery(content)
        plan["adopted_chain"] = chain
        result["latest_plan"] = plan
        return result

    def degradation_board(self, actor_id: str) -> dict[str, Any]:
        """值班后台：每台机器人当前能力缺口、替代链、人工待办与恢复前置证据。"""

        self._require(actor_id, "degradation.read")
        rows = self.connection.execute(
            "SELECT robot_id, max(revision) AS revision FROM health_snapshots GROUP BY robot_id ORDER BY robot_id"
        ).fetchall()
        return {"robots": [self.robot_degradation(row["robot_id"]) for row in rows]}
