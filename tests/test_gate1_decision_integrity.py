"""Testes de regressão da integridade de decisão do Gate 1 (marco G1-S).

Cobre os três problemas P0 de homologação:
* Problema A: decisão GO nunca é emitida com matriz de critérios vazia ou incompleta.
* Problema B: SYNC exige evidência independente do EDGE (jamais compara CORE com CORE).
* Problema C: indicadores de segurança usam evidências primárias (comandos, command_result
  e auditoria de comando), impedindo aprovações por inferências genéricas.
"""

from __future__ import annotations

import pytest

from contracts.canonical import CanonicalEvent
from contracts.commands import Command, CommandState
from contracts.devices import Device
from core.poc_report import (
    MANDATORY_CRITERIA,
    build_poc_report_from_store,
    decide_go_nogo,
    decide_go_nogo_tristate,
)
from core.storage.repository import CoreStore


@pytest.mark.parametrize(
    "criteria",
    [
        {},
        {"SYNC": "PASS"},
        {"INTEROPERABILITY": "PASS"},
        {k: "PASS" for k in MANDATORY_CRITERIA if k != "INTERLOCKS"},
    ],
)
def test_incomplete_evidence_cannot_generate_go(criteria):
    """Nenhum conjunto incompleto de evidências pode autorizar a homologação do Gate 1."""
    decision = decide_go_nogo_tristate(criteria)
    assert decision == "NOT_READY"
    assert decision != "GO", "Falha crítica: GO emitido com evidências incompletas."


def test_failed_safety_criterion_blocks_approval():
    criteria = {k: "PASS" for k in MANDATORY_CRITERIA}
    criteria["EXPIRED_COMMANDS"] = "FAIL"
    assert decide_go_nogo_tristate(criteria) == "NO-GO"


def test_complete_all_pass_authorizes_go():
    criteria = {k: "PASS" for k in MANDATORY_CRITERIA}
    criteria["PDR_NODE_A"] = "PASS"
    assert decide_go_nogo_tristate(criteria) == "GO"


def test_sync_requires_independent_edge_evidence(tmp_path):
    """Problema B: sem fonte independente do EDGE, SYNC permanece NOT_TESTED (não compara CORE consigo)."""
    store = CoreStore.open(f"sqlite:///{(tmp_path / 'core.db').as_posix()}")
    store.create_tenant("C1", "Fazenda")
    store.create_site("S1", "C1", "Sede")
    store.devices.register(
        Device.from_profile(
            {
                "device_id": "N1",
                "tenant_id": "C1",
                "site_id": "S1",
                "category": "NODE-S",
                "manufacturer": "FabricanteA",
                "model": "H1",
                "communication": {"type": "lorawan", "dev_eui": "0102030405060708"},
                "adapter": {"name": "lorawan_chirpstack"},
                "operational_status": "ACTIVE",
            }
        )
    )
    store.persist_event(
        CanonicalEvent(
            event_id="evt-1",
            tenant_id="C1",
            site_id="S1",
            device_id="N1",
            event_type="telemetry",
            observed_at="2026-11-22T12:00:00Z",
            received_at="2026-11-22T12:00:01Z",
            measurements={"water_level_m": 2.1},
            quality={"water_level_m": "valid", "timestamp_source": "network_server"},
        )
    )

    # Sem persisted_edge_events nem edge_event_ids -> NOT_TESTED
    rep_no_edge = build_poc_report_from_store(store, "C1", expected_messages_per_node=1)
    assert rep_no_edge["criteria_status"]["SYNC"] == "NOT_TESTED"
    assert rep_no_edge["summary"]["sync_evidence_source"] == "missing_independent_edge_source"

    # Com edge_event_ids faltando 1 evento no CORE -> FAIL
    rep_missing = build_poc_report_from_store(
        store, "C1", expected_messages_per_node=1, edge_event_ids={"evt-1", "evt-2-lost"}
    )
    assert rep_missing["criteria_status"]["SYNC"] == "FAIL"
    assert rep_missing["summary"]["sync_missing_in_core"] == 1
    assert rep_missing["decision"] == "NO-GO"

    # Com edge_event_ids totalmente presentes no CORE -> PASS
    rep_ok = build_poc_report_from_store(
        store, "C1", expected_messages_per_node=1, edge_event_ids={"evt-1"}
    )
    assert rep_ok["criteria_status"]["SYNC"] == "PASS"


