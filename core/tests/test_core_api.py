"""Testes de integração da API TERRANET CORE (E09, RBAC, isolamento multicliente, geoespacial)."""

from fastapi.testclient import TestClient

from core.api.app import create_app
from core.storage.repository import CoreStore


def _setup_core():
    store = CoreStore.open("sqlite:///:memory:")
    store.create_tenant("CLIENTE-A", "Cliente A")
    store.create_tenant("CLIENTE-B", "Cliente B")
    store.create_site("SITE-A1", "CLIENTE-A", "Fazenda A1")
    store.create_site("SITE-B1", "CLIENTE-B", "Fazenda B1")

    _, key_a_op = store.create_user("op_a", "CLIENTE-A", role="operator")
    _, key_a_view = store.create_user("view_a", "CLIENTE-A", role="viewer")
    _, key_b_op = store.create_user("op_b", "CLIENTE-B", role="operator")
    _, edge_a_tok = store.register_edge("EDGE-A1", "CLIENTE-A", ["SITE-A1"])

    app = create_app(store, master_key="test-master-key")
    client = TestClient(app)
    return store, client, {
        "op_a": {"Authorization": f"Bearer {key_a_op}"},
        "view_a": {"Authorization": f"Bearer {key_a_view}"},
        "op_b": {"Authorization": f"Bearer {key_b_op}"},
        "edge_a": {"Authorization": f"Bearer {edge_a_tok}"},
    }


def test_core_device_lifecycle_ingest_idempotence_and_analytics():
    _, client, hdr = _setup_core()

    # Cadastro de dispositivo (operador OK, viewer proibido)
    profile = {
        "device_id": "NODE-H01",
        "site_id": "SITE-A1",
        "category": "NODE-S",
        "communication": {"type": "lorawan", "dev_eui": "0102030405060708"},
        "adapter": {"name": "lorawan_chirpstack", "version": "1.0"},
        "capabilities": ["water_level_measurement"],
    }
    assert client.post("/api/v1/devices", json=profile, headers=hdr["view_a"]).status_code == 403
    r = client.post("/api/v1/devices", json=profile, headers=hdr["op_a"])
    assert r.status_code == 201

    # Provisionar e ativar
    assert client.post("/api/v1/devices/NODE-H01/transition", json={"state": "PROVISIONED"}, headers=hdr["op_a"]).status_code == 200
    assert client.post("/api/v1/devices/NODE-H01/transition", json={"state": "ACTIVE"}, headers=hdr["op_a"]).status_code == 200

    # Ingestão de lote + duplicado idempotente
    batch = {
        "edge_id": "EDGE-A1",
        "batch_id": "B-1",
        "events": [
            {
                "schema_version": "1.0",
                "event_id": "EVT-H01-1",
                "tenant_id": "CLIENTE-A",
                "site_id": "SITE-A1",
                "device_id": "NODE-H01",
                "event_type": "telemetry",
                "observed_at": "2026-11-22T12:00:00Z",
                "received_at": "2026-11-22T12:00:01Z",
                "measurements": {"water_level_m": 2.1},
                "quality": {"water_level_m": "valid", "timestamp_source": "network_server"},
            },
            {
                "schema_version": "1.0",
                "event_id": "EVT-H01-2",
                "tenant_id": "CLIENTE-A",
                "site_id": "SITE-A1",
                "device_id": "NODE-H01",
                "event_type": "telemetry",
                "observed_at": "2026-11-22T13:00:00Z",
                "received_at": "2026-11-22T13:00:01Z",
                "measurements": {"water_level_m": 2.6},
                "quality": {"water_level_m": "valid", "timestamp_source": "network_server"},
            },
        ],
    }
    res1 = client.post("/api/v1/ingest/batches", json=batch, headers=hdr["edge_a"])
    res2 = client.post("/api/v1/ingest/batches", json=batch, headers=hdr["edge_a"])
    assert res1.status_code == 200 and res1.json()["accepted"] == ["EVT-H01-1", "EVT-H01-2"]
    assert res2.status_code == 200 and res2.json()["accepted"] == ["EVT-H01-1", "EVT-H01-2"]

    # Consulta retorna apenas 2 eventos (sem duplicação)
    events = client.get("/api/v1/telemetry?device_id=NODE-H01", headers=hdr["view_a"]).json()
    assert len(events) == 2

    # Analytics nível inicial (§22.1)
    summary = client.get(
        "/api/v1/analytics/NODE-H01/summary?measurement=water_level_m", headers=hdr["view_a"]
    ).json()
    assert summary["count"] == 2
    assert summary["trend_per_h"] == 0.5


def test_core_multi_tenant_isolation_and_geospatial_track():
    _, client, hdr = _setup_core()

    # Cliente A cadastra TRACK-B e ativa
    track_profile = {
        "device_id": "TRACK-A1",
        "site_id": "SITE-A1",
        "category": "TRACK-B",
        "communication": {"type": "tracker", "imei": "356789012345678"},
        "adapter": {"name": "tracker_gnss_lte"},
    }
    assert client.post("/api/v1/devices", json=track_profile, headers=hdr["op_a"]).status_code == 201
    client.post("/api/v1/devices/TRACK-A1/transition", json={"state": "PROVISIONED"}, headers=hdr["op_a"])
    client.post("/api/v1/devices/TRACK-A1/transition", json={"state": "ACTIVE"}, headers=hdr["op_a"])

    # Ingestão direta do rastreador (§3, V01)
    packet = {
        "imei": "356789012345678",
        "timestamp": "2026-11-22T12:15:00Z",
        "gnss": {"lat": -23.55052, "lon": -46.633308, "speed_kmh": 55.0},
        "io": {"ignition": True, "battery_mv": 4150},
    }
    r = client.post("/api/v1/ingest/track", json=packet, headers=hdr["op_a"])
    assert r.status_code == 200 and r.json()["new_events"] == 1

    # Cliente A vê a posição e a consulta por raio
    pos_a = client.get("/api/v1/geospatial/positions", headers=hdr["op_a"]).json()
    assert len(pos_a) == 1 and pos_a[0]["device_id"] == "TRACK-A1"
    nearby = client.get(
        "/api/v1/geospatial/nearby?lat=-23.5505&lon=-46.6333&radius_m=500", headers=hdr["op_a"]
    ).json()
    assert len(nearby) == 1

    # Cliente B NÃO pode ver dispositivos, telemetria, posições ou auditoria do Cliente A (PIT-T15)
    assert client.get("/api/v1/devices", headers=hdr["op_b"]).json() == []
    assert client.get("/api/v1/devices/TRACK-A1", headers=hdr["op_b"]).status_code == 403
    assert client.get("/api/v1/telemetry?device_id=TRACK-A1", headers=hdr["op_b"]).status_code == 403
    assert client.get("/api/v1/geospatial/positions", headers=hdr["op_b"]).json() == []
    assert client.get("/api/v1/geospatial/tracks/TRACK-A1", headers=hdr["op_b"]).status_code == 403
    assert client.post("/api/v1/ingest/track", json=packet, headers=hdr["op_b"]).status_code == 403
