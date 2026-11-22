"""Hierarquia de ativos, arbitragem de modos de controle e regras regulatórias (ARQ-002 §14–16, §26).

* ADR-018: TENANT -> SITE -> ZONE -> ASSET -> DEVICE.
* Arbitragem de controle (Ajuste #3): o estado físico do seletor, intertravamentos e LOTO
  sempre prevalece sobre solicitações ou configurações digitais.
* Regras regulatórias parametrizadas (Ajuste #4): sem constantes legais fixas em código.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ControlMode(StrEnum):
    LOCAL_MANUAL = "LOCAL_MANUAL"
    LOCAL_AUTO = "LOCAL_AUTO"
    REMOTE_SUPERVISED = "REMOTE_SUPERVISED"
    MAINTENANCE = "MAINTENANCE"
    LOTO_LOCKED = "LOTO_LOCKED"
    FAULT = "FAULT"


def resolve_control_mode(
    *,
    physical_remote_enabled: bool,
    physical_loto_locked: bool = False,
    physical_fault: bool = False,
    physical_auto_enabled: bool = False,
    digital_mode: ControlMode | str | None = None,
) -> ControlMode:
    """Resolve o modo operacional efetivo dando precedência absoluta ao estado físico.

    O software pode apenas RESTRINGIR (ex.: MAINTENANCE / LOTO_LOCKED), jamais conceder
    controle remoto quando o campo está em FAULT, LOTO físico ou LOCAL_MANUAL.
    """
    if physical_fault:
        return ControlMode.FAULT
    if physical_loto_locked:
        return ControlMode.LOTO_LOCKED
    if not physical_remote_enabled:
        return ControlMode.LOCAL_AUTO if physical_auto_enabled else ControlMode.LOCAL_MANUAL

    if digital_mode is not None:
        dm = ControlMode(digital_mode)
        if dm in (ControlMode.LOTO_LOCKED, ControlMode.MAINTENANCE, ControlMode.FAULT):
            return dm
    return ControlMode.REMOTE_SUPERVISED


@dataclass
class RegulatoryRule:
    """Parâmetro regulatório versionado e rastreável (ANEEL, ANA/órgão estadual, MAPA IN 77)."""

    domain: str  # irrigation_tariff | water_grant | dairy_cooling | machine_safety
    authority: str  # ex.: ANEEL, ANA, DAEE-SP, IGAM-MG, MAPA
    reference_code: str  # ex.: IN-MAPA-77/2018, Outorga-2026-001
    scope: str  # ex.: unidade consumidora / bacia / tanque
    effective_from: str  # ISO-8601 date
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Asset:
    """Entidade operacional na hierarquia TENANT -> SITE -> ZONE -> ASSET -> DEVICE."""

    asset_id: str
    tenant_id: str
    site_id: str
    asset_type: str  # pump | pivot | well | river_station | cooling_tank | gate | vehicle
    name: str
    zone_id: str | None = None
    control_mode: str = ControlMode.REMOTE_SUPERVISED.value
    safe_state_policy: dict[str, str] = field(default_factory=dict)
    regulatory_rules: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        for field_name in ("asset_id", "tenant_id", "site_id", "asset_type", "name"):
            if not getattr(self, field_name):
                raise ValueError(f"Campo obrigatório vazio em Asset: {field_name}")
        ControlMode(self.control_mode)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
