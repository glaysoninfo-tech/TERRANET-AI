"""Testes de conformidade com os 6 deltas do ARQ-002 v2.0 e os 4 ajustes obrigatórios."""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from contracts.assets import Asset, ControlMode, RegulatoryRule, resolve_control_mode
from contracts.commands import (
    Command,
    CommandState,
    can_transition,
    derive_edge_key,
    sign_command,
    verify_command_keyring,
)
from contracts.devices import Device
from core.api.app import create_app
from core.poc_report import EnergyGate, build_poc_report_from_store, evaluate_energy_gate
from core.storage.repository import CoreStore
from core.track_gateway.codec8 import CODEC_8E, encode_avl_packet
from core.track_gateway.tcp_server import TeltonikaTcpServer
from edge.adapters.lorawan.downlink import SimulatedNodeCBridge
from edge.adapters.modbus.client import SimulatedPumpPLC
from edge.service import EdgeRuntime


def test_delta1_schemas_and_hmac_key_rotation():
    """ADR-015: todos os JSON Schemas normativos existem/são válidos e HMAC suporta rotação via key_id."""
    schemas_dir = pathlib.Path("contracts/schemas")
    expected = {
        "telemetry_event.v1.json",
        "position.v1.json",
        "command.v1.json",
        "command_result.v1.json",
        "device_state.v1.json",
        "audit_event.v1.json",
        "ingest_batch.v1.json",
        "device_profile.v1.json",
        "rule.v1.json",
    }
    present = {p.name for p in schemas_dir.glob("*.json")}
    assert expected.issubset(present)
    for p in schemas_dir.glob("*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        assert "$schema" in data

    # Rotação de chaves k1 -> k2 sem indisponibilidade (§3.3)
    k1 = derive_edge_key("master", "EDGE-001", key_id="k1")
    k2 = derive_edge_key("master", "EDGE-001", key_id="k2")
    assert k1 != k2
    keyring = {"k1": k1, "k2": k2}

    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    cmd = Command(
        command_id="CMD-ROT-1",
        tenant_id="C1",
        site_id="S1",
        device_id="P1",
        operation="start_pump",
        requested_by="op",
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=30)).isoformat(),
        configuration_version=1,
        key_id="k2",
    )
    cmd.signature = sign_command(cmd, k2)
    assert verify_command_keyring(cmd, keyring) is True
    assert verify_command_keyring(cmd, {"k1": k1}) is False

    # Estados estendidos ARQ-002 §6.3
    assert can_transition(CommandState.QUEUED, CommandState.TRANSMITTED)
    assert can_transition(CommandState.PLC_EXECUTED, CommandState.PHYSICAL_STATE_VERIFIED)
    assert not can_transition(CommandState.PHYSICAL_STATE_VERIFIED, CommandState.PLC_EXECUTED)


def test_delta2_p01_b_records_end_to_end_latency_and_arq2_state(tmp_path):
    """ARQ-002 §6.3 / §6.5: P01-B mede latência ponta a ponta e reporta PHYSICAL_STATE_VERIFIED."""
    edge_key = derive_edge_key("master", "EDGE-001")
    edge = EdgeRuntime("EDGE-001", f"sqlite:///{(tmp_path / 'e.db').as_posix()}", edge_key)
    edge.start_mqtt_subscriptions()
    edge.devices.register(
        Device.from_profile(
            {
                "device_id": "TN-NODEC-LAT",
                "tenant_id": "C1",
                "site_id": "S1",
                "category": "NODE-C",
                "communication": {"type": "lorawan", "dev_eui": "aa00112233445566", "application_id": "app1"},
                "adapter": {"name": "lorawan_node_c"},
                "operational_status": "ACTIVE",
                "metadata": {"allowed_operations": ["start_pump"]},
            }
        )
    )
    plc = SimulatedPumpPLC(pump_running=False)
    SimulatedNodeCBridge(edge.bus, "app1", "aa00112233445566", plc)

    now = datetime.now(UTC)
    cmd = Command(
        command_id="CMD-LAT-1",
        tenant_id="C1",
        site_id="S1",
        device_id="TN-NODEC-LAT",
        operation="start_pump",
        requested_by="op",
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=30)).isoformat(),
        configuration_version=1,
    )
    cmd.signature = sign_command(cmd, edge_key)
    edge.execute_command(cmd, now=now)

    res = edge.store.get_event("cmdres:CMD-LAT-1:verified")
    assert res is not None
    assert res["attributes"]["arq2_state"] == "PHYSICAL_STATE_VERIFIED"
    assert res["attributes"]["latency_ms"] >= 0.0
    edge.close()


