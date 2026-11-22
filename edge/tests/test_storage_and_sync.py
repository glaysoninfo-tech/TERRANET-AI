"""Testes de persistência local, Outbox, sincronização resiliente e reinicialização."""

from datetime import UTC, datetime, timedelta

from common.device_store import DeviceStore
from contracts.canonical import CanonicalEvent
from contracts.devices import Device
from edge.storage.repository import EdgeStore, OutboxStatus
from edge.synchronization.agent import SyncAgent
from edge.synchronization.client import SyncTransport, TransportError


class FlakyCoreTransport(SyncTransport):
    """Simula CORE remoto com falhas de rede e perda de ACK após commit."""

    def __init__(self) -> None:
        self.stored: dict[str, dict] = {}
        self.fail_before_commit = False
        self.fail_after_commit = False
        self.reject_ids: set[str] = set()

    def send_batch(self, batch: dict) -> dict:
        if self.fail_before_commit:
            raise TransportError("Sem internet")
        accepted = []
        rejected = []
        for ev in batch["events"]:
            eid = ev["event_id"]
            if eid in self.reject_ids:
                rejected.append({"event_id": eid, "reason": "schema_permanently_invalid"})
                continue
            self.stored.setdefault(eid, ev)
            accepted.append(eid)
        if self.fail_after_commit:
            raise TransportError("Conexão perdida após persistência remota")
        return {"batch_id": batch["batch_id"], "accepted": accepted, "rejected": rejected, "retryable": []}


def _seed_store(db_url: str = "sqlite:///:memory:") -> EdgeStore:
    store = EdgeStore.open(db_url, backoff_base_s=1.0)
    devices = DeviceStore(store.db)
    if devices.get("NODE-001") is None:
        devices.register(
            Device(
                device_id="NODE-001",
                tenant_id="CLIENTE-001",
                site_id="FAZENDA-001",
                category="NODE-S",
                source_type="lorawan",
                source_identifier="0102030405060708",
                adapter_id="lorawan_chirpstack",
                operational_status="ACTIVE",
            )
        )
    return store


def _event(eid: str, ts: str = "2026-11-22T12:00:00Z") -> CanonicalEvent:
    return CanonicalEvent(
        event_id=eid,
        tenant_id="CLIENTE-001",
        site_id="FAZENDA-001",
        device_id="NODE-001",
        event_type="telemetry",
        observed_at=ts,
        received_at=ts,
        measurements={"water_level_m": 2.45},
        quality={"water_level_m": "valid", "timestamp_source": "network_server"},
    )


def test_outbox_idempotence_and_failure_after_remote_commit():
    store = _seed_store()
    transport = FlakyCoreTransport()
    agent = SyncAgent("EDGE-001", store, transport)

    ev = _event("EVT-1")
    assert store.ingest(ev) is True
    assert store.ingest(ev) is False  # duplicado não gera nova entrada
    assert store.count_events() == 1
    assert store.count_pending() == 1

    # Falha após commit remoto (PIT-T08)
    transport.fail_after_commit = True
    res1 = agent.sync_once()
    assert res1.wan_connected is False
    assert len(transport.stored) == 1
    assert store.count_pending() == 1

    # Reconexão -> retransmissão idempotente sem duplicar
    transport.fail_after_commit = False
    future = datetime.now(UTC) + timedelta(minutes=5)
    res2 = agent.sync_once(now=future)
    assert res2.accepted == 1
    assert len(transport.stored) == 1
    assert store.count_pending() == 0
    assert store.outbox_stats()[OutboxStatus.SYNCED] == 1


def test_permanent_rejection_goes_to_dead_letter_and_restart_preserves_pending(tmp_path):
    db_file = tmp_path / "edge.db"
    db_url = f"sqlite:///{db_file.as_posix()}"

    # Sessão 1: recebe eventos offline e encerra (simulando reinicialização — PIT-T13)
    store1 = _seed_store(db_url)
    store1.ingest(_event("EVT-GOOD", "2026-11-22T12:00:00Z"))
    store1.ingest(_event("EVT-BAD", "2026-11-22T12:01:00Z"))
    store1.db.close()

    # Sessão 2: reabre banco em disco e sincroniza
    store2 = EdgeStore.open(db_url)
    assert store2.count_events() == 2
    assert store2.count_pending() == 2

    transport = FlakyCoreTransport()
    transport.reject_ids.add("EVT-BAD")
    agent = SyncAgent("EDGE-001", store2, transport)
    res = agent.sync_once()
    assert res.accepted == 1 and res.rejected == 1
    assert store2.count_pending() == 0
    assert len(store2.dead_letters(stage="sync")) == 1
    store2.db.close()
