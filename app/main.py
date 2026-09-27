"""FastAPI 应用工厂：装配认证、配额、幂等三个治理组件。"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .config import Settings
from .db import Database
from .idempotency import IdempotencyOutcome, IdempotencyStore
from .quota import QuotaLimiter, QuotaOutcome
from .tenants import AuthError, AuthErrorCode, TenantCredential, TenantContext, TenantDirectory

logger = logging.getLogger("governance")

AUTH_HTTP_STATUS = {
    AuthErrorCode.MISSING_CREDENTIAL: 401,
    AuthErrorCode.INVALID_CREDENTIAL: 401,
    AuthErrorCode.EXPIRED_CREDENTIAL: 401,
    AuthErrorCode.INSUFFICIENT_SCOPE: 403,
}


def default_tenants() -> list[TenantCredential]:
    """本地演示用租户目录。"""
    return [
        TenantCredential("demo-token-alpha", "tenant-alpha", frozenset({"read", "write"}), None),
        TenantCredential("demo-token-beta", "tenant-beta", frozenset({"read"}), None),
    ]


def _error(status: int, code: str, detail: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "detail": detail}}, headers=headers)


def create_app(
    settings: Settings,
    tenants: list[TenantCredential] | None = None,
    now_fn: Callable[[], int] | None = None,
) -> FastAPI:
    db = Database(settings.db_path)
    directory = TenantDirectory(tenants if tenants is not None else default_tenants())
    limiter = QuotaLimiter(db)
    idem = IdempotencyStore(db)
    clock = now_fn or (lambda: int(time.time()))

    app = FastAPI(title="tenant-api-governance")
    app.state.db = db

    def authenticate(request: Request, required_scope: str) -> TenantContext | JSONResponse:
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else None
        try:
            return directory.authenticate(token, required_scope, now=clock())
        except AuthError as e:
            logger.warning(
                "auth_failed code=%s tenant_token=%s route=%s",
                e.code.value, "<none>" if not token else token[:8] + "...", request.url.path,
            )
            return _error(AUTH_HTTP_STATUS[e.code], e.code.value, e.detail)

    def check_quota(ctx: TenantContext, route: str) -> JSONResponse | None:
        rule = settings.quota_for(route)
        decision = limiter.acquire(ctx.tenant_id, route, rule, now=clock())
        if decision.outcome is QuotaOutcome.REJECTED:
            logger.warning(
                "quota_rejected tenant=%s route=%s limit=%d window_start=%d retry_after=%d",
                ctx.tenant_id, route, decision.limit, decision.window_start,
                decision.retry_after_seconds,
            )
            return _error(
                429, "quota_exceeded",
                f"quota {decision.limit}/{rule.window_seconds}s exceeded; retry after "
                f"{decision.retry_after_seconds}s",
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )
        logger.info(
            "quota_allowed tenant=%s route=%s remaining=%d window_start=%d",
            ctx.tenant_id, route, decision.remaining, decision.window_start,
        )
        return None

    @app.get("/data")
    def read_data(request: Request) -> Response:
        ctx = authenticate(request, "read")
        if isinstance(ctx, JSONResponse):
            return ctx
        if (rej := check_quota(ctx, "/data")) is not None:
            return rej
        return {"tenant": ctx.tenant_id, "data": ["example"]}

    @app.post("/orders")
    async def create_order(request: Request) -> Response:
        ctx = authenticate(request, "write")
        if isinstance(ctx, JSONResponse):
            return ctx
        if (rej := check_quota(ctx, "/orders")) is not None:
            return rej

        key = request.headers.get("idempotency-key")
        if not key:
            return _error(400, "missing_idempotency_key", "Idempotency-Key header required")
        body = await request.body()
        request_hash = hashlib.sha256(body).hexdigest()

        decision = idem.begin(ctx.tenant_id, key, request_hash, now=clock())
        if decision.outcome is IdempotencyOutcome.CONFLICT:
            logger.warning(
                "idempotency_conflict tenant=%s key=%s", ctx.tenant_id, key,
            )
            return _error(409, "idempotency_conflict",
                          "same Idempotency-Key used with a different payload")
        if decision.outcome is IdempotencyOutcome.IN_PROGRESS:
            logger.info("idempotency_in_progress tenant=%s key=%s", ctx.tenant_id, key)
            return _error(409, "idempotency_in_progress",
                          "same key is being processed; retry later",
                          headers={"Retry-After": "1"})
        if decision.outcome is IdempotencyOutcome.REPLAYED:
            rec = decision.record
            logger.info(
                "idempotency_replayed tenant=%s key=%s status=%d",
                ctx.tenant_id, key, rec.response_status,
            )
            return Response(
                content=rec.response_body, status_code=rec.response_status,
                media_type="application/json",
                headers={"X-Idempotent-Replay": "true"},
            )

        # 首次执行：业务逻辑（此处为演示用订单创建）
        # 执行序号取自持久层，重启后不会重复分配
        seq = idem.execution_sequence(ctx.tenant_id)
        payload = json.loads(body) if body else {}
        result = {
            "order_id": f"order-{seq}",
            "tenant": ctx.tenant_id,
            "item": payload.get("item"),
        }
        response_body = json.dumps(result)
        idem.complete(ctx.tenant_id, key, 201, response_body)
        logger.info(
            "order_executed tenant=%s key=%s seq=%d", ctx.tenant_id, key, seq,
        )
        return JSONResponse(status_code=201, content=result)

    return app
