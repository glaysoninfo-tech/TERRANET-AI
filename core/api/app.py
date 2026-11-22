"""API Gateway modular do TERRANET CORE (FastAPI, PIT-001 §4.1, §13, §14, E09)."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from common.device_store import DeviceNotFound, LifecycleError
from common.metrics import Registry
from contracts.rules import Rule, RuleValidationError
from core.analytics.service import AnalyticsService
from core.commands.service import CommandManagementService, CommandRejected
from core.devices.service import DeviceManagementService
from core.geospatial.service import GeospatialService
from core.identity.auth import (
    AuthError,
    EdgePrincipal,
    ForbiddenError,
    IdentityService,
    UserPrincipal,
)
from core.storage.repository import CoreStore
from core.telemetry.service import TelemetryIngestionService


class _RateLimiter:
    """Limitador simples por chave (janela deslizante em memória, §13.3)."""

    def __init__(self, max_requests: int = 300, window_s: float = 60.0):
        self.max_requests = max_requests
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_s
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.max_requests:
                return False
            q.append(now)
            return True


# ----------------------------------------------------------- schemas de API
class TransitionRequest(BaseModel):
    state: str
    reason: str = ""


class CommandCreateRequest(BaseModel):
    device_id: str
    operation: str
    ttl_s: int = Field(default=30, ge=5, le=600)
    params: dict[str, Any] = Field(default_factory=dict)
    expected_state: dict[str, Any] = Field(default_factory=dict)
    confirmation_policy: str = "feedback"
    correlation_id: str | None = None
    require_edge_online: bool = True


class RuleCreateRequest(BaseModel):
    site_id: str
    rule: dict[str, Any]


def create_app(
    store: CoreStore,
    master_key: bytes | str,
    rate_limit_rpm: int = 300,
) -> FastAPI:
    app = FastAPI(
        title="TERRANET CORE API",
        version="1.0.0",
        description="PIT-001 — Supervisão centralizada, ingestão idempotente, comandos e geoespacial",
    )

    metrics = Registry()
    identity = IdentityService(store)
    devices_svc = DeviceManagementService(store)
    telemetry_svc = TelemetryIngestionService(store, metrics=metrics)
    commands_svc = CommandManagementService(store, master_key=master_key)
    geo_svc = GeospatialService(store)
    analytics_svc = AnalyticsService(store)
    limiter = _RateLimiter(max_requests=rate_limit_rpm, window_s=60.0)

    # Referências úteis em testes/inspeção
    app.state.store = store
    app.state.metrics = metrics
    app.state.commands = commands_svc

    def current_user(authorization: str | None = Header(default=None)) -> UserPrincipal:
        try:
            return identity.authenticate_user(authorization)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def current_edge(authorization: str | None = Header(default=None)) -> EdgePrincipal:
        try:
            return identity.authenticate_edge(authorization)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    # -------------------------------------------------------- observabilidade
    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "dialect": store.db.dialect}

    @app.get("/metrics")
    def prometheus_metrics() -> Response:
        return Response(
            content=metrics.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # ---------------------------------------------------- ingestão EDGE (§13)
    @app.post("/api/v1/ingest/batches")
    def ingest_batches(
        payload: dict[str, Any],
        edge: EdgePrincipal = Depends(current_edge),
    ) -> dict[str, Any]:
        if not limiter.allow(edge.edge_id):
            raise HTTPException(status_code=429, detail="Limite de requisições excedido")
        try:
            return telemetry_svc.ingest_batch(edge, payload)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # --------------------------------------------------- ingestão TRACK (§3)
    @app.post("/api/v1/ingest/track")
    def ingest_track(
        payload: dict[str, Any],
        user: UserPrincipal = Depends(current_user),
    ) -> dict[str, Any]:
        user.require_role("operator")
        try:
            return telemetry_svc.ingest_track_packet(payload, tenant_id=user.tenant_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------------- endpoints do EDGE
    @app.get("/api/v1/edges/{edge_id}/commands")
    def edge_pull_commands(
        edge_id: str,
        edge: EdgePrincipal = Depends(current_edge),
    ) -> dict[str, Any]:
        if edge.edge_id != edge_id:
            raise HTTPException(status_code=403, detail="EDGE não autorizado")
        return {"edge_id": edge_id, "commands": store.pop_pending_commands_for_edge(edge_id)}

    @app.get("/api/v1/edges/{edge_id}/config")
    def edge_pull_config(
        edge_id: str,
        edge: EdgePrincipal = Depends(current_edge),
    ) -> dict[str, Any]:
        if edge.edge_id != edge_id:
            raise HTTPException(status_code=403, detail="EDGE não autorizado")
        edge_row = store.get_edge(edge_id) or {}
        devices = store.devices.list(tenant_id=edge.tenant_id, site_ids=list(edge.site_ids))
        rules = store.list_rules(tenant_id=edge.tenant_id, site_ids=list(edge.site_ids))
        return {
            "edge_id": edge_id,
            "version": int(edge_row.get("config_version", 1)),
            "devices": [d.to_dict() for d in devices],
            "rules": rules,
        }

    @app.post("/api/v1/edges/{edge_id}/config-reports")
    def edge_config_report(
        edge_id: str,
        report: dict[str, Any],
        edge: EdgePrincipal = Depends(current_edge),
    ) -> dict[str, str]:
        if edge.edge_id != edge_id:
            raise HTTPException(status_code=403, detail="EDGE não autorizado")
        store.save_config_report(edge_id, report)
        store.audit(edge.tenant_id, edge_id, "config:report", edge_id, str(report.get("status", "ok")), report)
        return {"status": "recorded"}

    @app.post("/api/v1/edges/{edge_id}/heartbeat")
    def edge_heartbeat(
        edge_id: str,
        snapshot: dict[str, Any],
        edge: EdgePrincipal = Depends(current_edge),
    ) -> dict[str, str]:
        if edge.edge_id != edge_id:
            raise HTTPException(status_code=403, detail="EDGE não autorizado")
        store.record_edge_heartbeat(edge_id, snapshot)
        return {"status": "ok"}

    # ---------------------------------------------------- Device Registry (§7)
    @app.post("/api/v1/devices", status_code=201)
    def register_device(
        payload: dict[str, Any],
        user: UserPrincipal = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            device = devices_svc.register(user, payload)
            return device.to_dict()
        except ForbiddenError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/devices")
    def list_devices(
        site_id: str | None = None,
        user: UserPrincipal = Depends(current_user),
    ) -> list[dict[str, Any]]:
        return [d.to_dict() for d in devices_svc.list(user, site_id=site_id)]

    @app.get("/api/v1/devices/{device_id}")
    def get_device(
        device_id: str,
        user: UserPrincipal = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            return devices_svc.get(user, device_id).to_dict()
        except DeviceNotFound as exc:
            raise HTTPException(status_code=404, detail=f"Dispositivo não encontrado: {exc}") from exc
        except ForbiddenError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/api/v1/devices/{device_id}/transition")
    def transition_device(
        device_id: str,
        body: TransitionRequest,
        user: UserPrincipal = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            return devices_svc.transition(user, device_id, body.state, body.reason).to_dict()
        except DeviceNotFound as exc:
            raise HTTPException(status_code=404, detail=f"Dispositivo não encontrado: {exc}") from exc
        except ForbiddenError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (LifecycleError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # -------------------------------------------------------------- telemetria
    @app.get("/api/v1/telemetry")
    def query_telemetry(
        device_id: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
        user: UserPrincipal = Depends(current_user),
    ) -> list[dict[str, Any]]:
        if device_id:
            try:
                devices_svc.get(user, device_id)
            except DeviceNotFound as exc:
                raise HTTPException(status_code=404, detail="Dispositivo não encontrado") from exc
            except ForbiddenError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
        return store.query_events(
            tenant_id=user.tenant_id,
            device_id=device_id,
            event_type=event_type,
            since=since,
            limit=limit,
        )

    @app.get("/api/v1/devices/{device_id}/state")
    def device_state(
        device_id: str,
        user: UserPrincipal = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            devices_svc.get(user, device_id)
        except DeviceNotFound as exc:
            raise HTTPException(status_code=404, detail="Dispositivo não encontrado") from exc
        except ForbiddenError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        state = store.get_device_state(user.tenant_id, device_id)
        if state is None:
            raise HTTPException(status_code=404, detail="Sem estado registrado")
        return state

    # --------------------------------------------------------- comandos (§14)
    @app.post("/api/v1/commands", status_code=201)
    def issue_command(
        body: CommandCreateRequest,
        user: UserPrincipal = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            cmd = commands_svc.issue(
                principal=user,
                device_id=body.device_id,
                operation=body.operation,
                ttl_s=body.ttl_s,
                params=body.params,
                expected_state=body.expected_state,
                confirmation_policy=body.confirmation_policy,
                correlation_id=body.correlation_id,
                require_edge_online=body.require_edge_online,
            )
            return cmd.to_dict()
        except DeviceNotFound as exc:
            raise HTTPException(status_code=404, detail=f"Dispositivo não encontrado: {exc}") from exc
        except ForbiddenError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except CommandRejected as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/commands")
    def list_commands(
        device_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        user: UserPrincipal = Depends(current_user),
    ) -> list[dict[str, Any]]:
        return commands_svc.list(user, device_id=device_id, limit=limit)

    @app.get("/api/v1/commands/{command_id}")
    def get_command(
        command_id: str,
        user: UserPrincipal = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            return commands_svc.get(user, command_id)
        except ForbiddenError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    # ------------------------------------------------------------ geoespacial
    @app.get("/api/v1/geospatial/positions")
    def latest_positions(
        site_id: str | None = None,
        user: UserPrincipal = Depends(current_user),
    ) -> list[dict[str, Any]]:
        return geo_svc.latest_positions(user.tenant_id, site_id=site_id)

    @app.get("/api/v1/geospatial/tracks/{device_id}")
    def track_history(
        device_id: str,
        since: str | None = None,
        until: str | None = None,
        limit: int = Query(default=500, ge=1, le=2000),
        user: UserPrincipal = Depends(current_user),
    ) -> list[dict[str, Any]]:
        try:
            devices_svc.get(user, device_id)
        except DeviceNotFound as exc:
            raise HTTPException(status_code=404, detail="Dispositivo não encontrado") from exc
        except ForbiddenError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return geo_svc.track_history(
            user.tenant_id, device_id, since=since, until=until, limit=limit
        )

    @app.get("/api/v1/geospatial/nearby")
    def nearby_devices(
        lat: float = Query(ge=-90, le=90),
        lon: float = Query(ge=-180, le=180),
        radius_m: float = Query(gt=0, le=500_000),
        user: UserPrincipal = Depends(current_user),
    ) -> list[dict[str, Any]]:
        return geo_svc.within_radius(user.tenant_id, lat, lon, radius_m)

    # ----------------------------------------------------------------- regras
    @app.post("/api/v1/rules", status_code=201)
    def upsert_rule(
        body: RuleCreateRequest,
        user: UserPrincipal = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            user.require_role("operator")
        except ForbiddenError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        site = store.get_site(body.site_id)
        if not site or site["tenant_id"] != user.tenant_id:
            raise HTTPException(status_code=403, detail="Instalação não autorizada")
        try:
            rule = Rule.from_dict(body.rule)
        except RuleValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        saved = store.save_rule(user.tenant_id, body.site_id, rule)
        store.audit(
            user.tenant_id, user.username, "rule:upsert", rule.rule_id, "ok", {"version": rule.version}
        )
        return saved

    @app.get("/api/v1/rules")
    def list_rules(
        site_id: str | None = None,
        user: UserPrincipal = Depends(current_user),
    ) -> list[dict[str, Any]]:
        return store.list_rules(user.tenant_id, site_ids=[site_id] if site_id else None)

    # -------------------------------------------------------------- analytics
    @app.get("/api/v1/analytics/{device_id}/summary")
    def analytics_summary(
        device_id: str,
        measurement: str = Query(min_length=2),
        limit: int = Query(default=200, ge=1, le=1000),
        user: UserPrincipal = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            devices_svc.get(user, device_id)
        except DeviceNotFound as exc:
            raise HTTPException(status_code=404, detail="Dispositivo não encontrado") from exc
        except ForbiddenError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return analytics_svc.device_summary(user.tenant_id, device_id, measurement, limit=limit)

    # -------------------------------------------------------------- auditoria
    @app.get("/api/v1/audit")
    def list_audit(
        action: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        user: UserPrincipal = Depends(current_user),
    ) -> list[dict[str, Any]]:
        return store.list_audit(user.tenant_id, action=action, limit=limit)

    return app
