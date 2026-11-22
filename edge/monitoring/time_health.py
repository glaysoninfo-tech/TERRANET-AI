"""Time Health Monitor do TERRANET EDGE (ARQ-002 §10).

Monitora:
* Validade absoluta da data/hora (protege contra reset de RTC para 1970/2020 sem bateria).
* Saltos abruptos para frente ou para trás comparando relógio UTC vs relógio monotônico.
* Quando o relógio não é confiável, o EDGE bloqueia novos comandos remotos dependentes
  de TTL (§10.4), mantendo telemetria local e intertravamentos do CLP intactos.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from common.timeutil import utc_now


@dataclass
class TimeHealthStatus:
    healthy: bool
    reason: str
    last_jump_s: float
    wall_utc: str


class TimeHealthMonitor:
    def __init__(self, min_year: int = 2025, max_jump_s: float = 30.0):
        self.min_year = min_year
        self.max_jump_s = max_jump_s
        self._last_wall: datetime | None = None
        self._last_mono: float | None = None
        self._untrusted_reason: str | None = None
        self.last_jump_s: float = 0.0

    def check(
        self,
        now_utc: datetime | None = None,
        now_mono: float | None = None,
    ) -> TimeHealthStatus:
        wall = now_utc or utc_now()
        mono = time.monotonic() if now_mono is None else now_mono

        if wall.year < self.min_year:
            self._untrusted_reason = f"invalid_year:{wall.year}"
            return TimeHealthStatus(False, self._untrusted_reason, 0.0, wall.isoformat())

        if self._last_wall is not None and self._last_mono is not None:
            d_wall = (wall - self._last_wall).total_seconds()
            d_mono = mono - self._last_mono
            jump = d_wall - d_mono
            self.last_jump_s = round(jump, 3)
            if abs(jump) > self.max_jump_s:
                self._untrusted_reason = f"clock_jump:{self.last_jump_s:+.1f}s"
                self._last_wall = wall
                self._last_mono = mono
                return TimeHealthStatus(False, self._untrusted_reason, self.last_jump_s, wall.isoformat())

        self._last_wall = wall
        self._last_mono = mono
        if self._untrusted_reason is not None:
            return TimeHealthStatus(False, self._untrusted_reason, self.last_jump_s, wall.isoformat())
        return TimeHealthStatus(True, "ok", self.last_jump_s, wall.isoformat())

    def status(self) -> TimeHealthStatus:
        """Retorna o estado atual já avaliado (sem alterar a linha de base com timestamps sintéticos)."""
        if self._untrusted_reason is not None:
            wall = (self._last_wall or utc_now()).isoformat()
            return TimeHealthStatus(False, self._untrusted_reason, self.last_jump_s, wall)
        return self.check()

    @property
    def healthy(self) -> bool:
        return self.check().healthy

    def mark_resynchronized(
        self, now_utc: datetime | None = None, now_mono: float | None = None
    ) -> None:
        """Restabelece confiança após ressincronização explícita com NTP/GNSS."""
        wall = now_utc or utc_now()
        mono = time.monotonic() if now_mono is None else now_mono
        if wall.year < self.min_year:
            return
        self._untrusted_reason = None
        self.last_jump_s = 0.0
        self._last_wall = wall
        self._last_mono = mono
