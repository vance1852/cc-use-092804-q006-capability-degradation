"""供应服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS supply_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_index_quotes (
    quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_index TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close_cny TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_quote_id INTEGER REFERENCES market_index_quotes(quote_id),
    recorded_by TEXT NOT NULL REFERENCES supply_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(market_index, trade_date, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_quotes_series
ON market_index_quotes(market_index, trade_date, quote_id);

CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    timezone TEXT NOT NULL,
    capacity_control_slots TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    origin_id TEXT NOT NULL REFERENCES facilities(facility_id),
    destination_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    daily_capacity TEXT NOT NULL,
    loss_basis_points INTEGER NOT NULL,
    transit_hours INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    created_at TEXT NOT NULL,
    CHECK(origin_id <> destination_id)
);

CREATE TABLE IF NOT EXISTS route_outages (
    outage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    capacity_percent TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','active','closed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outages_route_time
ON route_outages(route_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS inventory_lots (
    lot_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    grade TEXT NOT NULL,
    quantity_control_slots TEXT NOT NULL,
    available_control_slots TEXT NOT NULL,
    unit_cost_cny TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_available
ON inventory_lots(facility_id, product, received_at);

CREATE TABLE IF NOT EXISTS inventory_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    delta_control_slots TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nominations (
    nomination_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    shipper_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    requested_control_slots TEXT NOT NULL,
    allocated_control_slots TEXT NOT NULL DEFAULT '0',
    delivered_control_slots TEXT NOT NULL DEFAULT '0',
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','allocated','in_transit','delivered','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nominations_schedule
ON nominations(route_id, service_date, priority, submitted_at);

CREATE TABLE IF NOT EXISTS allocation_runs (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    available_capacity TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(route_id, service_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    nomination_id TEXT NOT NULL UNIQUE REFERENCES nominations(nomination_id),
    inventory_lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    loaded_control_slots TEXT NOT NULL,
    expected_delivered_control_slots TEXT NOT NULL,
    departed_at TEXT NOT NULL,
    arrived_at TEXT,
    state TEXT NOT NULL DEFAULT 'in_transit' CHECK(state IN ('in_transit','delivered','disputed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supply_scenarios (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenario_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES supply_scenarios(scenario_id),
    as_of_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(scenario_id, as_of_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS supply_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS supply_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_supply_audit_entity
ON supply_audit_events(entity_type, entity_id, event_id);

CREATE TABLE IF NOT EXISTS degradation_robots (
    robot_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    robot_kind TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'nominal'
        CHECK(state IN ('nominal','restricted','waiting_human','safe_stopped')),
    context_version INTEGER NOT NULL DEFAULT 1,
    active_plan_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS robot_tasks (
    task_id TEXT PRIMARY KEY,
    robot_id TEXT NOT NULL REFERENCES degradation_robots(robot_id),
    required_capabilities_json TEXT NOT NULL,
    safety_limits_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','exited','completed')),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_robot_tasks_active
ON robot_tasks(robot_id, state);

CREATE TABLE IF NOT EXISTS robot_components (
    robot_id TEXT NOT NULL REFERENCES degradation_robots(robot_id),
    component_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    provides_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (robot_id, component_id)
);

CREATE TABLE IF NOT EXISTS component_health_reports (
    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
    robot_id TEXT NOT NULL REFERENCES degradation_robots(robot_id),
    component_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ok','degraded','failed')),
    metrics_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    reported_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_health_latest
ON component_health_reports(robot_id, component_id, observed_at, report_id);

CREATE TABLE IF NOT EXISTS execution_chains (
    robot_id TEXT NOT NULL REFERENCES degradation_robots(robot_id),
    chain_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    level INTEGER NOT NULL,
    requires_components_json TEXT NOT NULL,
    requires_resources_json TEXT NOT NULL,
    restrictions_json TEXT NOT NULL,
    requires_manual_action TEXT,
    priority INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (robot_id, chain_id)
);

CREATE TABLE IF NOT EXISTS control_resources (
    resource_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity >= 1),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS degradation_plans (
    plan_id TEXT PRIMARY KEY,
    robot_id TEXT NOT NULL REFERENCES degradation_robots(robot_id),
    purpose TEXT NOT NULL CHECK(purpose IN ('degrade','recover')),
    context_version INTEGER NOT NULL,
    inputs_sha256 TEXT NOT NULL,
    frozen_json TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('continue','restricted','wait_human','safe_stop')),
    target_mode TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'proposed'
        CHECK(state IN ('proposed','confirmed','executing','completed','invalidated','superseded')),
    revision INTEGER NOT NULL DEFAULT 1,
    last_receipt_seq INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_plans_robot_state
ON degradation_plans(robot_id, state);

CREATE TABLE IF NOT EXISTS resource_locks (
    lock_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES degradation_plans(plan_id),
    robot_id TEXT NOT NULL REFERENCES degradation_robots(robot_id),
    resource_id TEXT NOT NULL REFERENCES control_resources(resource_id),
    units INTEGER NOT NULL CHECK(units >= 1),
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','released')),
    created_at TEXT NOT NULL,
    released_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_locks_resource_state
ON resource_locks(resource_id, state);

CREATE INDEX IF NOT EXISTS idx_locks_robot_state
ON resource_locks(robot_id, state);

CREATE TABLE IF NOT EXISTS manual_actions (
    action_id TEXT PRIMARY KEY,
    robot_id TEXT NOT NULL REFERENCES degradation_robots(robot_id),
    plan_id TEXT NOT NULL REFERENCES degradation_plans(plan_id),
    description TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','completed','cancelled')),
    created_at TEXT NOT NULL,
    completed_by TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_manual_actions_robot
ON manual_actions(robot_id, state);

CREATE TABLE IF NOT EXISTS recovery_requirements (
    requirement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    robot_id TEXT NOT NULL REFERENCES degradation_robots(robot_id),
    kind TEXT NOT NULL CHECK(kind IN ('component_health','manual_action')),
    component_id TEXT,
    action_id TEXT,
    detail TEXT NOT NULL,
    blocked_observed_at TEXT,
    state TEXT NOT NULL DEFAULT 'outstanding' CHECK(state IN ('outstanding','satisfied','cancelled')),
    raised_at TEXT NOT NULL,
    satisfied_at TEXT,
    satisfied_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_recovery_robot_state
ON recovery_requirements(robot_id, state);

CREATE TABLE IF NOT EXISTS execution_receipts (
    receipt_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES degradation_plans(plan_id),
    robot_id TEXT NOT NULL REFERENCES degradation_robots(robot_id),
    seq INTEGER NOT NULL,
    reported_mode TEXT NOT NULL
        CHECK(reported_mode IN ('nominal','restricted','waiting_human','safe_stopped')),
    outcome TEXT NOT NULL
        CHECK(outcome IN ('applied','out_of_order','rejected_stale','rejected_terminal')),
    note TEXT NOT NULL DEFAULT '',
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_receipts_plan
ON execution_receipts(plan_id, seq);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
