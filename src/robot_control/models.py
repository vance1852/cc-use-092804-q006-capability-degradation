"""机器人控制平台调度领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
PRICE_INDEXES = {"PEAK_VALLEY", "ON_DEMAND", "RESERVED", "SPOT", "INTERNAL", "CUSTOM"}
PRODUCTS = {"gpu-h100", "gpu-a100", "gpu-l40s", "accelerator-npu", "cpu-highmem", "storage-io"}
ROUTE_KINDS = {"interconnect", "inference-pool", "tenant", "storage", "edge-site"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


@dataclass(frozen=True, slots=True)
class IndexQuote:
    market_index: str
    trade_date: str
    close_cny: Decimal
    source_revision: str
    observed_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "IndexQuote":
        market_index = required_text(raw.get("market_index"), "market_index", 16).upper()
        if market_index not in PRICE_INDEXES - {"CUSTOM"}:
            raise ValidationFailed("market_index 必须是 PEAK_VALLEY、ON_DEMAND、RESERVED、SPOT 或 INTERNAL")
        observed_at = required_text(raw.get("observed_at"), "observed_at", 40)
        try:
            parse_utc(observed_at, "observed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            market_index=market_index,
            trade_date=date_text(raw.get("trade_date"), "trade_date"),
            close_cny=decimal_value(raw.get("close_cny"), "close_cny", minimum=Decimal("0.01")),
            source_revision=identifier(raw.get("source_revision"), "source_revision"),
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class Facility:
    facility_id: str
    name: str
    kind: str
    timezone: str
    capacity_control_slots: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Facility":
        kind = required_text(raw.get("kind"), "kind", 24)
        if kind not in ROUTE_KINDS:
            raise ValidationFailed("kind 不是受支持的设施类型")
        timezone = required_text(raw.get("timezone"), "timezone", 64)
        if "/" not in timezone and timezone != "UTC":
            raise ValidationFailed("timezone 必须是 IANA 时区或 UTC")
        return cls(
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            name=required_text(raw.get("name"), "name"),
            kind=kind,
            timezone=timezone,
            capacity_control_slots=decimal_value(
                raw.get("capacity_control_slots"), "capacity_control_slots", minimum=Decimal("0")
            ),
        )


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    origin_id: str
    destination_id: str
    product: str
    daily_capacity: Decimal
    loss_basis_points: int
    transit_hours: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Route":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的资源类型")
        loss = raw.get("loss_basis_points", 0)
        if isinstance(loss, bool) or not isinstance(loss, int) or not 0 <= loss <= 1000:
            raise ValidationFailed("loss_basis_points 必须是 0 到 1000 的整数")
        origin = identifier(raw.get("origin_id"), "origin_id")
        destination = identifier(raw.get("destination_id"), "destination_id")
        if origin == destination:
            raise ValidationFailed("实时控制总线起点和终点不能相同")
        return cls(
            route_id=identifier(raw.get("route_id"), "route_id"),
            origin_id=origin,
            destination_id=destination,
            product=product,
            daily_capacity=decimal_value(
                raw.get("daily_capacity"), "daily_capacity", minimum=Decimal("0.001")
            ),
            loss_basis_points=loss,
            transit_hours=positive_integer(raw.get("transit_hours"), "transit_hours"),
        )


@dataclass(frozen=True, slots=True)
class InventoryLot:
    lot_id: str
    facility_id: str
    product: str
    grade: str
    quantity_control_slots: Decimal
    unit_cost_cny: Decimal
    received_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InventoryLot":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的资源类型")
        received_at = required_text(raw.get("received_at"), "received_at", 40)
        try:
            parse_utc(received_at, "received_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            lot_id=identifier(raw.get("lot_id"), "lot_id"),
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            product=product,
            grade=required_text(raw.get("grade"), "grade", 32).upper(),
            quantity_control_slots=decimal_value(
                raw.get("quantity_control_slots"), "quantity_control_slots", minimum=Decimal("0.001")
            ),
            unit_cost_cny=decimal_value(
                raw.get("unit_cost_cny"), "unit_cost_cny", minimum=Decimal("0")
            ),
            received_at=received_at,
        )


@dataclass(frozen=True, slots=True)
class NominationRequest:
    nomination_id: str
    route_id: str
    shipper_id: str
    service_date: str
    requested_control_slots: Decimal
    priority: int
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NominationRequest":
        priority = raw.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
            raise ValidationFailed("priority 必须是 1 到 999 的整数")
        return cls(
            nomination_id=identifier(raw.get("nomination_id"), "nomination_id"),
            route_id=identifier(raw.get("route_id"), "route_id"),
            shipper_id=identifier(raw.get("shipper_id"), "shipper_id"),
            service_date=date_text(raw.get("service_date"), "service_date"),
            requested_control_slots=decimal_value(
                raw.get("requested_control_slots"), "requested_control_slots", minimum=Decimal("0.001")
            ),
            priority=priority,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


CAPABILITY = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
METRIC_NAME = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
ROBOT_KINDS = {"transport", "precision-assembly", "inspection", "humanoid", "custom"}
HEALTH_STATUSES = {"ok", "degraded", "failed"}
REPORTED_MODES = {"nominal", "restricted", "waiting_human", "safe_stopped"}
RESTRICTION_FIELDS = {"max_speed_percent", "min_obstacle_distance_m"}


def capability_name(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not CAPABILITY.fullmatch(result):
        raise ValidationFailed(f"{field} 必须是能力标识（小写字母开头的点分名称）")
    return result


def capability_level(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise ValidationFailed(f"{field} 必须是 1 到 5 的整数")
    return value


def required_capabilities(value: object, field: str = "required_capabilities") -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValidationFailed(f"{field} 必须包含 1 到 8 项能力要求")
    seen: set[str] = set()
    parsed: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValidationFailed(f"{field}[{index}] 必须是对象")
        name = capability_name(item.get("capability"), f"{field}[{index}].capability")
        if name in seen:
            raise ValidationFailed(f"{field} 中能力 {name} 重复")
        seen.add(name)
        parsed.append({
            "capability": name,
            "min_level": capability_level(item.get("min_level"), f"{field}[{index}].min_level"),
        })
    return parsed


def metric_limits(value: object, field: str = "safety_limits") -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValidationFailed(f"{field} 必须是对象")
    unknown = set(value) - {"metric_limits"}
    if unknown:
        raise ValidationFailed(f"{field} 包含未知字段")
    limits = value.get("metric_limits", {})
    if not isinstance(limits, Mapping) or len(limits) > 8:
        raise ValidationFailed(f"{field}.metric_limits 必须是不超过 8 项的对象")
    parsed: dict[str, str] = {}
    for metric, limit in limits.items():
        if not isinstance(metric, str) or not METRIC_NAME.fullmatch(metric):
            raise ValidationFailed(f"{field}.metric_limits 的指标名 {metric} 格式不正确")
        parsed[metric] = str(decimal_value(limit, f"{field}.metric_limits.{metric}", minimum=Decimal("0")))
    return parsed


def restrictions_value(value: object, field: str = "restrictions") -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValidationFailed(f"{field} 必须是对象")
    unknown = set(value) - RESTRICTION_FIELDS
    if unknown:
        raise ValidationFailed(f"{field} 只支持 max_speed_percent 与 min_obstacle_distance_m")
    parsed: dict[str, Any] = {}
    if "max_speed_percent" in value:
        speed = value["max_speed_percent"]
        if isinstance(speed, bool) or not isinstance(speed, int) or not 1 <= speed <= 100:
            raise ValidationFailed(f"{field}.max_speed_percent 必须是 1 到 100 的整数")
        parsed["max_speed_percent"] = speed
    if "min_obstacle_distance_m" in value:
        parsed["min_obstacle_distance_m"] = str(decimal_value(
            value["min_obstacle_distance_m"], f"{field}.min_obstacle_distance_m",
            minimum=Decimal("0"), maximum=Decimal("100"),
        ))
    return parsed


@dataclass(frozen=True, slots=True)
class RobotRegistration:
    robot_id: str
    model: str
    robot_kind: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RobotRegistration":
        robot_kind = required_text(raw.get("robot_kind"), "robot_kind", 32)
        if robot_kind not in ROBOT_KINDS:
            raise ValidationFailed("robot_kind 不是受支持的机器人类型")
        return cls(
            robot_id=identifier(raw.get("robot_id"), "robot_id"),
            model=required_text(raw.get("model"), "model", 128),
            robot_kind=robot_kind,
        )


@dataclass(frozen=True, slots=True)
class ComponentRegistration:
    component_id: str
    kind: str
    provides: list[dict[str, Any]]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ComponentRegistration":
        provides = raw.get("provides")
        if not isinstance(provides, list) or not 1 <= len(provides) <= 8:
            raise ValidationFailed("provides 必须包含 1 到 8 项能力供给")
        parsed: list[dict[str, Any]] = []
        for index, item in enumerate(provides):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"provides[{index}] 必须是对象")
            parsed.append({
                "capability": capability_name(item.get("capability"), f"provides[{index}].capability"),
                "level": capability_level(item.get("level"), f"provides[{index}].level"),
            })
        return cls(
            component_id=identifier(raw.get("component_id"), "component_id"),
            kind=required_text(raw.get("kind"), "kind", 64),
            provides=parsed,
        )


@dataclass(frozen=True, slots=True)
class ChainRegistration:
    chain_id: str
    capability: str
    level: int
    requires_components: list[str]
    requires_resources: list[dict[str, Any]]
    restrictions: dict[str, Any]
    requires_manual_action: str | None
    priority: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ChainRegistration":
        components = raw.get("requires_components", [])
        if not isinstance(components, list) or len(components) > 8:
            raise ValidationFailed("requires_components 必须是不超过 8 项的列表")
        parsed_components = [
            identifier(item, f"requires_components[{index}]") for index, item in enumerate(components)
        ]
        resources = raw.get("requires_resources", [])
        if not isinstance(resources, list) or len(resources) > 8:
            raise ValidationFailed("requires_resources 必须是不超过 8 项的列表")
        parsed_resources: list[dict[str, Any]] = []
        for index, item in enumerate(resources):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"requires_resources[{index}] 必须是对象")
            units = item.get("units", 1)
            if isinstance(units, bool) or not isinstance(units, int) or not 1 <= units <= 9:
                raise ValidationFailed(f"requires_resources[{index}].units 必须是 1 到 9 的整数")
            parsed_resources.append({
                "resource_id": identifier(item.get("resource_id"), f"requires_resources[{index}].resource_id"),
                "units": units,
            })
        manual = raw.get("requires_manual_action")
        if manual is not None:
            manual = required_text(manual, "requires_manual_action", 256)
        priority = raw.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 999:
            raise ValidationFailed("priority 必须是 0 到 999 的整数")
        return cls(
            chain_id=identifier(raw.get("chain_id"), "chain_id"),
            capability=capability_name(raw.get("capability"), "capability"),
            level=capability_level(raw.get("level"), "level"),
            requires_components=parsed_components,
            requires_resources=parsed_resources,
            restrictions=restrictions_value(raw.get("restrictions")),
            requires_manual_action=manual,
            priority=priority,
        )


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    task_id: str
    required_capabilities: list[dict[str, Any]]
    safety_limits: dict[str, str]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TaskDefinition":
        return cls(
            task_id=identifier(raw.get("task_id"), "task_id"),
            required_capabilities=required_capabilities(raw.get("required_capabilities")),
            safety_limits=metric_limits(raw.get("safety_limits")),
        )


@dataclass(frozen=True, slots=True)
class HealthReport:
    component_id: str
    status: str
    metrics: dict[str, str]
    observed_at: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HealthReport":
        status = required_text(raw.get("status"), "status", 16)
        if status not in HEALTH_STATUSES:
            raise ValidationFailed("status 必须是 ok、degraded 或 failed")
        metrics = raw.get("metrics", {})
        if not isinstance(metrics, Mapping) or len(metrics) > 16:
            raise ValidationFailed("metrics 必须是不超过 16 项的对象")
        parsed_metrics: dict[str, str] = {}
        for metric, value in metrics.items():
            if not isinstance(metric, str) or not METRIC_NAME.fullmatch(metric):
                raise ValidationFailed(f"指标名 {metric} 格式不正确")
            parsed_metrics[metric] = str(decimal_value(value, f"metrics.{metric}"))
        observed_at = required_text(raw.get("observed_at"), "observed_at", 40)
        try:
            parse_utc(observed_at, "observed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            component_id=identifier(raw.get("component_id"), "component_id"),
            status=status,
            metrics=parsed_metrics,
            observed_at=observed_at,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class ResourceRegistration:
    resource_id: str
    kind: str
    capacity: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResourceRegistration":
        capacity = raw.get("capacity", 1)
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= 64:
            raise ValidationFailed("capacity 必须是 1 到 64 的整数")
        return cls(
            resource_id=identifier(raw.get("resource_id"), "resource_id"),
            kind=required_text(raw.get("kind"), "kind", 64),
            capacity=capacity,
        )


@dataclass(frozen=True, slots=True)
class ReceiptRequest:
    receipt_id: str
    seq: int
    reported_mode: str
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReceiptRequest":
        seq = raw.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or not 1 <= seq <= 1000000:
            raise ValidationFailed("seq 必须是 1 到 1000000 的整数")
        reported_mode = required_text(raw.get("reported_mode"), "reported_mode", 32)
        if reported_mode not in REPORTED_MODES:
            raise ValidationFailed("reported_mode 必须是 nominal、restricted、waiting_human 或 safe_stopped")
        note = raw.get("note", "")
        if not isinstance(note, str) or len(note) > 256:
            raise ValidationFailed("note 不能超过 256 个字符")
        return cls(
            receipt_id=identifier(raw.get("receipt_id"), "receipt_id"),
            seq=seq,
            reported_mode=reported_mode,
            note=note.strip(),
        )


@dataclass(frozen=True, slots=True)
class SupplyScenario:
    scenario_id: str
    name: str
    market_index_drop_percent: Decimal
    route_capacity_changes: Mapping[str, Decimal]
    demand_changes: Mapping[str, Decimal]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SupplyScenario":
        route_changes = raw.get("route_capacity_changes", {})
        demand_changes = raw.get("demand_changes", {})
        if not isinstance(route_changes, Mapping) or not isinstance(demand_changes, Mapping):
            raise ValidationFailed("情景变化必须是对象")
        parsed_routes = {
            identifier(key, "route_capacity_changes 键"): decimal_value(
                value, f"route_capacity_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in route_changes.items()
        }
        parsed_demand = {
            identifier(key, "demand_changes 键"): decimal_value(
                value, f"demand_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in demand_changes.items()
        }
        return cls(
            scenario_id=identifier(raw.get("scenario_id"), "scenario_id"),
            name=required_text(raw.get("name"), "name"),
            market_index_drop_percent=decimal_value(
                raw.get("market_index_drop_percent", 0),
                "market_index_drop_percent",
                minimum=Decimal("-500"),
                maximum=Decimal("100"),
            ),
            route_capacity_changes=parsed_routes,
            demand_changes=parsed_demand,
        )
