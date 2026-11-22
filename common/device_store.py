"""Repositório do Device Registry (E03) — usado pelo EDGE e pelo CORE.

As tabelas ``devices`` e ``device_lifecycle_log`` possuem a mesma estrutura nos dois
subsistemas, permitindo sincronizar o cadastro sem tradução.
"""

from __future__ import annotations

import uuid
from typing import Any

from common.db import Database, dumps, loads
from common.timeutil import now_iso
from contracts.devices import Device, LifecycleState, can_transition

_COLUMNS = (
    "device_id",
    "tenant_id",
    "site_id",
    "category",
    "source_type",
    "source_identifier",
    "adapter_id",
    "adapter_version",
    "manufacturer",
    "model",
    "hardware_version",
    "firmware_version",
    "communication_profile",
    "capability_profile",
    "operational_status",
    "configuration_version",
    "installed_at",
    "metadata",
    "last_seen_at",
)
_JSON_COLUMNS = {"communication_profile", "capability_profile", "metadata"}


class DeviceNotFound(LookupError):
    pass


class LifecycleError(ValueError):
    pass


class DeviceStore:
    def __init__(self, db: Database):
        self.db = db

    # ---------------------------------------------------------------- mapping
    @staticmethod
    def _to_row(device: Device) -> list[Any]:
        data = device.to_dict()
        return [dumps(data[c]) if c in _JSON_COLUMNS else data[c] for c in _COLUMNS]

    @staticmethod
    def _from_row(row: dict[str, Any]) -> Device:
        data = {c: row.get(c) for c in _COLUMNS}
        for column in _JSON_COLUMNS:
            data[column] = loads(data[column]) or ({} if column != "capability_profile" else [])
        return Device(**data)

    # ------------------------------------------------------------------ write
    def register(self, device: Device, actor: str = "system") -> Device:
        """PIT-T01: cadastro com identificação única (source_type, source_identifier)."""
        device.validate()
        now = now_iso()
        with self.db.transaction():
            if self.get(device.device_id):
                raise ValueError(f"Dispositivo já cadastrado: {device.device_id}")
            if self.find_by_source(device.source_type, device.source_identifier):
                raise ValueError(
                    f"Identificador externo já vinculado: {device.source_identifier}"
                )
            placeholders = ",".join("?" for _ in _COLUMNS)
            self.db.execute(
                f"INSERT INTO devices ({','.join(_COLUMNS)}, updated_at) "  # noqa: S608
                f"VALUES ({placeholders}, ?)",
                [*self._to_row(device), now],
            )
            self._log(device.device_id, None, device.operational_status, actor, "cadastro", now)
        return device

    def upsert(self, device: Device, actor: str = "sync") -> Device:
        """Aplica cadastro recebido (ex.: configuração vinda do CORE), registrando transições."""
        device.validate()
        current = self.get(device.device_id)
        if current is None:
            return self.register(device, actor)
        now = now_iso()
        with self.db.transaction():
            assignments = ",".join(f"{c} = ?" for c in _COLUMNS if c not in ("device_id",
                                                                             "last_seen_at"))
            values = [
                v for c, v in zip(_COLUMNS, self._to_row(device), strict=True)
                if c not in ("device_id", "last_seen_at")
            ]
            self.db.execute(
                f"UPDATE devices SET {assignments}, updated_at = ? WHERE device_id = ?",  # noqa: S608
                [*values, now, device.device_id],
            )
            if current.operational_status != device.operational_status:
                self._log(device.device_id, current.operational_status,
                          device.operational_status, actor, "sincronização de cadastro", now)
        return device

    def transition(self, device_id: str, new_state: str, actor: str, reason: str = "") -> Device:
        new_state = LifecycleState(new_state).value
        now = now_iso()
        with self.db.transaction():
            device = self.get(device_id)
            if device is None:
                raise DeviceNotFound(device_id)
            if not can_transition(device.operational_status, new_state):
                raise LifecycleError(
                    f"Transição não permitida: {device.operational_status} -> {new_state}"
                )
            self.db.execute(
                "UPDATE devices SET operational_status = ?, updated_at = ? WHERE device_id = ?",
                (new_state, now, device_id),
            )
            self._log(device_id, device.operational_status, new_state, actor, reason, now)
            device.operational_status = new_state
        return device

    def touch(self, device_id: str, seen_at: str) -> None:
        self.db.execute(
            "UPDATE devices SET last_seen_at = ? WHERE device_id = ? "
            "AND (last_seen_at IS NULL OR last_seen_at < ?)",
            (seen_at, device_id, seen_at),
        )

    def _log(self, device_id: str, old: str | None, new: str, actor: str, reason: str,
             at: str) -> None:
        self.db.execute(
            "INSERT INTO device_lifecycle_log (id, device_id, from_state, to_state, actor, "
            "reason, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), device_id, old, new, actor, reason, at),
        )

    # ------------------------------------------------------------------- read
    def get(self, device_id: str) -> Device | None:
        row = self.db.query_one("SELECT * FROM devices WHERE device_id = ?", (device_id,))
        return self._from_row(row) if row else None

    def find_by_source(self, source_type: str, identifier: str) -> Device | None:
        row = self.db.query_one(
            "SELECT * FROM devices WHERE source_type = ? AND source_identifier = ?",
            (source_type, str(identifier).lower() if source_type == "lorawan" else identifier),
        )
        return self._from_row(row) if row else None

    def list(
        self,
        tenant_id: str | None = None,
        site_ids: list[str] | set[str] | None = None,
        source_type: str | None = None,
    ) -> list[Device]:
        sql = "SELECT * FROM devices WHERE 1 = 1"
        params: list[Any] = []
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            params.append(tenant_id)
        if site_ids is not None:
            site_ids = list(site_ids)
            if not site_ids:
                return []
            sql += f" AND site_id IN ({','.join('?' for _ in site_ids)})"
            params.extend(site_ids)
        if source_type is not None:
            sql += " AND source_type = ?"
            params.append(source_type)
        return [self._from_row(r) for r in self.db.query(sql + " ORDER BY device_id", params)]

    def lifecycle_log(self, device_id: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM device_lifecycle_log WHERE device_id = ? ORDER BY at, id",
            (device_id,),
        )

    def decommissioned_at(self, device_id: str) -> str | None:
        return self.db.scalar(
            "SELECT MAX(at) FROM device_lifecycle_log WHERE device_id = ? AND to_state = ?",
            (device_id, LifecycleState.DECOMMISSIONED.value),
        )
