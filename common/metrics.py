"""Registro de métricas compatível com o formato de exposição do Prometheus (§18)."""

from __future__ import annotations

import threading


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metric:
    def __init__(self, name: str, help_text: str, kind: str):
        self.name = name
        self.help = help_text
        self.kind = kind
        self._values: dict[tuple[tuple[str, str], ...], float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(labels: dict[str, object]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((k, str(v)) for k, v in labels.items()))

    def inc(self, amount: float = 1.0, **labels: object) -> None:
        if self.kind == "counter" and amount < 0:
            raise ValueError("Contadores não podem diminuir")
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def set(self, value: float, **labels: object) -> None:
        with self._lock:
            self._values[self._key(labels)] = float(value)

    def get(self, **labels: object) -> float:
        with self._lock:
            return self._values.get(self._key(labels), 0.0)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]
        with self._lock:
            items = list(self._values.items()) or [((), 0.0)]
        for key, value in items:
            labels = ",".join(f'{k}="{_escape(v)}"' for k, v in key)
            lines.append(f"{self.name}{{{labels}}} {value}" if labels else f"{self.name} {value}")
        return lines


class Registry:
    def __init__(self) -> None:
        self._metrics: dict[str, Metric] = {}
        self._lock = threading.Lock()

    def _get(self, name: str, help_text: str, kind: str) -> Metric:
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                metric = Metric(name, help_text, kind)
                self._metrics[name] = metric
            return metric

    def counter(self, name: str, help_text: str = "") -> Metric:
        return self._get(name, help_text, "counter")

    def gauge(self, name: str, help_text: str = "") -> Metric:
        return self._get(name, help_text, "gauge")

    def render(self) -> str:
        with self._lock:
            metrics = list(self._metrics.values())
        lines: list[str] = []
        for metric in metrics:
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"
