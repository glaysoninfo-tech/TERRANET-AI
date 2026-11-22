"""Orquestrador do TERRANET EDGE (PIT-001 §5, §6, §12, §14, §15, §20).

Reúne em um runtime coeso:
* Device Registry local (`DeviceStore`)
* Armazenamento persistente + Transactional Outbox (`EdgeStore`)
* Adaptadores de protocolo (LoRaWAN/ChirpStack, Modbus, TRACK)
* Motor de regras local determinístico (`RulesEngine`)
* Validador e executor de comandos (`CommandValidator`)
* Mensageria local MQTT (`MessageBus`)
* Sincronização assíncrona com o CORE (`SyncAgent`)
* Observabilidade e saúde (`MonitoringAgent`)
* Aplicação controlada de intenções de configuração (§20.2)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

from common.device_store import DeviceStore
from common.metrics import Registry
from common.timeutil import now_iso, utc_now
from contracts.canonical import CanonicalEvent, EventType
from contracts.commands import Command, CommandState
from contracts.devices import TELEMETRY_ACCEPTING, Device
from contracts.rules import Rule
from contracts.topics import chirpstack_subscription, parse_chirpstack_topic
from edge.adapters import AdapterError, adapter_for_source, get_adapter
from edge.adapters.lorawan.downlink import ChirpStackDownlinkClient
from edge.adapters.modbus.adapter import ModbusAdapter
from edge.adapters.modbus.client import ModbusClient, create_modbus_client
from edge.commands.validator import CommandValidator
from edge.monitoring.agent import MonitoringAgent
from edge.mqtt.bus import InMemoryBus, MessageBus
from edge.rules.engine import RuleStore, RulesEngine
from edge.storage.repository import EdgeStore
from edge.synchronization.agent import SyncAgent, SyncResult
from edge.synchronization.client import SyncTransport

log = logging.getLogger("terranet.edge")


class EdgeRuntime:
    def __init__(
        self,
        edge_id: str,
        db_url: str,
        edge_key: bytes,
        transport: SyncTransport | None = None,
        bus: MessageBus | None = None,
        chirpstack_prefix: str = "",
        storage_path: str = ".",
    ):
        self.edge_id = edge_id
        self.chirpstack_prefix = chirpstack_prefix
        self.metrics = Registry()
        self.store = EdgeStore.open(db_url)
        self.devices = DeviceStore(self.store.db)
        self.rule_store = RuleStore(self.store.db)
        self.rules = RulesEngine(self.store, self.devices, self.rule_store)
        self.bus = bus or InMemoryBus()
        self.downlink = ChirpStackDownlinkClient(self.bus, prefix=chirpstack_prefix)
        self.modbus_adapter = ModbusAdapter()
        self.modbus_clients: dict[str, ModbusClient] = {}
        self._last_modbus_poll: dict[str, float] = {}
        self.commands = CommandValidator(
            edge_id=edge_id,
            edge_key=edge_key,
            store=self.store,
            devices=self.devices,
            modbus_clients=self.modbus_clients,
            modbus_adapter=self.modbus_adapter,
            downlink_client=self.downlink,
        )
        self.monitoring = MonitoringAgent(
            edge_id=edge_id,
            store=self.store,
            bus=self.bus,
            metrics=self.metrics,
            storage_path=storage_path,
        )
        self.sync: SyncAgent | None = (
            SyncAgent(edge_id=edge_id, store=self.store, transport=transport, metrics=self.metrics)
            if transport is not None
            else None
        )
        self.applied_config_version: int = 0

    def start_mqtt_subscriptions(self) -> None:
        """Assina tópicos de entrada do ChirpStack (camada de entrada -> adaptador -> TERRANET)."""
        pattern = chirpstack_subscription(self.chirpstack_prefix)
        self.bus.subscribe(pattern, self._on_chirpstack_message, qos=1)

    def _on_chirpstack_message(self, topic: str, payload: bytes) -> None:
        parsed = parse_chirpstack_topic(topic, prefix=self.chirpstack_prefix)
        if not parsed:
            return
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.monitoring.adapter_errors.inc(source="lorawan")
            self.store.dead_letter("mqtt_decode", str(exc), topic, payload.decode("utf-8", "replace"))
            return
        self.ingest_raw("lorawan", raw, context={"event": parsed["event"], "topic": topic})

    # -------------------------------------------------------------- ingestão
    def ingest_raw(
        self,
        source_type: str,
        raw: dict[str, Any],
        context: dict[str, Any] | None = None,
        received_at: str | None = None,
    ) -> list[CanonicalEvent]:
        """Fluxo completo: identificar -> Device Registry -> adaptador -> persistência ->
        outbox -> motor de regras -> publicação nos tópicos TERRANET."""
        recv_ts = received_at or now_iso()
        try:
            default_adapter = adapter_for_source(source_type)
            identifier = default_adapter.identify(raw)
        except AdapterError as exc:
            self.monitoring.adapter_errors.inc(source=source_type)
            self.store.dead_letter("identify", str(exc), source_type, raw)
            return []

        device = self.devices.find_by_source(source_type, identifier)
        if device is None:
            self.monitoring.adapter_errors.inc(source=source_type)
            self.store.dead_letter("registry", f"Dispositivo não cadastrado: {identifier}", source_type, raw)
            return []
        if device.operational_status not in TELEMETRY_ACCEPTING:
            self.store.dead_letter(
                "lifecycle",
                f"Dispositivo em estado {device.operational_status} não aceita telemetria",
                device.device_id,
                raw,
            )
            return []

        try:
            adapter = get_adapter(device.adapter_id)
            events = adapter.to_events(raw, device, recv_ts, context=context)
        except AdapterError as exc:
            self.monitoring.adapter_errors.inc(source=device.adapter_id)
            self.store.dead_letter("adapter", str(exc), device.device_id, raw)
            return []

        accepted: list[CanonicalEvent] = []
        for event in events:
            if self.process_canonical(event):
                accepted.append(event)
        return accepted

    def process_canonical(self, event: CanonicalEvent) -> bool:
        """Valida, persiste atomicamente com outbox, avalia regras locais e publica no MQTT."""
        event.processed_at = now_iso()
        event.validate()
        inserted = self.store.ingest(event)
        if not inserted:
            return False  # duplicado: mantém 1 ocorrência lógica (PIT-T04)

        self.devices.touch(event.device_id, event.observed_at)
        self.monitoring.device_messages.inc(device_id=event.device_id, event_type=event.event_type)
        if event.event_type == EventType.COMMAND_RESULT:
            self.commands.record_async_result(event)
            attrs = event.attributes or {}
            if attrs.get("state"):
                self.monitoring.commands_total.inc(
                    state=str(attrs["state"]), operation=str(attrs.get("operation", "unknown"))
                )
            if attrs.get("interlock_blocked"):
                self.monitoring.interlock_blocks.inc(device_id=event.device_id)
        self._publish_safe(event)

        # Regras locais determinísticas (§15)
        try:
            alarms = self.rules.evaluate_event(event)
        except Exception as exc:  # noqa: BLE001
            self.monitoring.rule_errors.inc()
            log.exception("Erro ao avaliar regras locais: %s", exc)
            alarms = []

        for alarm in alarms:
            if self.store.ingest(alarm):
                self._publish_safe(alarm)
        return True

    def poll_modbus(
        self,
        device_id: str,
        poll_id: str | None = None,
        observed_at: str | None = None,
    ) -> CanonicalEvent | None:
        device = self.devices.get(device_id)
        client = self.modbus_clients.get(device_id)
        if device is None or client is None:
            return None
        try:
            event = self.modbus_adapter.poll(
                device, client, poll_id=poll_id, observed_at=observed_at
            )
        except AdapterError as exc:
            self.monitoring.adapter_errors.inc(source="modbus")
            self.store.dead_letter("modbus_poll", str(exc), device_id, None)
            return None
        return event if self.process_canonical(event) else None

    def attach_modbus_client(self, device_id: str, client: ModbusClient) -> None:
        self.modbus_clients[device_id] = client

    def poll_all_modbus(self, now_mono: float | None = None) -> list[CanonicalEvent]:
        """Executa o agendador de polling Modbus (TCP ou RTU/RS-485) dos dispositivos ativos."""
        ts_mono = time.monotonic() if now_mono is None else now_mono
        emitted: list[CanonicalEvent] = []
        for dev in self.devices.list(source_type="modbus"):
            if dev.operational_status not in TELEMETRY_ACCEPTING:
                continue
            interval = float(dev.metadata.get("poll_interval_s", 15.0))
            last = self._last_modbus_poll.get(dev.device_id)
            if last is not None and (ts_mono - last) < interval:
                continue
            if dev.device_id not in self.modbus_clients:
                comm = dev.communication_profile or {}
                if comm.get("host") or comm.get("serial_port"):
                    try:
                        self.modbus_clients[dev.device_id] = create_modbus_client(comm)
                    except Exception as exc:  # noqa: BLE001
                        self.monitoring.adapter_errors.inc(source="modbus_connect")
                        self.store.dead_letter("modbus_connect", str(exc), dev.device_id, comm)
                        self._last_modbus_poll[dev.device_id] = ts_mono
                        continue
                else:
                    continue
            self._last_modbus_poll[dev.device_id] = ts_mono
            ev = self.poll_modbus(dev.device_id)
            if ev is not None:
                emitted.append(ev)
        return emitted

    @staticmethod
    def clock_healthy(min_year: int = 2025) -> bool:
        """Verifica se o RTC/NTP do EDGE não voltou para data de fábrica (1970/2020)."""
        return utc_now().year >= min_year

    def sync_config_if_needed(self) -> dict[str, Any] | None:
        """Busca intenção de configuração no CORE e aplica somente se a versão avançou (§20)."""
        if self.sync is None:
            return None
        cfg = self.sync.pull_config()
        if not cfg:
            return None
        intended = int(cfg.get("version", 0))
        if intended <= self.applied_config_version:
            return None
        return self.apply_config_intention(cfg)

    def check_no_data_rules(self, now: datetime | None = None) -> list[CanonicalEvent]:
        alarms = self.rules.evaluate_no_data(now=now)
        emitted: list[CanonicalEvent] = []
        for alarm in alarms:
            if self.store.ingest(alarm):
                self._publish_safe(alarm)
                emitted.append(alarm)
        return emitted

    # -------------------------------------------------------------- comandos
    def execute_command(
        self, command: Command, now: datetime | None = None
    ) -> tuple[CommandState, str, CanonicalEvent | None]:
        state, reason, result_event = self.commands.handle(command, now=now)
        self.monitoring.commands_total.inc(state=state.value, operation=command.operation)
        if result_event and result_event.attributes and result_event.attributes.get("interlock_blocked"):
            self.monitoring.interlock_blocks.inc(device_id=command.device_id)
        if result_event is not None:
            self._publish_safe(result_event)
        return state, reason, result_event

    def poll_and_execute_commands(self, now: datetime | None = None) -> list[tuple[str, CommandState, str]]:
        if self.sync is None:
            return []
        outcomes: list[tuple[str, CommandState, str]] = []
        for raw_cmd in self.sync.pull_commands():
            try:
                cmd = Command.from_dict(raw_cmd)
            except ValueError as exc:
                self.store.dead_letter("command_parse", str(exc), "core", raw_cmd)
                continue
            state, reason, _ = self.execute_command(cmd, now=now)
            outcomes.append((cmd.command_id, state, reason))
        return outcomes

    # --------------------------------------------------------- sincronização
    def sync_now(self, now: datetime | None = None) -> list[SyncResult]:
        if self.sync is None:
            return []
        return self.sync.drain(now=now)

    # ---------------------------------------------------------- configuração
    def apply_config_intention(self, config: dict[str, Any], actor: str = "core") -> dict[str, Any]:
        """Aplica intenção de configuração recebida do CORE (§20.2)."""
        version = int(config.get("version", 0))
        applied_devices: list[str] = []
        rejected_items: list[dict[str, str]] = []

        for raw_dev in config.get("devices") or []:
            try:
                dev = (
                    Device.from_profile(raw_dev)
                    if "communication" in raw_dev
                    else Device.from_dict(raw_dev)
                )
                self.devices.upsert(dev, actor=actor)
                applied_devices.append(dev.device_id)
            except Exception as exc:  # noqa: BLE001
                rejected_items.append({"device_id": str(raw_dev.get("device_id")), "reason": str(exc)})

        applied_rules: list[str] = []
        for raw_rule in config.get("rules") or []:
            try:
                rule = Rule.from_dict(raw_rule)
                ok, reason = self.rule_store.apply(rule, actor=actor, remote=True)
                if ok:
                    applied_rules.append(rule.rule_id)
                else:
                    rejected_items.append({"rule_id": rule.rule_id, "reason": reason})
            except Exception as exc:  # noqa: BLE001
                rejected_items.append({"rule_id": str(raw_rule.get("rule_id")), "reason": str(exc)})

        if not rejected_items and version > self.applied_config_version:
            self.applied_config_version = version
        report = {
            "edge_id": self.edge_id,
            "intended_version": version,
            "applied_version": self.applied_config_version,
            "status": "applied" if not rejected_items else "partial",
            "applied_devices": applied_devices,
            "applied_rules": applied_rules,
            "rejected": rejected_items,
            "reported_at": now_iso(),
        }
        self.store.audit(actor, "config_apply", self.edge_id, report["status"], report)
        if self.sync is not None:
            self.sync.send_config_report(report)
        return report

    def _publish_safe(self, event: CanonicalEvent) -> None:
        if not self.bus.connected:
            return
        try:
            self.bus.publish_canonical(event.to_dict())
        except Exception as exc:  # noqa: BLE001
            log.warning("Falha ao publicar evento no MQTT local: %s", exc)

    def close(self) -> None:
        self.monitoring.stop_http()
        self.bus.disconnect()
        for client in self.modbus_clients.values():
            client.close()
        self.store.db.close()