def test_delta3_time_health_clock_jump_blocks_commands_and_graceful_shutdown(tmp_path):
    """ARQ-002 §10.4 e §11.1: salto de relógio bloqueia comandos remotos; shutdown limpa WAL/auditoria."""
    edge_key = derive_edge_key("master", "EDGE-001")
    edge = EdgeRuntime("EDGE-001", f"sqlite:///{(tmp_path / 'e.db').as_posix()}", edge_key)
    edge.devices.register(
        Device.from_profile(
            {
                "device_id": "TN-PUMP-CLK",
                "tenant_id": "C1",
                "site_id": "S1",
                "category": "NODE-C",
                "communication": {"type": "modbus", "identifier": "plc-clk"},
                "adapter": {"name": "modbus_plc"},
                "operational_status": "ACTIVE",
                "metadata": {"allowed_operations": ["start_pump"]},
            }
        )
    )
    plc = SimulatedPumpPLC(pump_running=False)
    edge.attach_modbus_client("TN-PUMP-CLK", plc)

    t0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    assert edge.time_health.check(now_utc=t0, now_mono=100.0).healthy is True
    # Salto abrupto de +5 minutos em 1 segundo monotônico -> relógio não confiável!
    t_jump = t0 + timedelta(minutes=5)
    st_jump = edge.time_health.check(now_utc=t_jump, now_mono=101.0)
    assert st_jump.healthy is False and "clock_jump" in st_jump.reason

    cmd = Command(
        command_id="CMD-CLK-1",
        tenant_id="C1",
        site_id="S1",
        device_id="TN-PUMP-CLK",
        operation="start_pump",
        requested_by="op",
        issued_at=t_jump.isoformat(),
        expires_at=(t_jump + timedelta(seconds=30)).isoformat(),
        configuration_version=1,
    )
    cmd.signature = sign_command(cmd, edge_key)
    state, reason, _ = edge.execute_command(cmd, now=t_jump)
    assert state == CommandState.REJECTED and "clock_untrusted" in reason
    assert plc.physical_actuations == 0

    # Ressincronização NTP restaura confiança
    edge.time_health.mark_resynchronized(now_utc=t_jump, now_mono=102.0)
    assert edge.time_health.check(now_utc=t_jump + timedelta(seconds=1), now_mono=103.0).healthy is True

    # Desligamento ordenado sob bateria baixa (§11.1)
    report = edge.graceful_shutdown(reason="ups_low_battery_11.4v")
    assert report["status"] == "shutdown_complete"


