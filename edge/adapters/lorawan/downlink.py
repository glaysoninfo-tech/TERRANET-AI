"""Downlink LoRaWAN (ChirpStack Classe C) e ponte NODE-C -> Modbus RTU -> CLP (ARQ-002 §6).

Caminho P01-B (bomba remota):
    TERRANET EDGE -> ChirpStack MQTT Downlink -> NODE-C -> Modbus RTU -> CLP -> Intertravamentos
    CLP -> NODE-C -> Uplink de confirmação (fPort 20) -> TERRANET EDGE -> command_result

Nota regulatória (RP002-1.0.5 / ARQ-002 Ajuste #1):
    Na região AU915, o limite de dwell time (400 ms) depende da regulamentação local e
    da direção do enlace — não deve ser tratado como regra universal. O frame abaixo é
    mantido compacto (11 bytes) por eficiência de tempo-no-ar, mas taxa de dados (DR),
    conformidade Anatel e TTL por operação devem ser validados com medição real da
    latência ponta a ponta em bancada.

Formato binário:
* Downlink (11 bytes, big-endian):
    [0]     0xC1 (magic downlink NODE-C)
    [1..4]  tag de 4 bytes (SHA-256 prefix do command_id)
    [5]     Modbus unit_id (uint8)
    [6]     coil_address (uint8)
    [7]     action (1 = start_pump / ligar, 0 = stop_pump / desligar)
    [8]     ttl_remaining_s (uint8, saturado em 255 s)
    [9..10] CRC-16/MODBUS (uint16)
* Uplink de confirmação (9 bytes, big-endian, fPort = 20):
    [0]     0xC2 (magic ack NODE-C)
    [1..4]  tag de 4 bytes (eco do command_id)
    [5]     status_code (0=VERIFIED, 1=INTERLOCK_BLOCKED, 2=MODBUS_ERROR, 3=EXPIRED, 4=REJECTED)
    [6]     observed_bits (bit0=pump_running, bit1=remote_ok, bit2=dry_run_ok, bit3=overload, bit4=estop, bit5=loto)
    [7..8]  CRC-16/MODBUS (uint16)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import struct
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from common.timeutil import now_iso, parse_ts, utc_now
from contracts.canonical import CanonicalEvent, EventType, TimestampSource
from contracts.commands import Command, CommandState
from contracts.devices import Device
from contracts.topics import chirpstack_downlink_topic
from edge.adapters import AdapterError, register_adapter
from edge.adapters.lorawan.chirpstack import ChirpStackAdapter
from edge.adapters.modbus.client import ModbusClient, ModbusTransportError
from edge.mqtt.bus import MessageBus

DOWNLINK_MAGIC = 0xC1
ACK_MAGIC = 0xC2
NODE_C_FPORT = 20

STATUS_VERIFIED = 0
STATUS_INTERLOCK_BLOCKED = 1
STATUS_MODBUS_ERROR = 2
STATUS_EXPIRED = 3
STATUS_REJECTED = 4

_STATUS_MAP: dict[int, tuple[CommandState, str, str, bool]] = {
    STATUS_VERIFIED: (
        CommandState.VERIFIED,
        CommandState.PHYSICAL_STATE_VERIFIED.value,
        "physical_state_verified_via_node_c",
        False,
    ),
    STATUS_INTERLOCK_BLOCKED: (
        CommandState.REJECTED,
        CommandState.PLC_REJECTED.value,
        "interlock_failed: blocked_at_plc_via_node_c",
        True,
    ),
    STATUS_MODBUS_ERROR: (
        CommandState.FAILED,
        CommandState.EXECUTION_FAILED.value,
        "node_c_modbus_rtu_error",
        False,
    ),
    STATUS_EXPIRED: (
        CommandState.EXPIRED,
        CommandState.EXPIRED.value,
        "expired_at_node_c",
        False,
    ),
    STATUS_REJECTED: (
        CommandState.REJECTED,
        CommandState.PLC_REJECTED.value,
        "rejected_by_node_c",
        False,
    ),
}


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc & 0xFFFF


def command_tag(command_id: str) -> bytes:
    return hashlib.sha256(command_id.encode("utf-8")).digest()[:4]


def encode_node_c_downlink(
    command_id: str,
    unit_id: int,
    coil_address: int,
    target_running: bool,
    ttl_remaining_s: int,
) -> bytes:
    body = struct.pack(
        ">B4sBBBB",
        DOWNLINK_MAGIC,
        command_tag(command_id),
        unit_id & 0xFF,
        coil_address & 0xFF,
        1 if target_running else 0,
        max(0, min(255, int(ttl_remaining_s))),
    )
    return body + struct.pack(">H", crc16_modbus(body))


def decode_node_c_downlink(frame: bytes) -> dict[str, Any]:
    if len(frame) != 11 or frame[0] != DOWNLINK_MAGIC:
        raise AdapterError("Frame downlink NODE-C inválido")
    expected_crc = crc16_modbus(frame[:9])
    actual_crc = struct.unpack(">H", frame[9:11])[0]
    if expected_crc != actual_crc:
        raise AdapterError("CRC-16 inválido no downlink NODE-C")
    _, tag, unit_id, coil_addr, action, ttl_s = struct.unpack(">B4sBBBB", frame[:9])
    return {
        "tag": tag,
        "unit_id": unit_id,
        "coil_address": coil_addr,
        "target_running": bool(action),
        "ttl_remaining_s": ttl_s,
    }


def encode_node_c_ack(tag: bytes, status_code: int, observed_bits: int) -> bytes:
    body = struct.pack(">B4sBB", ACK_MAGIC, tag, status_code & 0xFF, observed_bits & 0xFF)
    return body + struct.pack(">H", crc16_modbus(body))


def decode_node_c_ack(frame: bytes) -> dict[str, Any]:
    if len(frame) != 9 or frame[0] != ACK_MAGIC:
        raise AdapterError("Frame de confirmação NODE-C inválido")
    expected_crc = crc16_modbus(frame[:7])
    actual_crc = struct.unpack(">H", frame[7:9])[0]
    if expected_crc != actual_crc:
        raise AdapterError("CRC-16 inválido na confirmação NODE-C")
    _, tag, status_code, bits = struct.unpack(">B4sBB", frame[:7])
    return {
        "tag": tag,
        "status_code": status_code,
        "observed_state": {
            "pump_running_state": bool(bits & 0x01),
            "remote_mode_state": bool(bits & 0x02),
            "dry_run_ok_state": bool(bits & 0x04),
            "overload_fault_state": bool(bits & 0x08),
            "emergency_stop_state": bool(bits & 0x10),
        },
    }


class ChirpStackDownlinkClient:
    """Enfileira downlinks no ChirpStack v4 via MQTT (nunca retidos)."""

    def __init__(self, bus: MessageBus, prefix: str = ""):
        self.bus = bus
        self.prefix = prefix

    def enqueue(
        self,
        application_id: str,
        dev_eui: str,
        payload: bytes,
        f_port: int = NODE_C_FPORT,
        confirmed: bool = True,
    ) -> str:
        topic = chirpstack_downlink_topic(application_id, dev_eui, prefix=self.prefix)
        envelope = {
            "devEui": dev_eui.lower(),
            "confirmed": confirmed,
            "fPort": int(f_port),
            "data": base64.b64encode(payload).decode("ascii"),
        }
        # Comandos físicos NUNCA são retidos no MQTT (§10.3)
        self.bus.publish(topic, envelope, qos=1, retain=False)
        return topic


class NodeCBridgeAdapter(ChirpStackAdapter):
    """Adaptador LoRaWAN bidirecional para NODE-C (ponte LoRaWAN <-> Modbus RTU <-> CLP)."""

    adapter_id = "lorawan_node_c"
    version = "1.0"
    source_type = "lorawan"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending_by_tag: dict[bytes, dict[str, Any]] = {}

    def register_pending(self, command: Command) -> bytes:
        tag = command_tag(command.command_id)
        with self._lock:
            self._pending_by_tag[tag] = {
                "command_id": command.command_id,
                "correlation_id": command.correlation_id,
                "operation": command.operation,
                "dispatched_mono": time.monotonic(),
            }
        return tag

    def pop_pending(self, tag: bytes) -> dict[str, Any] | None:
        with self._lock:
            return self._pending_by_tag.pop(tag, None)

    def send_command(
        self,
        command: Command,
        device: Device,
        downlink: ChirpStackDownlinkClient,
        now: datetime | None = None,
    ) -> tuple[CommandState, str]:
        if command.operation not in ("start_pump", "stop_pump"):
            return CommandState.REJECTED, f"Operação NODE-C não suportada: {command.operation}"
        now_dt = now or utc_now()
        remaining = int((parse_ts(command.expires_at) - now_dt).total_seconds())
        if remaining <= 0:
            return CommandState.EXPIRED, "command_expired"

        app_id = str(device.communication_profile.get("application_id", "app1"))
        unit_id = int(device.communication_profile.get("unit_id", 1))
        coil_addr = int(device.metadata.get("command_coil_address", 0))
        frame = encode_node_c_downlink(
            command_id=command.command_id,
            unit_id=unit_id,
            coil_address=coil_addr,
            target_running=(command.operation == "start_pump"),
            ttl_remaining_s=remaining,
        )
        self.register_pending(command)
        try:
            downlink.enqueue(
                application_id=app_id,
                dev_eui=device.source_identifier,
                payload=frame,
                f_port=int(device.metadata.get("command_f_port", NODE_C_FPORT)),
                confirmed=True,
            )
        except Exception as exc:  # noqa: BLE001
            self.pop_pending(command_tag(command.command_id))
            return CommandState.FAILED, f"Falha ao publicar downlink ChirpStack: {exc}"
        return CommandState.DISPATCHED, "dispatched_via_lorawan_node_c"

    def to_events(
        self,
        raw: dict[str, Any],
        device: Device,
        received_at: str,
        context: dict[str, Any] | None = None,
    ) -> list[CanonicalEvent]:
        context = context or {}
        cmd_port = int(device.metadata.get("command_f_port", NODE_C_FPORT))
        if context.get("event", "up") == "up" and raw.get("fPort") == cmd_port and raw.get("data"):
            try:
                frame = base64.b64decode(raw["data"], validate=True)
            except (binascii.Error, ValueError) as exc:
                raise AdapterError("Payload base64 inválido no ACK NODE-C") from exc
            ack = decode_node_c_ack(frame)
            pending = self.pop_pending(ack["tag"])
            cmd_id = pending["command_id"] if pending else f"tag:{ack['tag'].hex()}"
            corr_id = pending["correlation_id"] if pending else ""
            op = pending["operation"] if pending else "unknown"
            latency_ms = (
                round((time.monotonic() - float(pending["dispatched_mono"])) * 1000.0, 2)
                if pending and "dispatched_mono" in pending
                else None
            )
            state, arq2_state, reason, interlock_blocked = _STATUS_MAP.get(
                ack["status_code"],
                (
                    CommandState.UNKNOWN,
                    CommandState.UNKNOWN.value,
                    f"unknown_node_c_status:{ack['status_code']}",
                    False,
                ),
            )
            observed_at, ts_source, _ = self._resolve_ack_time(raw.get("time"), received_at)
            attrs: dict[str, Any] = {
                "command_id": cmd_id,
                "correlation_id": corr_id,
                "operation": op,
                "state": state.value,
                "arq2_state": arq2_state,
                "reason": reason,
                "interlock_blocked": interlock_blocked,
                "observed_state": ack["observed_state"],
            }
            if latency_ms is not None:
                attrs["latency_ms"] = latency_ms
            event = CanonicalEvent(
                event_id=f"cmdres:{cmd_id}:{state.value.lower()}",
                tenant_id=device.tenant_id,
                site_id=device.site_id,
                device_id=device.device_id,
                event_type=EventType.COMMAND_RESULT.value,
                observed_at=observed_at,
                received_at=received_at,
                measurements={},
                quality={"timestamp_source": ts_source},
                source=self.source(
                    network_server="chirpstack",
                    transport="lorawan_class_c",
                    f_port=cmd_port,
                ),
                attributes=attrs,
            )
            return [event]
        return super().to_events(raw, device, received_at, context=context)

    @staticmethod
    def _resolve_ack_time(candidate: Any, received_at: str) -> tuple[str, str, bool]:
        from edge.normalization import DEFAULT_NORMALIZER

        return DEFAULT_NORMALIZER.resolve_timestamp(
            candidate, received_at, TimestampSource.NETWORK_SERVER
        )


class SimulatedNodeCBridge:
    """Simulador de bancada do NODE-C (recebe downlink ChirpStack, atua no CLP e envia uplink ACK)."""

    def __init__(
        self,
        bus: MessageBus,
        application_id: str,
        dev_eui: str,
        plc: ModbusClient,
        prefix: str = "",
        f_port: int = NODE_C_FPORT,
        transit_delay_s: float = 0.0,
    ):
        self.bus = bus
        self.application_id = application_id
        self.dev_eui = dev_eui.lower()
        self.plc = plc
        self.prefix = prefix
        self.f_port = f_port
        self.transit_delay_s = transit_delay_s
        self.downlinks_received = 0
        topic = chirpstack_downlink_topic(application_id, self.dev_eui, prefix=prefix)
        self.bus.subscribe(topic, self._on_downlink, qos=1)

    def _on_downlink(self, topic: str, payload: bytes) -> None:
        self.downlinks_received += 1
        msg = json.loads(payload.decode("utf-8"))
        frame = base64.b64decode(msg["data"])
        decoded = decode_node_c_downlink(frame)
        tag = decoded["tag"]
        unit_id = decoded["unit_id"]
        coil = decoded["coil_address"]
        target = decoded["target_running"]

        # AUD-001 §6.1: rejeita comando que expirou durante o trânsito pelo rádio
        if decoded["ttl_remaining_s"] <= self.transit_delay_s:
            self._publish_ack(tag, STATUS_EXPIRED, 0)
            return

        try:
            self.plc.read_coils(0, 5, unit_id=unit_id)
            accepted = self.plc.write_coil(coil, target, unit_id=unit_id)
            coils_post = self.plc.read_coils(0, 5, unit_id=unit_id)
        except ModbusTransportError:
            self._publish_ack(tag, STATUS_MODBUS_ERROR, 0)
            return

        bits = sum((1 << i) for i, val in enumerate(coils_post[:5]) if val)
        if not accepted:
            self._publish_ack(tag, STATUS_INTERLOCK_BLOCKED, bits)
        elif bool(coils_post[0]) == target:
            self._publish_ack(tag, STATUS_VERIFIED, bits)
        else:
            self._publish_ack(tag, STATUS_INTERLOCK_BLOCKED if target else STATUS_REJECTED, bits)

    def _publish_ack(self, tag: bytes, status_code: int, bits: int) -> None:
        ack_frame = encode_node_c_ack(tag, status_code, bits)
        up_topic = f"{self.prefix}application/{self.application_id}/device/{self.dev_eui}/event/up"
        self.bus.publish(
            up_topic,
            {
                "deduplicationId": f"nodec-ack-{uuid.uuid4().hex[:10]}",
                "time": now_iso(),
                "deviceInfo": {"devEui": self.dev_eui},
                "fPort": self.f_port,
                "data": base64.b64encode(ack_frame).decode("ascii"),
                "rxInfo": [{"rssi": -82, "snr": 8.5}],
            },
            qos=1,
            retain=False,
        )


NODE_C_ADAPTER = register_adapter(NodeCBridgeAdapter())
