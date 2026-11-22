"""Testes integrados dos 5 complementos de software da POC-001 (Gate 1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from contracts.commands import Command, CommandState, derive_edge_key, sign_command
from contracts.devices import Device
from contracts.rules import Rule
from core.api.app import create_app
from core.notifications.service import InMemoryNotifier, NotificationService
from core.poc_report import (
    NodeTest,
    SystemTest,
    decide_go_nogo,
    evaluate_poc,
)
from core.storage.repository import CoreStore
from core.track_gateway.codec8 import (
    CODEC_8,
    CODEC_8E,
    Codec8Error,
    decode_avl_packet,
    encode_avl_packet,
)
from core.track_gateway.tcp_server import TeltonikaTcpServer
from edge.adapters.lorawan.downlink import (
    SimulatedNodeCBridge,
    decode_node_c_downlink,
    encode_node_c_downlink,
)
from edge.adapters.modbus.client import SimulatedPumpPLC
from edge.service import EdgeRuntime


def test_p01_b_lorawan_downlink_node_c_bridge_and_interlocks(tmp_path):
    """P01-B: EDGE -> ChirpStack Downlink Classe C -> NODE-C -> Modbus RTU -> CLP -> Uplink ACK."""
    master_key = "gate1-master-key"
    edge_key = derive_edge_key(master_key, "EDGE-001")
    edge = EdgeRuntime(
        edge_id="EDGE-001",
        db_url=f"sqlite:///{(tmp_path / 'edge.db').as_posix()}",
        edge_key=edge_key,
    )
    edge.start_mqtt_subscriptions()

    # Cadastra NODE-C remoto falando com CLP via Modbus RTU
    dev = Device.from_profile(
        {
            "device_id": "TN-NODEC-P01",
            "tenant_id": "CLIENTE-001",
            "site_id": "FAZENDA-001",
            "category": "NODE-C",
            "manufacturer": "FabricanteNodeC",
            "model": "LoRaModbus-ClassC",
            "communication": {
                "type": "lorawan",
                "dev_eui": "cc11223344556677",
                "application_id": "app-poc",
                "unit_id": 1,
            },
            "adapter": {"name": "lorawan_node_c", "version": "1.0"},
            "operational_status": "ACTIVE",
            "metadata": {"allowed_operations": ["start_pump", "stop_pump"]},
        }
    )
    edge.devices.register(dev)

    plc = SimulatedPumpPLC(pump_running=False, dry_run_interlock_ok=False)
    bridge = SimulatedNodeCBridge(edge.bus, "app-poc", "cc11223344556677", plc)

    # Verifica codificação compacta (11 bytes -> cabe com folga no dwell time AU915)
    sample = encode_node_c_downlink("CMD-X", 1, 0, True, 30)
    assert len(sample) == 11
    assert decode_node_c_downlink(sample)["target_running"] is True

    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)

    def make_cmd(cmd_id: str, op: str, ttl_s: int = 30) -> Command:
        c = Command(
            command_id=cmd_id,
            tenant_id="CLIENTE-001",
            site_id="FAZENDA-001",
            device_id="TN-NODEC-P01",
            operation=op,
            requested_by="operador",
            issued_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=ttl_s)).isoformat(),
            configuration_version=1,
            correlation_id=f"corr-{cmd_id}",
        )
        c.signature = sign_command(c, edge_key)
        return c

    # 1. Intertravamento a seco ativo no CLP -> NODE-C retorna ACK REJECTED (interlock_blocked)
    st1, _, _ = edge.execute_command(make_cmd("CMD-NC-1", "start_pump"), now=now)
    assert st1 == CommandState.DISPATCHED
    assert bridge.downlinks_received == 1
    assert plc.pump_running is False and plc.physical_actuations == 0
    saved1 = edge.commands.get_command("CMD-NC-1")
    assert saved1 is not None and saved1["state"] == "REJECTED"
    assert "interlock" in saved1["reason"]

    # 2. Restaura água na sucção -> NODE-C aciona CLP e confirma VERIFIED via uplink fPort 20
    plc.dry_run_interlock_ok = True
    st2, _, _ = edge.execute_command(make_cmd("CMD-NC-2", "start_pump"), now=now)
    assert st2 == CommandState.DISPATCHED
    assert plc.pump_running is True and plc.physical_actuations == 1
    saved2 = edge.commands.get_command("CMD-NC-2")
    assert saved2 is not None and saved2["state"] == "VERIFIED"

    # 3. Comando expirado nem chega a publicar downlink no rádio
    st3, _, _ = edge.execute_command(
        make_cmd("CMD-NC-EXP", "stop_pump", ttl_s=10), now=now + timedelta(seconds=20)
    )
    assert st3 == CommandState.EXPIRED
    assert bridge.downlinks_received == 2
    edge.close()


def test_modbus_polling_scheduler_respects_interval(tmp_path):
    edge = EdgeRuntime(
        edge_id="EDGE-001",
        db_url=f"sqlite:///{(tmp_path / 'edge.db').as_posix()}",
        edge_key=b"k",
    )
    dev = Device.from_profile(
        {
            "device_id": "TN-PLC-RTU",
            "tenant_id": "CLIENTE-001",
            "site_id": "FAZENDA-001",
            "category": "NODE-C",
            "communication": {"type": "modbus_rtu", "identifier": "rtu-1", "unit_id": 1},
            "adapter": {"name": "modbus_plc"},
            "operational_status": "ACTIVE",
            "metadata": {"poll_interval_s": 30},
        }
    )
    edge.devices.register(dev)
    edge.attach_modbus_client("TN-PLC-RTU", SimulatedPumpPLC())

    assert len(edge.poll_all_modbus(now_mono=100.0)) == 1
    assert len(edge.poll_all_modbus(now_mono=110.0)) == 0  # dentro do intervalo de 30s
    assert len(edge.poll_all_modbus(now_mono=135.0)) == 1  # após 30s coleta novamente
    assert edge.clock_healthy() is True
    edge.close()


def test_teltonika_codec8e_can_j1939_delayed_and_traccar_webhook(tmp_path):
    """TRACK-B e TRACK-I: Codec 8/8E binário + CAN/J1939 + POC-T12 atrasado + Traccar webhook."""
    store = CoreStore.open(f"sqlite:///{(tmp_path / 'core.db').as_posix()}")
    store.create_tenant("CLIENTE-001", "Fazenda")
    store.create_site("FAZENDA-001", "CLIENTE-001", "Sede")
    _, op_key = store.create_user("op1", "CLIENTE-001", role="operator")

    # TRACK-I com CAN/J1939 (FMC250 referência POC-001 §4.3)
    store.devices.register(
        Device.from_profile(
            {
                "device_id": "TN-TRACK-I01",
                "tenant_id": "CLIENTE-001",
                "site_id": "FAZENDA-001",
                "category": "TRACK",
                "manufacturer": "Teltonika",
                "model": "FMC250",
                "communication": {"type": "tracker", "imei": "861234567890999"},
                "adapter": {"name": "tracker_gnss_lte"},
                "operational_status": "ACTIVE",
            }
        )
    )

    # 1. Codifica e decodifica lote Codec 8E com registro atrasado (buffer offline 1h atrás) + CAN
    old_ts_ms = int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000)
    pkt_8e = encode_avl_packet(
        [
            {
                "timestamp_ms": old_ts_ms,
                "gnss": {"lat": -22.12345, "lon": -51.39123, "alt_m": 480, "heading_deg": 120, "satellites": 12, "speed_kmh": 18},
                "io_raw": {
                    239: 1,       # ignition
                    66: 13800,    # external_mv
                    67: 4100,     # battery_mv
                    85: 1850,     # CAN engine RPM
                    89: 72,       # CAN fuel %
                    115: 845,     # CAN coolant 84.5 degC
                    103: 1420,    # CAN engine hours
                },
            }
        ],
        codec_id=CODEC_8E,
    )
    records = decode_avl_packet(pkt_8e)
    assert len(records) == 1
    assert records[0]["gnss"]["lat"] == pytest.approx(-22.12345, abs=1e-5)

    # Verifica rejeição de CRC corrompido
    corrupted = pkt_8e[:-1] + bytes([pkt_8e[-1] ^ 0xFF])
    with pytest.raises(Codec8Error, match="CRC-16"):
        decode_avl_packet(corrupted)

    # Também valida Codec 8 clássico
    pkt_8 = encode_avl_packet(
        [{"timestamp_ms": old_ts_ms + 60000, "gnss": {"lat": -22.124, "lon": -51.392, "speed_kmh": 22}, "io_raw": {239: 1}}],
        codec_id=CODEC_8,
    )
    assert len(decode_avl_packet(pkt_8)) == 1

    # Persiste via TeltonikaTcpServer.handle_avl_bytes (idempotente para POC-T12)
    gw = TeltonikaTcpServer(store)
    assert gw.handle_avl_bytes("861234567890999", pkt_8e) == 1
    assert gw.handle_avl_bytes("861234567890999", pkt_8e) == 1  # retransmissão sem duplicar

    events = store.query_events("CLIENTE-001", device_id="TN-TRACK-I01")
    assert len(events) == 1
    ev = events[0]
    assert ev["attributes"]["delayed"] is True
    assert ev["attributes"]["can_j1939"] is True
    assert ev["measurements"]["engine_rpm"] == 1850.0
    assert ev["measurements"]["coolant_temperature_c"] == 84.5
    assert ev["measurements"]["engine_hours_h"] == 1420.0

    # 2. Testa Traccar Webhook no CORE API (outro formato comercial, knots -> km/h)
    app = create_app(store, master_key="k", track_webhook_token="wh-secret")
    client = TestClient(app)
    traccar_payload = {
        "device": {"uniqueId": "861234567890999"},
        "position": {
            "latitude": -22.125,
            "longitude": -51.393,
            "fixTime": datetime.now(UTC).isoformat(),
            "speed": 10.0,  # 10 knots = 18.52 km/h
            "course": 90.0,
            "attributes": {"ignition": True, "battery": 4.12, "sat": 10},
        },
    }
    assert client.post("/api/v1/ingest/track/webhook", json=traccar_payload).status_code == 401
    r_ok = client.post(
        "/api/v1/ingest/track/webhook",
        json=traccar_payload,
        headers={"X-Track-Webhook-Token": "wh-secret"},
    )
    assert r_ok.status_code == 200 and r_ok.json()["new_events"] == 1
    latest = client.get(
        "/api/v1/telemetry?device_id=TN-TRACK-I01",
        headers={"Authorization": f"Bearer {op_key}"},
    ).json()[0]
    assert latest["measurements"]["speed_kmh"] == pytest.approx(18.52, abs=0.05)


def test_h01_four_level_bands_notifications_and_poc_report(tmp_path):
    """H01 4 níveis (§13.3) + Notificações + Relatório automático POC-E16 (§21, §25)."""
    edge = EdgeRuntime(
        edge_id="EDGE-001",
        db_url=f"sqlite:///{(tmp_path / 'edge.db').as_posix()}",
        edge_key=b"k",
    )
    for did, eui, mfg in (
        ("NODE-H01-A", "0102030405060708", "FabricanteA"),
        ("NODE-H01-B", "1122334455667788", "FabricanteB"),
    ):
        edge.devices.register(
            Device.from_profile(
                {
                    "device_id": did,
                    "tenant_id": "CLIENTE-001",
                    "site_id": "FAZENDA-001",
                    "category": "NODE-S",
                    "manufacturer": mfg,
                    "communication": {"type": "lorawan", "dev_eui": eui},
                    "adapter": {"name": "lorawan_chirpstack"},
                    "operational_status": "ACTIVE",
                }
            )
        )

    # Regra de 4 faixas conforme POC-001 §13.3
    bands_rule = Rule.from_dict(
        {
            "rule_id": "R-H01-BANDS",
            "version": 1,
            "rule_type": "bands",
            "device_ids": ["NODE-H01-A"],
            "params": {
                "measurement": "water_level_m",
                "hysteresis": 0.15,
                "levels": [
                    {"name": "ATENCAO", "min": 2.50, "severity": "info"},
                    {"name": "ALERTA", "min": 3.00, "severity": "warning"},
                    {"name": "CRITICO", "min": 3.50, "severity": "critical"},
                ],
            },
        }
    )
    assert edge.rule_store.apply(bands_rule)[0] is True

    def send_level(idx: int, lvl: float):
        edge.ingest_raw(
            "lorawan",
            {
                "deduplicationId": f"b-{idx}",
                "time": f"2026-11-22T12:{idx:02d}:00Z",
                "deviceInfo": {"devEui": "0102030405060708"},
                "object": {"water_level_m": lvl},
            },
        )

    send_level(1, 2.20)  # NORMAL -> sem evento
    send_level(2, 2.60)  # -> ATENCAO
    send_level(3, 3.10)  # -> ALERTA (warning)
    send_level(4, 3.60)  # -> CRITICO (critical)
    send_level(5, 3.40)  # dentro da histerese (3.50 - 0.15 = 3.35) -> mantém CRITICO
    send_level(6, 3.20)  # desce para ALERTA

    alarms = [
        edge.store.get_event(r["event_id"])
        for r in edge.store.db.query(
            "SELECT event_id FROM telemetry_events WHERE event_type = 'event' ORDER BY observed_at"
        )
    ]
    bands_seq = [a["attributes"]["details"]["band"] for a in alarms]
    assert bands_seq == ["ATENCAO", "ALERTA", "CRITICO", "ALERTA"]

    # Notificação automática no CORE para warning/critical
    core_store = CoreStore.open(f"sqlite:///{(tmp_path / 'core.db').as_posix()}")
    core_store.create_tenant("CLIENTE-001", "Fazenda")
    core_store.create_site("FAZENDA-001", "CLIENTE-001", "Sede")
    _, op_key = core_store.create_user("op1", "CLIENTE-001", role="operator")
    _, edge_tok = core_store.register_edge("EDGE-001", "CLIENTE-001", ["FAZENDA-001"])
    for dev in edge.devices.list():
        core_store.devices.register(dev)

    notifier_ch = InMemoryNotifier()
    app = create_app(
        core_store,
        master_key="k",
        notifications=NotificationService([notifier_ch], cooldown_s=0.0),
    )
    client = TestClient(app)
    pending = edge.store.pending(limit=100)
    r_ing = client.post(
        "/api/v1/ingest/batches",
        json={"edge_id": "EDGE-001", "batch_id": "B1", "events": pending},
        headers={"Authorization": f"Bearer {edge_tok}"},
    )
    assert r_ing.status_code == 200 and len(r_ing.json()["accepted"]) == len(pending)
    # 3 notificações: ALERTA (warning), CRITICO (critical), ALERTA (warning); ATENCAO é info
    assert len(notifier_ch.sent) == 3

    # Avaliador POC-001 §21 + endpoint /api/v1/poc/report
    synthetic = evaluate_poc(
        [NodeTest("A", 672, 660), NodeTest("B", 672, 651)],
        SystemTest(24, 50, 75, 288, 288, 0, 0, 3, 2),
    )
    assert decide_go_nogo(synthetic) == "GO"

    unsafe = evaluate_poc(
        [NodeTest("A", 672, 660)],
        SystemTest(24, 50, 75, 288, 288, 1, 0, 3, 2),  # 1 comando expirado executado -> NO-GO
    )
    assert decide_go_nogo(unsafe) == "NO-GO"

    rep_api = client.get(
        "/api/v1/poc/report?expected_messages=6&offline_hours=24&gateway_autonomy_hours=50&node_autonomy_hours=75",
        headers={"Authorization": f"Bearer {op_key}"},
    )
    assert rep_api.status_code == 200
    body = rep_api.json()
    assert body["summary"]["independent_node_manufacturers"] == 2
    assert any(n["device_id"] == "NODE-H01-A" and n["pdr_pct"] == 100.0 for n in body["nodes"])
    edge.close()
