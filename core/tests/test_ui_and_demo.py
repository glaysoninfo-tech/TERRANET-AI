"""Testes da IHC web local e do servidor de demonstração de bancada (demo_server)."""

from fastapi.testclient import TestClient

from core.demo_server import build_demo_app


def test_demo_ui_assets_and_interactive_flows(tmp_path):
    app = build_demo_app(work_dir=str(tmp_path))
    client = TestClient(app)

    # IHC servida em / e /ui/*
    r_root = client.get("/")
    assert r_root.status_code == 200 and "TERRANET AI" in r_root.text
    assert client.get("/ui/app.js").status_code == 200
    assert client.get("/ui/styles.css").status_code == 200

    # Perfis de demonstração e status inicial
    profiles = client.get("/api/v1/sandbox/profiles").json()["profiles"]
    op1 = next(p for p in profiles if p["id"] == "op_c1")
    view1 = next(p for p in profiles if p["id"] == "view_c1")
    op2 = next(p for p in profiles if p["id"] == "op_c2")
    hdr_op1 = {"Authorization": f"Bearer {op1['api_key']}"}
    hdr_view1 = {"Authorization": f"Bearer {view1['api_key']}"}
    hdr_op2 = {"Authorization": f"Bearer {op2['api_key']}"}

    # Isolamento multicliente visível pela API
    assert len(client.get("/api/v1/devices", headers=hdr_op1).json()) == 4
    assert len(client.get("/api/v1/devices", headers=hdr_op2).json()) == 1
    assert len(client.get("/api/v1/edges", headers=hdr_op1).json()) == 1

    # Modo offline + uplink acima de 4.0m gera alarme local no EDGE + duplicado ignorado
    assert client.post("/api/v1/sandbox/edge/wan", json={"online": False}).status_code == 200
    u1 = client.post(
        "/api/v1/sandbox/uplink",
        json={"manufacturer": "alfa", "water_level_m": 4.4, "duplicate": False},
    ).json()
    assert u1["new_local_events"] == 2  # telemetria + alarme local R-H01-CHEIA
    assert u1["pending_outbox"] == 2

    u_dup = client.post(
        "/api/v1/sandbox/uplink",
        json={"manufacturer": "alfa", "water_level_m": 4.4, "duplicate": True},
    ).json()
    assert u_dup["duplicate_detected"] is True
    assert u_dup["pending_outbox"] == 2

    # Reconecta e testa queda pós-commit + ressincronização idempotente
    assert client.post("/api/v1/sandbox/edge/wan", json={"online": True}).status_code == 200
    s_drop = client.post("/api/v1/sandbox/edge/sync", json={"simulate_drop_after_commit": True}).json()
    assert s_drop["pending_outbox"] == 2
    s_ok = client.post("/api/v1/sandbox/edge/sync", json={"simulate_drop_after_commit": False}).json()
    assert s_ok["pending_outbox"] == 0

    # RBAC: visualizador não pode acionar bomba
    assert client.post(
        "/api/v1/commands",
        json={"device_id": "TN-PUMP-P01", "operation": "start_pump", "ttl_s": 30},
        headers=hdr_view1,
    ).status_code == 403

    # Intertravamento físico a seco bloqueia acionamento
    client.post("/api/v1/sandbox/plc/interlocks", json={"dry_run_interlock_ok": False})
    assert client.post(
        "/api/v1/commands",
        json={"device_id": "TN-PUMP-P01", "operation": "start_pump", "ttl_s": 30},
        headers=hdr_op1,
    ).status_code == 201
    disp = client.post("/api/v1/sandbox/edge/dispatch-commands").json()
    assert disp["outcomes"][0]["state"] == "REJECTED"

    # Comando expirado (PIT-T11)
    exp = client.post("/api/v1/sandbox/test-expired-command", json={}).json()
    assert exp["state"] == "EXPIRED"

    # Passo GNSS do rastreador V01
    step = client.post("/api/v1/sandbox/track/step").json()
    assert step["new_events"] == 1