def test_security_criteria_require_primary_command_and_audit_evidence(tmp_path):
    """Problema C: rejeição genérica não aprova UNAUTHORIZED_COMMANDS; violação física em command_result reprova INTERLOCKS."""
    store = CoreStore.open(f"sqlite:///{(tmp_path / 'core.db').as_posix()}")
    store.create_tenant("C1", "Fazenda")
    store.create_site("S1", "C1", "Sede")
    store.register_edge("E1", "C1", ["S1"])
    store.devices.register(
        Device.from_profile(
            {
                "device_id": "PUMP-1",
                "tenant_id": "C1",
                "site_id": "S1",
                "category": "NODE-C",
                "manufacturer": "CLPReal",
                "model": "P1",
                "communication": {"type": "modbus", "identifier": "plc-1"},
                "adapter": {"name": "modbus_plc"},
                "operational_status": "ACTIVE",
            }
        )
    )

    # Auditoria rejeitada que NÃO é de comando não aprova UNAUTHORIZED_COMMANDS
    store.audit("C1", "usr", "config:apply", "E1", "rejected", {"reason": "bad_config"})
    rep1 = build_poc_report_from_store(store, "C1")
    assert rep1["criteria_status"]["UNAUTHORIZED_COMMANDS"] == "NOT_TESTED"

    # Evidência primária de rejeição de comando aprova UNAUTHORIZED_COMMANDS
    store.audit("C1", "viewer", "command:issue:start_pump", "PUMP-1", "rejected", {"reason": "forbidden"})
    rep2 = build_poc_report_from_store(store, "C1")
    assert rep2["criteria_status"]["UNAUTHORIZED_COMMANDS"] == "PASS"

    # Violação física primária em command_result (bomba ligada com proteção a seco atuada!) -> INTERLOCKS FAIL -> NO-GO
    cmd = Command(
        command_id="CMD-VIOL-1",
        tenant_id="C1",
        site_id="S1",
        device_id="PUMP-1",
        operation="start_pump",
        requested_by="op",
        issued_at="2026-11-22T12:00:00+00:00",
        expires_at="2026-11-22T12:01:00+00:00",
        configuration_version=1,
        state=CommandState.DISPATCHED.value,
    )
    store.save_command(cmd, edge_id="E1")
    store.persist_event(
        CanonicalEvent(
            event_id="cmdres:CMD-VIOL-1:verified",
            tenant_id="C1",
            site_id="S1",
            device_id="PUMP-1",
            event_type="command_result",
            observed_at="2026-11-22T12:00:05+00:00",
            received_at="2026-11-22T12:00:06+00:00",
            quality={"timestamp_source": "edge"},
            attributes={
                "command_id": "CMD-VIOL-1",
                "operation": "start_pump",
                "state": "VERIFIED",
                "reason": "physical_state_verified",
                "observed_state": {"pump_running_state": True, "dry_run_ok_state": False},
            },
        )
    )
    rep3 = build_poc_report_from_store(store, "C1")
    assert rep3["summary"]["interlock_violations"] == 1
    assert rep3["criteria_status"]["INTERLOCKS"] == "FAIL"
    assert rep3["decision"] == "NO-GO"


def test_legacy_go_cannot_approve_empty_or_partial_evidence():
    """Ressalva 1: função legada decide_go_nogo() não pode aprovar dicionário vazio ou parcial."""
    assert decide_go_nogo({}) != "GO"
    assert decide_go_nogo({"SYNC": True}) != "GO"
    assert decide_go_nogo({k: True for k in MANDATORY_CRITERIA}) == "GO"


def test_executed_command_without_auth_or_physical_timestamp_cannot_approve(tmp_path):
    """Ressalva 2: comando executado sem vínculo de autorização ou sem command_result impede PASS."""
    store = CoreStore.open(f"sqlite:///{(tmp_path / 'core.db').as_posix()}")
    store.create_tenant("C1", "Fazenda")
    store.create_site("S1", "C1", "Sede")
    store.register_edge("E1", "C1", ["S1"])
    store.devices.register(
        Device.from_profile(
            {
                "device_id": "PUMP-1",
                "tenant_id": "C1",
                "site_id": "S1",
                "category": "NODE-C",
                "manufacturer": "CLPReal",
                "model": "P1",
                "communication": {"type": "modbus", "identifier": "plc-1"},
                "adapter": {"name": "modbus_plc"},
                "operational_status": "ACTIVE",
            }
        )
    )

    # Registra bloqueios negativos (rejeição + expiração)
    store.audit("C1", "viewer", "command:issue:start_pump", "PUMP-1", "rejected", {"reason": "forbidden"})
    exp_cmd = Command(
        command_id="CMD-EXP-1",
        tenant_id="C1",
        site_id="S1",
        device_id="PUMP-1",
        operation="start_pump",
        requested_by="op",
        issued_at="2026-11-22T12:00:00+00:00",
        expires_at="2026-11-22T12:00:30+00:00",
        configuration_version=1,
        state=CommandState.EXPIRED.value,
    )
    store.save_command(exp_cmd, edge_id="E1")

    # Comando marcado VERIFIED mas SEM auditoria de autorização e SEM command_result (sem timestamp físico!)
    unlinked = Command(
        command_id="CMD-UNLINKED",
        tenant_id="C1",
        site_id="S1",
        device_id="PUMP-1",
        operation="start_pump",
        requested_by="op",
        issued_at="2026-11-22T12:01:00+00:00",
        expires_at="2026-11-22T12:01:30+00:00",
        configuration_version=1,
        state=CommandState.VERIFIED.value,
    )
    store.save_command(unlinked, edge_id="E1")

    rep1 = build_poc_report_from_store(store, "C1")
    assert rep1["summary"]["unlinked_auth_executions"] == 1
    assert rep1["summary"]["unlinked_exec_timestamps"] == 1
    assert rep1["criteria_status"]["UNAUTHORIZED_COMMANDS"] == "NOT_TESTED"
    assert rep1["criteria_status"]["EXPIRED_COMMANDS"] == "NOT_TESTED"

    # Quando existe auditoria de autorização de outro comando, CMD-UNLINKED passa a ser FAIL (NO-GO)
    store.audit("C1", "op", "command:issue:stop_pump", "PUMP-1", "authorized", {"command_id": "CMD-OTHER"})
    rep2 = build_poc_report_from_store(store, "C1")
    assert rep2["summary"]["unauthorized_commands_executed"] == 1
    assert rep2["criteria_status"]["UNAUTHORIZED_COMMANDS"] == "FAIL"
    assert rep2["decision"] == "NO-GO"
