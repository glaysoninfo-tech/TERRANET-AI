"""Decodificadores de payload LoRaWAN selecionados por perfil de dispositivo.

Cada decodificador retorna ``(medições brutas, dicas de qualidade)`` onde medições
brutas = ``{nome_canônico: (valor, unidade_de_origem)}``. A conversão para a unidade
canônica é feita pelo Normalizer.
"""

from __future__ import annotations

import base64
import binascii
import struct
from collections.abc import Callable
from typing import Any

from contracts.canonical import Quality
from contracts.devices import Device
from edge.adapters import AdapterError

RawMeasurements = dict[str, tuple[Any, str | None]]
DecoderResult = tuple[RawMeasurements, dict[str, str]]
Decoder = Callable[[dict[str, Any], Device], DecoderResult]

DECODERS: dict[str, Decoder] = {}


def decoder(name: str) -> Callable[[Decoder], Decoder]:
    def wrap(fn: Decoder) -> Decoder:
        DECODERS[name] = fn
        return fn

    return wrap


def get_decoder(name: str) -> Decoder:
    try:
        return DECODERS[name]
    except KeyError as exc:
        raise AdapterError(f"Decodificador desconhecido: {name}") from exc


@decoder("chirpstack_object")
def decode_object(event: dict[str, Any], device: Device) -> DecoderResult:
    """Usa o campo ``object`` produzido pelo codec do ChirpStack.

    ``metadata.field_map`` mapeia nomes canônicos para campos do fabricante:
    ``{"water_level_m": {"field": "level", "unit": "cm"}}``. Sem mapeamento, campos
    já nomeados canonicamente são usados diretamente.
    """
    obj = event.get("object")
    if not isinstance(obj, dict):
        raise AdapterError("Uplink sem campo 'object' decodificado")
    field_map: dict[str, Any] = device.metadata.get("field_map") or {}
    raw: RawMeasurements = {}
    if field_map:
        for canonical, spec in field_map.items():
            spec = {"field": spec} if isinstance(spec, str) else spec
            value: Any = obj
            for part in str(spec["field"]).split("."):
                value = value.get(part) if isinstance(value, dict) else None
            scale = spec.get("scale")
            if isinstance(value, int | float) and not isinstance(value, bool) and scale:
                value = value * scale
            raw[canonical] = (value, spec.get("unit"))
    else:
        raw = {k: (v, None) for k, v in obj.items()}
    return raw, {}


@decoder("terranet_level_v1")
def decode_level_v1(event: dict[str, Any], device: Device) -> DecoderResult:
    """Payload binário de referência (big-endian):

    bytes 0-1  nível em mm (uint16)      bytes 2-3  bateria em mV (uint16)
    byte  4    flags (bit0 = falha do sensor, bit1 = bateria externa)  [opcional]
    """
    data = event.get("data")
    if not data:
        raise AdapterError("Uplink sem campo 'data'")
    try:
        payload = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AdapterError("Payload base64 inválido") from exc
    if len(payload) < 4:
        raise AdapterError(f"Payload curto: {len(payload)} bytes")
    level_mm, battery_mv = struct.unpack(">HH", payload[:4])
    flags = payload[4] if len(payload) > 4 else 0
    hints: dict[str, str] = {}
    raw: RawMeasurements = {
        "water_level_m": (level_mm, "mm"),
        "battery_voltage_v": (battery_mv, "mV"),
    }
    if level_mm == 0xFFFF:
        raw["water_level_m"] = (None, "mm")
    if flags & 0x01:
        hints["water_level_m"] = Quality.INVALID.value
    return raw, hints


def encode_level_v1(level_mm: int, battery_mv: int, flags: int = 0) -> str:
    """Utilitário para simuladores e testes."""
    return base64.b64encode(struct.pack(">HHB", level_mm, battery_mv, flags)).decode()
