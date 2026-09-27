"""统一错误分类：错误码可区分、可被客户端程序化处理。"""
from __future__ import annotations


class GovernanceError(Exception):
    error_code: str = "governance_error"
    status_code: int = 400

    def __init__(self, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


class AuthenticationError(GovernanceError):
    status_code = 401
    error_code = "unauthenticated"


class InvalidCredentialError(AuthenticationError):
    error_code = "invalid_credential"


class ExpiredCredentialError(AuthenticationError):
    error_code = "expired_credential"


class TenantNotFoundError(AuthenticationError):
    error_code = "tenant_not_found"


class ScopeDeniedError(GovernanceError):
    status_code = 403
    error_code = "scope_denied"


class QuotaExceededError(GovernanceError):
    status_code = 429
    error_code = "quota_exceeded"


class IdempotencyConflictError(GovernanceError):
    status_code = 409
    error_code = "idempotency_conflict"
