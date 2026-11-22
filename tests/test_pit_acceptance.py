"""Suíte integrada de critérios de aceitação PIT-T01 a PIT-T15 (PIT-001 §25 e §33).

Exercita o MVP completo (H01, P01 e V01) integrando TERRANET EDGE e TERRANET CORE
via API HTTP (TestClient) com falhas controladas de rede, reinicialização e segurança.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from contracts.commands import Command, CommandState, derive_edge_key, sign_command
from contracts.devices import Device
from contracts.rules import Rule
from core.api.app import create_app
from core.storage.repository import CoreStore
from edge.adapters.lorawan.decoders import encode_level_v1
from edge.adapters.modbus.client import SimulatedPumpPLC
from edge.service import EdgeRuntime
from edge.synchronization.client import SyncTransport, TransportError


class CoreTestClientTransport(SyncTransport):
    """Conecta o SyncAgent do EDGE diretamente ao FastAPI TestClient do CORE,
    com chaves de injeção de falha (sem internet / perda após persistência)."""

    def __init__(self, client: TestClient, edge_token: str):
        self.client = client
        self.headers = {"Authorization": f"Bearer {edge_token}"}
        self.offline = False
        self.drop_after_commit = False

    def send_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        if self.offline:
            raise TransportError("Internet indisponível")
        resp = self.client.post("/api/v1/ingest/batches", json=batch, headers=self.headers)
        if resp.status_code != 200:
            raise TransportError(f"HTTP {resp.status_code}: {resp.text}", resp.status_code)
        if self.drop_after_commit:
            raise TransportError("Conexão perdida após persistência central e antes do ACK")
        return resp.json()

    def pull_commands(self, edge_id: str) -> list[dict[str, Any]]:
        if self.offline:
            raise TransportError("Internet indisponível")
        resp = self.client.get(f"/api/v1/edges/{edge_id}/commands", headers=self.headers)
        return resp.json().get("commands", []) if resp.status_code == 200 else []

    def pull_config(self, edge_id: str) -> dict[str, Any] | None:
        if self.offline:
            raise TransportError("Internet indisponível")
        resp = self.client.get(f"/api/v1/edges/{edge_id}/config", headers=self.headers)
        return resp.json() if resp.status_code == 200 else None

    def send_config_report(self, edge_id: str, report: dict[str, Any]) -> None:
        if not self.offline:
            self.client.post(
                f"/api/v1/edges/{edge_id}/config-reports", json=report, headers=self.headers
            )

    def send_heartbeat(self, edge_id: str, metrics: dict[str, Any]) -> None:
        if not self.offline:
            self.client.post(
                f"/api/v1/edges/{edge_id}/heartbeat", json=metrics, headers=self.headers
            )


def _bootstrap_environment(tmp_path):
    master_key = "pit-001-acceptance-master-key"
    core_db = f"sqlite:///{(tmp_path / 'core.db').as_posix()}"
    edge_db = f"sqlite:///{(tmp_path / 'edge.db').as_posix()}"

    core_store = CoreStore.open(core_db)
    core_store.create_tenant("CLIENTE-001", "Fazenda Modelo")
    core_store.create_tenant("CLIENTE-002", "Outro Cliente")
    core_store.create_site("FAZENDA-001", "CLIENTE-001", "Sede Hidrológica")
    core_store.create_site("FAZENDA-002", "CLIENTE-002", "Outra Fazenda")

    _, key_op1 = core_store.create_user("operador_c1", "CLIENTE-001", role="operator")
    _, key_view1 = core_store.create_user("viewer_c1", "CLIENTE-001", role="viewer")
    _, key_op2 = core_store.create_user("operador_c2", "CLIENTE-002", role="operator")
    _, edge_token = core_store.register_edge("EDGE-001", "CLIENTE-001", ["FAZENDA-001"])

    app = create_app(core_store, master_key=master_key)
    client = TestClient(app)
    transport = CoreTestClientTransport(client, edge_token)

    edge = EdgeRuntime(
        edge_id="EDGE-001",
        db_url=edge_db,
        edge_key=derive_edge_key(master_key, "EDGE-001"),
        transport=transport,
    )
    edge.start_mqtt_subscriptions()

    return {
        "master_key": master_key,
        "edge_db": edge_db,
        "core_store": core_store,
        "client": client,
        "transport": transport,
        "edge": edge,
        "hdr_op1": {"Authorization": f"Bearer {key_op1}"},
        "hdr_view1": {"Authorization": f"Bearer {key_view1}"},
        "hdr_op2": {"Authorization": f"Bearer {key_op2}"},
    }


def _register_and_activate(client: TestClient, headers: dict, profile: dict) -> dict:
    r = client.post("/api/v1/devices", json=profile, headers=headers)
    assert r.status_code == 201, r.text
    did = profile["device_id"]
    assert client.post(f"/api/v1/devices/{did}/transition", json={"state": "PROVISIONED"}, headers=headers).status_code == 200
    assert client.post(f"/api/v1/devices/{did}/transition", json={"state": "ACTIVE"}, headers=headers).status_code == 200
    return client.get(f"/api/v1/devices/{did}", headers=headers).json()


def test_pit_t01_to_t08_h01_lorawan_offline_sync_and_idempotence(tmp_path):
    """PIT-T01 a PIT-T08 + aplicação H01 (nível de rio, 2 fabricantes, regras offline, Outbox)."""
    env = _bootstrap_environment(tmp_path)
    client, edge, transport, hdr = env["client"], env["edge"], env["transport"], env["hdr_op1"]

    # PIT-T01: Cadastro de dois dispositivos de fabricantes diferentes
    _register_and_activate(
        client,
        hdr,
        {
            "device_id": "TN-NODE-H01-A",
            "site_id": "FAZENDA-001",
            "category": "NODE-S",
            "manufacturer": "FabricanteAlfa",
            "model": "HydroLora-1",
            "communication": {"type": "lorawan", "dev_eui": "0102030405060708"},
            "adapter": {"name": "lorawan_chirpstack", "version": "1.0"},
            "capabilities": ["water_level_measurement", "battery_monitoring"],
            "metadata": {
                "decoder": "chirpstack_object",
                "field_map": {
                    "water_level_m": {"field": "nivel_cm", "unit": "cm"},
                    "battery_voltage_v": {"field": "bat_mv", "unit": "mV"},
                },
                "ranges": {"water_level_m": [0, 20]},
            },
        },
    )
    _register_and_activate(
        client,
        hdr,
        {
            "device_id": "TN-NODE-H01-B",
            "site_id": "FAZENDA-001",
            "category": "NODE-S",
            "manufacturer": "FabricanteBeta",
            "model": "LevelBin-2",
            "communication": {"type": "lorawan", "dev_eui": "1122334455667788"},
            "adapter": {"name": "lorawan_chirpstack", "version": "1.0"},
            "capabilities": ["water_level_measurement", "battery_monitoring"],
            "metadata": {"decoder": "terranet_level_v1", "ranges": {"water_level_m": [0, 20]}},
        },
    )
    # Regra hidrológica H01 publicada via intenção de configuração do CORE
    rule_resp = client.post(
        "/api/v1/rules",
        json={
            "site_id": "FAZENDA-001",
            "rule": Rule(
                rule_id="R-H01-CHEIA",
                version=1,
                rule_type="threshold",
                device_ids=["TN-NODE-H01-A", "TN-NODE-H01-B"],
                severity="critical",
                params={"measurement": "water_level_m", "operator": ">", "value": 4.0, "hysteresis": 0.2},
            ).to_dict(),
        },
        headers=hdr,
    )
    assert rule_resp.status_code == 201

    cfg = edge.sync.pull_config()
    report = edge.apply_config_intention(cfg)
    assert report["status"] == "applied"
    assert edge.devices.find_by_source("lorawan", "0102030405060708").device_id == "TN-NODE-H01-A"

    # PIT-T06: Simula queda de internet -> processamento local e regras continuam operando
    transport.offline = True

    # PIT-T02 e PIT-T03: Uplinks ChirpStack via MQTT para os dois fabricantes
    uplink_a = {
        "deduplicationId": "up-a-001",
        "time": "2026-11-22T12:00:00Z",
        "deviceInfo": {"devEui": "0102030405060708"},
        "object": {"nivel_cm": 450, "bat_mv": 12700},
        "rxInfo": [{"rssi": -85, "snr": 8.0}],
    }
    uplink_b = {
        "deduplicationId": "up-b-001",
        "time": "2026-11-22T12:01:00Z",
        "deviceInfo": {"devEui": "1122334455667788"},
        "data": encode_level_v1(level_mm=2450, battery_mv=12500),
    }
    edge.bus.publish("application/app1/device/0102030405060708/event/up", uplink_a)
    # PIT-T04: Duplicação do mesmo uplink não cria segundo evento lógico
    edge.bus.publish("application/app1/device/0102030405060708/event/up", uplink_a)
    edge.bus.publish("application/app1/device/1122334455667788/event/up", uplink_b)

    # PIT-T05: Persistido localmente (2 telemetrias + 1 alarme local da regra R-H01-CHEIA)
    assert edge.store.count_events(event_type="telemetry") == 2
    assert edge.store.count_events(event_type="event") == 1
    assert edge.store.count_pending() == 3

    # Tentativa de sync sem internet mantém pendentes
    res_offline = edge.sync_now()
    assert res_offline[0].wan_connected is False
    assert edge.store.count_pending() == 3

    # PIT-T08: Internet volta mas conexão cai APÓS persistência no CORE e ANTES do ACK
    transport.offline = False
    transport.drop_after_commit = True
    future1 = datetime.now(UTC) + timedelta(minutes=10)
    edge.sync_now(now=future1)
    assert env["core_store"].count_events("CLIENTE-001") == 3
    assert edge.store.count_pending() == 3  # EDGE não confirmou porque perdeu o ACK

    # PIT-T07: Reconexão plena -> retransmite sem duplicar nenhum evento no CORE
    transport.drop_after_commit = False
    future2 = datetime.now(UTC) + timedelta(minutes=30)
    edge.sync_now(now=future2)
    assert env["core_store"].count_events("CLIENTE-001") == 3
    assert edge.store.count_pending() == 0

    # Verifica normalização idêntica no CORE
    events_core = client.get("/api/v1/telemetry", headers=hdr).json()
    by_dev = {e["device_id"]: e for e in events_core if e["event_type"] == "telemetry"}
    assert by_dev["TN-NODE-H01-A"]["measurements"]["water_level_m"] == 4.5
    assert by_dev["TN-NODE-H01-B"]["measurements"]["water_level_m"] == 2.45
    edge.close()


def test_pit_t09_v01_tracker_gnss(tmp_path):
    """PIT-T09 + aplicação V01: rastreador GNSS/LTE com ingestão direta no CORE."""
    env = _bootstrap_environment(tmp_path)
    client, hdr = env["client"], env["hdr_op1"]

    _register_and_activate(
        client,
        hdr,
        {
            "device_id": "TN-TRACK-V01",
            "site_id": "FAZENDA-001",
            "category": "TRACK-B",
            "manufacturer": "Teltonika",
            "model": "FMC130",
            "communication": {"type": "tracker", "imei": "861234567890123"},
            "adapter": {"name": "tracker_gnss_lte"},
        },
    )
    resp = client.post(
        "/api/v1/ingest/track",
        json={
            "imei": "861234567890123",
            "timestamp": "2026-11-22T14:00:00Z",
            "gnss": {"lat": -15.793889, "lon": -47.882778, "speed_kmh": 62.0, "satellites": 12},
            "io": {"ignition": True, "battery_mv": 4050},
        },
        headers=hdr,
    )
    assert resp.status_code == 200 and resp.json()["new_events"] == 1
    positions = client.get("/api/v1/geospatial/positions", headers=hdr).json()
    assert len(positions) == 1
    assert positions[0]["device_id"] == "TN-TRACK-V01"
    assert positions[0]["lat"] == -15.793889
    env["edge"].close()


def test_pit_t10_to_t12_p01_pump_commands_security_expiration_interlock(tmp_path):
    """PIT-T10, PIT-T11, PIT-T12 + aplicação P01 (bomba Modbus em bancada)."""
    env = _bootstrap_environment(tmp_path)
    client, edge, hdr_op, hdr_view = env["client"], env["edge"], env["hdr_op1"], env["hdr_view1"]

    _register_and_activate(
        client,
        hdr_op,
        {
            "device_id": "TN-PUMP-P01",
            "site_id": "FAZENDA-001",
            "category": "NODE-C",
            "manufacturer": "BancadaCLP",
            "model": "SimPLC-P01",
            "communication": {"type": "modbus", "identifier": "plc-p01", "unit_id": 1},
            "adapter": {"name": "modbus_plc"},
            "metadata": {"allowed_operations": ["start_pump", "stop_pump"]},
        },
    )
    edge.apply_config_intention(edge.sync.pull_config())
    edge.sync.send_heartbeat(edge.monitoring.collect(wan_connected=True))

    plc = SimulatedPumpPLC(pump_running=False, dry_run_interlock_ok=True)
    edge.attach_modbus_client("TN-PUMP-P01", plc)
    edge.poll_modbus("TN-PUMP-P01", poll_id="init-1")

    # PIT-T10a: Usuário sem permissão (viewer) bloqueado no CORE
    r_unauth = client.post(
        "/api/v1/commands",
        json={"device_id": "TN-PUMP-P01", "operation": "start_pump", "ttl_s": 30},
        headers=hdr_view,
    )
    assert r_unauth.status_code == 403

    # PIT-T10b: Comando forjado / assinatura inválida bloqueado no EDGE
    forged = Command(
        command_id="CMD-FORGED",
        tenant_id="CLIENTE-001",
        site_id="FAZENDA-001",
        device_id="TN-PUMP-P01",
        operation="start_pump",
        requested_by="atacante",
        issued_at="2026-11-22T12:00:00Z",
        expires_at="2026-11-22T12:05:00Z",
        configuration_version=1,
        signature="00" * 32,
    )
    st, _, _ = edge.execute_command(forged, now=datetime(2026, 11, 22, 12, 0, 10, tzinfo=UTC))
    assert st == CommandState.REJECTED and plc.physical_actuations == 0

    # PIT-T11: Comando expirado rejeitado sem acionar CLP
    expired = Command(
        command_id="CMD-EXPIRED",
        tenant_id="CLIENTE-001",
        site_id="FAZENDA-001",
        device_id="TN-PUMP-P01",
        operation="start_pump",
        requested_by="operador_c1",
        issued_at="2026-11-22T12:00:00Z",
        expires_at="2026-11-22T12:00:30Z",
        configuration_version=1,
    )
    expired.signature = sign_command(expired, derive_edge_key(env["master_key"], "EDGE-001"))
    st, _, _ = edge.execute_command(expired, now=datetime(2026, 11, 22, 12, 1, 0, tzinfo=UTC))
    assert st == CommandState.EXPIRED and plc.physical_actuations == 0

    # PIT-T12: Falha de intertravamento físico (proteção a seco) -> nenhuma execução física
    plc.dry_run_interlock_ok = False
    r_cmd1 = client.post(
        "/api/v1/commands",
        json={"device_id": "TN-PUMP-P01", "operation": "start_pump", "ttl_s": 60},
        headers=hdr_op,
    )
    assert r_cmd1.status_code == 201
    cmd1_id = r_cmd1.json()["command_id"]
    outcomes = edge.poll_and_execute_commands()
    assert outcomes == [(cmd1_id, CommandState.REJECTED, "interlock_failed: dry_run_protection")]
    assert plc.pump_running is False and plc.physical_actuations == 0

    # Restaura intertravamento -> comando autorizado aciona e confirma fisicamente (VERIFIED)
    plc.dry_run_interlock_ok = True
    r_cmd2 = client.post(
        "/api/v1/commands",
        json={"device_id": "TN-PUMP-P01", "operation": "start_pump", "ttl_s": 60},
        headers=hdr_op,
    )
    assert r_cmd2.status_code == 201
    cmd2_id = r_cmd2.json()["command_id"]
    outcomes2 = edge.poll_and_execute_commands()
    assert outcomes2[0][1] == CommandState.VERIFIED
    assert plc.pump_running is True and plc.physical_actuations == 1

    # Sincroniza command_result para o CORE e valida atualização do estado do comando
    edge.sync_now()
    assert client.get(f"/api/v1/commands/{cmd1_id}", headers=hdr_op).json()["state"] == "REJECTED"
    assert client.get(f"/api/v1/commands/{cmd2_id}", headers=hdr_op).json()["state"] == "VERIFIED"
    edge.close()


def test_pit_t13_t14_t15_restart_audit_and_tenant_isolation(tmp_path):
    """PIT-T13 (reinicialização), PIT-T14 (auditoria) e PIT-T15 (isolamento entre clientes)."""
    env = _bootstrap_environment(tmp_path)
    client, edge, transport = env["client"], env["edge"], env["transport"]
    hdr1, hdr2 = env["hdr_op1"], env["hdr_op2"]

    _register_and_activate(
        client,
        hdr1,
        {
            "device_id": "TN-NODE-RESTART",
            "site_id": "FAZENDA-001",
            "category": "NODE-S",
            "communication": {"type": "lorawan", "dev_eui": "a1b2c3d4e5f60708"},
            "adapter": {"name": "lorawan_chirpstack"},
        },
    )
    edge.apply_config_intention(edge.sync.pull_config())

    # Recebe dado offline e fecha o processo EDGE (simulando reinicialização — PIT-T13)
    transport.offline = True
    edge.ingest_raw(
        "lorawan",
        {
            "deduplicationId": "restart-evt-1",
            "time": "2026-11-22T15:00:00Z",
            "deviceInfo": {"devEui": "a1b2c3d4e5f60708"},
            "object": {"water_level_m": 3.12},
        },
    )
    assert edge.store.count_pending() == 1
    edge.close()

    # Novo processo EdgeRuntime sobre o mesmo banco em disco recupera pendentes e sincroniza
    transport.offline = False
    edge_restarted = EdgeRuntime(
        edge_id="EDGE-001",
        db_url=env["edge_db"],
        edge_key=derive_edge_key(env["master_key"], "EDGE-001"),
        transport=transport,
    )
    assert edge_restarted.devices.get("TN-NODE-RESTART") is not None
    assert edge_restarted.store.count_pending() == 1
    edge_restarted.sync_now()
    assert edge_restarted.store.count_pending() == 0
    assert env["core_store"].count_events("CLIENTE-001") == 1

    # Métricas Prometheus obrigatórias do EDGE presentes (§18.1)
    prom = edge_restarted.monitoring.render_prometheus(wan_connected=True)
    for metric_name in (
        "edge_uptime_seconds",
        "edge_cpu_usage",
        "edge_memory_usage",
        "edge_storage_free",
        "edge_mqtt_connected",
        "edge_wan_connected",
        "edge_sync_pending",
        "edge_sync_failures",
        "edge_adapter_errors",
        "edge_rule_execution_errors",
    ):
        assert metric_name in prom

    # PIT-T14: Auditoria registra operações relevantes no CORE e no EDGE
    audit_core = client.get("/api/v1/audit", headers=hdr1).json()
    actions = {a["action"] for a in audit_core}
    assert "device:register" in actions
    assert "device:transition" in actions
    assert len(edge_restarted.store.audit_entries()) >= 1

    # PIT-T15: Cliente 2 não acessa dispositivos, telemetria ou auditoria do Cliente 1
    assert client.get("/api/v1/devices/TN-NODE-RESTART", headers=hdr2).status_code == 403
    assert client.get("/api/v1/telemetry?device_id=TN-NODE-RESTART", headers=hdr2).status_code == 403
    assert client.get("/api/v1/audit", headers=hdr2).json() == []
    edge_restarted.close()
