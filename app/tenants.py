"""租户凭证与作用域校验。"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass


class AuthErrorCode(enum.Enum):
    """可区分的认证/授权失败类别。"""

    MISSING_CREDENTIAL = "missing_credential"
    INVALID_CREDENTIAL = "invalid_credential"
    EXPIRED_CREDENTIAL = "expired_credential"
    INSUFFICIENT_SCOPE = "insufficient_scope"


class AuthError(Exception):
    def __init__(self, code: AuthErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class TenantCredential:
    """租户凭证：token -> 租户、作用域、过期时间。"""

    token: str
    tenant_id: str
    scopes: frozenset[str]
    expires_at: int | None  # unix 秒；None 表示永不过期


@dataclass(frozen=True)
class TenantContext:
    """认证通过后的租户上下文。"""

    tenant_id: str
    scopes: frozenset[str]


class TenantDirectory:
    """内存租户目录（本地可复现，无需外部服务）。"""

    def __init__(self, credentials: list[TenantCredential]) -> None:
        self._by_token = {c.token: c for c in credentials}

    def authenticate(self, token: str | None, required_scope: str, now: int | None = None) -> TenantContext:
        """校验凭证与作用域；失败抛出带分类的 AuthError。"""
        if not token:
            raise AuthError(AuthErrorCode.MISSING_CREDENTIAL, "missing bearer token")
        cred = self._by_token.get(token)
        if cred is None:
            raise AuthError(AuthErrorCode.INVALID_CREDENTIAL, "unknown token")
        now = int(time.time()) if now is None else now
        if cred.expires_at is not None and now >= cred.expires_at:
            raise AuthError(AuthErrorCode.EXPIRED_CREDENTIAL, "token expired")
        if required_scope not in cred.scopes:
            raise AuthError(
                AuthErrorCode.INSUFFICIENT_SCOPE,
                f"scope '{required_scope}' required",
            )
        return TenantContext(tenant_id=cred.tenant_id, scopes=cred.scopes)