def test_delta4_energy_gate_and_radio_pdr_vs_data_availability(tmp_path):
    """ARQ-002 §7–8 (EnergyGate) e §13.3–13.4 (PDR_radio vs Disponibilidade + testes negativos)."""
    # Tabela exata do ARQ-002: ARM 15W (aprovado), x86 25W/100Ah (reprovado), x86 25W/200Ah/500Wp (aprovado)
    arm_ok = evaluate_energy_gate(EnergyGate(avg_power_w=15, battery_nominal_wh=1280, panel_wp=300))
    assert arm_ok["autonomy_hours"] == pytest.approx(59.73, abs=0.05)
    assert arm_ok["daily_surplus_wh"] == pytest.approx(322.5, abs=0.1)
    assert arm_ok["approved"] is True

    x86_fail = evaluate_energy_gate(EnergyGate(avg_power_w=25, battery_nominal_wh=1280, panel_wp=300))
    assert x86_fail["autonomy_hours"] == pytest.approx(35.84, abs=0.05)
    assert x86_fail["approved"] is False

    x86_ok = evaluate_energy_gate(EnergyGate(avg_power_w=25, battery_nominal_wh=2560, panel_wp=500))
    assert x86_ok["autonomy_hours"] == pytest.approx(71.68, abs=0.05)
    assert x86_ok["daily_surplus_wh"] == pytest.approx(537.5, abs=0.1)
    assert x86_ok["approved"] is True

    # Separação PDR_radio (via fCnt: 10..19 com 9 recebidos -> 90%) vs Disponibilidade (9/10 -> 90%)
    store = CoreStore.open(f"sqlite:///{(tmp_path / 'core.db').as_posix()}")
    store.create_tenant("C1", "Fazenda")
    store.create_site("S1", "C1", "Sede")
    _, op_key = store.create_user("op", "C1", role="operator")
    _, edge_tok = store.register_edge("E1", "C1", ["S1"])
    store.devices.register(
        Device.from_profile(
            {
                "device_id": "N1",
                "tenant_id": "C1",
                "site_id": "S1",
                "category": "NODE-S",
                "manufacturer": "MfgA",
                "communication": {"type": "lorawan", "dev_eui": "0102030405060708"},
                "adapter": {"name": "lorawan_chirpstack"},
                "operational_status": "ACTIVE",
            }
        )
    )
    app = create_app(store, master_key="k")
    client = TestClient(app)

    # 9 frames recebidos entre fCnt 10 e 19 (faltou fCnt 14) -> PDR_radio = 9/10 = 90.0%,
    # enquanto se expected=20, Disponibilidade = 9/20 = 45.0%
    events = [
        {
            "schema_version": "1.0",
            "event_id": f"ev-{fc}",
            "tenant_id": "C1",
            "site_id": "S1",
            "device_id": "N1",
            "event_type": "telemetry",
            "observed_at": f"2026-11-22T12:{fc:02d}:00Z",
            "received_at": f"2026-11-22T12:{fc:02d}:01Z",
            "measurements": {"water_level_m": 2.0},
            "quality": {"water_level_m": "valid", "timestamp_source": "network_server"},
            "source": {"protocol": "lorawan", "f_cnt": fc},
        }
        for fc in [10, 11, 12, 13, 15, 16, 17, 18, 19]
    ]
    assert client.post(
        "/api/v1/ingest/batches",
        json={"edge_id": "E1", "batch_id": "B1", "events": events},
        headers={"Authorization": f"Bearer {edge_tok}"},
    ).status_code == 200

    rep = build_poc_report_from_store(store, "C1", expected_messages_per_node=20)
    node = rep["nodes"][0]
    assert node["radio_pdr_pct"] == 90.0
    assert node["data_availability_pct"] == 45.0
    assert "negative_security_tests" in rep

    # Endpoint /api/v1/poc/energy-gate
    r_eg = client.post(
        "/api/v1/poc/energy-gate",
        json={"avg_power_w": 15, "battery_nominal_wh": 1280, "panel_wp": 300},
        headers={"Authorization": f"Bearer {op_key}"},
    )
    assert r_eg.status_code == 200 and r_eg.json()["approved"] is True


