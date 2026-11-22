"""Agente de sincronização EDGE -> CORE (PIT-001 §12, §13, E08).

Propriedades garantidas:
* Lê eventos pendentes do ``sync_outbox`` em lotes ordenados.
* Marca ``SYNCED`` somente os IDs confirmados em ``accepted`` pela resposta do CORE.
* Trata rejeições permanentes (``rejected``) marcando ``FAILED`` e enviando à dead-letter.
* Trata falhas de rede, timeouts, ``retryable`` ou IDs ausentes da resposta com backoff
  exponencial, preservando a entrega pelo menos uma vez com idempotência no CORE.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from common.metrics import Registry
from common.timeutil import now_iso
from edge.storage.repository import EdgeStore
from edge.synchronization.client import SyncTransport, TransportError


@dataclass
class SyncResult:
    batch_id: str | None
    sent: int
    accepted: int
    rejected: int
    retryable: int
    wan_connected: bool
    error: str | None = None


class SyncAgent:
    def __init__(
        self,
        edge_id: str,
        store: EdgeStore,
        transport: SyncTransport,
        metrics: Registry | None = None,
        batch_size: int = 100,
    ):
        self.edge_id = edge_id
        self.store = store
        self.transport = transport
        self.batch_size = batch_size
        self.wan_connected = False
        self.metrics = metrics or Registry()
        self._m_wan = self.metrics.gauge("edge_wan_connected", "Estado da conexão WAN/CORE")
        self._m_pending = self.metrics.gauge("edge_sync_pending", "Eventos aguardando sincronização")
        self._m_failures = self.metrics.counter("edge_sync_failures", "Falhas de sincronização")

    def sync_once(self, now: datetime | None = None) -> SyncResult:
        """Executa uma rodada de envio de lote pendente."""
        events = self.store.pending(limit=self.batch_size, now=now)
        self._m_pending.set(self.store.count_pending())
        if not events:
            return SyncResult(None, 0, 0, 0, 0, self.wan_connected)

        batch_id = f"BATCH-{self.edge_id}-{uuid.uuid4().hex[:12]}"
        sent_ids = [ev["event_id"] for ev in events]
        payload = {
            "edge_id": self.edge_id,
            "batch_id": batch_id,
            "sent_at": now_iso(),
            "events": events,
        }

        try:
            response = self.transport.send_batch(payload)
        except (TransportError, ConnectionError, TimeoutError) as exc:
            self.wan_connected = False
            self._m_wan.set(0)
            self._m_failures.inc()
            self.store.mark_retry(sent_ids, str(exc))
            self._m_pending.set(self.store.count_pending())
            return SyncResult(batch_id, len(events), 0, 0, len(events), False, str(exc))

        self.wan_connected = True
        self._m_wan.set(1)

        sent_set = set(sent_ids)
        accepted = [eid for eid in (response.get("accepted") or []) if eid in sent_set]

        rejected_pairs: list[tuple[str, str]] = []
        rejected_ids: set[str] = set()
        for item in response.get("rejected") or []:
            if isinstance(item, dict) and item.get("event_id") in sent_set:
                eid = str(item["event_id"])
                rejected_ids.add(eid)
                rejected_pairs.append((eid, str(item.get("reason", "rejected_by_core"))))
            elif isinstance(item, str) and item in sent_set:
                rejected_ids.add(item)
                rejected_pairs.append((item, "rejected_by_core"))

        explicit_retry = {
            eid for eid in (response.get("retryable") or []) if eid in sent_set
        }
        # Qualquer evento não confirmado explicitamente permanece para retransmissão
        unconfirmed = list((sent_set - set(accepted) - rejected_ids) | explicit_retry)

        if accepted:
            self.store.mark_synced(accepted)
        if rejected_pairs:
            self.store.mark_failed(rejected_pairs)
        if unconfirmed:
            self._m_failures.inc(len(unconfirmed))
            self.store.mark_retry(unconfirmed, "retryable_or_unconfirmed")

        self._m_pending.set(self.store.count_pending())
        return SyncResult(
            batch_id=batch_id,
            sent=len(events),
            accepted=len(accepted),
            rejected=len(rejected_pairs),
            retryable=len(unconfirmed),
            wan_connected=True,
        )

    def drain(self, max_batches: int = 50, now: datetime | None = None) -> list[SyncResult]:
        """Drena todos os lotes prontos até esvaziar ou ocorrer falha de rede."""
        results: list[SyncResult] = []
        for _ in range(max_batches):
            res = self.sync_once(now=now)
            if res.sent == 0:
                break
            results.append(res)
            if not res.wan_connected or res.accepted == 0 and res.rejected == 0:
                break
        return results

    def pull_commands(self) -> list[dict[str, Any]]:
        try:
            commands = self.transport.pull_commands(self.edge_id)
            self.wan_connected = True
            self._m_wan.set(1)
            return commands
        except (TransportError, ConnectionError, TimeoutError):
            self.wan_connected = False
            self._m_wan.set(0)
            return []

    def pull_config(self) -> dict[str, Any] | None:
        try:
            cfg = self.transport.pull_config(self.edge_id)
            self.wan_connected = True
            self._m_wan.set(1)
            return cfg
        except (TransportError, ConnectionError, TimeoutError):
            self.wan_connected = False
            self._m_wan.set(0)
            return None

    def send_config_report(self, report: dict[str, Any]) -> bool:
        try:
            self.transport.send_config_report(self.edge_id, report)
            return True
        except (TransportError, ConnectionError, TimeoutError):
            return False

    def send_heartbeat(self, snapshot: dict[str, Any]) -> bool:
        try:
            self.transport.send_heartbeat(self.edge_id, snapshot)
            self.wan_connected = True
            self._m_wan.set(1)
            return True
        except (TransportError, ConnectionError, TimeoutError):
            self.wan_connected = False
            self._m_wan.set(0)
            return False
