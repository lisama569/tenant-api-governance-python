"""应用级依赖容器：DB、仓储、服务的组装与生命周期管理。

播种的本地租户（仅用于本地验证，生产应由租户管理系统提供）：
- tenant-acme : order:read, order:write
- tenant-readonly : order:read        （作用域不足用例）
伪造/不存在的 tenant_id 即使签名合法也会被 tenant_not_found 拒绝。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.governance.idempotency import IdempotencyService
from app.governance.quotas import QuotaService
from app.logging_setup import configure_logging
from app.security.tokens import issue_token
from app.services.orders import OrderService
from app.storage.database import connect, init_db
from app.storage.repositories import IdempotencyRepository, OrderRepository, QuotaRepository, TenantRepository

SEED_TENANTS = [
    {"tenant_id": "tenant-acme", "name": "Acme Corp", "scopes": ["order:read", "order:write"]},
    {"tenant_id": "tenant-readonly", "name": "Read Only Ltd", "scopes": ["order:read"]},
]


@dataclass
class Container:
    conn: sqlite3.Connection
    logger: logging.Logger
    settings: Settings
    tenant_repo: TenantRepository
    quota_repo: QuotaRepository
    idem_repo: IdempotencyRepository
    order_repo: OrderRepository
    quota: QuotaService
    idempotency: IdempotencyService
    orders: OrderService

    def issue_token(self, tenant_id: str, scopes: list[str] | None = None, ttl_seconds: int | None = None,
                    *, now: int | None = None) -> str:
        if scopes is None:
            row = self.tenant_repo.get(tenant_id)
            scopes = json.loads(row["scopes"]) if row is not None else []
        return issue_token(tenant_id, scopes, ttl_seconds or self.settings.token_ttl_seconds,
                           self.settings.token_secret, now=now)

    def close(self) -> None:
        self.conn.close()


def build_container(settings: Settings, *, reset: bool = False) -> Container:
    db_path = Path(settings.database_path)
    if reset and db_path.exists():
        db_path.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if reset and side.exists():
            side.unlink()

    logger = configure_logging(Path(settings.log_path))
    conn = connect(db_path)
    init_db(conn)

    tenant_repo = TenantRepository(conn)
    tenant_repo.seed(SEED_TENANTS)

    quota_repo = QuotaRepository(conn, logger)
    idem_repo = IdempotencyRepository(conn, logger)
    order_repo = OrderRepository(conn, logger)

    limits = {
        "order.create": settings.order_create_limit,
        "order.list": settings.order_list_limit,
    }
    quota = QuotaService(quota_repo, limits, settings.window_seconds, logger)
    idempotency = IdempotencyService(idem_repo, logger)
    orders = OrderService(order_repo, logger)

    # 重启恢复：把崩溃残留的 processing 标记为 interrupted（成功的 completed 保留）
    recovered = idempotency.recover_pending()
    logger.info(
        "container_ready",
        extra={"audit": {"event": "container_ready", "database": str(db_path),
                         "recovered_pending": recovered, "status": "ready"}},
    )

    return Container(
        conn=conn, logger=logger, settings=settings,
        tenant_repo=tenant_repo, quota_repo=quota_repo, idem_repo=idem_repo, order_repo=order_repo,
        quota=quota, idempotency=idempotency, orders=orders,
    )
