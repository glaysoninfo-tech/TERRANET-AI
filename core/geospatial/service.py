"""Serviço geoespacial do TERRANET CORE (ADR-008, V01, PostGIS + fallback SQLite)."""

from __future__ import annotations

import math
from typing import Any

from core.storage.repository import CoreStore


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distância aproximada em metros sobre a esfera terrestre (WGS84 médio)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class GeospatialService:
    def __init__(self, store: CoreStore):
        self.store = store

    def latest_positions(self, tenant_id: str, site_id: str | None = None) -> list[dict[str, Any]]:
        """Última posição conhecida de cada dispositivo do cliente."""
        sql = (
            "SELECT p.* FROM telemetry_positions p "
            "JOIN (SELECT device_id, MAX(observed_at) AS max_ts FROM telemetry_positions "
            "WHERE tenant_id = ? GROUP BY device_id) latest "
            "ON p.device_id = latest.device_id AND p.observed_at = latest.max_ts "
            "WHERE p.tenant_id = ?"
        )
        params: list[Any] = [tenant_id, tenant_id]
        if site_id:
            sql += " AND p.site_id = ?"
            params.append(site_id)
        return self.store.db.query(sql + " ORDER BY p.device_id", params)

    def track_history(
        self,
        tenant_id: str,
        device_id: str,
        since: str | None = None,
        until: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT event_id, device_id, observed_at, lat, lon, alt_m, speed_kmh, heading_deg "
            "FROM telemetry_positions WHERE tenant_id = ? AND device_id = ?"
        )
        params: list[Any] = [tenant_id, device_id]
        if since:
            sql += " AND observed_at >= ?"
            params.append(since)
        if until:
            sql += " AND observed_at <= ?"
            params.append(until)
        sql += " ORDER BY observed_at ASC LIMIT ?"
        params.append(int(limit))
        return self.store.db.query(sql, params)

    def within_radius(
        self, tenant_id: str, lat: float, lon: float, radius_m: float
    ) -> list[dict[str, Any]]:
        """Dispositivos cuja última posição está dentro de ``radius_m`` metros do ponto."""
        if self.store.db.dialect == "postgresql":
            return self.store.db.query(
                "SELECT DISTINCT ON (device_id) device_id, site_id, observed_at, lat, lon, "
                "speed_kmh, ST_Distance(geom, ST_SetSRID(ST_MakePoint(?, ?), 4326)::geography) "
                "AS distance_m FROM telemetry_positions WHERE tenant_id = ? "
                "AND ST_DWithin(geom, ST_SetSRID(ST_MakePoint(?, ?), 4326)::geography, ?) "
                "ORDER BY device_id, observed_at DESC",
                (lon, lat, tenant_id, lon, lat, float(radius_m)),
            )
        results = []
        for pos in self.latest_positions(tenant_id):
            dist = haversine_m(lat, lon, float(pos["lat"]), float(pos["lon"]))
            if dist <= radius_m:
                item = dict(pos)
                item["distance_m"] = round(dist, 2)
                results.append(item)
        return results
