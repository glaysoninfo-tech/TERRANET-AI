"""Monitoring Agent do EDGE: métricas Prometheus (§18.1–18.3) e endpoints /healthz e /metrics."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from common.metrics import Registry
from edge.mqtt.bus import MessageBus
from edge.storage.repository import EdgeStore


class MonitoringAgent:
    def __init__(
        self,
        edge_id: str,
        store: EdgeStore,
        bus: MessageBus,
        metrics: Registry | None = None,
        storage_path: str = ".",
    ):
        self.edge_id = edge_id
        self.store = store
        self.bus = bus
        self.metrics = metrics or Registry()
        self.storage_path = storage_path
        self._started_at = time.monotonic()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

        # Indicadores obrigatórios do EDGE (§18.1)
        self.uptime = self.metrics.gauge("edge_uptime_seconds", "Tempo de operação do EDGE em segundos")
        self.cpu_usage = self.metrics.gauge("edge_cpu_usage", "Utilização estimada de CPU (0..1)")
        self.memory_usage = self.metrics.gauge("edge_memory_usage", "Consumo de memória do processo em bytes")
        self.storage_free = self.metrics.gauge("edge_storage_free", "Espaço livre em disco em bytes")
        self.mqtt_connected = self.metrics.gauge("edge_mqtt_connected", "Estado da conexão MQTT local")
        self.wan_connected = self.metrics.gauge("edge_wan_connected", "Estado da conexão WAN/CORE")
        self.sync_pending = self.metrics.gauge("edge_sync_pending", "Eventos aguardando sincronização")
        self.sync_failures = self.metrics.counter("edge_sync_failures", "Falhas de sincronização")
        self.adapter_errors = self.metrics.counter("edge_adapter_errors", "Erros nos adaptadores")
        self.rule_errors = self.metrics.counter("edge_rule_execution_errors", "Erros no motor de regras")
        self.clock_healthy = self.metrics.gauge("edge_clock_healthy", "Saúde do relógio RTC/NTP do EDGE")
        self.clock_healthy.set(1.0)

        # Indicadores de dispositivos e comandos (§18.2, §18.3)
        self.device_messages = self.metrics.counter("terranet_device_messages_total", "Mensagens recebidas")
        self.commands_total = self.metrics.counter("terranet_commands_total", "Comandos por estado")
        self.interlock_blocks = self.metrics.counter("terranet_interlock_blocks_total", "Bloqueios por intertravamento")

    def collect(self, wan_connected: bool | None = None) -> dict[str, Any]:
        uptime_s = round(time.monotonic() - self._started_at, 2)
        self.uptime.set(uptime_s)
        self.mqtt_connected.set(1.0 if self.bus.connected else 0.0)
        if wan_connected is not None:
            self.wan_connected.set(1.0 if wan_connected else 0.0)
        pending = self.store.count_pending()
        self.sync_pending.set(pending)

        free_bytes = self._disk_free()
        self.storage_free.set(free_bytes)
        cpu = self._cpu_estimate()
        self.cpu_usage.set(cpu)
        mem = self._memory_bytes()
        self.memory_usage.set(mem)

        return {
            "edge_id": self.edge_id,
            "uptime_seconds": uptime_s,
            "cpu_usage": cpu,
            "memory_usage_bytes": mem,
            "storage_free_bytes": free_bytes,
            "mqtt_connected": self.bus.connected,
            "wan_connected": bool(self.wan_connected.get()),
            "sync_pending": pending,
            "outbox": self.store.outbox_stats(),
        }

    def _disk_free(self) -> int:
        try:
            target = Path(self.storage_path).resolve()
            if not target.exists():
                target = Path.cwd()
            return int(shutil.disk_usage(target).free)
        except OSError:
            return 0

    @staticmethod
    def _cpu_estimate() -> float:
        if hasattr(os, "getloadavg"):
            try:
                load1, _, _ = os.getloadavg()
                cpus = os.cpu_count() or 1
                return round(min(1.0, load1 / cpus), 4)
            except OSError:
                return 0.0
        return 0.0

    @staticmethod
    def _memory_bytes() -> int:
        try:
            import resource  # POSIX

            return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
        except ImportError:
            return 0

    def render_prometheus(self, wan_connected: bool | None = None) -> str:
        self.collect(wan_connected=wan_connected)
        return self.metrics.render()

    def start_http(self, host: str = "127.0.0.1", port: int = 9101) -> None:
        agent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/metrics":
                    body = agent.render_prometheus().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path in ("/healthz", "/readyz"):
                    snapshot = agent.collect()
                    body = json.dumps(snapshot).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                return None

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop_http(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
