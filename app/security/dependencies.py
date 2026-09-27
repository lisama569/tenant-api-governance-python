"""FastAPI 依赖：认证与作用域校验。

顺序保证：认证在一切业务/配额/幂等逻辑之前完成——
未认证请求不会消耗任何租户额度，也无法以任何 tenant_id 触碰幂等记录。
错误可区分：invalid_credential / expired_credential / tenant_not_found / scope_denied。
"""
from __future__ import annotations

from fastapi import Header

from app.errors import (
    InvalidCredentialError,
    ScopeDeniedError,
    TenantNotFoundError,
)
from app.models.schemas import Principal
from app.security.tokens import parse_token
from app.storage.repositories import TenantRepository


def authenticate(
    authorization: str | None = Header(default=None),
    *,
    tenant_repo: TenantRepository,
    secret: str,
    logger,
    now_ts: int | None = None,
) -> Principal:
    if not authorization or not authorization.strip():
        raise InvalidCredentialError("missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise InvalidCredentialError("authorization must be a Bearer token")

    payload = parse_token(token.strip(), secret, now=now_ts)
    tenant_id = payload["tenant_id"]
    row = tenant_repo.get(tenant_id)
    if row is None:
        logger.warning(
            "auth_tenant_not_found",
            extra={"audit": {"event": "auth_failed", "tenant_id": tenant_id,
                             "reason": "tenant_not_found", "status": "rejected"}},
        )
        raise TenantNotFoundError(f"tenant does not exist or is revoked: {tenant_id}")

    principal = Principal(tenant_id=tenant_id, scopes=list(payload["scopes"]), expires_at=int(payload["exp"]))
    logger.info(
        "auth_ok",
        extra={"audit": {"event": "authenticated", "tenant_id": tenant_id,
                         "scopes": principal.scopes, "status": "ok"}},
    )
    return principal


def ensure_scope(principal: Principal, scope: str) -> None:
    if scope not in principal.scopes:
        raise ScopeDeniedError(
            f"tenant '{principal.tenant_id}' lacks required scope '{scope}'; "
            f"held scopes={principal.scopes}"
        )
