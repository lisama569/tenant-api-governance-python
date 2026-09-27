"""业务服务：订单写/读。所有写操作仅产生一次可核对的持久化副作用。"""
from __future__ import annotations

from app.storage.repositories import OrderRepository


class OrderService:
    def __init__(self, repo: OrderRepository, logger):
        self.repo = repo
        self.logger = logger

    def create_order(self, tenant_id: str, item: str, quantity: int) -> dict:
        order = self.repo.create(tenant_id, item, quantity)
        self.logger.info(
            "order_created",
            extra={"audit": {"event": "order_created", "tenant_id": tenant_id,
                             "order_id": order["order_id"], "item": item,
                             "quantity": quantity, "status": "executed"}},
        )
        return order

    def list_orders(self, tenant_id: str) -> list[dict]:
        orders = self.repo.list_for_tenant(tenant_id)
        self.logger.info(
            "orders_listed",
            extra={"audit": {"event": "orders_listed", "tenant_id": tenant_id,
                             "returned": len(orders), "status": "executed"}},
        )
        return orders
