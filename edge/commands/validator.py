"""Command Validator do TERRANET EDGE (PIT-001 §14, PIT-T10, PIT-T11, PIT-T12).

Validações executadas antes de qualquer envio ao adaptador industrial:
1. Deduplicação por ``command_id`` (não reexecutar comandos já recebidos/UNKNOWN).
2. Autoridade: verificação da assinatura HMAC-SHA256 derivada para este EDGE (PIT-T10).
3. Prazo de validade: rejeição como ``EXPIRED`` se expirado (PIT-T11).
4. Cadastro, escopo (tenant/site), ciclo de vida (somente ``ACTIVE``), versão de
   configuração e operação permitida.
5. Estado esperado (``expected_state``) contra o último estado local conhecido.
6. Delegação ao adaptador industrial (que checa permissivos e aciona o CLP, preservando
   intertravamentos locais — PIT-T12).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.db import dumps, loads
from common.device_store import DeviceStore
from common.timeutil import now_iso, utc_now
from contracts.canonical import CanonicalEvent, EventType, Quality, TimestampSource
from contracts.commands import Command, CommandState, verify_command
from contracts.devices import COMMAND_ACCEPTING
from edge.adapters.modbus.adapter import ModbusAdapter
from edge.adapters.modbus.client import ModbusClient
from edge.storage.repository import EdgeStore


class CommandValidator:
    def __init__(
        self,
        edge_id: str,
        edge_key: bytes,
        store: EdgeStore,
        devices: DeviceStore,
        modbus_clients: dict[str, ModbusClient] | None = None,
        modbus_adapter: ModbusAdapter | None = None,
    ):
        self.edge_id = edge_id
        self.edge_key = edge_key
        self.store = store
        self.devices = devices
        self.modbus_clients = modbus_clients if modbus_clients is not None else {}
        self.modbus_adapter = modbus_adapter or ModbusAdapter()

    def attach_modbus_client(self, device_id: str, client: ModbusClient) -> None:
        self.modbus_clients[device_id] = client

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        row = self.store.db.query_one(
            "SELECT * FROM commands WHERE command_id = ?", (command_id,)
        )
        if not row:
            return None
        row["payload"] = loads(row["payload"])
        return row

    def handle(
        self, command: Command, now: datetime | None = None
    ) -> tuple[CommandState, str, CanonicalEvent | None]:
        """Processa um comando e retorna ``(estado_final, motivo, evento_command_result)``."""
        now_dt = now or utc_now()
        existing = self.get_command(command.command_id)
        if existing is not None:
            # Nunca reexecutar automaticamente um comando já processado ou UNKNOWN (§14.3)
            return CommandState(existing["state"]), "duplicate_command_ignored", None

        self._save(command, CommandState.RECEIVED, "received_by_edge")

        # 1. Autoridade (assinatura HMAC por EDGE) — PIT-T10
        if not verify_command(command, self.edge_key):
            return self._finish(command, CommandState.REJECTED, "invalid_signature")

        # 2. Prazo de validade — PIT-T11
        if command.is_expired(now_dt):
            return self._finish(command, CommandState.EXPIRED, "command_expired")

        # 3. Dispositivo, escopo e ciclo de vida
        device = self.devices.get(command.device_id)
        if device is None:
            return self._finish(command, CommandState.REJECTED, "unknown_device")
        if device.tenant_id != command.tenant_id or device.site_id != command.site_id:
            return self._finish(command, CommandState.REJECTED, "tenant_or_site_mismatch")
        if device.operational_status not in COMMAND_ACCEPTING:
            return self._finish(
                command,
                CommandState.REJECTED,
                f"device_not_active:{device.operational_status}",
            )

        # 4. Versão de configuração e operação autorizada
        if command.configuration_version != device.configuration_version:
            return self._finish(
                command,
                CommandState.REJECTED,
                f"configuration_version_mismatch:{command.configuration_version}!={device.configuration_version}",
            )
        if command.operation not in device.allowed_operations:
            return self._finish(
                command, CommandState.REJECTED, f"operation_not_allowed:{command.operation}"
            )

        # 5. Validação do estado esperado, quando declarado
        if command.expected_state:
            latest = self.store.latest_state(device.device_id)
            current = latest["measurements"] if latest else {}
            for key, expected in command.expected_state.items():
                if current.get(key) != expected:
                    return self._finish(
                        command,
                        CommandState.REJECTED,
                        f"expected_state_mismatch:{key}={current.get(key)}!={expected}",
                    )

        # 6. Comando aceito localmente -> encaminhar ao adaptador industrial
        self._save(command, CommandState.ACCEPTED, "validated_by_edge")
        if device.source_type != "modbus":
            return self._finish(
                command,
                CommandState.REJECTED,
                f"unsupported_command_source:{device.source_type}",
            )
        client = self.modbus_clients.get(device.device_id)
        if client is None:
            return self._finish(command, CommandState.FAILED, "plc_client_not_attached")

        outcome = self.modbus_adapter.execute_command(command, device, client)
        return self._finish(
            command,
            outcome.state,
            outcome.reason,
            observed_state=outcome.observed_state,
            interlock_blocked=outcome.interlock_blocked,
        )

    def _save(self, command: Command, state: CommandState, reason: str) -> None:
        now = now_iso()
        self.store.db.execute(
            "INSERT INTO commands (command_id, device_id, operation, state, reason, payload, "
            "received_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (command_id) DO UPDATE SET state = excluded.state, "
            "reason = excluded.reason, updated_at = excluded.updated_at",
            (
                command.command_id,
                command.device_id,
                command.operation,
                state.value,
                reason,
                dumps(command.to_dict()),
                now,
                now,
            ),
        )

    def _finish(
        self,
        command: Command,
        state: CommandState,
        reason: str,
        observed_state: dict[str, Any] | None = None,
        interlock_blocked: bool = False,
    ) -> tuple[CommandState, str, CanonicalEvent | None]:
        self._save(command, state, reason)
        self.store.audit(
            actor=command.requested_by,
            action=f"command:{command.operation}",
            target=command.device_id,
            outcome=state.value,
            details={
                "command_id": command.command_id,
                "correlation_id": command.correlation_id,
                "reason": reason,
                "interlock_blocked": interlock_blocked,
            },
        )
        # Só gera evento canônico para dispositivos cadastrados (FK em telemetry_events)
        device = self.devices.get(command.device_id)
        if device is None:
            return state, reason, None
        now = now_iso()
        event = CanonicalEvent(
            event_id=f"cmdres:{command.command_id}:{state.value.lower()}",
            tenant_id=device.tenant_id,
            site_id=device.site_id,
            device_id=device.device_id,
            event_type=EventType.COMMAND_RESULT.value,
            observed_at=now,
            received_at=now,
            processed_at=now,
            measurements={},
            quality={"timestamp_source": TimestampSource.EDGE.value},
            source={"protocol": "internal", "adapter": "command_validator", "edge_id": self.edge_id},
            attributes={
                "command_id": command.command_id,
                "correlation_id": command.correlation_id,
                "operation": command.operation,
                "state": state.value,
                "reason": reason,
                "interlock_blocked": interlock_blocked,
                "observed_state": observed_state or {},
            },
        )
        self.store.ingest(event)
        return state, reason, event
