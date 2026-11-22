"""Gerenciamento central de dispositivos e ciclo de vida (PIT-001 §7, §8, E03)."""

from __future__ import annotations

from typing import Any

from common.device_store import DeviceNotFound
from contracts.devices import Device, LifecycleState
from core.identity.auth import ForbiddenError, UserPrincipal
from core.storage.repository import CoreStore


class DeviceManagementService:
    def __init__(self, store: CoreStore):
        self.store = store

    def register(self, principal: UserPrincipal, data: dict[str, Any]) -> Device:
        principal.require_role("operator")
        payload = dict(data)
        payload["tenant_id"] = principal.tenant_id
        site = self.store.get_site(str(payload.get("site_id", "")))
        if not site or site["tenant_id"] != principal.tenant_id:
            raise ForbiddenError("Instalação (site_id) inexistente ou pertence a outro cliente")
        device = (
            Device.from_profile(payload)
            if "communication" in payload
            else Device.from_dict(payload)
        )
        registered = self.store.devices.register(device, actor=principal.username)
        self.store.audit(
            principal.tenant_id,
            principal.username,
            "device:register",
            registered.device_id,
            "ok",
            {"site_id": registered.site_id, "source_type": registered.source_type},
        )
        return registered

    def get(self, principal: UserPrincipal, device_id: str) -> Device:
        device = self.store.devices.get(device_id)
        if device is None:
            raise DeviceNotFound(device_id)
        principal.require_tenant(device.tenant_id)
        return device

    def list(self, principal: UserPrincipal, site_id: str | None = None) -> list[Device]:
        sites = [site_id] if site_id else None
        return self.store.devices.list(tenant_id=principal.tenant_id, site_ids=sites)

    def transition(
        self, principal: UserPrincipal, device_id: str, new_state: str, reason: str = ""
    ) -> Device:
        principal.require_role("operator")
        device = self.get(principal, device_id)
        updated = self.store.devices.transition(
            device.device_id, new_state, actor=principal.username, reason=reason
        )
        if updated.operational_status == LifecycleState.DECOMMISSIONED.value:
            # Ao desativar (§8.2): impede novos comandos e registra auditoria formal
            self.store.audit(
                principal.tenant_id,
                principal.username,
                "device:decommission",
                device_id,
                "ok",
                {"reason": reason},
            )
        else:
            self.store.audit(
                principal.tenant_id,
                principal.username,
                "device:transition",
                device_id,
                "ok",
                {"from": device.operational_status, "to": new_state, "reason": reason},
            )
        return updated
