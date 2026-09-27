"""HTTP 路由：健康检查、订单写/读。

POST /v1/orders 的治理顺序（每一步都有确定的拒绝点）：
1. 认证（无效/过期/租户不存在/作用域不足）——未通过则不消耗任何额度；
2. 配额扣减（超限立即 429，携带 retry_after，不进入业务）；
3. 幂等 begin（同键回放 / 同键异体冲突 / 获得唯一执行权）；
4. 业务执行一次并落盘；成功结果写入幂等记录供后续重放。
"""
from __future__ import annotations

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from app import clock
from app.errors import InvalidCredentialError
from app.models.schemas import OrderCreateRequest
from app.security.dependencies import authenticate, ensure_scope

router = APIRouter()

ENDPOINT_CREATE = "order.create"
ENDPOINT_LIST = "order.list"


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.post("/v1/orders", status_code=201)
async def create_order(
    req: Request,
    payload: OrderCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
):
    container = req.app.state.container

    # 1) 认证：失败在此终止，不触碰配额/幂等/业务
    principal = authenticate(
        authorization,
        tenant_repo=container.tenant_repo,
        secret=container.settings.token_secret,
        logger=container.logger,
        now_ts=clock.now_ts(),
    )
    ensure_scope(principal, "order:write")

    # 2) 幂等键必须存在（写请求的必要凭证）
    if not idempotency_key or not idempotency_key.strip():
        raise InvalidCredentialError(
            "write requests require a non-empty 'Idempotency-Key' header"
        )
    key = idempotency_key.strip()
    payload_dict = payload.model_dump()

    # 3) 幂等判定：命中既有成功结果则原样回放，不重复扣额度、不重复执行；
    #    同键异体在此 409，不会进入配额与业务。
    action, replay = container.idempotency.begin_or_replay(
        principal.tenant_id, key, ENDPOINT_CREATE, payload_dict
    )
    if action == "replay":
        container.logger.info(
            "request_replayed",
            extra={"audit": {"event": "request_replayed", "tenant_id": principal.tenant_id,
                             "key": key, "endpoint": ENDPOINT_CREATE, "http_status": replay["status_code"]}},
        )
        return JSONResponse(status_code=replay["status_code"], content=replay["body"])

    # 4) 首次执行才占用配额：超限立即 429（携带 retry_after），业务绝不执行。
    #    若因限流未执行，释放幂等占位，允许下个窗口用同键同报文安全重试。
    try:
        container.quota.check_and_consume(principal.tenant_id, ENDPOINT_CREATE)
    except Exception:
        container.idempotency.fail(principal.tenant_id, key)
        raise

    # 5) 唯一执行权：业务只执行一次
    try:
        body = container.orders.create_order(
            principal.tenant_id, payload.item, payload.quantity
        )
    except Exception:
        container.idempotency.fail(principal.tenant_id, key)
        raise
    container.idempotency.finish(principal.tenant_id, key, 201, body)
    return JSONResponse(status_code=201, content=body)


@router.get("/v1/orders")
async def list_orders(req: Request, authorization: str | None = Header(default=None)):
    container = req.app.state.container

    # 1) 认证 + 作用域
    principal = authenticate(
        authorization,
        tenant_repo=container.tenant_repo,
        secret=container.settings.token_secret,
        logger=container.logger,
        now_ts=clock.now_ts(),
    )
    ensure_scope(principal, "order:read")

    # 2) 读接口同样有独立配额
    container.quota.check_and_consume(principal.tenant_id, ENDPOINT_LIST)

    # 3) 只能看到本租户数据（查询始终带 tenant_id）
    orders = container.orders.list_orders(principal.tenant_id)
    return {"tenant_id": principal.tenant_id, "orders": orders}
