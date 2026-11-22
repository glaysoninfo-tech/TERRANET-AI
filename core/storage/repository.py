"""Repositório central do TERRANET CORE (PostgreSQL/PostGIS e SQLite)."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from common.db import Database, dumps, loads
from common.device_store import DeviceStore
from common.timeutil import now_iso, parse_ts, utc_now
from contracts.canonical import CanonicalEvent, EventType
from contracts.commands import Command, CommandState, can_transition
from contracts.rules import Rule

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class CoreStore:
    def __init__(self, db: Database):
        self.db = db
        self.devices = DeviceStore(db)

    @classmethod
    def open(cls, url: str) -> CoreStore:
        db = Database(url)
        db.apply_schema(MIGRATIONS_DIR)
        return cls(db)

    # --------------------------------------------------------- tenants/sites
    def create_tenant(self, tenant_id: str, name: str) -> dict[str, Any]:
        now = now_iso()
        self.db.execute(
            "INSERT INTO tenants (tenant_id, name, status, created_at) VALUES (?, ?, 'ACTIVE', ?) "
            "ON CONFLICT (tenant_id) DO NOTHING",
            (tenant_id, name, now),
        )
        return self.db.query_one("SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)) or {}

    def create_site(
        self, site_id: str, tenant_id: str, name: str, location: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        now = now_iso()
        self.db.execute(
            "INSERT INTO sites (site_id, tenant_id, name, location, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (site_id) DO NOTHING",
            (site_id, tenant_id, name, dumps(location or {}), now),
        )
        return self.db.query_one("SELECT * FROM sites WHERE site_id = ?", (site_id,)) or {}

    def get_site(self, site_id: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM sites WHERE site_id = ?", (site_id,))

    # ----------------------------------------------------------- edges/users
    def register_edge(
        self,
        edge_id: str,
        tenant_id: str,
        site_ids: list[str],
        token: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        raw_token = token or f"tn_edge_{secrets.token_urlsafe(24)}"
        now = now_iso()
        self.db.execute(
            "INSERT INTO edges (edge_id, tenant_id, site_ids, token_hash, status, "
            "config_version, applied_config_version, created_at) "
            "VALUES (?, ?, ?, ?, 'ACTIVE', 1, 0, ?)",
            (edge_id, tenant_id, dumps(site_ids), hash_token(raw_token), now),
        )
        return self.get_edge(edge_id) or {}, raw_token

    def get_edge(self, edge_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM edges WHERE edge_id = ?", (edge_id,))
        if not row:
            return None
        row["site_ids"] = loads(row["site_ids"]) or []
        row["last_metrics"] = loads(row["last_metrics"]) or {}
        return row

    def find_edge_by_token(self, token: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM edges WHERE token_hash = ? AND status = 'ACTIVE'",
            (hash_token(token),),
        )
        if not row:
            return None
        row["site_ids"] = loads(row["site_ids"]) or []
        row["last_metrics"] = loads(row["last_metrics"]) or {}
        return row

    def find_edge_for_site(self, tenant_id: str, site_id: str) -> dict[str, Any] | None:
        rows = self.db.query(
            "SELECT * FROM edges WHERE tenant_id = ? AND status = 'ACTIVE'", (tenant_id,)
        )
        for row in rows:
            sites = loads(row["site_ids"]) or []
            if site_id in sites:
                row["site_ids"] = sites
                row["last_metrics"] = loads(row["last_metrics"]) or {}
                return row
        return None

    def record_edge_heartbeat(
        self, edge_id: str, metrics: dict[str, Any], at: str | None = None
    ) -> None:
        ts = at or now_iso()
        self.db.execute(
            "UPDATE edges SET last_heartbeat_at = ?, last_metrics = ? WHERE edge_id = ?",
            (ts, dumps(metrics), edge_id),
        )

    def is_edge_online(
        self, edge_id: str, max_age_s: float = 120.0, now: datetime | None = None
    ) -> bool:
        edge = self.get_edge(edge_id)
        if not edge or edge["status"] != "ACTIVE" or not edge.get("last_heartbeat_at"):
            return False
        now_dt = now or utc_now()
        return (now_dt - parse_ts(edge["last_heartbeat_at"])) <= timedelta(seconds=max_age_s)

    def revoke_edge(self, edge_id: str) -> None:
        self.db.execute("UPDATE edges SET status = 'REVOKED' WHERE edge_id = ?", (edge_id,))

    def create_user(
        self,
        username: str,
        tenant_id: str,
        role: str = "operator",
        api_key: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        if role not in ("admin", "operator", "viewer"):
            raise ValueError(f"Função inválida: {role}")
        raw_key = api_key or f"tn_usr_{secrets.token_urlsafe(24)}"
        user_id = f"USR-{uuid.uuid4().hex[:10]}"
        self.db.execute(
            "INSERT INTO users (user_id, tenant_id, username, role, api_key_hash, status, "
            "created_at) VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?)",
            (user_id, tenant_id, username, role, hash_token(raw_key), now_iso()),
        )
        row = self.db.query_one("SELECT * FROM users WHERE user_id = ?", (user_id,)) or {}
        return row, raw_key

    def find_user_by_key(self, api_key: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM users WHERE api_key_hash = ? AND status = 'ACTIVE'",
            (hash_token(api_key),),
        )

    def revoke_user(self, user_id: str) -> None:
        self.db.execute("UPDATE users SET status = 'REVOKED' WHERE user_id = ?", (user_id,))

    # -------------------------------------------------------------- telemetry
    def persist_event(self, event: CanonicalEvent, edge_id: str | None = None) -> bool:
        """Insere evento de forma idempotente (ON CONFLICT DO NOTHING).

        Retorna True se o evento é novo; False se já havia sido persistido antes.
        """
        now = now_iso()
        payload = event.to_dict()
        with self.db.transaction():
            cursor = self.db.execute(
                "INSERT INTO telemetry_events (event_id, tenant_id, site_id, device_id, "
                "event_type, observed_at, received_at, processed_at, ingested_at, edge_id, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (event_id) DO NOTHING",
                (
                    event.event_id,
                    event.tenant_id,
                    event.site_id,
                    event.device_id,
                    str(event.event_type),
                    event.observed_at,
                    event.received_at,
                    event.processed_at,
                    now,
                    edge_id,
                    dumps(payload),
                ),
            )
            if cursor.rowcount != 1:
                return False

            self.devices.touch(event.device_id, event.observed_at)
            if event.event_type in (EventType.TELEMETRY, EventType.POSITION, EventType.DIAGNOSTIC):
                self._merge_device_state(event, now)
            if event.location is not None:
                loc = event.location
                self.db.execute(
                    "INSERT INTO telemetry_positions (event_id, tenant_id, site_id, device_id, "
                    "observed_at, lat, lon, alt_m, speed_kmh, heading_deg) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (event_id) DO NOTHING",
                    (
                        event.event_id,
                        event.tenant_id,
                        event.site_id,
                        event.device_id,
                        event.observed_at,
                        float(loc["lat"]),
                        float(loc["lon"]),
                        loc.get("alt_m"),
                        loc.get("speed_kmh"),
                        loc.get("heading_deg"),
                    ),
                )
            if event.event_type == EventType.COMMAND_RESULT and event.attributes:
                cmd_id = event.attributes.get("command_id")
                state = event.attributes.get("state")
                reason = str(event.attributes.get("reason", ""))
                if cmd_id and state:
                    self._update_command_from_result(str(cmd_id), str(state), reason, now)
        return True

    def _merge_device_state(self, event: CanonicalEvent, now: str) -> None:
        row = self.db.query_one(
            "SELECT observed_at, measurements, quality, location FROM device_state "
            "WHERE device_id = ?",
            (event.device_id,),
        )
        measurements, quality, location = {}, {}, event.location
        if row:
            if parse_ts(row["observed_at"]) > parse_ts(event.observed_at):
                return
            measurements = loads(row["measurements"]) or {}
            quality = loads(row["quality"]) or {}
            if location is None:
                location = loads(row["location"])
        measurements.update(event.measurements)
        quality.update({k: v for k, v in event.quality.items() if k != "timestamp_source"})
        self.db.execute(
            "INSERT INTO device_state (device_id, tenant_id, site_id, observed_at, measurements, "
            "quality, location, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (device_id) DO UPDATE SET observed_at = excluded.observed_at, "
            "measurements = excluded.measurements, quality = excluded.quality, "
            "location = excluded.location, updated_at = excluded.updated_at",
            (
                event.device_id,
                event.tenant_id,
                event.site_id,
                event.observed_at,
                dumps(measurements),
                dumps(quality),
                dumps(location) if location is not None else None,
                now,
            ),
        )

    def _update_command_from_result(
        self, command_id: str, new_state: str, reason: str, now: str
    ) -> None:
        row = self.db.query_one(
            "SELECT state FROM commands WHERE command_id = ?", (command_id,)
        )
        if not row:
            return
        if can_transition(row["state"], new_state):
            self.db.execute(
                "UPDATE commands SET state = ?, reason = ?, updated_at = ? WHERE command_id = ?",
                (new_state, reason[:500], now, command_id),
            )

    def query_events(
        self,
        tenant_id: str,
        device_id: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT payload FROM telemetry_events WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]
        if device_id:
            sql += " AND device_id = ?"
            params.append(device_id)
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        if since:
            sql += " AND observed_at >= ?"
            params.append(since)
        sql += " ORDER BY observed_at DESC LIMIT ?"
        params.append(int(limit))
        return [loads(r["payload"]) for r in self.db.query(sql, params)]

    def count_events(self, tenant_id: str | None = None) -> int:
        if tenant_id:
            return int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM telemetry_events WHERE tenant_id = ?", (tenant_id,)
                )
                or 0
            )
        return int(self.db.scalar("SELECT COUNT(*) FROM telemetry_events") or 0)

    def get_device_state(self, tenant_id: str, device_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM device_state WHERE tenant_id = ? AND device_id = ?",
            (tenant_id, device_id),
        )
        if not row:
            return None
        row["measurements"] = loads(row["measurements"]) or {}
        row["quality"] = loads(row["quality"]) or {}
        row["location"] = loads(row["location"])
        return row

    # --------------------------------------------------------------- commands
    def save_command(self, cmd: Command, edge_id: str, reason: str = "") -> None:
        now = now_iso()
        self.db.execute(
            "INSERT INTO commands (command_id, tenant_id, site_id, edge_id, device_id, "
            "operation, requested_by, issued_at, expires_at, state, reason, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cmd.command_id,
                cmd.tenant_id,
                cmd.site_id,
                edge_id,
                cmd.device_id,
                cmd.operation,
                cmd.requested_by,
                cmd.issued_at,
                cmd.expires_at,
                cmd.state,
                reason,
                dumps(cmd.to_dict()),
                now,
            ),
        )

    def get_command(self, tenant_id: str, command_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM commands WHERE tenant_id = ? AND command_id = ?",
            (tenant_id, command_id),
        )
        if not row:
            return None
        row["payload"] = loads(row["payload"])
        return row

    def list_commands(
        self, tenant_id: str, device_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM commands WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]
        if device_id:
            sql += " AND device_id = ?"
            params.append(device_id)
        sql += " ORDER BY issued_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self.db.query(sql, params)
        for row in rows:
            row["payload"] = loads(row["payload"])
        return rows

    def pop_pending_commands_for_edge(
        self, edge_id: str, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Retorna comandos AUTHORIZED ainda válidos para o EDGE e marca DISPATCHED.
        Comandos vencidos são marcados como EXPIRED e não são enviados."""
        now_dt = now or utc_now()
        now_s = now_iso()
        dispatched: list[dict[str, Any]] = []
        with self.db.transaction():
            rows = self.db.query(
                "SELECT * FROM commands WHERE edge_id = ? AND state = ? ORDER BY issued_at",
                (edge_id, CommandState.AUTHORIZED.value),
            )
            for row in rows:
                if now_dt >= parse_ts(row["expires_at"]):
                    self.db.execute(
                        "UPDATE commands SET state = ?, reason = 'expired_before_dispatch', "
                        "updated_at = ? WHERE command_id = ?",
                        (CommandState.EXPIRED.value, now_s, row["command_id"]),
                    )
                    continue
                self.db.execute(
                    "UPDATE commands SET state = ?, updated_at = ? WHERE command_id = ?",
                    (CommandState.DISPATCHED.value, now_s, row["command_id"]),
                )
                payload = loads(row["payload"])
                payload["state"] = CommandState.DISPATCHED.value
                dispatched.append(payload)
        return dispatched

    # --------------------------------------------------------- rules / config
    def save_rule(self, tenant_id: str, site_id: str, rule: Rule) -> dict[str, Any]:
        rule.validate()
        now = now_iso()
        enabled_val: Any = int(rule.enabled) if self.db.dialect == "sqlite" else bool(rule.enabled)
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO automation_rules (rule_id, tenant_id, site_id, version, definition, "
                "enabled, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (rule_id) DO UPDATE SET version = excluded.version, "
                "definition = excluded.definition, enabled = excluded.enabled, "
                "updated_at = excluded.updated_at",
                (rule.rule_id, tenant_id, site_id, rule.version, dumps(rule.to_dict()),
                 enabled_val, now),
            )
            self.db.execute(
                "UPDATE edges SET config_version = config_version + 1 WHERE tenant_id = ?",
                (tenant_id,),
            )
        return rule.to_dict()

    def list_rules(
        self, tenant_id: str, site_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT definition FROM automation_rules WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]
        if site_ids is not None:
            if not site_ids:
                return []
            sql += f" AND site_id IN ({','.join('?' for _ in site_ids)})"  # nosec B608
            params.extend(site_ids)
        return [loads(r["definition"]) for r in self.db.query(sql + " ORDER BY rule_id", params)]

    def save_config_report(self, edge_id: str, report: dict[str, Any]) -> None:
        now = now_iso()
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO config_reports (id, edge_id, intended_version, applied_version, "
                "status, payload, reported_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    edge_id,
                    int(report.get("intended_version", 0)),
                    int(report.get("applied_version", 0)),
                    str(report.get("status", "unknown")),
                    dumps(report),
                    report.get("reported_at") or now,
                ),
            )
            self.db.execute(
                "UPDATE edges SET applied_config_version = ? WHERE edge_id = ?",
                (int(report.get("applied_version", 0)), edge_id),
            )

    # ------------------------------------------------------------------ audit
    def audit(
        self,
        tenant_id: str | None,
        actor: str,
        action: str,
        target: str,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO audit_log (id, at, tenant_id, actor, action, target, outcome, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                now_iso(),
                tenant_id,
                actor,
                action,
                target,
                outcome,
                dumps(details or {}),
            ),
        )

    def list_audit(
        self, tenant_id: str, action: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit_log WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]
        if action:
            sql += " AND action = ?"
            params.append(action)
        sql += " ORDER BY at DESC LIMIT ?"
        params.append(int(limit))
        rows = self.db.query(sql, params)
        for row in rows:
            row["details"] = loads(row["details"]) or {}
        return rows

    # ------------------------------------------------- evidências de testes
    def record_test_execution(
        self,
        tenant_id: str,
        test_code: str,
        category: str,
        status: str,
        expected_result: str,
        observed_result: str,
        *,
        git_commit: str = "",
        environment: str = "",
        equipment: str = "",
        firmware_version: str = "",
        started_at: str | None = None,
        finished_at: str | None = None,
        evidence: dict[str, Any] | None = None,
        approved_by: str = "",
    ) -> dict[str, Any]:
        if category not in ("unit", "integration", "hil", "field"):
            raise ValueError(f"Categoria de teste inválida: {category}")
        if status not in ("PASS", "FAIL", "NOT_TESTED"):
            raise ValueError(f"Status de teste inválido: {status}")
        now = now_iso()
        exec_id = f"TEX-{uuid.uuid4().hex[:12]}"
        self.db.execute(
            "INSERT INTO test_executions (execution_id, tenant_id, test_code, category, "
            "git_commit, environment, equipment, firmware_version, started_at, finished_at, "
            "expected_result, observed_result, status, evidence, approved_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                exec_id,
                tenant_id,
                test_code,
                category,
                git_commit,
                environment,
                equipment,
                firmware_version,
                started_at or now,
                finished_at or now,
                expected_result,
                observed_result,
                status,
                dumps(evidence or {}),
                approved_by,
            ),
        )
        row = self.db.query_one(
            "SELECT * FROM test_executions WHERE execution_id = ?", (exec_id,)
        ) or {}
        row["evidence"] = loads(row.get("evidence")) or {}
        return row

    def list_test_executions(
        self, tenant_id: str, test_code: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM test_executions WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]
        if test_code:
            sql += " AND test_code = ?"
            params.append(test_code)
        sql += " ORDER BY finished_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self.db.query(sql, params)
        for row in rows:
            row["evidence"] = loads(row.get("evidence")) or {}
        return rows
