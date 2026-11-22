"""Transporte HTTPS/TLS de sincronização EDGE -> CORE (ADR-006, PIT-001 §13)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class TransportError(IOError):
    """Falha temporária de comunicação ou CORE indisponível."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class SyncTransport(ABC):
    @abstractmethod
    def send_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        """Envia ``POST /api/v1/ingest/batches``."""

    def pull_commands(self, edge_id: str) -> list[dict[str, Any]]:
        """Busca comandos pendentes em ``GET /api/v1/edges/{edge_id}/commands``."""
        return []

    def pull_config(self, edge_id: str) -> dict[str, Any] | None:
        """Busca intenção de configuração em ``GET /api/v1/edges/{edge_id}/config``."""
        return None

    def send_config_report(self, edge_id: str, report: dict[str, Any]) -> None:
        """Reporta configuração aplicada em ``POST /api/v1/edges/{edge_id}/config-reports``."""
        return None

    def send_heartbeat(self, edge_id: str, metrics: dict[str, Any]) -> None:
        """Envia heartbeat de saúde do EDGE em ``POST /api/v1/edges/{edge_id}/heartbeat``."""
        return None


class HttpSyncTransport(SyncTransport):
    """Cliente HTTPS com suporte a TLS, mTLS (certificado do EDGE) e Bearer token."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_s: float = 10.0,
        ca_bundle: str | bool = True,
        client_cert: str | tuple[str, str] | None = None,
        client: Any | None = None,
    ):
        import httpx

        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout_s,
            verify=ca_bundle,
            cert=client_cert,
            headers={"Authorization": f"Bearer {token}"},
        )
        self._headers = {"Authorization": f"Bearer {token}"}

    def _request(self, method: str, path: str, json_body: Any = None) -> Any:
        import httpx

        try:
            resp = self._client.request(method, path, json=json_body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise TransportError(f"Erro de rede ao contactar CORE: {exc}") from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            raise TransportError(
                f"CORE indisponível ({resp.status_code}): {resp.text[:200]}",
                status_code=resp.status_code,
            )
        if resp.status_code >= 400:
            raise TransportError(
                f"Erro HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        return resp.json() if resp.content else None

    def send_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/v1/ingest/batches", json_body=batch)

    def pull_commands(self, edge_id: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/api/v1/edges/{edge_id}/commands")
        return list(data.get("commands", [])) if isinstance(data, dict) else []

    def pull_config(self, edge_id: str) -> dict[str, Any] | None:
        return self._request("GET", f"/api/v1/edges/{edge_id}/config")

    def send_config_report(self, edge_id: str, report: dict[str, Any]) -> None:
        self._request("POST", f"/api/v1/edges/{edge_id}/config-reports", json_body=report)

    def send_heartbeat(self, edge_id: str, metrics: dict[str, Any]) -> None:
        self._request("POST", f"/api/v1/edges/{edge_id}/heartbeat", json_body=metrics)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
