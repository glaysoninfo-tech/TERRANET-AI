"""Testes do motor de regras local (§15) e validador de comandos (§14)."""

from datetime import UTC, datetime, timedelta

from common.device_store import DeviceStore
from contracts.canonical import CanonicalEvent
from contracts.commands import Command, CommandState, derive_edge_key, sign_command
from contracts.devices import Device
from contracts.rules import Rule
from edge.adapters.modbus.client import SimulatedPumpPLC
from edge.commands.validator import CommandValidator
from edge.rules.engine import RuleStore, RulesEngine
from edge.storage.repository import EdgeStore


def _make_hydro_env():
    store = EdgeStore.open("sqlite:///:memory:")
    devices = DeviceStore(store.db)
    devices.register(
        Device(
            device_id="NODE-HYDRO-001",
            tenant_id="CLIENTE-001",
            site_id="FAZENDA-001",
            category="NODE-S",
            source_type="lorawan",
            source_identifier="0102030405060708",
            adapter_id="lorawan_chirpstack",
            operational_status="ACTIVE",
        )
    )
    rules = RuleStore(store.db)
    engine = RulesEngine(store, devices, rules)
    return store, devices, rules, engine


def _hydro_event(eid: str, ts: str, level: float, battery: float = 12.6) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=eid,
        tenant_id="CLIENTE-001",
        site_id="FAZENDA-001",
        device_id="NODE-HYDRO-001",
        event_type="telemetry",
        observed_at=ts,
        received_at=ts,
        measurements={"water_level_m": level, "battery_voltage_v": battery},
        quality={"water_level_m": "valid", "battery_voltage_v": "valid", "timestamp_source": "network_server"},
    )


def test_threshold_rule_with_hysteresis():
    store, _, rules, engine = _make_hydro_env()
    ok, _ = rules.apply(
        Rule(
            rule_id="R-FLOOD",
            version=1,
            rule_type="threshold",
            device_ids=["NODE-HYDRO-001"],
            severity="critical",
            params={"measurement": "water_level_m", "operator": ">", "value": 4.0, "hysteresis": 0.3},
        )
    )
    assert ok

    # Abaixo do limiar: nenhum alarme
    assert engine.evaluate_event(_hydro_event("e1", "2026-11-22T12:00:00Z", 3.5)) == []
    # Acima de 4.0: dispara alarme
    alarms = engine.evaluate_event(_hydro_event("e2", "2026-11-22T12:15:00Z", 4.2))
    assert len(alarms) == 1 and alarms[0].attributes["alarm_status"] == "raised"
    # Oscilação em 3.85 (dentro da banda de histerese 4.0 - 0.3 = 3.7): mantém ativo
    assert engine.evaluate_event(_hydro_event("e3", "2026-11-22T12:30:00Z", 3.85)) == []
    # Abaixo de 3.7: normaliza (cleared)
    cleared = engine.evaluate_event(_hydro_event("e4", "2026-11-22T12:45:00Z", 3.6))
    assert len(cleared) == 1 and cleared[0].attributes["alarm_status"] == "cleared"


