"""Módulo Analytics & IA — nível inicial determinístico e estatístico (PIT-001 §22, ADR-014).

Implementa a base descritiva exigida antes de evoluir para modelos Scikit-Learn / XGBoost:
* Estatísticas por variável (mínimo, máximo, média, desvio-padrão).
* Tendência linear (taxa por hora).
* Diagnóstico de ausência de dados (silêncio desde última medição).
* Detecção simples de desvios por z-score sobre medições válidas.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from common.timeutil import parse_ts, utc_now
from contracts.canonical import Quality
from core.storage.repository import CoreStore


class AnalyticsService:
    def __init__(self, store: CoreStore):
        self.store = store

    def device_summary(
        self,
        tenant_id: str,
        device_id: str,
        measurement: str,
        limit: int = 200,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        events = self.store.query_events(
            tenant_id=tenant_id, device_id=device_id, limit=limit
        )
        series: list[tuple[datetime, float]] = []
        for ev in reversed(events):  # ordem cronológica
            val = (ev.get("measurements") or {}).get(measurement)
            qual = (ev.get("quality") or {}).get(measurement, Quality.VALID.value)
            if (
                qual == Quality.VALID.value
                and isinstance(val, int | float)
                and not isinstance(val, bool)
            ):
                series.append((parse_ts(ev["observed_at"]), float(val)))

        now_dt = now or utc_now()
        if not series:
            return {
                "device_id": device_id,
                "measurement": measurement,
                "count": 0,
                "silence_s": None,
                "stats": None,
                "trend_per_h": None,
                "anomalies": [],
            }

        values = [v for _, v in series]
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        std = math.sqrt(variance)

        trend_per_h: float | None = None
        if len(series) >= 2:
            dt_h = (series[-1][0] - series[0][0]).total_seconds() / 3600.0
            if dt_h > 0:
                trend_per_h = round((series[-1][1] - series[0][1]) / dt_h, 4)

        anomalies: list[dict[str, Any]] = []
        if std > 0:
            for ts, val in series:
                z = (val - mean) / std
                if abs(z) >= 3.0:
                    anomalies.append(
                        {"observed_at": ts.isoformat(), "value": val, "z_score": round(z, 2)}
                    )

        silence_s = max(0.0, round((now_dt - series[-1][0]).total_seconds(), 1))
        return {
            "device_id": device_id,
            "measurement": measurement,
            "count": len(series),
            "silence_s": silence_s,
            "stats": {
                "min": min(values),
                "max": max(values),
                "mean": round(mean, 4),
                "std": round(std, 4),
                "latest": values[-1],
            },
            "trend_per_h": trend_per_h,
            "anomalies": anomalies,
        }
