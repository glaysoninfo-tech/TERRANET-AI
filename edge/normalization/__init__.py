"""Telemetry Processor: validação, conversão de unidades e qualidade (PIT-001 §6.2, §9.1)."""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

from common.timeutil import iso, parse_ts
from contracts.canonical import (
    Quality,
    TimestampSource,
    canonical_unit,
    is_valid_measurement_name,
)
from contracts.devices import Device

# (unidade de origem, unidade canônica) -> função de conversão
_CONVERSIONS: dict[tuple[str, str], Any] = {
    ("cm", "m"): lambda v: v / 100.0,
    ("mm", "m"): lambda v: v / 1000.0,
    ("km", "m"): lambda v: v * 1000.0,
    ("m", "km"): lambda v: v / 1000.0,
    ("m", "mm"): lambda v: v * 1000.0,
    ("mV", "V"): lambda v: v / 1000.0,
    ("mA", "A"): lambda v: v / 1000.0,
    ("degF", "degC"): lambda v: (v - 32.0) * 5.0 / 9.0,
    ("K", "degC"): lambda v: v - 273.15,
    ("psi", "bar"): lambda v: v * 0.0689475729,
    ("kPa", "bar"): lambda v: v / 100.0,
    ("mbar", "bar"): lambda v: v / 1000.0,
    ("W", "kW"): lambda v: v / 1000.0,
    ("Wh", "kWh"): lambda v: v / 1000.0,
    ("m/s", "km/h"): lambda v: v * 3.6,
    ("knots", "km/h"): lambda v: v * 1.852,
    ("mph", "km/h"): lambda v: v * 1.609344,
    ("l/s", "m3/h"): lambda v: v * 3.6,
    ("ms", "s"): lambda v: v / 1000.0,
    ("min", "s"): lambda v: v * 60.0,
}

MAX_FUTURE_SKEW = timedelta(minutes=5)


class NormalizationError(ValueError):
    pass


def convert(value: float, from_unit: str | None, to_unit: str) -> float:
    if not from_unit or from_unit == to_unit:
        return value
    try:
        return _CONVERSIONS[(from_unit, to_unit)](value)
    except KeyError as exc:
        raise NormalizationError(f"Conversão não suportada: {from_unit} -> {to_unit}") from exc


def _in_range(value: float, bounds: Any) -> bool:
    if not bounds:
        return True
    low, high = bounds
    return (low is None or value >= low) and (high is None or value <= high)


class Normalizer:
    """Converte medições brutas ``{nome: (valor, unidade)}`` para o modelo canônico."""

    def normalize(
        self,
        raw: dict[str, tuple[Any, str | None]],
        device: Device,
        hints: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        hints = hints or {}
        ranges = device.metadata.get("ranges", {})
        suspect_ranges = device.metadata.get("suspect_ranges", {})
        measurements: dict[str, Any] = {}
        quality: dict[str, str] = {}

        for name, (value, unit) in raw.items():
            if not is_valid_measurement_name(name):
                raise NormalizationError(f"Medição sem unidade canônica: {name}")
            target = canonical_unit(name)
            if value is None:
                measurements[name], quality[name] = None, Quality.MISSING.value
                continue
            if target == "state" or isinstance(value, bool):
                measurements[name] = bool(value) if isinstance(value, bool | int) else None
                quality[name] = hints.get(name, Quality.VALID.value)
                continue
            if isinstance(value, bool) or not isinstance(value, int | float) \
                    or not math.isfinite(value):
                measurements[name], quality[name] = None, Quality.INVALID.value
                continue
            try:
                converted = convert(float(value), unit, target)
            except NormalizationError:
                measurements[name], quality[name] = None, Quality.INVALID.value
                continue
            converted = round(converted, 6)
            measurements[name] = converted
            if not _in_range(converted, ranges.get(name)):
                quality[name] = Quality.INVALID.value
            elif not _in_range(converted, suspect_ranges.get(name)):
                quality[name] = Quality.SUSPECT.value
            else:
                quality[name] = Quality.VALID.value
            if name in hints:
                quality[name] = hints[name]

        for name in device.metadata.get("expected_measurements", []):
            if name not in measurements:
                measurements[name], quality[name] = None, Quality.MISSING.value
        return measurements, quality

    @staticmethod
    def resolve_timestamp(
        candidate: Any, received_at: str, source: TimestampSource
    ) -> tuple[str, str, bool]:
        """Retorna (observed_at, timestamp_source, suspeito).

        Sem relógio confiável usa-se o horário de recepção, identificando a origem.
        """
        received = parse_ts(received_at)
        if candidate in (None, ""):
            return iso(received), TimestampSource.EDGE.value, False
        try:
            observed = parse_ts(candidate)
        except ValueError:
            return iso(received), TimestampSource.EDGE.value, True
        if observed - received > MAX_FUTURE_SKEW:
            return iso(received), TimestampSource.EDGE.value, True
        return iso(observed), source.value, False


DEFAULT_NORMALIZER = Normalizer()