def test_trend_persistence_no_data_and_safety_critical_protection():
    store, devices, rules, engine = _make_hydro_env()

    # Proteção contra alteração remota de regra safety_critical (§15.3)
    safety_rule = Rule(
        rule_id="R-SAFETY",
        version=1,
        rule_type="threshold",
        device_ids=["NODE-HYDRO-001"],
        safety_critical=True,
        params={"measurement": "water_level_m", "operator": ">", "value": 10.0},
    )
    assert rules.apply(safety_rule, actor="local", remote=False)[0] is True
    ok, reason = rules.apply(
        Rule(**{**safety_rule.to_dict(), "version": 2, "enabled": False}),
        actor="core",
        remote=True,
    )
    assert ok is False and "segurança" in reason

    # Regra de tendência (+0.5 m/h) e ausência de dados (1800s)
    rules.apply(
        Rule(
            rule_id="R-TREND",
            version=1,
            rule_type="trend",
            device_ids=["NODE-HYDRO-001"],
            params={"measurement": "water_level_m", "window_s": 3600, "min_rate_per_h": 0.5},
        )
    )
    rules.apply(
        Rule(
            rule_id="R-SILENCE",
            version=1,
            rule_type="no_data",
            device_ids=["NODE-HYDRO-001"],
            params={"max_silence_s": 1800},
        )
    )
    e1 = _hydro_event("t1", "2026-11-22T12:00:00Z", 2.0)
    store.ingest(e1)
    devices.touch("NODE-HYDRO-001", e1.observed_at)
    assert engine.evaluate_event(e1) == []

    e2 = _hydro_event("t2", "2026-11-22T12:30:00Z", 2.4)  # +0.8 m/h
    store.ingest(e2)
    devices.touch("NODE-HYDRO-001", e2.observed_at)
    trend_alarms = engine.evaluate_event(e2)
    assert len(trend_alarms) == 1 and trend_alarms[0].attributes["rule_id"] == "R-TREND"

    # 1 hora depois sem telemetria -> alarme no_data
    no_data = engine.evaluate_no_data(now=datetime(2026, 11, 22, 13, 30, tzinfo=UTC))
    assert len(no_data) == 1 and no_data[0].attributes["rule_id"] == "R-SILENCE"


def test_command_validator_security_expiration_and_plc_interlock():
    store = EdgeStore.open("sqlite:///:memory:")
    devices = DeviceStore(store.db)
    devices.register(
        Device(
            device_id="PUMP-001",
            tenant_id="CLIENTE-001",
            site_id="FAZENDA-001",
            category="NODE-C",
            source_type="modbus",
            source_identifier="plc-01",
            adapter_id="modbus_plc",
            operational_status="ACTIVE",
            configuration_version=2,
            metadata={"allowed_operations": ["start_pump", "stop_pump"]},
        )
    )
    edge_key = derive_edge_key("master-key", "EDGE-001")
    plc = SimulatedPumpPLC(pump_running=False, dry_run_interlock_ok=True)
    validator = CommandValidator("EDGE-001", edge_key, store, devices, {"PUMP-001": plc})
    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)

    def make_cmd(cmd_id: str, op: str = "start_pump", ttl_s: int = 30, key: bytes = edge_key):
        cmd = Command(
            command_id=cmd_id,
            tenant_id="CLIENTE-001",
            site_id="FAZENDA-001",
            device_id="PUMP-001",
            operation=op,
            requested_by="operador1",
            issued_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=ttl_s)).isoformat(),
            configuration_version=2,
            correlation_id=f"corr-{cmd_id}",
        )
        cmd.signature = sign_command(cmd, key)
        return cmd

    # 1. Assinatura inválida (PIT-T10)
    state, reason, _ = validator.handle(make_cmd("C-BAD-SIG", key=b"wrong-key"), now=now)
    assert state == CommandState.REJECTED and reason == "invalid_signature"
    assert plc.pump_running is False and plc.physical_actuations == 0

    # 2. Comando expirado (PIT-T11)
    state, reason, _ = validator.handle(make_cmd("C-EXP", ttl_s=10), now=now + timedelta(seconds=15))
    assert state == CommandState.EXPIRED
    assert plc.pump_running is False and plc.physical_actuations == 0

    # 3. Falha de intertravamento físico (bomba a seco) (PIT-T12)
    plc.dry_run_interlock_ok = False
    state, reason, ev = validator.handle(make_cmd("C-DRY"), now=now)
    assert state == CommandState.REJECTED and "interlock" in reason
    assert ev is not None and ev.attributes["interlock_blocked"] is True
    assert plc.pump_running is False and plc.physical_actuations == 0

    # 4. Intertravamentos OK -> execução e confirmação física (VERIFIED)
    plc.dry_run_interlock_ok = True
    state, reason, ev = validator.handle(make_cmd("C-OK"), now=now)
    assert state == CommandState.VERIFIED
    assert plc.pump_running is True and plc.physical_actuations == 1

    # 5. Reenvio do mesmo comando não reexecuta
    state2, reason2, _ = validator.handle(make_cmd("C-OK"), now=now)
    assert state2 == CommandState.VERIFIED and reason2 == "duplicate_command_ignored"
    assert plc.physical_actuations == 1
