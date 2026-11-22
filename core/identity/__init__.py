"""Identidade, autorização RBAC e isolamento multicliente (PIT-001 §19.3, §21, PIT-T15)."""

from core.identity.auth import AuthError, EdgePrincipal, ForbiddenError, IdentityService, UserPrincipal

__all__ = ["AuthError", "EdgePrincipal", "ForbiddenError", "IdentityService", "UserPrincipal"]