def test_delta5_track_gateway_no_ack_when_core_fails(tmp_path):
    """Ajuste #2 / POC-T12: se a persistência no CORE falhar, o gateway NÃO confirma o lote."""
    store = CoreStore.open(f"sqlite:///{(tmp_path / 'core.db').as_posix()}")
    store.create_tenant("C1", "Fazenda")
    store.create_site("S1", "C1", "Sede")
    store.devices.register(
        Device.from_profile(
            {
                "device_id": "TRK-1",
                "tenant_id": "C1",
                "site_id": "S1",
                "category": "TRACK-B",
                "communication": {"type": "tracker", "imei": "861234567890555"},
                "adapter": {"name": "tracker_gnss_lte"},
                "operational_status": "ACTIVE",
            }
        )
    )
    gw = TeltonikaTcpServer(store)
    ts_ms = int((datetime.now(UTC) - timedelta(minutes=30)).timestamp() * 1000)
    pkt = encode_avl_packet(
        [{"timestamp_ms": ts_ms, "gnss": {"lat": -22.1, "lon": -51.3, "speed_kmh": 15}, "io_raw": {239: 1}}],
        codec_id=CODEC_8E,
    )

    # Simula CORE indisponível (erro de persistência) -> exceção impede envio de ACK binário
    original_persist = store.persist_event

    def _boom(*args, **kwargs):
        raise RuntimeError("CORE database unavailable")

    store.persist_event = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="unavailable"):
        gw.handle_avl_bytes("861234567890555", pkt)

    # CORE volta -> rastreador retransmite o mesmo buffer e persiste sem perdas/duplicatas
    store.persist_event = original_persist  # type: ignore[method-assign]
    assert gw.handle_avl_bytes("861234567890555", pkt) == 1
    assert gw.handle_avl_bytes("861234567890555", pkt) == 1
    assert store.count_events("C1") == 1


def test_delta6_physical_loto_precedence_and_asset_regulatory_model(tmp_path):
    """ARQ-002 §14–16 & Ajustes #3/#4: LOTO físico prevalece sempre + Asset + RegulatoryRule."""
    # Ajuste #3: bloqueio físico LOTO ou seletor manual SEMPRE vencem modo digital remoto
    assert (
        resolve_control_mode(
            physical_remote_enabled=True,
            physical_loto_locked=True,
            digital_mode=ControlMode.REMOTE_SUPERVISED,
        )
        == ControlMode.LOTO_LOCKED
    )
    assert (
        resolve_control_mode(
            physical_remote_enabled=False,
            digital_mode=ControlMode.REMOTE_SUPERVISED,
        )
        == ControlMode.LOCAL_MANUAL
    )
    # Software PODE restringir para MAINTENANCE mesmo com painel em remoto
    assert (
        resolve_control_mode(
            physical_remote_enabled=True,
            digital_mode=ControlMode.MAINTENANCE,
        )
        == ControlMode.MAINTENANCE
    )

    # CLP com LOTO físico ativo bloqueia comando remoto
    plc = SimulatedPumpPLC(pump_running=False, loto_locked=True)
    assert plc.control_mode == ControlMode.LOTO_LOCKED
    edge_key = derive_edge_key("master", "EDGE-001")
    edge = EdgeRuntime("EDGE-001", f"sqlite:///{(tmp_path / 'e.db').as_posix()}", edge_key)
    edge.devices.register(
        Device.from_profile(
            {
                "device_id": "TN-PUMP-LOTO",
                "tenant_id": "C1",
                "site_id": "S1",
                "category": "NODE-C",
                "communication": {"type": "modbus", "identifier": "plc-loto"},
                "adapter": {"name": "modbus_plc"},
                "operational_status": "ACTIVE",
                "metadata": {
                    "allowed_operations": ["start_pump"],
                    "zone_id": "TALHAO-NORTE",
                    "asset_id": "ASSET-POCO-01",
                },
            }
        )
    )
    edge.attach_modbus_client("TN-PUMP-LOTO", plc)
    now = datetime.now(UTC)
    cmd = Command(
        command_id="CMD-LOTO-1",
        tenant_id="C1",
        site_id="S1",
        device_id="TN-PUMP-LOTO",
        operation="start_pump",
        requested_by="op",
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=30)).isoformat(),
        configuration_version=1,
    )
    cmd.signature = sign_command(cmd, edge_key)
    st, reason, _ = edge.execute_command(cmd, now=now)
    assert st == CommandState.REJECTED and "loto" in reason
    assert plc.physical_actuations == 0

    # Ajuste #4: regras regulatórias parametrizadas por autoridade e vigência (sem constantes fixas)
    rule = RegulatoryRule(
        domain="water_grant",
        authority="DAEE-SP",
        reference_code="Portaria-2026-042",
        scope="Poço Artesiano Norte",
        effective_from="2026-01-01",
        parameters={"max_daily_m3": 120.0, "max_flow_m3h": 15.0},
    )
    asset = Asset(
        asset_id="ASSET-POCO-01",
        tenant_id="C1",
        site_id="S1",
        zone_id="TALHAO-NORTE",
        asset_type="well",
        name="Poço Artesiano Norte",
        safe_state_policy={"power_loss": "fail_closed", "comm_loss": "keep_interlocks"},
        regulatory_rules=[rule.to_dict()],
    )
    asset.validate()
    assert edge.devices.get("TN-PUMP-LOTO").asset_id == "ASSET-POCO-01"
    edge.close()


