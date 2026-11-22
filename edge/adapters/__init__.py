"""Contrato dos adaptadores de dispositivos (ADR-003).

Os adaptadores traduzem interfaces de fabricantes para o modelo canônico. O núcleo da
plataforma depende apenas desta interface, permitindo substituir equipamentos sem
modificar as aplicações.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from contracts.canonical import CanonicalEvent
from contracts.devices import Device


class AdapterError(Exception):
    def __init__(self, message: str, permanent: bool = True):
        super().__init__(message)
        self.permanent = permanent


class Adapter(ABC):
    adapter_id: str = ""
    version: str = "1.0"
    source_type: str = ""

    @abstractmethod
    def identify(self, raw: dict[str, Any]) -> str:
        """Extrai o identificador externo (DevEUI, IMEI...) da mensagem bruta."""

    @abstractmethod
    def to_events(
        self,
        raw: dict[str, Any],
        device: Device,
        received_at: str,
        context: dict[str, Any] | None = None,
    ) -> list[CanonicalEvent]:
        """Converte a mensagem bruta em eventos canônicos."""

    def source(self, **extra: Any) -> dict[str, Any]:
        return {
            "protocol": self.source_type,
            "adapter": self.adapter_id,
            "adapter_version": self.version,
            **extra,
        }


_ADAPTERS: dict[str, Adapter] = {}


def register_adapter(adapter: Adapter, *aliases: str) -> Adapter:
    for name in (adapter.adapter_id, *aliases):
        _ADAPTERS[name] = adapter
    return adapter


def get_adapter(adapter_id: str) -> Adapter:
    _load_builtin()
    try:
        return _ADAPTERS[adapter_id]
    except KeyError as exc:
        raise AdapterError(f"Adaptador não registrado: {adapter_id}") from exc


def adapter_for_source(source_type: str) -> Adapter:
    """Adaptador padrão usado para identificar o dispositivo antes da consulta ao registro."""
    _load_builtin()
    for adapter in _ADAPTERS.values():
        if adapter.source_type == source_type:
            return adapter
    raise AdapterError(f"Nenhum adaptador para origem {source_type}")


def available_adapters() -> list[str]:
    _load_builtin()
    return sorted(_ADAPTERS)


def _load_builtin() -> None:
    # Importação tardia evita ciclos e registra os adaptadores embarcados.
    from edge.adapters.lorawan import chirpstack, downlink  # noqa: F401
    from edge.adapters.modbus import adapter as _modbus  # noqa: F401
    from edge.adapters.tracker import adapter as _tracker  # noqa: F401
