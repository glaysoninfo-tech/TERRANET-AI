"""Adaptador Modbus industrial (E06 / P01).

Responsabilidades:
* Pollear registradores e bobinas conforme o mapa configurado no perfil do dispositivo,
  convertendo para o modelo canônico de dados.
* Executar comandos supervisionados (``start_pump`` / ``stop_pump``) somente após
  verificar permissivos locais, respeitando os intertravamentos do CLP e confirmando o
  estado físico resultante.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from common.timeutil import now_iso
from contracts.canonical import CanonicalEvent, EventType, TimestampSource
from contracts.commands import Command, CommandState
from contracts.devices import Device
from edge.adapters import Adapter, AdapterError, register_adapter
from edge.adapters.modbus.client import ModbusClient, ModbusTransportError
from edge.normalization import DEFAULT_NORMALIZER, NormalizationError

# Mapa padrão P01 compatível com SimulatedPumpPLC
DEFAULT_PUMP_REGISTER_MAP: dict[str, dict[str, Any]] = {
    "discharge_pressure_bar": {"kind": "holding", "address": 0, "scale": 0.01, "unit": "bar"},
    "motor_current_a": {"kind": "holding", "address": 1, "scale": 0.1, "unit": "A"},
    "flow_rate_m3h": {"kind": "holding", "address": 2, "scale": 0.1, "unit": "m3/h"},
    "suction_level_m": {"kind": "holding", "address": 3, "scale": 0.01, "unit": "m"},
    "motor_temperature_c": {"kind": "holding", "address": 4, "scale": 0.1, "unit": "degC"},
    "pump_running_state": {"kind": "coil", "address": 0},
    "remote_mode_state": {"kind": "coil", "address": 1},
    "dry_run_ok_state": {"kind": "coil", "address": 2},
    "overload_fault_state": {"kind": "coil", "address": 3},
    "emergency_stop_state": {"kind": "coil", "address": 4},
}


@dataclass
class ExecutionOutcome:
    state: CommandState
    reason: str
    observed_state: dict[str, Any]
    interlock_blocked: bool = False


class ModbusAdapter(Adapter):
    adapter_id = "modbus_plc"
    version = "1.0"
    source_type = "modbus"

    def identify(self, raw: dict[str, Any]) -> str:
        identifier = raw.get("source_identifier") or raw.get("plc_id")
        if not identifier:
            raise AdapterError("Mensagem Modbus sem source_identifier/plc_id")
        return str(identifier)

    @staticmethod
    def _register_map(device: Device) -> dict[str, dict[str, Any]]:
        return dict(device.metadata.get("register_map") or DEFAULT_PUMP_REGISTER_MAP)

    def read_snapshot(self, device: Device, client: ModbusClient) -> dict[str, Any]:
        """Lê registradores e bobinas do CLP conforme o mapa configurado no dispositivo."""
        unit_id = int(device.communication_profile.get("unit_id", 1))
        reg_map = self._register_map(device)

        holdings = [spec["address"] for spec in reg_map.values() if spec.get("kind") == "holding"]
        coils = [spec["address"] for spec in reg_map.values() if spec.get("kind") == "coil"]

        holding_values: dict[int, int] = {}
        if holdings:
            start, end = min(holdings), max(holdings)
            vals = client.read_holding_registers(start, end - start + 1, unit_id=unit_id)
            holding_values = {start + i: v for i, v in enumerate(vals)}

        coil_values: dict[int, bool] = {}
        if coils:
            start, end = min(coils), max(coils)
            vals = client.read_coils(start, end - start + 1, unit_id=unit_id)
            coil_values = {start + i: bool(v) for i, v in enumerate(vals)}

        raw_values: dict[str, Any] = {}
        for name, spec in reg_map.items():
            kind = spec.get("kind")
            addr = int(spec["address"])
            if kind == "holding":
                val = holding_values[addr]
                scale = float(spec.get("scale", 1.0))
                raw_values[name] = (val * scale, spec.get("unit"))
            elif kind == "coil":
                raw_values[name] = (coil_values[addr], None)
        return raw_values

    def poll(
        self,
        device: Device,
        client: ModbusClient,
        poll_id: str | None = None,
        observed_at: str | None = None,
    ) -> CanonicalEvent:
        """Executa um ciclo de coleta do CLP e gera o evento canônico."""
        ts = observed_at or now_iso()
        try:
            raw_values = self.read_snapshot(device, client)
        except ModbusTransportError as exc:
            raise AdapterError(f"Controlador indisponível: {exc}", permanent=False) from exc
        raw = {
            "source_identifier": device.source_identifier,
            "poll_id": poll_id,
            "time": ts,
            "values": raw_values,
        }
        return self.to_events(raw, device, received_at=ts)[0]

    def to_events(
        self,
        raw: dict[str, Any],
        device: Device,
        received_at: str,
        context: dict[str, Any] | None = None,
    ) -> list[CanonicalEvent]:
        identifier = self.identify(raw)
        if identifier != device.source_identifier:
            raise AdapterError("Identificador Modbus não corresponde ao dispositivo resolvido")
        observed_at, ts_source, _ = DEFAULT_NORMALIZER.resolve_timestamp(
            raw.get("time"), received_at, TimestampSource.EDGE
        )
        raw_values = raw.get("values")
        if not isinstance(raw_values, dict):
            raise AdapterError("Leitura Modbus sem campo 'values'")

        normalized_input: dict[str, tuple[Any, str | None]] = {}
        for name, entry in raw_values.items():
            if isinstance(entry, tuple | list) and len(entry) == 2:
                normalized_input[name] = (entry[0], entry[1])
            else:
                normalized_input[name] = (entry, None)
        try:
            measurements, quality = DEFAULT_NORMALIZER.normalize(normalized_input, device)
        except NormalizationError as exc:
            raise AdapterError(str(exc)) from exc

        quality["timestamp_source"] = ts_source
        poll_id = raw.get("poll_id")
        if not poll_id:
            digest = hashlib.sha256(f"{device.device_id}|{observed_at}".encode()).hexdigest()[:20]
            poll_id = digest
        event = CanonicalEvent(
            event_id=f"modbus:{device.source_identifier}:{poll_id}",
            tenant_id=device.tenant_id,
            site_id=device.site_id,
            device_id=device.device_id,
            event_type=EventType.TELEMETRY.value,
            observed_at=observed_at,
            received_at=received_at,
            measurements=measurements,
            quality=quality,
            source=self.source(unit_id=device.communication_profile.get("unit_id", 1)),
        )
        return [event]

    def execute_command(
        self, command: Command, device: Device, client: ModbusClient
    ) -> ExecutionOutcome:
        """Executa operação supervisionada em bomba (P01) respeitando permissivos e CLP."""
        unit_id = int(device.communication_profile.get("unit_id", 1))
        if command.operation not in ("start_pump", "stop_pump"):
            return ExecutionOutcome(
                state=CommandState.REJECTED,
                reason=f"Operação Modbus não suportada: {command.operation}",
                observed_state={},
            )
        target_running = command.operation == "start_pump"

        # 1. Ler estado e permissivos atuais do controlador antes de atuar
        try:
            pre_raw = self.read_snapshot(device, client)
            pre_state, _ = DEFAULT_NORMALIZER.normalize(pre_raw, device)
        except ModbusTransportError as exc:
            return ExecutionOutcome(
                state=CommandState.FAILED,
                reason=f"Controlador indisponível antes da execução: {exc}",
                observed_state={},
            )

        # 2. Para partida, verificar permissivos locais no EDGE (defesa em profundidade;
        # o CLP continua mantendo seus próprios intertravamentos independentemente).
        if target_running:
            if pre_state.get("emergency_stop_state"):
                return ExecutionOutcome(
                    state=CommandState.REJECTED,
                    reason="interlock_failed: emergency_stop_state",
                    observed_state=pre_state,
                    interlock_blocked=True,
                )
            if pre_state.get("remote_mode_state") is False:
                return ExecutionOutcome(
                    state=CommandState.REJECTED,
                    reason="interlock_failed: remote_mode_disabled",
                    observed_state=pre_state,
                    interlock_blocked=True,
                )
            if pre_state.get("dry_run_ok_state") is False:
                return ExecutionOutcome(
                    state=CommandState.REJECTED,
                    reason="interlock_failed: dry_run_protection",
                    observed_state=pre_state,
                    interlock_blocked=True,
                )
            if pre_state.get("overload_fault_state"):
                return ExecutionOutcome(
                    state=CommandState.REJECTED,
                    reason="interlock_failed: motor_overload_fault",
                    observed_state=pre_state,
                    interlock_blocked=True,
                )

        # 3. Enviar comando ao CLP (bobina 0)
        try:
            coil_addr = int(device.metadata.get("command_coil_address", 0))
            accepted_by_plc = client.write_coil(coil_addr, target_running, unit_id=unit_id)
        except ModbusTransportError as exc:
            # Falha durante escrita: estado físico pode ser desconhecido -> UNKNOWN (§14.3)
            return ExecutionOutcome(
                state=CommandState.UNKNOWN,
                reason=f"Falha de comunicação durante escrita no CLP: {exc}",
                observed_state=pre_state,
            )

        if not accepted_by_plc:
            return ExecutionOutcome(
                state=CommandState.REJECTED,
                reason="interlock_failed: rejected_by_plc_interlock",
                observed_state=pre_state,
                interlock_blocked=True,
            )

        # 4. Confirmação física (§14.1: CONFIRMAR ESTADO)
        if command.confirmation_policy == "none":
            return ExecutionOutcome(
                state=CommandState.EXECUTED,
                reason="executed_without_feedback",
                observed_state=pre_state,
            )

        try:
            post_raw = self.read_snapshot(device, client)
            post_state, _ = DEFAULT_NORMALIZER.normalize(post_raw, device)
        except ModbusTransportError as exc:
            return ExecutionOutcome(
                state=CommandState.UNKNOWN,
                reason=f"Comando escrito, mas leitura de confirmação falhou: {exc}",
                observed_state={},
            )

        if bool(post_state.get("pump_running_state")) == target_running:
            return ExecutionOutcome(
                state=CommandState.VERIFIED,
                reason="physical_state_verified",
                observed_state=post_state,
            )
        return ExecutionOutcome(
            state=CommandState.FAILED,
            reason="physical_state_mismatch_after_execution",
            observed_state=post_state,
            interlock_blocked=target_running,
        )


ADAPTER = register_adapter(ModbusAdapter(), "modbus_tcp")
