"""Serviço de ingestão de telemetria (PIT-001 §3, §13).

Suporta dois fluxos documentados:
1. Lotes sincronizados a partir do TERRANET EDGE (``POST /api/v1/ingest/batches``),
   com verificação rigorosa de payload, conferência de cliente/instalação contra o
   cadastro e idempotência por ``event_id``.
2. Ingestão direta de rastreadores celulares TRACK (``POST /api/v1/ingest/track``),
   pois rastreadores GNSS/LTE não dependem de um Gateway EDGE fixo (§3).
"""

from __future__ import annotations

from typing import Any

from common.metrics import Registry
from common.timeutil import now_iso
from contracts.canonical import CanonicalEvent, ContractError
from contracts.devices import TELEMETRY_ACCEPTING
from core.identity.auth import EdgePrincipal
from core.storage.repository import CoreStore
from edge.adapters import AdapterError
from edge.adapters.tracker.adapter import TrackerAdapter


class TelemetryIngestionService:
    def __init__(self, store: CoreStore, metrics: Registry | None = None):
        self.store = store
        self.metrics = metrics or Registry()
        self.tracker_adapter = TrackerAdapter()
        self._m_ingested = self.metrics.counter("core_events_ingested_total", "Eventos novos persistidos")
        self._m_duplicates = self.metrics.counter("core_events_duplicate_total", "Eventos duplicados idempotentes")
        self._m_rejected = self.metrics.counter("core_events_rejected_total", "Eventos rejeitados")

    def ingest_batch(
        self, edge: EdgePrincipal, batch: dict[str, Any]
    ) -> dict[str, Any]:
        """Implementa o contrato ``POST /api/v1/ingest/batches`` (§13.1–13.3)."""
        batch_id = str(batch.get("batch_id") or "")
        declared_edge = str(batch.get("edge_id") or "")
        if declared_edge and declared_edge != edge.edge_id:
            raise PermissionError("edge_id do payload não corresponde à credencial autenticada")

        events = batch.get("events")
        if not isinstance(events, list):
            raise ValueError("Campo 'events' deve ser uma lista")

        accepted: list[str] = []
        rejected: list[dict[str, Any]] = []
        retryable: list[str] = []

        for raw_event in events:
            event_id = raw_event.get("event_id") if isinstance(raw_event, dict) else None
            try:
                event = CanonicalEvent.from_dict(raw_event)
            except ContractError as exc:
                self._m_rejected.inc(reason="schema")
                rejected.append({"event_id": event_id, "reason": str(exc)})
                continue

            # Conferência de autorização do EDGE e propriedade do dispositivo (§13.3)
            if event.tenant_id != edge.tenant_id or event.site_id not in edge.site_ids:
                self._m_rejected.inc(reason="unauthorized_scope")
                rejected.append(
                    {
                        "event_id": event.event_id,
                        "reason": "edge_not_authorized_for_tenant_or_site",
                    }
                )
                continue

            device = self.store.devices.get(event.device_id)
            if device is None:
                self._m_rejected.inc(reason="unknown_device")
                rejected.append({"event_id": event.event_id, "reason": "unknown_device"})
                continue

            if device.tenant_id != event.tenant_id or device.site_id != event.site_id:
                self._m_rejected.inc(reason="device_scope_mismatch")
                rejected.append({"event_id": event.event_id, "reason": "device_scope_mismatch"})
                continue

            if device.operational_status not in TELEMETRY_ACCEPTING and event.event_type != "command_result":
                self._m_rejected.inc(reason="device_inactive")
                rejected.append(
                    {
                        "event_id": event.event_id,
                        "reason": f"device_status:{device.operational_status}",
                    }
                )
                continue

            try:
                is_new = self.store.persist_event(event, edge_id=edge.edge_id)
            except self.store.db.transient_errors:
                retryable.append(event.event_id)
                continue

            if is_new:
                self._m_ingested.inc(source="edge")
            else:
                self._m_duplicates.inc(source="edge")
            # Eventos já recebidos são confirmados sem duplicação (§13.2)
            accepted.append(event.event_id)

        return {
            "batch_id": batch_id,
            "accepted": accepted,
            "rejected": rejected,
            "retryable": retryable,
        }

    def ingest_track_packet(
        self, raw_packet: dict[str, Any], tenant_id: str | None = None
    ) -> dict[str, Any]:
        """Fluxo direto TRACK -> TRACK ADAPTER -> TERRANET CORE (§3, V01, PIT-T09)."""
        try:
            imei = self.tracker_adapter.identify(raw_packet)
        except AdapterError as exc:
            self._m_rejected.inc(reason="track_identify")
            raise ValueError(str(exc)) from exc

        device = self.store.devices.find_by_source("tracker", imei)
        if device is None:
            self._m_rejected.inc(reason="unknown_tracker")
            raise LookupError(f"Rastreador não cadastrado: {imei}")
        if tenant_id is not None and device.tenant_id != tenant_id:
            raise PermissionError("Isolamento de cliente: rastreador pertence a outro cliente")
        if device.operational_status not in TELEMETRY_ACCEPTING:
            raise PermissionError(f"Rastreador em estado {device.operational_status}")

        recv = now_iso()
        try:
            events = self.tracker_adapter.to_events(raw_packet, device, received_at=recv)
        except AdapterError as exc:
            self._m_rejected.inc(reason="track_decode")
            raise ValueError(str(exc)) from exc

        persisted: list[str] = []
        duplicates: list[str] = []
        for event in events:
            event.processed_at = now_iso()
            event.validate()
            if self.store.persist_event(event, edge_id=None):
                self._m_ingested.inc(source="track")
                persisted.append(event.event_id)
            else:
                self._m_duplicates.inc(source="track")
                duplicates.append(event.event_id)

        return {
            "device_id": device.device_id,
            "accepted": persisted + duplicates,
            "new_events": len(persisted),
            "duplicates": len(duplicates),
        }
