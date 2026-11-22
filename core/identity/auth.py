"""Identity & Access Management do TERRANET CORE (ADR-013, PIT-001 §19.3, §21)."""

from __future__ import annotations

from dataclasses import dataclass

from core.storage.repository import CoreStore

ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}


class AuthError(PermissionError):
    """Credenciais ausentes, inválidas ou revogadas (HTTP 401)."""


class ForbiddenError(PermissionError):
    """Usuário ou EDGE autenticado sem permissão para o recurso (HTTP 403)."""


@dataclass(frozen=True)
class UserPrincipal:
    user_id: str
    tenant_id: str
    username: str
    role: str

    def require_role(self, minimum: str) -> None:
        if ROLE_RANK.get(self.role, 0) < ROLE_RANK[minimum]:
            raise ForbiddenError(f"Função '{self.role}' insuficiente; requer '{minimum}'")

    def require_tenant(self, tenant_id: str) -> None:
        if self.tenant_id != tenant_id:
            raise ForbiddenError("Isolamento de cliente: acesso negado a outro tenant")


@dataclass(frozen=True)
class EdgePrincipal:
    edge_id: str
    tenant_id: str
    site_ids: tuple[str, ...]

    def require_site(self, tenant_id: str, site_id: str) -> None:
        if self.tenant_id != tenant_id or site_id not in self.site_ids:
            raise ForbiddenError(
                f"EDGE '{self.edge_id}' não autorizado para {tenant_id}/{site_id}"
            )


class IdentityService:
    def __init__(self, store: CoreStore):
        self.store = store

    @staticmethod
    def _extract_bearer(authorization: str | None) -> str:
        if not authorization:
            raise AuthError("Cabeçalho Authorization ausente")
        parts = authorization.strip().split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            raise AuthError("Formato esperado: Authorization: Bearer <token>")
        return parts[1].strip()

    def authenticate_user(self, authorization: str | None) -> UserPrincipal:
        token = self._extract_bearer(authorization)
        row = self.store.find_user_by_key(token)
        if not row:
            raise AuthError("Credencial de usuário inválida ou revogada")
        return UserPrincipal(
            user_id=row["user_id"],
            tenant_id=row["tenant_id"],
            username=row["username"],
            role=row["role"],
        )

    def authenticate_edge(self, authorization: str | None) -> EdgePrincipal:
        token = self._extract_bearer(authorization)
        row = self.store.find_edge_by_token(token)
        if not row:
            raise AuthError("Credencial de EDGE inválida ou revogada")
        return EdgePrincipal(
            edge_id=row["edge_id"],
            tenant_id=row["tenant_id"],
            site_ids=tuple(row["site_ids"]),
        )