def test_aud001_no_false_go_when_untested_and_in_transit_expiry(tmp_path):
    """AUD-001 §6.1 e §7: relatório nunca retorna GO com itens NOT_TESTED; trânsito > TTL expira."""
    store = CoreStore.open(f"sqlite:///{(tmp_path / 'core.db').as_posix()}")
    store.create_tenant("C1", "Fazenda")
    store.create_site("S1", "C1", "Sede")
    _, op_key = store.create_user("op", "C1", role="operator")
    app = create_app(store, master_key="k")
    client = TestClient(app)
    hdr = {"Authorization": f"Bearer {op_key}"}

    # Sem testes de segurança executados -> critérios aparecem como NOT_TESTED e decisão = NOT_READY (jamais GO!)
    rep = client.get("/api/v1/poc/report", headers=hdr).json()
    assert rep["decision"] == "NOT_READY"
    assert rep["criteria_status"]["EXPIRED_COMMANDS"] == "NOT_TESTED"
    assert rep["criteria_status"]["UNAUTHORIZED_COMMANDS"] == "NOT_TESTED"
    assert rep["criteria_status"]["INTERLOCKS"] == "NOT_TESTED"

    # Registro formal de execução de teste via POST /api/v1/poc/executions
    r_exec = client.post(
        "/api/v1/poc/executions",
        json={
            "test_code": "POC-T09",
            "category": "integration",
            "status": "PASS",
            "expected_result": "Comando não autorizado rejeitado",
            "observed_result": "HTTP 403 + assinatura inválida rejeitada no EDGE",
            "git_commit": "test-sha",
            "environment": "bancada",
        },
        headers=hdr,
    )
    assert r_exec.status_code == 201
    rep2 = client.get("/api/v1/poc/report", headers=hdr).json()
    assert rep2["criteria_status"]["UNAUTHORIZED_COMMANDS"] == "PASS"
    assert rep2["decision"] == "NOT_READY"  # ainda faltam outros critérios obrigatórios

    # AUD-001 §6.1: comando com TTL 10s mas atraso de trânsito de 15s no rádio expira no NODE-C
    edge_key = derive_edge_key("master", "EDGE-001")
    edge = EdgeRuntime("EDGE-001", f"sqlite:///{(tmp_path / 'e.db').as_posix()}", edge_key)
    edge.start_mqtt_subscriptions()
    edge.devices.register(
        Device.from_profile(
            {
                "device_id": "TN-NC-DELAY",
                "tenant_id": "C1",
                "site_id": "S1",
                "category": "NODE-C",
                "communication": {"type": "lorawan", "dev_eui": "bb00112233445566", "application_id": "app1"},
                "adapter": {"name": "lorawan_node_c"},
                "operational_status": "ACTIVE",
                "metadata": {"allowed_operations": ["start_pump"]},
            }
        )
    )
    plc = SimulatedPumpPLC(pump_running=False)
    SimulatedNodeCBridge(edge.bus, "app1", "bb00112233445566", plc, transit_delay_s=15.0)
    now = datetime.now(UTC)
    cmd = Command(
        command_id="CMD-TRANSIT-EXP",
        tenant_id="C1",
        site_id="S1",
        device_id="TN-NC-DELAY",
        operation="start_pump",
        requested_by="op",
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=10)).isoformat(),
        configuration_version=1,
    )
    cmd.signature = sign_command(cmd, edge_key)
    edge.execute_command(cmd, now=now)
    saved = edge.commands.get_command("CMD-TRANSIT-EXP")
    assert saved is not None and saved["state"] == "EXPIRED"
    assert plc.pump_running is False and plc.physical_actuations == 0
    edge.close()


