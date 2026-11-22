"""Ponto de entrada CLI do TERRANET EDGE."""

from __future__ import annotations

import logging
import os
import signal
import time

from contracts.commands import derive_edge_key
from edge.mqtt.bus import InMemoryBus, PahoMqttBus
from edge.service import EdgeRuntime
from edge.synchronization.client import HttpSyncTransport

logging.basicConfig(
    level=os.environ.get("TERRANET_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("terranet.edge.main")


def main() -> None:
    edge_id = os.environ.get("TERRANET_EDGE_ID", "EDGE-001")
    db_url = os.environ.get("TERRANET_EDGE_DB_URL", "sqlite:///data/edge.db")
    core_url = os.environ.get("TERRANET_CORE_URL", "")
    edge_token = os.environ.get("TERRANET_EDGE_TOKEN", "")
    master_key = os.environ.get("TERRANET_COMMAND_KEY") or os.environ.get(
        "TERRANET_MASTER_KEY", "dev-insecure-change-me"
    )
    mqtt_host = os.environ.get("TERRANET_MQTT_HOST", "")
    mqtt_port = int(os.environ.get("TERRANET_MQTT_PORT", "1883"))
    metrics_port = int(os.environ.get("TERRANET_EDGE_METRICS_PORT", "9101"))
    sync_interval = float(os.environ.get("TERRANET_SYNC_INTERVAL_S", "5"))
    chirpstack_prefix = os.environ.get("TERRANET_CHIRPSTACK_PREFIX", "")

    edge_key = derive_edge_key(master_key, edge_id)
    transport = HttpSyncTransport(core_url, edge_token) if core_url and edge_token else None
    bus = (
        PahoMqttBus(
            host=mqtt_host,
            port=mqtt_port,
            client_id=f"terranet-{edge_id}",
            username=os.environ.get("TERRANET_MQTT_USER"),
            password=os.environ.get("TERRANET_MQTT_PASSWORD"),
        )
        if mqtt_host
        else InMemoryBus()
    )

    runtime = EdgeRuntime(
        edge_id=edge_id,
        db_url=db_url,
        edge_key=edge_key,
        transport=transport,
        bus=bus,
        chirpstack_prefix=chirpstack_prefix,
    )
    if mqtt_host:
        bus.connect()
    runtime.start_mqtt_subscriptions()
    runtime.monitoring.start_http(host="0.0.0.0", port=metrics_port)  # noqa: S104
    log.info("TERRANET EDGE '%s' iniciado (metrics :%s)", edge_id, metrics_port)

    running = True

    def _stop(*_: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        config_interval = float(os.environ.get("TERRANET_CONFIG_POLL_S", "60"))
        last_config_poll = 0.0
        while running:
            if not runtime.clock_healthy():
                log.error("Relógio do sistema inválido (RTC/NTP); comandos remotos suspensos")
            runtime.poll_all_modbus()
            if runtime.sync is not None:
                runtime.sync_now()
                if runtime.clock_healthy():
                    runtime.poll_and_execute_commands()
                if (time.monotonic() - last_config_poll) >= config_interval:
                    runtime.sync_config_if_needed()
                    last_config_poll = time.monotonic()
                snapshot = runtime.monitoring.collect(wan_connected=runtime.sync.wan_connected)
                runtime.sync.send_heartbeat(snapshot)
            runtime.check_no_data_rules()
            time.sleep(sync_interval)
    finally:
        runtime.close()
        if transport is not None:
            transport.close()
        log.info("TERRANET EDGE '%s' encerrado.", edge_id)


if __name__ == "__main__":
    main()
