"""Tratamento de tempo: todos os instantes são armazenados em UTC ISO-8601.

Regra do modelo canônico (PIT-001 §9.1): timestamps sem fuso horário são rejeitados.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """Formato único (UTC, microssegundos) para permitir comparação lexical no SQLite."""
    if dt.tzinfo is None:
        raise ValueError("Timestamp sem fuso horário")
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def now_iso() -> str:
    return iso(utc_now())


def parse_ts(value: object) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, bool):
        raise ValueError("Timestamp inválido")
    elif isinstance(value, int | float):
        seconds = value / 1000.0 if value > 1e12 else float(value)
        dt = datetime.fromtimestamp(seconds, tz=UTC)
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"Timestamp inválido: {value!r}") from exc
    else:
        raise ValueError(f"Timestamp inválido: {value!r}")
    if dt.tzinfo is None:
        raise ValueError("Timestamp sem fuso horário")
    return dt.astimezone(UTC)


def normalize_iso(value: object) -> str:
    return iso(parse_ts(value))