def test_aud001_g1_r01_edge_deployment_consistency():
    """AUD-001 §4, §5, §10: garante consistência entre Compose, Mosquitto ACL e ChirpStack."""
    root = pathlib.Path("deployments/edge")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    arm = (root / "docker-compose.arm.yml").read_text(encoding="utf-8")
    mosq = (root / "mosquitto/mosquitto.conf").read_text(encoding="utf-8")
    acl = (root / "mosquitto/acl.conf").read_text(encoding="utf-8")
    cs = (root / "chirpstack/chirpstack.toml").read_text(encoding="utf-8")
    au = (root / "chirpstack/region_au915_0.toml").read_text(encoding="utf-8")

    # Zero senhas literais ou acesso anônimo
    assert "allow_anonymous false" in mosq
    assert "CHANGE_ME" not in cs
    assert "REPLACE_WITH_" not in cs

    # Mosquitto ACL e credenciais montadas e injetadas em todos os serviços
    assert "acl.conf" in compose
    assert "entrypoint.sh" in compose
    assert "init-chirpstack-db.sh" in compose
    assert "TERRANET_MQTT_USER" in compose
    assert "TERRANET_MQTT_PASSWORD" in compose
    assert "INTEGRATION__MQTT__AUTH__GENERIC__USERNAME" in compose
    assert "username = \"chirpstack\"" in cs and "username = \"chirpstack\"" in au
    assert "user gw_bridge" in acl and "user chirpstack" in acl and "user terranet_edge" in acl

    # ARM configurado como override da stack completa (com limites em todos os serviços)
    for svc in ("postgres:", "redis:", "mosquitto:", "chirpstack:", "terranet-edge:"):
        assert svc in arm


