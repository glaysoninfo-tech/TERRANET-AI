"""Avaliador e gerador automático de relatório da POC-001 (POC-001 §17, §21, §25, §26.1, E16).

Mantém compatibilidade integral com `NodeTest`, `SystemTest`, `evaluate_poc` e `print_report`
descritos na §21.1, e adiciona `build_poc_report_from_store` para auditar dados reais do
TERRANET CORE e classificar a decisão formal em GO / CONDITIONAL GO / NO-GO (§25).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from common.db import loads
from core.storage.repository import CoreStore


@dataclass
class EnergyGate:
    """Validador preliminar do Gate energético (ARQ-002 §7–8)."""

    avg_power_w: float
    battery_nominal_wh: float
    panel_wp: float
    autonomy_hours: float = 48.0
    usable_fraction: float = 0.70
    hsp: float = 3.5
    system_efficiency: float = 0.65
    recovery_days: float = 3.0


def evaluate_energy_gate(system: EnergyGate) -> dict[str, Any]:
    if (
        min(
            system.avg_power_w,
            system.battery_nominal_wh,
            system.panel_wp,
            system.autonomy_hours,
            system.hsp,
            system.recovery_days,
        )
        <= 0
    ):
        raise ValueError("Parâmetros devem ser positivos")
    if not 0 < system.usable_fraction <= 1:
        raise ValueError("Fração útil inválida")
    if not 0 < system.system_efficiency <= 1:
        raise ValueError("Eficiência inválida")

    daily_consumption = system.avg_power_w * 24.0
    usable_battery = system.battery_nominal_wh * system.usable_fraction
    autonomy = usable_battery / system.avg_power_w
    daily_generation = system.panel_wp * system.hsp * system.system_efficiency
    daily_surplus = daily_generation - daily_consumption
    energy_to_recover = system.avg_power_w * system.autonomy_hours
    required_daily_surplus = energy_to_recover / system.recovery_days

    autonomy_ok = autonomy >= system.autonomy_hours
    recovery_ok = daily_surplus >= required_daily_surplus
    return {
        "daily_consumption_wh": round(daily_consumption, 2),
        "usable_battery_wh": round(usable_battery, 2),
        "autonomy_hours": round(autonomy, 2),
        "daily_generation_wh": round(daily_generation, 2),
        "daily_surplus_wh": round(daily_surplus, 2),
        "required_daily_surplus_wh": round(required_daily_surplus, 2),
        "autonomy_ok": autonomy_ok,
        "recovery_ok": recovery_ok,
        "approved": autonomy_ok and recovery_ok,
    }


@dataclass
class NodeTest:
    name: str
    expected_messages: int
    received_messages: int

    def pdr(self) -> float:
        if self.expected_messages <= 0:
            raise ValueError("Quantidade esperada inválida")
        if not 0 <= self.received_messages <= self.expected_messages:
            raise ValueError("Mensagens recebidas inválidas")
        return self.received_messages / self.expected_messages * 100.0


@dataclass
class SystemTest:
    offline_hours: float
    gateway_autonomy_hours: float
    node_autonomy_hours: float
    persisted_events: int
    synchronized_events: int
    expired_commands_executed: int
    unauthorized_commands_executed: int
    applications_validated: int
    independent_node_profiles: int
    interlock_violations: int = 0


def evaluate_poc(nodes: list[NodeTest], system: SystemTest) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for node in nodes:
        results[f"PDR_{node.name}"] = node.pdr() >= 95.0

    results["EDGE_OFFLINE"] = system.offline_hours >= 24
    results["GATEWAY_ENERGY"] = system.gateway_autonomy_hours >= 48
    results["NODE_ENERGY"] = system.node_autonomy_hours >= 72
    results["SYNC"] = (
        system.persisted_events > 0
        and system.synchronized_events == system.persisted_events
    )
    results["EXPIRED_COMMANDS"] = system.expired_commands_executed == 0
    results["UNAUTHORIZED_COMMANDS"] = system.unauthorized_commands_executed == 0
    results["INTERLOCKS"] = system.interlock_violations == 0
    results["APPLICATIONS"] = system.applications_validated >= 3
    results["INTEROPERABILITY"] = system.independent_node_profiles >= 2
    return results


def decide_go_nogo(results: dict[str, bool]) -> str:
    """Aplica critérios formais §25 exigindo matriz completa (delega para tri-estado)."""
    mapped = {k: ("PASS" if bool(v) else "FAIL") for k, v in results.items()}
    return decide_go_nogo_tristate(mapped)


MANDATORY_SAFETY_CRITERIA = (
    "SYNC",
    "EXPIRED_COMMANDS",
    "UNAUTHORIZED_COMMANDS",
    "INTERLOCKS",
    "INTEROPERABILITY",
)
MANDATORY_CRITERIA = (
    *MANDATORY_SAFETY_CRITERIA,
    "EDGE_OFFLINE",
    "GATEWAY_ENERGY",
    "NODE_ENERGY",
    "APPLICATIONS",
)
VALID_CRITERIA_STATES = frozenset({"PASS", "FAIL", "NOT_TESTED"})


def decide_go_nogo_tristate(
    criteria_status: dict[str, str],
    required_criteria: tuple[str, ...] = MANDATORY_CRITERIA,
) -> str:
    """Decisão auditável (G1-S / AUD-002): exige matriz completa antes de autorizar GO.

    * Qualquer critério de segurança/integridade em FAIL -> NO-GO.
    * Conjunto vazio, critério obrigatório ausente ou qualquer NOT_TESTED -> NOT_READY.
    * GO somente quando TODOS os critérios obrigatórios e reportados estiverem em PASS.
    """
    if not isinstance(criteria_status, dict):
        raise ValueError("criteria_status deve ser um dicionário")
    for name, state in criteria_status.items():
        if state not in VALID_CRITERIA_STATES:
            raise ValueError(f"Estado inválido para critério {name}: {state!r}")

    if any(criteria_status.get(k) == "FAIL" for k in MANDATORY_SAFETY_CRITERIA):
        return "NO-GO"

    if not criteria_status:
        return "NOT_READY"

    missing = [k for k in required_criteria if k not in criteria_status]
    if missing or any(v == "NOT_TESTED" for v in criteria_status.values()):
        return "NOT_READY"

    if all(v == "PASS" for v in criteria_status.values()):
        return "GO"
    return "CONDITIONAL GO"


def reuse_ratio(reused_integrations: int, total_integrations: int) -> float:
    """Indicador de reutilização R = (Ir / It) * 100 (§26.1)."""
    if total_integrations <= 0:
        return 0.0
    return round((reused_integrations / total_integrations) * 100.0, 2)


def build_poc_report_from_store(
    store: CoreStore,
    tenant_id: str,
    expected_messages_per_node: int = 672,
    offline_hours: float = 0.0,
    gateway_autonomy_hours: float = 0.0,
    node_autonomy_hours: float = 0.0,
    persisted_edge_events: int | None = None,
    edge_event_ids: set[str] | list[str] | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any]:
    """Gera o relatório POC-E16 a partir dos registros reais do TERRANET CORE.

    Garantias de integridade (AUD-002 / G1-S):
    * Janela temporal explícita (`started_at` / `finished_at`).
    * Interoperabilidade exige `manufacturer` físico verificável (sem fallback por decoder).
    * Sincronização exige evidência independente do EDGE (`persisted_edge_events` ou
      `edge_event_ids`); sem fonte independente do EDGE, `SYNC` fica `NOT_TESTED`.
    * Indicadores de segurança (`EXPIRED_COMMANDS`, `UNAUTHORIZED_COMMANDS`, `INTERLOCKS`)
      são derivados das evidências primárias (comandos, `command_result` e `audit_log` de
      comando), nunca de inferências genéricas.
    """
    from common.timeutil import normalize_iso, parse_ts

    start_iso = normalize_iso(started_at) if started_at else None
    end_iso = normalize_iso(finished_at) if finished_at else None
    win_start = start_iso or "1970-01-01T00:00:00+00:00"
    win_end = end_iso or "9999-12-31T23:59:59+00:00"

    devices = store.devices.list(tenant_id=tenant_id)
    node_s_devices = [
        d for d in devices if d.category in ("NODE", "NODE-S") and d.source_type == "lorawan"
    ]
    pump_devices = [d for d in devices if d.category == "NODE-C" or d.source_type == "modbus"]
    track_devices = [d for d in devices if d.source_type == "tracker"]

    node_tests: list[NodeTest] = []
    node_details: list[dict[str, Any]] = []
    manufacturers: set[str] = set()
    unverified_nodes: list[str] = []

    for dev in node_s_devices:
        # AUD-002 §9.2: exige fabricante físico verificável (nunca usa decoder/adapter como fallback)
        mfg = dev.manufacturer.strip()
        if mfg:
            manufacturers.add(mfg.lower())
        else:
            unverified_nodes.append(dev.device_id)

        ev_rows = store.db.query(
            "SELECT payload FROM telemetry_events WHERE tenant_id = ? "
            "AND device_id = ? AND event_type = 'telemetry' "
            "AND observed_at >= ? AND observed_at <= ?",
            (tenant_id, dev.device_id, win_start, win_end),
        )
        received = len(ev_rows)
        # ARQ-002 §13.3: distingue PDR_radio (via fCnt LoRaWAN) de Disponibilidade_dados
        fcnts = sorted(
            {
                int(src["f_cnt"])
                for r in ev_rows
                for src in [((loads(r["payload"]) or {}).get("source") or {})]
                if isinstance(src.get("f_cnt"), int)
            }
        )
        if len(fcnts) >= 2:
            transmitted_est = (fcnts[-1] - fcnts[0]) + 1
            radio_pdr = round(min(100.0, len(fcnts) / transmitted_est * 100.0), 2)
        else:
            radio_pdr = None

        capped = min(received, expected_messages_per_node)
        nt = NodeTest(dev.device_id, expected_messages_per_node, capped)
        node_tests.append(nt)
        data_avail = round(nt.pdr(), 2)
        node_details.append(
            {
                "device_id": dev.device_id,
                "manufacturer": mfg or None,
                "model": dev.model.strip() or None,
                "firmware_version": dev.firmware_version.strip() or None,
                "hardware_verified": bool(mfg and dev.model.strip()),
                "expected_messages": expected_messages_per_node,
                "received_messages": received,
                "data_availability_pct": data_avail,
                "radio_pdr_pct": radio_pdr,
                "pdr_pct": data_avail,
                "approved": nt.pdr() >= 95.0,
            }
        )

    # Contagens de comandos filtradas pela janela temporal do ensaio
    commands = store.db.query(
        "SELECT * FROM commands WHERE tenant_id = ? "
        "AND issued_at >= ? AND issued_at <= ? ORDER BY issued_at DESC",
        (tenant_id, win_start, win_end),
    )
    expired_rejected = sum(1 for c in commands if c["state"] == "EXPIRED")
    verified_commands = sum(
        1 for c in commands if c["state"] in ("VERIFIED", "PHYSICAL_STATE_VERIFIED")
    )
    rejected_commands = sum(
        1 for c in commands if c["state"] in ("REJECTED", "PLC_REJECTED")
    )
    interlock_blocked = sum(
        1
        for c in commands
        if c["state"] in ("REJECTED", "PLC_REJECTED") and "interlock" in (c.get("reason") or "")
    )

    audit_cmd_rejected = int(
        store.db.scalar(
            "SELECT COUNT(*) FROM audit_log WHERE tenant_id = ? "
            "AND action LIKE 'command:%' AND outcome = 'rejected' "
            "AND at >= ? AND at <= ?",
            (tenant_id, win_start, win_end),
        )
        or 0
    )
    authorized_cmd_ids = {
        str((loads(r["details"]) or {}).get("command_id"))
        for r in store.db.query(
            "SELECT details FROM audit_log WHERE tenant_id = ? "
            "AND action LIKE 'command:issue:%' AND outcome = 'authorized' "
            "AND at >= ? AND at <= ?",
            (tenant_id, win_start, win_end),
        )
    }
    unauthorized_cmd_rejected = sum(
        1
        for c in commands
        if c["state"] in ("REJECTED", "PLC_REJECTED")
        and any(
            tok in (c.get("reason") or "")
            for tok in ("invalid_signature", "operation_not_allowed", "tenant_or_site_mismatch")
        )
    )
    unauthorized_blocked_total = audit_cmd_rejected + unauthorized_cmd_rejected

    # Evidências registradas formalmente na tabela test_executions (na janela do ensaio)
    executions = store.db.query(
        "SELECT * FROM test_executions WHERE tenant_id = ? "
        "AND finished_at >= ? AND finished_at <= ? ORDER BY finished_at DESC",
        (tenant_id, win_start, win_end),
    )
    latest_by_code: dict[str, dict[str, Any]] = {}
    for ex in executions:
        latest_by_code.setdefault(ex["test_code"], ex)

    # Evidências primárias de command_result (execução real, intertravamento e assinatura)
    cmd_res_rows = store.db.query(
        "SELECT observed_at, payload FROM telemetry_events WHERE tenant_id = ? "
        "AND event_type = 'command_result' AND observed_at >= ? AND observed_at <= ?",
        (tenant_id, win_start, win_end),
    )
    executed_at_by_cmd: dict[str, str] = {}
    interlock_blocked_events = 0
    interlock_violations = 0
    expired_result_events = 0
    for row in cmd_res_rows:
        payload = loads(row["payload"]) or {}
        attrs = payload.get("attributes") or {}
        cid = attrs.get("command_id")
        st = attrs.get("state")
        if st == "EXPIRED":
            expired_result_events += 1
        if st in ("REJECTED", "PLC_REJECTED") and (
            attrs.get("interlock_blocked") is True or "interlock" in str(attrs.get("reason", ""))
        ):
            interlock_blocked_events += 1
        if cid and st in ("EXECUTED", "PLC_EXECUTED", "VERIFIED", "PHYSICAL_STATE_VERIFIED"):
            executed_at_by_cmd[str(cid)] = str(payload.get("observed_at") or row["observed_at"])
            obs = attrs.get("observed_state") or {}
            interlock_tripped = (
                obs.get("dry_run_ok_state") is False
                or obs.get("emergency_stop_state") is True
                or obs.get("overload_fault_state") is True
                or obs.get("loto_locked_state") is True
                or obs.get("remote_mode_state") is False
            )
            if attrs.get("interlock_blocked") is True or (
                attrs.get("operation") == "start_pump"
                and obs.get("pump_running_state") is True
                and interlock_tripped
            ):
                interlock_violations += 1

    expired_executed = 0
    unauthorized_executed = 0
    unlinked_auth_executions = 0
    unlinked_exec_timestamps = 0
    for c in commands:
        if c["state"] not in ("EXECUTED", "PLC_EXECUTED", "VERIFIED", "PHYSICAL_STATE_VERIFIED"):
            continue
        effective_ts = executed_at_by_cmd.get(c["command_id"])
        if effective_ts is None:
            unlinked_exec_timestamps += 1
        elif parse_ts(effective_ts) > parse_ts(c["expires_at"]):
            expired_executed += 1

        if not authorized_cmd_ids:
            unlinked_auth_executions += 1
        elif c["command_id"] not in authorized_cmd_ids:
            unauthorized_executed += 1

    # Vetos explícitos registrados em test_executions (um FAIL de QA sempre bloqueia PASS)
    qa_unauth_fail = any(
        ex["test_code"] in ("POC-T09", "PIT-T10") and ex["status"] == "FAIL" for ex in executions
    )
    qa_interlock_fail = any(
        ex["test_code"] in ("POC-T10", "PIT-T12") and ex["status"] == "FAIL" for ex in executions
    )

    # Posições e registros atrasados de TRACK na janela do ensaio (POC-T11 / POC-T12)
    track_rows = store.db.query(
        "SELECT payload FROM telemetry_events WHERE tenant_id = ? "
        "AND event_type = 'position' AND observed_at >= ? AND observed_at <= ?",
        (tenant_id, win_start, win_end),
    )
    track_events = [loads(r["payload"]) for r in track_rows]
    delayed_track = sum(
        1 for ev in track_events if (ev.get("attributes") or {}).get("delayed") is True
    )
    can_events = sum(
        1 for ev in track_events if (ev.get("attributes") or {}).get("can_j1939") is True
    )

    # Aplicações demonstradas (H01, P01, V01)
    apps = 0
    if any(n["received_messages"] > 0 for n in node_details):
        apps += 1  # H01
    if pump_devices and (verified_commands > 0 or rejected_commands > 0):
        apps += 1  # P01
    if track_devices and len(track_events) > 0:
        apps += 1  # V01

    core_event_rows = store.db.query(
        "SELECT event_id FROM telemetry_events WHERE tenant_id = ? "
        "AND observed_at >= ? AND observed_at <= ?",
        (tenant_id, win_start, win_end),
    )
    core_event_ids = {str(r["event_id"]) for r in core_event_rows}
    core_total = len(core_event_ids)

    # G1-S Problema B: SYNC exige evidência independente do EDGE (jamais compara CORE com CORE)
    sync_missing_in_core = 0
    if edge_event_ids is not None:
        edge_set = {str(e) for e in edge_event_ids}
        sync_missing_in_core = len(edge_set - core_event_ids)
        sync_status = "NOT_TESTED" if not edge_set else ("PASS" if sync_missing_in_core == 0 else "FAIL")
        sync_evidence_source = "independent_edge_event_ids"
    elif persisted_edge_events is not None:
        sync_status = (
            "NOT_TESTED"
            if persisted_edge_events == 0
            else ("PASS" if core_total >= persisted_edge_events and core_total == persisted_edge_events else "FAIL")
        )
        sync_evidence_source = "independent_edge_counter"
    else:
        sync_status = "NOT_TESTED"
        sync_evidence_source = "missing_independent_edge_source"

    # Avaliação tri-estado (PASS / FAIL / NOT_TESTED) — impede falsa aprovação!
    criteria_status: dict[str, str] = {}
    for nd in node_details:
        if nd["received_messages"] == 0:
            criteria_status[f"PDR_{nd['device_id']}"] = "NOT_TESTED"
        else:
            criteria_status[f"PDR_{nd['device_id']}"] = "PASS" if nd["approved"] else "FAIL"

    def _field_metric(value: float, threshold: float, test_code: str) -> str:
        ex = latest_by_code.get(test_code)
        if ex is not None:
            return ex["status"]
        if value <= 0:
            return "NOT_TESTED"
        return "PASS" if value >= threshold else "FAIL"

    expired_blocked_total = max(expired_rejected, expired_result_events)
    interlock_blocked_total = max(interlock_blocked, interlock_blocked_events)

    criteria_status["EDGE_OFFLINE"] = _field_metric(offline_hours, 24.0, "POC-T05")
    criteria_status["GATEWAY_ENERGY"] = _field_metric(gateway_autonomy_hours, 48.0, "POC-T13-GW")
    criteria_status["NODE_ENERGY"] = _field_metric(node_autonomy_hours, 72.0, "POC-T13-NODE")
    criteria_status["SYNC"] = sync_status
    criteria_status["EXPIRED_COMMANDS"] = (
        "FAIL"
        if expired_executed > 0
        else (
            "NOT_TESTED"
            if unlinked_exec_timestamps > 0
            else ("PASS" if expired_blocked_total > 0 else "NOT_TESTED")
        )
    )
    criteria_status["UNAUTHORIZED_COMMANDS"] = (
        "FAIL"
        if (unauthorized_executed > 0 or qa_unauth_fail)
        else (
            "NOT_TESTED"
            if unlinked_auth_executions > 0
            else ("PASS" if unauthorized_blocked_total > 0 else "NOT_TESTED")
        )
    )
    criteria_status["INTERLOCKS"] = (
        "FAIL"
        if (interlock_violations > 0 or qa_interlock_fail)
        else ("PASS" if interlock_blocked_total > 0 else "NOT_TESTED")
    )
    criteria_status["APPLICATIONS"] = "PASS" if apps >= 3 else ("NOT_TESTED" if apps == 0 else "FAIL")
    criteria_status["INTEROPERABILITY"] = (
        "PASS"
        if len(manufacturers) >= 2
        else ("NOT_TESTED" if len(manufacturers) == 0 else "FAIL")
    )

    decision = decide_go_nogo_tristate(criteria_status)
    # Compatibilidade booleana (somente True quando efetivamente PASS)
    criteria = {k: (v == "PASS") for k, v in criteria_status.items()}

    # Indicador de reutilização R (§26.1): dispositivos que compartilham adaptadores já existentes
    adapters_used = [d.adapter_id for d in devices]
    reused = max(0, len(adapters_used) - len(set(adapters_used)))

    return {
        "tenant_id": tenant_id,
        "window": {"started_at": start_iso, "finished_at": end_iso},
        "decision": decision,
        "criteria": criteria,
        "criteria_status": criteria_status,
        "nodes": node_details,
        "unverified_nodes": unverified_nodes,
        "summary": {
            "total_devices": len(devices),
            "independent_node_manufacturers": len(manufacturers),
            "applications_validated": apps,
            "core_events": core_total,
            "sync_evidence_source": sync_evidence_source,
            "sync_missing_in_core": sync_missing_in_core,
            "commands_total": len(commands),
            "commands_verified": verified_commands,
            "commands_rejected": rejected_commands,
            "commands_expired_blocked": expired_blocked_total,
            "expired_commands_executed": expired_executed,
            "unauthorized_commands_executed": unauthorized_executed,
            "unlinked_auth_executions": unlinked_auth_executions,
            "unlinked_exec_timestamps": unlinked_exec_timestamps,
            "interlock_violations": interlock_violations,
            "track_positions": len(track_events),
            "track_delayed_offline_recovered": delayed_track,
            "track_can_j1939_events": can_events,
            "reuse_ratio_pct": reuse_ratio(reused, len(adapters_used)),
            "recorded_test_executions": len(executions),
        },
        "negative_security_tests": {
            "expired_blocked_evidence": expired_blocked_total,
            "interlock_blocked_evidence": interlock_blocked_total,
            "unauthorized_command_blocked_evidence": unauthorized_blocked_total,
            "verified": (
                expired_blocked_total > 0
                and interlock_blocked_total > 0
                and unauthorized_blocked_total > 0
            ),
        },
        "field_measurements": {
            "offline_hours": offline_hours,
            "gateway_autonomy_hours": gateway_autonomy_hours,
            "node_autonomy_hours": node_autonomy_hours,
        },
    }


def print_report(results: dict[str, bool]) -> None:
    print("\nTERRANET AI - RELATÓRIO POC")
    print("-" * 40)
    for test, approved in results.items():
        status = "APROVADO" if approved else "REPROVADO"
        print(f"{test}: {status}")
    print("-" * 40)
    decision = decide_go_nogo(results)
    if decision == "GO":
        print("RESULTADO: CRITÉRIOS AVALIADOS ATENDIDOS (GO)")
    elif decision == "CONDITIONAL GO":
        print("RESULTADO: AVANÇAR COM RESTRIÇÕES (CONDITIONAL GO)")
    else:
        print("RESULTADO: CRITÉRIOS CRÍTICOS NÃO ATENDIDOS (NO-GO)")


def main() -> None:
    nodes = [
        NodeTest(name="NODE_A", expected_messages=672, received_messages=660),
        NodeTest(name="NODE_B", expected_messages=672, received_messages=651),
    ]
    system = SystemTest(
        offline_hours=24,
        gateway_autonomy_hours=50,
        node_autonomy_hours=75,
        persisted_events=288,
        synchronized_events=288,
        expired_commands_executed=0,
        unauthorized_commands_executed=0,
        applications_validated=3,
        independent_node_profiles=2,
    )
    print_report(evaluate_poc(nodes, system))


__all__ = [
    "MANDATORY_CRITERIA",
    "MANDATORY_SAFETY_CRITERIA",
    "EnergyGate",
    "NodeTest",
    "SystemTest",
    "build_poc_report_from_store",
    "decide_go_nogo",
    "decide_go_nogo_tristate",
    "evaluate_energy_gate",
    "evaluate_poc",
    "loads",
    "print_report",
    "reuse_ratio",
]

if __name__ == "__main__":
    main()
