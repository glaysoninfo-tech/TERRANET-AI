"""Armazenamento local persistente do EDGE com Transactional Outbox (PIT-001 §12, E08).

Garantias:
* evento e pendência de sincronização gravados na MESMA transação;
* ``event_id`` duplicado não cria nova ocorrência nem nova pendência (PIT-T04);
* status do outbox só muda para ``SYNCED`` após confirmação de persistência pelo CORE;
* eventos pendentes nunca são removidos pela política de retenção.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from common.db import Database, dumps, loads
from common.timeutil import iso, now_iso, parse_ts, utc_now
from contracts.canonical import CanonicalEvent, Quality

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    RETRY = "RETRY"
    SYNCED = "SYNCED"
    FAILED = "FAILED"  # rejeição permanente pelo CORE -> dead letter para análise


class EdgeStore:
    def __init__(
        self,
        db: Database,
        backoff_base_s: float = 5.0,
        backoff_max_s: float = 600.0,
    ):
        self.db = db
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s

    @classmethod
    def open(cls, url: str, **kwargs: Any) -> EdgeStore:
        db = Database(url)
        db.apply_schema(MIGRATIONS_DIR)
        return cls(db, **kwargs)

    # ----------------------------------------------------------------- ingest
    def ingest(self, event: CanonicalEvent) -> bool:
        """Persiste o evento + outbox atomicamente. Retorna False para duplicados."""
        payload = event.to_dict()
        now = now_iso()
        with self.db.transaction():
            cursor = self.db.execute(
                "INSERT INTO telemetry_events (event_id, tenant_id, site_id, device_id, "
                "event_type, observed_at, received_at, processed_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (event_id) DO NOTHING",
                (
                    event.event_id,
                    event.tenant_id,
                    event.site_id,
                    event.device_id,
                    str(event.event_type),
                    event.observed_at,
                    event.received_at,
                    event.processed_at,
                    dumps(payload),
                ),
            )
            if cursor.rowcount != 1:
                return False
            self.db.execute(
                "INSERT INTO sync_outbox (event_id, status, created_at, next_attempt_at) "
                "VALUES (?, ?, ?, ?)",
                (event.event_id, OutboxStatus.PENDING.value, now, now),
            )
            if event.event_type in ("telemetry", "position", "diagnostic") and event.measurements:
                self._merge_state(event, now)
        return True

    def _merge_state(self, event: CanonicalEvent, now: str) -> None:
        row = self.db.query_one(
            "SELECT observed_at, measurements, quality FROM device_state WHERE device_id = ?",
            (event.device_id,),
        )
        measurements, quality = {}, {}
        if row:
            if parse_ts(row["observed_at"]) > parse_ts(event.observed_at):
                return  # evento fora de ordem não sobrescreve estado mais recente
            measurements, quality = loads(row["measurements"]), loads(row["quality"])
        measurements.update(event.measurements)
        quality.update({k: v for k, v in event.quality.items() if k != "timestamp_source"})
        self.db.execute(
            "INSERT INTO device_state (device_id, observed_at, measurements, quality, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (device_id) DO UPDATE SET "
            "observed_at = excluded.observed_at, measurements = excluded.measurements, "
            "quality = excluded.quality, updated_at = excluded.updated_at",
            (event.device_id, event.observed_at, dumps(measurements), dumps(quality), now),
        )

    # ----------------------------------------------------------------- outbox
    def pending(self, limit: int = 100, now: datetime | None = None) -> list[dict[str, Any]]:
        now_s = iso(now or utc_now())
        rows = self.db.query(
            "SELECT e.payload FROM telemetry_events e JOIN sync_outbox o "
            "ON e.event_id = o.event_id WHERE o.status IN (?, ?) AND o.next_attempt_at <= ? "
            "ORDER BY e.observed_at, e.event_id LIMIT ?",
            (OutboxStatus.PENDING.value, OutboxStatus.RETRY.value, now_s, int(limit)),
        )
        return [loads(r["payload"]) for r in rows]

    def mark_synced(self, event_ids: list[str]) -> None:
        now = now_iso()
        with self.db.transaction():
            for event_id in event_ids:
                self.db.execute(
                    "UPDATE sync_outbox SET status = ?, synchronized_at = ?, last_attempt_at = ?, "
                    "last_error = NULL WHERE event_id = ? AND status <> ?",
                    (OutboxStatus.SYNCED.value, now, now, event_id, OutboxStatus.SYNCED.value),
                )

    def mark_retry(self, event_ids: list[str], error: str) -> None:
        now = utc_now()
        with self.db.transaction():
            for event_id in event_ids:
                row = self.db.query_one(
                    "SELECT retry_count, status FROM sync_outbox WHERE event_id = ?", (event_id,)
                )
                if not row or row["status"] in (OutboxStatus.SYNCED, OutboxStatus.FAILED):
                    continue
                retries = int(row["retry_count"]) + 1
                delay = min(self.backoff_max_s, self.backoff_base_s * (2 ** min(retries, 16)))
                delay *= random.uniform(0.8, 1.2)
                self.db.execute(
                    "UPDATE sync_outbox SET status = ?, retry_count = ?, last_attempt_at = ?, "
                    "next_attempt_at = ?, last_error = ? WHERE event_id = ?",
                    (
                        OutboxStatus.RETRY.value,
                        retries,
                        iso(now),
                        iso(now + timedelta(seconds=delay)),
                        error[:500],
                        event_id,
                    ),
                )

    def mark_failed(self, failures: list[tuple[str, str]]) -> None:
        now = now_iso()
        with self.db.transaction():
            for event_id, reason in failures:
                self.db.execute(
                    "UPDATE sync_outbox SET status = ?, last_attempt_at = ?, last_error = ? "
                    "WHERE event_id = ?",
                    (OutboxStatus.FAILED.value, now, reason[:500], event_id),
                )
                row = self.db.query_one(
                    "SELECT payload FROM telemetry_events WHERE event_id = ?", (event_id,)
                )
                self.dead_letter("sync", reason, "core", loads(row["payload"]) if row else None)

    def outbox_stats(self) -> dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM sync_outbox GROUP BY status")
        stats = {s.value: 0 for s in OutboxStatus}
        stats.update({r["status"]: int(r["n"]) for r in rows})
        return stats

    def count_pending(self) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM sync_outbox WHERE status IN (?, ?)",
                (OutboxStatus.PENDING.value, OutboxStatus.RETRY.value),
            )
            or 0
        )

    def count_events(self, event_type: str | None = None) -> int:
        if event_type:
            return int(self.db.scalar(
                "SELECT COUNT(*) FROM telemetry_events WHERE event_type = ?", (event_type,)) or 0)
        return int(self.db.scalar("SELECT COUNT(*) FROM telemetry_events") or 0)

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT payload FROM telemetry_events WHERE event_id = ?", (event_id,)
        )
        return loads(row["payload"]) if row else None

    def purge_synced(self, older_than: datetime) -> int:
        """Retenção local: remove apenas eventos já confirmados pelo CORE."""
        cutoff = iso(older_than)
        with self.db.transaction():
            ids = [
                r["event_id"]
                for r in self.db.query(
                    "SELECT o.event_id FROM sync_outbox o JOIN telemetry_events e "
                    "ON e.event_id = o.event_id WHERE o.status = ? AND e.observed_at < ?",
                    (OutboxStatus.SYNCED.value, cutoff),
                )
            ]
            for event_id in ids:
                self.db.execute("DELETE FROM sync_outbox WHERE event_id = ?", (event_id,))
                self.db.execute("DELETE FROM telemetry_events WHERE event_id = ?", (event_id,))
        return len(ids)

    # ------------------------------------------------------------ dead letters
    def dead_letter(self, stage: str, reason: str, source: str, payload: Any) -> None:
        self.db.execute(
            "INSERT INTO dead_letters (id, stage, reason, source, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), stage, reason[:1000], source,
             dumps(payload) if payload is not None else None, now_iso()),
        )

    def dead_letters(self, stage: str | None = None) -> list[dict[str, Any]]:
        if stage:
            return self.db.query(
                "SELECT * FROM dead_letters WHERE stage = ? ORDER BY created_at", (stage,))
        return self.db.query("SELECT * FROM dead_letters ORDER BY created_at")

    # ------------------------------------------------------------------ state
    def latest_state(self, device_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM device_state WHERE device_id = ?", (device_id,))
        if not row:
            return None
        return {
            "device_id": device_id,
            "observed_at": row["observed_at"],
            "measurements": loads(row["measurements"]),
            "quality": loads(row["quality"]),
        }

    def recent_values(
        self, device_id: str, measurement: str, since: str, until: str
    ) -> list[tuple[str, float]]:
        rows = self.db.query(
            "SELECT observed_at, payload FROM telemetry_events WHERE device_id = ? "
            "AND event_type IN ('telemetry', 'position') AND observed_at >= ? "
            "AND observed_at <= ? ORDER BY observed_at",
            (device_id, since, until),
        )
        values = []
        for row in rows:
            payload = loads(row["payload"])
            value = payload.get("measurements", {}).get(measurement)
            quality = payload.get("quality", {}).get(measurement, Quality.VALID)
            if isinstance(value, int | float) and not isinstance(value, bool) \
                    and quality == Quality.VALID:
                values.append((payload["observed_at"], float(value)))
        return values

    # ------------------------------------------------------------------ audit
    def audit(self, actor: str, action: str, target: str, outcome: str,
              details: dict[str, Any] | None = None) -> None:
        self.db.execute(
            "INSERT INTO audit_log (id, at, actor, action, target, outcome, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), now_iso(), actor, action, target, outcome, dumps(details or {})),
        )

    def audit_entries(self, action: str | None = None) -> list[dict[str, Any]]:
        if action:
            return self.db.query(
                "SELECT * FROM audit_log WHERE action = ? ORDER BY at", (action,))
        return self.db.query("SELECT * FROM audit_log ORDER BY at")
