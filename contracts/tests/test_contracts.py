"""Testes unitários dos contratos canônicos, tópicos, dispositivos, comandos e regras."""

from datetime import UTC, datetime, timedelta

import pytest

from contracts.canonical import CanonicalEvent, ContractError, validate_event
from contracts.commands import (
    Command,
    CommandState,
    derive_edge_key,
    sign_command,
    verify_command,
)
from contracts.commands import (
    can_transition as cmd_can_transition,
)
from contracts.devices import Device, LifecycleState
from contracts.devices import can_transition as dev_can_transition
from contracts.rules import Rule, RuleValidationError
from contracts.topics import (
    DELIVERY_POLICY,
    TopicKind,
    build_topic,
    parse_chirpstack_topic,
    parse_topic,
)


def _valid_event() -> dict:
    return {
        "schema_version": "1.0",
        "event_id": "EVT-000001",
        "tenant_id": "CLIENTE-001",
        "site_id": "FAZENDA-001",
        "device_id": "TN-NODE-0001",
        "event_type": "telemetry",
        "observed_at": "2026-11-22T12:00:00Z",
        "received_at": "2026-11-22T12:00:02Z",
        "measurements": {"water_level_m": 2.45, "battery_voltage_v": 12.7},
        "quality": {"water_level_m": "valid", "timestamp_source": "network_server"},
        "source": {"protocol": "lorawan", "adapter": "lorawan_chirpstack"},
    }


def test_canonical_event_valid():
    ev = CanonicalEvent.from_dict(_valid_event())
    ev.validate()
    assert ev.measurements["water_level_m"] == 2.45


def test_canonical_event_rejects_naive_timestamp_and_unknown_unit():
    bad = _valid_event()
    bad["observed_at"] = "2026-11-22T12:00:00"
    bad["measurements"]["water_level"] = 2.45  # sem sufixo de unidade canônica
    errors = validate_event(bad)
    assert any("fuso" in e for e in errors)
    assert any("unidade" in e for e in errors)


def test_canonical_event_rejects_unsupported_schema_version():
    bad = _valid_event()
    bad["schema_version"] = "2.0"
    with pytest.raises(ContractError):
        CanonicalEvent.from_dict(bad)


def test_canonical_position_requires_location():
    ev = _valid_event()
    ev["event_type"] = "position"
    errors = validate_event(ev)
    assert any("location" in e for e in errors)


def test_topics_build_parse_and_policies():
    topic = build_topic("CLIENTE-01", "Fazenda_01", "Node-001", TopicKind.TELEMETRY)
    assert topic == "terranet/v1/cliente-01/fazenda_01/node-001/telemetry"
    assert parse_topic(topic)["device"] == "node-001"
    # Comandos físicos nunca têm retenção MQTT (§10.3)
    assert DELIVERY_POLICY[TopicKind.COMMAND] == (1, False)
    assert DELIVERY_POLICY[TopicKind.STATE] == (1, True)

    cs = parse_chirpstack_topic("application/app1/device/0102030405060708/event/up")
    assert cs == {"application_id": "app1", "dev_eui": "0102030405060708", "event": "up"}


def test_device_profile_rejects_secrets_and_validates_lifecycle():
    profile = {
        "device_id": "TN-NODE-0001",
        "tenant_id": "CLIENTE-001",
        "site_id": "FAZENDA-001",
        "category": "NODE-S",
        "communication": {"type": "lorawan", "dev_eui": "0102030405060708"},
        "adapter": {"name": "lorawan_chirpstack", "version": "1.0"},
        "capabilities": ["water_level_measurement"],
    }
    dev = Device.from_profile(profile)
    assert dev.source_identifier == "0102030405060708"
    assert dev_can_transition(LifecycleState.REGISTERED, LifecycleState.PROVISIONED)
    assert not dev_can_transition(LifecycleState.DECOMMISSIONED, LifecycleState.ACTIVE)

    profile["communication"]["app_key"] = "deadbeef"
    with pytest.raises(ValueError, match="segredos"):
        Device.from_profile(profile)


def test_command_signing_expiration_and_state_machine():
    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    cmd = Command(
        command_id="CMD-1",
        tenant_id="CLIENTE-001",
        site_id="FAZENDA-001",
        device_id="PUMP-001",
        operation="start_pump",
        requested_by="op1",
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=30)).isoformat(),
        configuration_version=1,
        correlation_id="CORR-1",
    )
    key_edge_1 = derive_edge_key("master-secret", "EDGE-001")
    key_edge_2 = derive_edge_key("master-secret", "EDGE-002")
    cmd.signature = sign_command(cmd, key_edge_1)
    assert verify_command(cmd, key_edge_1)
    assert not verify_command(cmd, key_edge_2)
    assert not cmd.is_expired(now + timedelta(seconds=10))
    assert cmd.is_expired(now + timedelta(seconds=31))

    assert cmd_can_transition(CommandState.AUTHORIZED, CommandState.DISPATCHED)
    assert cmd_can_transition(CommandState.UNKNOWN, CommandState.VERIFIED)
    assert not cmd_can_transition(CommandState.VERIFIED, CommandState.EXECUTED)


def test_rule_validation():
    rule = Rule.from_dict(
        {
            "rule_id": "R-HYDRO-HIGH",
            "version": 1,
            "rule_type": "threshold",
            "device_ids": ["NODE-001"],
            "params": {"measurement": "water_level_m", "operator": ">", "value": 4.0, "hysteresis": 0.2},
        }
    )
    assert rule.version == 1

    with pytest.raises(RuleValidationError):
        Rule.from_dict(
            {
                "rule_id": "R-BAD",
                "version": 1,
                "rule_type": "threshold",
                "device_ids": ["NODE-001"],
                "params": {"measurement": "nivel_sem_unidade", "operator": ">", "value": 4.0},
            }
        )
