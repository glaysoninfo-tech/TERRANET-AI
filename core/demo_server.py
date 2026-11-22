"""Servidor local de demonstração e bancada IHC da plataforma TERRANET AI (PIT-001).

Integra em um único processo (para visualização imediata no navegador):
* TERRANET CORE (FastAPI + SQLite + serviços reais de ingestão, comandos, geoespacial, analytics)
* TERRANET EDGE real (`EdgeRuntime` com SQLite local, Transactional Outbox, Motor de Regras,
  Command Validator, adaptadores LoRaWAN/Modbus/TRACK e `SimulatedPumpPLC`)
* Rotas `/api/v1/sandbox/*` para acionar eventos de bancada diretamente pela IHC.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from common.timeutil import iso, now_iso, utc_now
from contracts.commands import Command, CommandState, derive_edge_key, sign_command
from contracts.devices import Device
from contracts.rules import Rule
from core.api.app import create_app
from core.identity.auth import EdgePrincipal
from core.storage.repository import CoreStore
from core.telemetry.service import TelemetryIngestionService
from edge.adapters.lorawan.decoders import encode_level_v1
from edge.adapters.modbus.client import SimulatedPumpPLC
from edge.service import EdgeRuntime
from edge.synchronization.client import SyncTransport, TransportError


class InProcessCoreTransport(SyncTransport):
    """Conecta o SyncAgent do EDGE diretamente aos serviços do CORE, com injeção de falhas."""

    def __init__(self, store: CoreStore, edge_id: str):
        self.store = store
        self.edge_id = edge_id
        self.telemetry = TelemetryIngestionService(store)
        self.offline = False
        self.drop_after_commit = False

    def _principal(self) -> EdgePrincipal:
        row = self.store.get_edge(self.edge_id)
        if not row or row["status"] != "ACTIVE":
            raise TransportError("EDGE revogado ou inexistente", 401)
        return EdgePrincipal(
            edge_id=row["edge_id"],
            tenant_id=row["tenant_id"],
            site_ids=tuple(row["site_ids"]),
        )

    def send_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        if self.offline:
            raise TransportError("Internet indisponível (modo offline simulado)")
        resp = self.telemetry.ingest_batch(self._principal(), batch)
        if self.drop_after_commit:
            raise TransportError("Conexão perdida após persistência central e antes do ACK (PIT-T08)")
        return resp

    def pull_commands(self, edge_id: str) -> list[dict[str, Any]]:
        if self.offline:
            raise TransportError("Internet indisponível")
        return self.store.pop_pending_commands_for_edge(edge_id)

    def pull_config(self, edge_id: str) -> dict[str, Any] | None:
        if self.offline:
            raise TransportError("Internet indisponível")
        p = self._principal()
        edge_row = self.store.get_edge(edge_id) or {}
        devices = self.store.devices.list(tenant_id=p.tenant_id, site_ids=list(p.site_ids))
        rules = self.store.list_rules(tenant_id=p.tenant_id, site_ids=list(p.site_ids))
        return {
            "edge_id": edge_id,
            "version": int(edge_row.get("config_version", 1)),
            "devices": [d.to_dict() for d in devices],
            "rules": rules,
        }

    def send_config_report(self, edge_id: str, report: dict[str, Any]) -> None:
        if not self.offline:
            self.store.save_config_report(edge_id, report)

    def send_heartbeat(self, edge_id: str, metrics: dict[str, Any]) -> None:
        if not self.offline:
            self.store.record_edge_heartbeat(edge_id, metrics)


# Trajeto simulado para o veículo V01 (região de Brasília / área rural)
_TRACK_ROUTE: list[tuple[float, float, float, float]] = [
    (-15.793889, -47.882778, 38.0, 90.0),
    (-15.792100, -47.878200, 46.5, 72.0),
    (-15.789400, -47.873100, 54.0, 58.0),
    (-15.786200, -47.868900, 61.2, 45.0),
    (-15.782900, -47.865400, 49.0, 30.0),
    (-15.780500, -47.869800, 34.5, 290.0),
    (-15.783400, -47.876200, 42.0, 240.0),
    (-15.788900, -47.881000, 28.0, 205.0),
]


class UplinkSimRequest(BaseModel):
    manufacturer: str = "alfa"  # "alfa" (JSON cm) ou "beta" (binário mm)
    water_level_m: float = 2.65
    battery_voltage_v: float = 12.6
    duplicate: bool = False


class PlcInterlockRequest(BaseModel):
    dry_run_interlock_ok: bool | None = None
    remote_mode_enabled: bool | None = None
    overload_fault: bool | None = None
    emergency_stop_active: bool | None = None
    suction_level_m: float | None = None


class WanToggleRequest(BaseModel):
    online: bool


class SyncRequest(BaseModel):
    simulate_drop_after_commit: bool = False


class ExpiredCmdRequest(BaseModel):
    device_id: str = "TN-PUMP-P01"
    operation: str = "start_pump"


def build_demo_app(work_dir: str | None = None) -> FastAPI:
    base = Path(work_dir or tempfile.mkdtemp(prefix="terranet_demo_"))
    base.mkdir(parents=True, exist_ok=True)
    master_key = "terranet-pit001-demo-master-key"

    core_store = CoreStore.open(f"sqlite:///{(base / 'core.db').as_posix()}")
    core_store.create_tenant("CLIENTE-001", "Fazenda Modelo Hidrológica")
    core_store.create_tenant("CLIENTE-002", "Agropecuária Horizonte")
    core_store.create_site("FAZENDA-001", "CLIENTE-001", "Estação Rio Claro (Sede)")
    core_store.create_site("FAZENDA-002", "CLIENTE-002", "Unidade Cerrado")

    u_op1, key_op1 = core_store.create_user("operador_c1", "CLIENTE-001", role="operator")
    u_view1, key_view1 = core_store.create_user("auditor_c1", "CLIENTE-001", role="viewer")
    u_op2, key_op2 = core_store.create_user("operador_c2", "CLIENTE-002", role="operator")
    core_store.register_edge("EDGE-001", "CLIENTE-001", ["FAZENDA-001"])
    core_store.register_edge("EDGE-002", "CLIENTE-002", ["FAZENDA-002"])

    # Dispositivos MVP — Cliente 001 (H01 x2 fabricantes, P01 bomba, V01 rastreador)
    devices_seed = [
        Device.from_profile(
            {
                "device_id": "TN-NODE-H01-A",
                "tenant_id": "CLIENTE-001",
                "site_id": "FAZENDA-001",
                "category": "NODE-S",
                "manufacturer": "Fabricante Alfa",
                "model": "HydroLora-100",
                "firmware_version": "2.4.1",
                "communication": {"type": "lorawan", "dev_eui": "0102030405060708"},
                "adapter": {"name": "lorawan_chirpstack", "version": "1.0"},
                "capabilities": ["water_level_measurement", "battery_monitoring"],
                "operational_status": "ACTIVE",
                "metadata": {
                    "decoder": "chirpstack_object",
                    "field_map": {
                        "water_level_m": {"field": "nivel_cm", "unit": "cm"},
                        "battery_voltage_v": {"field": "bat_mv", "unit": "mV"},
                    },
                    "ranges": {"water_level_m": [0, 20], "battery_voltage_v": [8, 16]},
                },
            }
        ),
        Device.from_profile(
            {
                "device_id": "TN-NODE-H01-B",
                "tenant_id": "CLIENTE-001",
                "site_id": "FAZENDA-001",
                "category": "NODE-S",
                "manufacturer": "Fabricante Beta",
                "model": "RiverProbe-Bin2",
                "firmware_version": "1.8.0",
                "communication": {"type": "lorawan", "dev_eui": "1122334455667788"},
                "adapter": {"name": "lorawan_chirpstack", "version": "1.0"},
                "capabilities": ["water_level_measurement", "battery_monitoring"],
                "operational_status": "ACTIVE",
                "metadata": {
                    "decoder": "terranet_level_v1",
                    "ranges": {"water_level_m": [0, 20], "battery_voltage_v": [8, 16]},
                },
            }
        ),
        Device.from_profile(
            {
                "device_id": "TN-PUMP-P01",
                "tenant_id": "CLIENTE-001",
                "site_id": "FAZENDA-001",
                "category": "NODE-C",
                "manufacturer": "CLP Bancada Industrial",
                "model": "SimPLC-P01",
                "firmware_version": "3.1.0",
                "communication": {"type": "modbus", "identifier": "plc-p01", "unit_id": 1},
                "adapter": {"name": "modbus_plc", "version": "1.0"},
                "capabilities": ["pump_supervision", "interlock_monitoring", "supervised_control"],
                "operational_status": "ACTIVE",
                "metadata": {"allowed_operations": ["start_pump", "stop_pump"]},
            }
        ),
        Device.from_profile(
            {
                "device_id": "TN-TRACK-V01",
                "tenant_id": "CLIENTE-001",
                "site_id": "FAZENDA-001",
                "category": "TRACK-B",
                "manufacturer": "Teltonika",
                "model": "FMC130",
                "firmware_version": "03.29.00",
                "communication": {"type": "tracker", "imei": "861234567890123"},
                "adapter": {"name": "tracker_gnss_lte", "version": "1.0"},
                "capabilities": ["gnss_tracking", "ignition_monitoring", "battery_monitoring"],
                "operational_status": "ACTIVE",
            }
        ),
        # Dispositivo de outro cliente para comprovar isolamento multicliente (PIT-T15)
        Device.from_profile(
            {
                "device_id": "TN-NODE-C2-01",
                "tenant_id": "CLIENTE-002",
                "site_id": "FAZENDA-002",
                "category": "NODE-S",
                "manufacturer": "Fabricante Alfa",
                "model": "HydroLora-100",
                "firmware_version": "2.4.1",
                "communication": {"type": "lorawan", "dev_eui": "9988776655443322"},
                "adapter": {"name": "lorawan_chirpstack", "version": "1.0"},
                "capabilities": ["water_level_measurement"],
                "operational_status": "ACTIVE",
            }
        ),
    ]
    for dev in devices_seed:
        core_store.devices.register(dev, actor="bootstrap")

    # Regra hidrológica H01 com histerese
    core_store.save_rule(
        "CLIENTE-001",
        "FAZENDA-001",
        Rule(
            rule_id="R-H01-CHEIA",
            version=1,
            rule_type="threshold",
            device_ids=["TN-NODE-H01-A", "TN-NODE-H01-B"],
            severity="critical",
            author="eng_hidrologia",
            description="Alerta de cheia acima de 4,0 m (histerese 0,3 m -> normaliza abaixo de 3,7 m)",
            params={"measurement": "water_level_m", "operator": ">", "value": 4.0, "hysteresis": 0.3},
        ),
    )

    transport = InProcessCoreTransport(core_store, "EDGE-001")
    edge = EdgeRuntime(
        edge_id="EDGE-001",
        db_url=f"sqlite:///{(base / 'edge.db').as_posix()}",
        edge_key=derive_edge_key(master_key, "EDGE-001"),
        transport=transport,
    )
    edge.start_mqtt_subscriptions()
    plc = SimulatedPumpPLC(pump_running=False, suction_level_m=1.85)
    edge.attach_modbus_client("TN-PUMP-P01", plc)

    # Sincroniza configuração inicial CORE -> EDGE e registra heartbeat
    edge.apply_config_intention(edge.sync.pull_config())
    edge.sync.send_heartbeat(edge.monitoring.collect(wan_connected=True))

    # Popula série histórica realista de H01, P01 e V01
    now_dt = utc_now()
    history_levels = [2.15, 2.28, 2.45, 2.62, 2.74, 2.85]
    for idx, lvl in enumerate(history_levels):
        ts = iso(now_dt - timedelta(minutes=(len(history_levels) - idx) * 15))
        edge.bus.publish(
            "application/app1/device/0102030405060708/event/up",
            {
                "deduplicationId": f"seed-a-{idx}",
                "time": ts,
                "deviceInfo": {"devEui": "0102030405060708"},
                "object": {"nivel_cm": round(lvl * 100), "bat_mv": 12720 - idx * 15},
                "rxInfo": [{"rssi": -84 - idx, "snr": 8.2}],
            },
        )
    edge.bus.publish(
        "application/app1/device/1122334455667788/event/up",
        {
            "deduplicationId": "seed-b-0",
            "time": iso(now_dt - timedelta(minutes=8)),
            "deviceInfo": {"devEui": "1122334455667788"},
            "data": encode_level_v1(level_mm=2810, battery_mv=12550),
            "rxInfo": [{"rssi": -91, "snr": 6.4}],
        },
    )
    edge.poll_modbus("TN-PUMP-P01", poll_id="seed-plc-0", observed_at=iso(now_dt - timedelta(minutes=2)))
    edge.sync_now()

    telemetry_svc = TelemetryIngestionService(core_store)
    for idx in range(4):
        lat, lon, spd, hdg = _TRACK_ROUTE[idx]
        telemetry_svc.ingest_track_packet(
            {
                "imei": "861234567890123",
                "record_id": f"seed-trk-{idx}",
                "timestamp": iso(now_dt - timedelta(minutes=(4 - idx) * 3)),
                "gnss": {"lat": lat, "lon": lon, "speed_kmh": spd, "heading_deg": hdg, "satellites": 11},
                "io": {"ignition": True, "battery_mv": 4120, "odometer_m": 152100 + idx * 450},
            }
        )

    state_box = {"track_idx": 4, "last_dedup": None}

    app = create_app(core_store, master_key=master_key)

    profiles = [
        {
            "id": "op_c1",
            "label": "CLIENTE-001 · Operador (Fazenda Modelo)",
            "tenant_id": "CLIENTE-001",
            "username": u_op1["username"],
            "role": "operator",
            "api_key": key_op1,
        },
        {
            "id": "view_c1",
            "label": "CLIENTE-001 · Visualizador (RBAC somente leitura)",
            "tenant_id": "CLIENTE-001",
            "username": u_view1["username"],
            "role": "viewer",
            "api_key": key_view1,
        },
        {
            "id": "op_c2",
            "label": "CLIENTE-002 · Operador (Isolamento multicliente)",
            "tenant_id": "CLIENTE-002",
            "username": u_op2["username"],
            "role": "operator",
            "api_key": key_op2,
        },
    ]

    # ---------------------------------------------------- Rotas de bancada IHC
    @app.get("/api/v1/sandbox/profiles")
    def sandbox_profiles() -> dict[str, Any]:
        return {"profiles": profiles}

    @app.get("/api/v1/sandbox/status")
    def sandbox_status() -> dict[str, Any]:
        if not transport.offline:
            edge.sync.send_heartbeat(edge.monitoring.collect(wan_connected=True))
        ok, interlock_reason = plc.interlocks_ok()
        return {
            "edge_id": edge.edge_id,
            "wan_online": not transport.offline,
            "outbox": edge.store.outbox_stats(),
            "edge_pending": edge.store.count_pending(),
            "edge_total_events": edge.store.count_events(),
            "core_total_events": core_store.count_events("CLIENTE-001"),
            "edge_local_alarms": edge.store.count_events(event_type="event"),
            "plc": {
                "pump_running": plc.pump_running,
                "remote_mode_enabled": plc.remote_mode_enabled,
                "dry_run_interlock_ok": plc.dry_run_interlock_ok,
                "overload_fault": plc.overload_fault,
                "emergency_stop_active": plc.emergency_stop_active,
                "suction_level_m": plc.suction_level_m,
                "discharge_pressure_bar": plc.discharge_pressure_bar,
                "motor_current_a": plc.motor_current_a,
                "flow_rate_m3h": plc.flow_rate_m3h,
                "motor_temperature_c": plc.motor_temperature_c,
                "interlocks_ok": ok,
                "interlock_reason": interlock_reason,
                "physical_actuations": plc.physical_actuations,
            },
            "rule_h01_state": edge.rule_store.get_state("R-H01-CHEIA", "TN-NODE-H01-A"),
        }

    @app.post("/api/v1/sandbox/uplink")
    def sandbox_uplink(body: UplinkSimRequest) -> dict[str, Any]:
        dedup = (
            state_box["last_dedup"]
            if (body.duplicate and state_box["last_dedup"])
            else f"sim-{uuid.uuid4().hex[:10]}"
        )
        state_box["last_dedup"] = dedup
        ts = now_iso()
        before_events = edge.store.count_events()
        if body.manufacturer == "beta":
            dev_eui = "1122334455667788"
            raw = {
                "deduplicationId": dedup,
                "time": ts,
                "deviceInfo": {"devEui": dev_eui},
                "data": encode_level_v1(
                    level_mm=max(0, int(round(body.water_level_m * 1000))),
                    battery_mv=max(0, int(round(body.battery_voltage_v * 1000))),
                ),
                "rxInfo": [{"rssi": -90, "snr": 6.5}],
            }
        else:
            dev_eui = "0102030405060708"
            raw = {
                "deduplicationId": dedup,
                "time": ts,
                "deviceInfo": {"devEui": dev_eui},
                "object": {
                    "nivel_cm": round(body.water_level_m * 100, 1),
                    "bat_mv": round(body.battery_voltage_v * 1000),
                },
                "rxInfo": [{"rssi": -85, "snr": 8.0}],
            }
        edge.bus.publish(f"application/app1/device/{dev_eui}/event/up", raw)
        created = edge.store.count_events() - before_events
        synced = 0
        if not transport.offline:
            results = edge.sync_now(now=utc_now() + timedelta(hours=1))
            synced = sum(r.accepted for r in results)
        return {
            "deduplication_id": dedup,
            "duplicate_detected": created == 0,
            "new_local_events": created,
            "synced_now": synced,
            "pending_outbox": edge.store.count_pending(),
        }

    @app.post("/api/v1/sandbox/plc/interlocks")
    def sandbox_plc_interlocks(body: PlcInterlockRequest) -> dict[str, Any]:
        if body.dry_run_interlock_ok is not None:
            plc.dry_run_interlock_ok = body.dry_run_interlock_ok
        if body.remote_mode_enabled is not None:
            plc.remote_mode_enabled = body.remote_mode_enabled
        if body.overload_fault is not None:
            plc.overload_fault = body.overload_fault
        if body.emergency_stop_active is not None:
            plc.emergency_stop_active = body.emergency_stop_active
        if body.suction_level_m is not None:
            plc.suction_level_m = body.suction_level_m
        plc.trigger_interlock_trip()
        edge.poll_modbus("TN-PUMP-P01", poll_id=f"plc-{uuid.uuid4().hex[:8]}")
        if not transport.offline:
            edge.sync_now(now=utc_now() + timedelta(hours=1))
        return sandbox_status()["plc"]

    @app.post("/api/v1/sandbox/edge/wan")
    def sandbox_wan(body: WanToggleRequest) -> dict[str, Any]:
        transport.offline = not body.online
        if body.online:
            edge.sync.send_heartbeat(edge.monitoring.collect(wan_connected=True))
        else:
            edge.monitoring.wan_connected.set(0)
        return {"wan_online": not transport.offline, "pending_outbox": edge.store.count_pending()}

    @app.post("/api/v1/sandbox/edge/sync")
    def sandbox_sync(body: SyncRequest) -> dict[str, Any]:
        if transport.offline:
            raise HTTPException(status_code=409, detail="EDGE está offline; reconecte a WAN antes de sincronizar")
        transport.drop_after_commit = body.simulate_drop_after_commit
        try:
            # 1. Buscar e executar comandos autorizados pendentes no CORE
            cmd_outcomes = edge.poll_and_execute_commands()
            # 2. Sincronizar eventos + command_results (avança clock para ignorar backoff no teste manual)
            results = edge.sync_now(now=utc_now() + timedelta(hours=2))
            if not body.simulate_drop_after_commit:
                edge.poll_modbus("TN-PUMP-P01", poll_id=f"sync-{uuid.uuid4().hex[:8]}")
                results.extend(edge.sync_now(now=utc_now() + timedelta(hours=2)))
                edge.sync.send_heartbeat(edge.monitoring.collect(wan_connected=True))
        finally:
            transport.drop_after_commit = False
        return {
            "command_outcomes": [
                {"command_id": cid, "state": st.value, "reason": rsn}
                for cid, st, rsn in cmd_outcomes
            ],
            "batches": [
                {
                    "batch_id": r.batch_id,
                    "accepted": r.accepted,
                    "rejected": r.rejected,
                    "retryable": r.retryable,
                    "wan_connected": r.wan_connected,
                    "error": r.error,
                }
                for r in results
            ],
            "pending_outbox": edge.store.count_pending(),
            "core_total_events": core_store.count_events("CLIENTE-001"),
        }

    @app.post("/api/v1/sandbox/edge/dispatch-commands")
    def sandbox_dispatch_commands() -> dict[str, Any]:
        if transport.offline:
            return {"dispatched": 0, "reason": "edge_offline"}
        outcomes = edge.poll_and_execute_commands()
        edge.poll_modbus("TN-PUMP-P01", poll_id=f"cmd-{uuid.uuid4().hex[:8]}")
        edge.sync_now(now=utc_now() + timedelta(hours=2))
        return {
            "dispatched": len(outcomes),
            "outcomes": [
                {"command_id": cid, "state": st.value, "reason": rsn}
                for cid, st, rsn in outcomes
            ],
        }

    @app.post("/api/v1/sandbox/test-expired-command")
    def sandbox_expired_command(body: ExpiredCmdRequest) -> dict[str, Any]:
        """Simula PIT-T11: comando assinado, porém entregue ao EDGE após expirar."""
        now = datetime.now(UTC)
        cmd = Command(
            command_id=f"CMD-EXP-{uuid.uuid4().hex[:6]}",
            tenant_id="CLIENTE-001",
            site_id="FAZENDA-001",
            device_id=body.device_id,
            operation=body.operation,
            requested_by="operador_c1",
            issued_at=iso(now - timedelta(minutes=2)),
            expires_at=iso(now - timedelta(seconds=30)),
            configuration_version=1,
            correlation_id=f"CORR-EXP-{uuid.uuid4().hex[:6]}",
            state=CommandState.DISPATCHED.value,
        )
        cmd.signature = sign_command(cmd, derive_edge_key(master_key, "EDGE-001"))
        core_store.save_command(cmd, edge_id="EDGE-001", reason="simulated_expired_in_transit")
        st, reason, _ = edge.execute_command(cmd, now=now)
        if not transport.offline:
            edge.sync_now(now=utc_now() + timedelta(hours=2))
        return {"command_id": cmd.command_id, "state": st.value, "reason": reason}

    @app.post("/api/v1/sandbox/track/step")
    def sandbox_track_step() -> dict[str, Any]:
        idx = state_box["track_idx"] % len(_TRACK_ROUTE)
        state_box["track_idx"] += 1
        lat, lon, spd, hdg = _TRACK_ROUTE[idx]
        res = telemetry_svc.ingest_track_packet(
            {
                "imei": "861234567890123",
                "record_id": f"trk-{uuid.uuid4().hex[:8]}",
                "timestamp": now_iso(),
                "gnss": {"lat": lat, "lon": lon, "speed_kmh": spd, "heading_deg": hdg, "satellites": 12},
                "io": {"ignition": True, "battery_mv": 4090, "odometer_m": 153800 + state_box["track_idx"] * 380},
            }
        )
        return {"step": idx, "lat": lat, "lon": lon, "speed_kmh": spd, **res}

    return app


def main() -> None:
    import uvicorn

    host = os.environ.get("TERRANET_UI_HOST", "127.0.0.1")
    port = int(os.environ.get("TERRANET_UI_PORT", "8000"))
    app = build_demo_app()
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
