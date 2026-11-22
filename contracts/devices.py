"""Modelo lógico de dispositivos independente de fabricante (PIT-001 §7, §8)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class LifecycleState(StrEnum):
    IDENTIFIED = "IDENTIFIED"
    REGISTERED = "REGISTERED"
    PROVISIONED = "PROVISIONED"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"
    MAINTENANCE = "MAINTENANCE"
    DECOMMISSIONED = "DECOMMISSIONED"


S = LifecycleState
LIFECYCLE_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    S.IDENTIFIED: frozenset({S.REGISTERED, S.DECOMMISSIONED}),
    S.REGISTERED: frozenset({S.PROVISIONED, S.DECOMMISSIONED}),
    S.PROVISIONED: frozenset({S.ACTIVE, S.MAINTENANCE, S.DECOMMISSIONED}),
    S.ACTIVE: frozenset({S.DEGRADED, S.OFFLINE, S.MAINTENANCE, S.DECOMMISSIONED}),
    S.DEGRADED: frozenset({S.ACTIVE, S.OFFLINE, S.MAINTENANCE, S.DECOMMISSIONED}),
    S.OFFLINE: frozenset({S.ACTIVE, S.DEGRADED, S.MAINTENANCE, S.DECOMMISSIONED}),
    S.MAINTENANCE: frozenset({S.ACTIVE, S.PROVISIONED, S.DECOMMISSIONED}),
    S.DECOMMISSIONED: frozenset(),
}

# Telemetria aceita a partir do provisionamento (validação de comunicação, §8.1 passo 8).
TELEMETRY_ACCEPTING = frozenset({S.PROVISIONED, S.ACTIVE, S.DEGRADED, S.OFFLINE, S.MAINTENANCE})
# Comandos físicos somente para equipamentos em operação normal.
COMMAND_ACCEPTING = frozenset({S.ACTIVE})

CATEGORIES = frozenset({"NODE", "NODE-S", "NODE-C", "TRACK", "TRACK-B", "GATEWAY"})
SOURCE_TYPES = frozenset({"lorawan", "modbus", "tracker", "internal"})

_SECRET_KEYS = re.compile(r"(app_?key|nwk_?key|apps_?key|password|secret|private_?key|token)", re.I)
_DEV_EUI_RE = re.compile(r"^[0-9a-f]{16}$")


def can_transition(current: str, new: str) -> bool:
    return LifecycleState(new) in LIFECYCLE_TRANSITIONS[LifecycleState(current)]


def _find_secret(obj: Any, path: str = "") -> str | None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if _SECRET_KEYS.search(str(key)):
                return f"{path}{key}"
            found = _find_secret(value, f"{path}{key}.")
            if found:
                return found
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            found = _find_secret(value, f"{path}{i}.")
            if found:
                return found
    return None


def normalize_identifier(source_type: str, identifier: str) -> str:
    identifier = str(identifier).strip()
    if source_type == "lorawan":
        identifier = identifier.lower().replace("-", "").replace(":", "")
        if not _DEV_EUI_RE.match(identifier):
            raise ValueError(f"DevEUI inválido: {identifier}")
    if not identifier:
        raise ValueError("Identificador externo vazio")
    return identifier


@dataclass
class Device:
    device_id: str
    tenant_id: str
    site_id: str
    category: str
    source_type: str
    source_identifier: str
    adapter_id: str
    adapter_version: str = "1.0"
    manufacturer: str = ""
    model: str = ""
    hardware_version: str = ""
    firmware_version: str = ""
    communication_profile: dict[str, Any] = field(default_factory=dict)
    capability_profile: list[str] = field(default_factory=list)
    operational_status: str = LifecycleState.REGISTERED.value
    configuration_version: int = 1
    installed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    last_seen_at: str | None = None

    def validate(self) -> None:
        for name in ("device_id", "tenant_id", "site_id", "adapter_id"):
            if not getattr(self, name):
                raise ValueError(f"Campo obrigatório vazio: {name}")
        if self.category not in CATEGORIES:
            raise ValueError(f"Categoria inválida: {self.category}")
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"Tipo de origem inválido: {self.source_type}")
        LifecycleState(self.operational_status)
        self.source_identifier = normalize_identifier(self.source_type, self.source_identifier)
        secret = _find_secret(
            {"communication": self.communication_profile, "metadata": self.metadata}
        )
        if secret:
            raise ValueError(
                f"Perfil não pode conter segredos ({secret}); use o gerenciador de segredos"
            )

    @property
    def allowed_operations(self) -> list[str]:
        return list(self.metadata.get("allowed_operations", []))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Device:
        known = set(cls.__dataclass_fields__)
        device = cls(**{k: v for k, v in data.items() if k in known})
        device.validate()
        return device

    @classmethod
    def from_profile(cls, profile: dict[str, Any]) -> Device:
        """Aceita o formato de perfil do §7.2 (``communication`` / ``adapter`` aninhados)."""
        secret = _find_secret(profile)
        if secret:
            raise ValueError(
                f"Perfil não pode conter segredos ({secret}); use o gerenciador de segredos"
            )
        comm = dict(profile.get("communication") or {})
        adapter = profile.get("adapter") or {}
        source_type = comm.get("type", profile.get("source_type", ""))
        if source_type in ("lte", "gnss", "tracker_lte"):
            source_type = "tracker"
        if source_type in ("modbus_tcp", "modbus_rtu"):
            source_type = "modbus"
        identifier = (
            comm.get("dev_eui")
            or comm.get("imei")
            or comm.get("identifier")
            or profile.get("external_identifier")
            or profile.get("source_identifier")
            or profile["device_id"]
        )
        device = cls(
            device_id=profile["device_id"],
            tenant_id=profile["tenant_id"],
            site_id=profile["site_id"],
            category=profile.get("category") or profile.get("equipment_category", ""),
            source_type=source_type,
            source_identifier=identifier,
            adapter_id=adapter.get("name") or profile.get("adapter_id", ""),
            adapter_version=str(adapter.get("version", "1.0")),
            manufacturer=profile.get("manufacturer", ""),
            model=profile.get("model", ""),
            hardware_version=profile.get("hardware_version", ""),
            firmware_version=profile.get("firmware_version", ""),
            communication_profile=comm,
            capability_profile=list(profile.get("capabilities", [])),
            operational_status=profile.get("operational_status", "REGISTERED"),
            configuration_version=int(profile.get("configuration_version", 1)),
            installed_at=profile.get("installed_at"),
            metadata=dict(profile.get("metadata") or {}),
        )
        device.validate()
        return device
