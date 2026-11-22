"""Modelo canônico de dados — envelope de eventos (PIT-001 §9).

Regras obrigatórias implementadas:
* Identificação: ``event_id`` estável e obrigatório.
* Temporalidade: ``observed_at`` / ``received_at`` / ``processed_at`` distintos e com fuso.
* Unidades: nomes de medição terminam com sufixo de unidade canônica (``_m``, ``_v``...).
* Qualidade: ``valid`` / ``suspect`` / ``missing`` / ``invalid`` + origem do timestamp.
* Versionamento: ``schema_version`` controlado; versões desconhecidas são rejeitadas.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from common.timeutil import parse_ts

SCHEMA_VERSION = "1.0"
SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0"})


class EventType(StrEnum):
    TELEMETRY = "telemetry"
    STATE = "state"
    EVENT = "event"
    POSITION = "position"
    COMMAND_RESULT = "command_result"
    DIAGNOSTIC = "diagnostic"


class Quality(StrEnum):
    VALID = "valid"
    SUSPECT = "suspect"
    MISSING = "missing"
    INVALID = "invalid"


class TimestampSource(StrEnum):
    DEVICE = "device"
    NETWORK_SERVER = "network_server"
    GATEWAY = "gateway"
    EDGE = "edge"
    CORE = "core"


# Sufixo do nome da medição -> unidade canônica (SI ou usual na operação).
CANONICAL_UNITS: dict[str, str] = {
    "_m": "m",
    "_mm": "mm",
    "_km": "km",
    "_v": "V",
    "_a": "A",
    "_c": "degC",
    "_pct": "%",
    "_bar": "bar",
    "_hz": "Hz",
    "_kw": "kW",
    "_kwh": "kWh",
    "_deg": "deg",
    "_kmh": "km/h",
    "_dbm": "dBm",
    "_db": "dB",
    "_s": "s",
    "_m3h": "m3/h",
    "_rpm": "rpm",
    "_state": "state",
    "_count": "count",
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,199}$")
_ALLOWED_KEYS = {
    "schema_version",
    "event_id",
    "tenant_id",
    "site_id",
    "device_id",
    "event_type",
    "observed_at",
    "received_at",
    "processed_at",
    "measurements",
    "quality",
    "source",
    "location",
    "attributes",
}
_REQUIRED_KEYS = (
    "schema_version",
    "event_id",
    "tenant_id",
    "site_id",
    "device_id",
    "event_type",
    "observed_at",
    "received_at",
)


class ContractError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def canonical_unit(name: str) -> str | None:
    """Unidade canônica implícita no nome (sufixo mais longo vence: ``_kmh`` antes de ``_m``)."""
    for suffix in sorted(CANONICAL_UNITS, key=len, reverse=True):
        if name.endswith(suffix):
            return CANONICAL_UNITS[suffix]
    return None


def is_valid_measurement_name(name: str) -> bool:
    return bool(_NAME_RE.match(name)) and canonical_unit(name) is not None


def is_valid_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def validate_event(data: Any) -> list[str]:
    """Retorna a lista de violações do contrato (vazia quando válido)."""
    if not isinstance(data, dict):
        return ["evento deve ser um objeto JSON"]
    errors: list[str] = []
    for key in _REQUIRED_KEYS:
        if key not in data:
            errors.append(f"campo obrigatório ausente: {key}")
    unknown = set(data) - _ALLOWED_KEYS
    if unknown:
        errors.append(f"campos desconhecidos: {sorted(unknown)}")
    if errors:
        return errors

    if data["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(f"schema_version não suportada: {data['schema_version']}")
    for key in ("event_id", "tenant_id", "site_id", "device_id"):
        if not is_valid_identifier(data[key]):
            errors.append(f"identificador inválido em {key}")
    if data["event_type"] not in {e.value for e in EventType}:
        errors.append(f"event_type inválido: {data['event_type']}")
    for key in ("observed_at", "received_at", "processed_at"):
        if key in data and data[key] is not None:
            try:
                parse_ts(data[key])
            except ValueError as exc:
                errors.append(f"{key}: {exc}")

    measurements = data.get("measurements", {})
    if not isinstance(measurements, dict):
        errors.append("measurements deve ser objeto")
        measurements = {}
    for name, value in measurements.items():
        if not is_valid_measurement_name(name):
            errors.append(f"nome de medição sem unidade canônica: {name}")
        if value is None or isinstance(value, bool):
            continue
        if not _is_number(value) or not math.isfinite(value):
            errors.append(f"valor inválido para {name}")

    quality = data.get("quality", {})
    if not isinstance(quality, dict):
        errors.append("quality deve ser objeto")
        quality = {}
    for name, value in quality.items():
        if name == "timestamp_source":
            if value not in {t.value for t in TimestampSource}:
                errors.append(f"timestamp_source inválido: {value}")
        elif value not in {q.value for q in Quality}:
            errors.append(f"qualidade inválida para {name}: {value}")

    for key in ("source", "attributes"):
        if key in data and not isinstance(data[key], dict):
            errors.append(f"{key} deve ser objeto")

    location = data.get("location")
    if data["event_type"] == EventType.POSITION and location is None:
        errors.append("evento position exige location")
    if location is not None:
        if not isinstance(location, dict):
            errors.append("location deve ser objeto")
        else:
            lat, lon = location.get("lat"), location.get("lon")
            if not (_is_number(lat) and -90 <= lat <= 90):
                errors.append("location.lat inválida")
            if not (_is_number(lon) and -180 <= lon <= 180):
                errors.append("location.lon inválida")

    if data["event_type"] == EventType.COMMAND_RESULT:
        attrs = data.get("attributes") or {}
        if not attrs.get("command_id") or not attrs.get("state"):
            errors.append("command_result exige attributes.command_id e attributes.state")
    return errors


@dataclass
class CanonicalEvent:
    event_id: str
    tenant_id: str
    site_id: str
    device_id: str
    event_type: str
    observed_at: str
    received_at: str
    measurements: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, str] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION
    processed_at: str | None = None
    location: dict[str, Any] | None = None
    attributes: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "tenant_id": self.tenant_id,
            "site_id": self.site_id,
            "device_id": self.device_id,
            "event_type": str(self.event_type),
            "observed_at": self.observed_at,
            "received_at": self.received_at,
            "measurements": dict(self.measurements),
            "quality": dict(self.quality),
            "source": dict(self.source),
        }
        if self.processed_at is not None:
            data["processed_at"] = self.processed_at
        if self.location is not None:
            data["location"] = dict(self.location)
        if self.attributes is not None:
            data["attributes"] = dict(self.attributes)
        return data

    def validate(self) -> None:
        errors = validate_event(self.to_dict())
        if errors:
            raise ContractError(errors)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CanonicalEvent:
        errors = validate_event(data)
        if errors:
            raise ContractError(errors)
        return cls(
            event_id=data["event_id"],
            tenant_id=data["tenant_id"],
            site_id=data["site_id"],
            device_id=data["device_id"],
            event_type=data["event_type"],
            observed_at=data["observed_at"],
            received_at=data["received_at"],
            measurements=dict(data.get("measurements") or {}),
            quality=dict(data.get("quality") or {}),
            source=dict(data.get("source") or {}),
            schema_version=data["schema_version"],
            processed_at=data.get("processed_at"),
            location=data.get("location"),
            attributes=data.get("attributes"),
        )