def test_aud002_report_window_verified_manufacturers_and_effective_exec_time(tmp_path):
    """AUD-002 §9.1 (janela temporal), §9.2 (fabricante real) e §9.3 (execução efetiva vs updated_at)."""
    from contracts.canonical import CanonicalEvent

    store = CoreStore.open(f"sqlite:///{(tmp_path / 'core.db').as_posix()}")
    store.create_tenant("C1", "Fazenda")
    store.create_site("S1", "C1", "Sede")
    store.register_edge("E1", "C1", ["S1"])

    # Dois nodes sem manufacturer preenchido NÃO podem contar como 2 fabricantes físicos (§9.2)
    store.devices.register(
        Device.from_profile(
            {
                "device_id": "N-NOMFG-1",
                "tenant_id": "C1",
                "site_id": "S1",
                "category": "NODE-S",
                "manufacturer": "",
                "communication": {"type": "lorawan", "dev_eui": "0101010101010101"},
                "adapter": {"name": "lorawan_chirpstack"},
                "operational_status": "ACTIVE",
                "metadata": {"decoder": "chirpstack_object"},
            }
        )
    )
    store.devices.register(
        Device.from_profile(
            {
                "device_id": "N-NOMFG-2",
                "tenant_id": "C1",
                "site_id": "S1",
                "category": "NODE-S",
                "manufacturer": "",
                "communication": {"type": "lorawan", "dev_eui": "0202020202020202"},
                "adapter": {"name": "lorawan_chirpstack"},
                "operational_status": "ACTIVE",
                "metadata": {"decoder": "terranet_level_v1"},
            }
        )
    )
    store.devices.register(
        Device.from_profile(
            {
                "device_id": "PUMP-1",
                "tenant_id": "C1",
                "site_id": "S1",
                "category": "NODE-C",
                "communication": {"type": "modbus", "identifier": "plc-1"},
                "adapter": {"name": "modbus_plc"},
                "operational_status": "ACTIVE",
            }
        )
    )

    # Evento FORA da janela (ontem) e evento DENTRO da janela (hoje) (§9.1)
    store.persist_event(
        CanonicalEvent(
            event_id="old-1",
            tenant_id="C1",
            site_id="S1",
            device_id="N-NOMFG-1",
            event_type="telemetry",
            observed_at="2026-11-10T10:00:00Z",
            received_at="2026-11-10T10:00:01Z",
            measurements={"water_level_m": 2.0},
            quality={"water_level_m": "valid", "timestamp_source": "network_server"},
        )
    )
    store.persist_event(
        CanonicalEvent(
            event_id="in-win-1",
            tenant_id="C1",
            site_id="S1",
            device_id="N-NOMFG-1",
            event_type="telemetry",
            observed_at="2026-11-22T10:00:00Z",
            received_at="2026-11-22T10:00:01Z",
            measurements={"water_level_m": 2.5},
            quality={"water_level_m": "valid", "timestamp_source": "network_server"},
        )
    )

    # Comando executado NO PRAZO (10:00:10 <= expires_at 10:00:30), porém sincronizado 1h depois (updated_at > expires_at) (§9.3)
    cmd_ok = Command(
        command_id="CMD-ONTIME-LATESYNC",
        tenant_id="C1",
        site_id="S1",
        device_id="PUMP-1",
        operation="start_pump",
        requested_by="op",
        issued_at="2026-11-22T10:00:00+00:00",
        expires_at="2026-11-22T10:00:30+00:00",
        configuration_version=1,
        state=CommandState.DISPATCHED.value,
    )
    store.save_command(cmd_ok, edge_id="E1")
    store.persist_event(
        CanonicalEvent(
            event_id="cmdres:CMD-ONTIME-LATESYNC:verified",
            tenant_id="C1",
            site_id="S1",
            device_id="PUMP-1",
            event_type="command_result",
            observed_at="2026-11-22T10:00:10+00:00",  # executado 20s ANTES de expirar!
            received_at="2026-11-22T11:00:00+00:00",  # sincronizado 1h depois
            quality={"timestamp_source": "edge"},
            attributes={"command_id": "CMD-ONTIME-LATESYNC", "state": "VERIFIED", "reason": "ok"},
        )
    )

    rep = build_poc_report_from_store(
        store,
        "C1",
        expected_messages_per_node=1,
        started_at="2026-11-22T00:00:00Z",
        finished_at="2026-11-22T23:59:59Z",
    )
    # §9.1: apenas o evento dentro da janela foi contado (1, não 2)
    n1 = next(n for n in rep["nodes"] if n["device_id"] == "N-NOMFG-1")
    assert n1["received_messages"] == 1
    # §9.2: nenhum fabricante físico verificado -> 0 (não 2!) e listados em unverified_nodes
    assert rep["summary"]["independent_node_manufacturers"] == 0
    assert set(rep["unverified_nodes"]) == {"N-NOMFG-1", "N-NOMFG-2"}
    assert rep["criteria_status"]["INTEROPERABILITY"] == "NOT_TESTED"
    # §9.3: comando executado no prazo e sincronizado tarde NÃO conta como violação de expiração
    assert rep["summary"]["expired_commands_executed"] == 0
