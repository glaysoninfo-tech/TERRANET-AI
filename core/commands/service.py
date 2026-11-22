"""Gerenciamento central de comandos físicos (PIT-001 §14, PIT-T10, PIT-T11, PIT-T14)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from common.device_store import DeviceNotFound
from common.timeutil import iso, utc_now
from contracts.commands import (
    MAX_TTL_S,
    MIN_TTL_S,
    Command,
    CommandState,
    derive_edge_key,
    sign_command,
)
from contracts.devices import COMMAND_ACCEPTING
from core.identity.auth import ForbiddenError, UserPrincipal
from core.storage.repository import CoreStore


class CommandRejected(ValueError):
    pass


class CommandManagementService:
    def __init__(self, store: CoreStore, master_key: bytes | str, edge_online_ttl_s: float = 120.0):
        self.store = store
        self.master_key = master_key
        self.edge_online_ttl_s = edge_online_ttl_s

    def issue(
        self,
        principal: UserPrincipal,
        device_id: str,
        operation: str,
        ttl_s: int = 30,
        params: dict[str, Any] | None = None,
        expected_state: dict[str, Any] | None = None,
        confirmation_policy: str = "feedback",
        correlation_id: str | None = None,
        require_edge_online: bool = True,
        now: datetime | None = None,
    ) -> Command:
        """Valida permissões, verifica estado/EDGE, assina e persiste comando (§14.1–14.2)."""
        principal.require_role("operator")
        if not (MIN_TTL_S <= int(ttl_s) <= MAX_TTL_S):
            raise CommandRejected(f"ttl_s deve estar entre {MIN_TTL_S} e {MAX_TTL_S} segundos")

        device = self.store.devices.get(device_id)
        if device is None:
            raise DeviceNotFound(device_id)
        principal.require_tenant(device.tenant_id)

        if device.operational_status not in COMMAND_ACCEPTING:
            self.store.audit(
                principal.tenant_id,
                principal.username,
                f"command:issue:{operation}",
                device_id,
                "rejected",
                {"reason": f"device_status:{device.operational_status}"},
            )
            raise CommandRejected(
                f"Equipamento em estado '{device.operational_status}' não aceita comandos"
            )

        if operation not in device.allowed_operations:
            self.store.audit(
                principal.tenant_id,
                principal.username,
                f"command:issue:{operation}",
                device_id,
                "rejected",
                {"reason": "operation_not_allowed"},
            )
            raise CommandRejected(f"Operação '{operation}' não autorizada para {device_id}")

        edge = self.store.find_edge_for_site(device.tenant_id, device.site_id)
        if edge is None:
            raise CommandRejected(f"Nenhum EDGE ativo associado à instalação {device.site_id}")

        now_dt = now or utc_now()
        if require_edge_online and not self.store.is_edge_online(
            edge["edge_id"], max_age_s=self.edge_online_ttl_s, now=now_dt
        ):
            self.store.audit(
                principal.tenant_id,
                principal.username,
                f"command:issue:{operation}",
                device_id,
                "rejected",
                {"reason": "edge_offline", "edge_id": edge["edge_id"]},
            )
            raise CommandRejected(f"EDGE '{edge['edge_id']}' offline; comando rejeitado")

        cmd = Command(
            command_id=f"CMD-{uuid.uuid4().hex[:12]}",
            tenant_id=device.tenant_id,
            site_id=device.site_id,
            device_id=device.device_id,
            operation=operation,
            requested_by=principal.username,
            issued_at=iso(now_dt),
            expires_at=iso(now_dt + timedelta(seconds=int(ttl_s))),
            configuration_version=device.configuration_version,
            params=dict(params or {}),
            expected_state=dict(expected_state or {}),
            confirmation_policy=confirmation_policy,
            correlation_id=correlation_id or f"CORR-{uuid.uuid4().hex[:10]}",
            state=CommandState.AUTHORIZED.value,
        )
        key = derive_edge_key(self.master_key, edge["edge_id"])
        cmd.signature = sign_command(cmd, key)
        self.store.save_command(cmd, edge_id=edge["edge_id"], reason="authorized")
        self.store.audit(
            principal.tenant_id,
            principal.username,
            f"command:issue:{operation}",
            device_id,
            "authorized",
            {
                "command_id": cmd.command_id,
                "edge_id": edge["edge_id"],
                "expires_at": cmd.expires_at,
                "correlation_id": cmd.correlation_id,
            },
        )
        return cmd

    def get(self, principal: UserPrincipal, command_id: str) -> dict[str, Any]:
        row = self.store.get_command(principal.tenant_id, command_id)
        if row is None:
            raise ForbiddenError("Comando inexistente ou pertence a outro cliente")
        return row

    def list(
        self, principal: UserPrincipal, device_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self.store.list_commands(principal.tenant_id, device_id=device_id, limit=limit)
