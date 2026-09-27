"""数据访问层：租户、配额计数、幂等记录、订单副作用。

所有方法只做单条/单事务的原子 SQL；并发互斥由治理服务层的统一临界区保证。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid


class TenantRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, tenant_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT tenant_id, name, scopes FROM tenants WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()

    def seed(self, tenants: list[dict]) -> None:
        # 幂等播种：已存在的租户不覆盖，方便重启重复初始化
        for t in tenants:
            self.conn.execute(
                "INSERT OR IGNORE INTO tenants(tenant_id, name, scopes) VALUES (?, ?, ?)",
                (t["tenant_id"], t["name"], json.dumps(t["scopes"])),
            )
        self.conn.commit()


class QuotaRepository:
    """固定窗口计数：一个 (租户, 接口, 窗口) 一行，UPSERT 原子扣减。"""

    def __init__(self, conn: sqlite3.Connection, logger):
        self.conn = conn
        self.logger = logger

    def consume(self, tenant_id: str, endpoint: str, window_start: int, limit: int) -> tuple[bool, int]:
        """原子地尝试占用一个额度（须在治理临界区内调用）。

        返回 (allowed, count_after)。
        allowed=False 表示已达限额，count 不会增加（不翻倍）。
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO quota_counters(tenant_id, endpoint, window_start, count) "
            "VALUES (?, ?, ?, 0)",
            (tenant_id, endpoint, window_start),
        )
        cur = self.conn.execute(
            "UPDATE quota_counters SET count = count + 1 "
            "WHERE tenant_id=? AND endpoint=? AND window_start=? AND count < ?",
            (tenant_id, endpoint, window_start, limit),
        )
        row = self.conn.execute(
            "SELECT count FROM quota_counters WHERE tenant_id=? AND endpoint=? AND window_start=?",
            (tenant_id, endpoint, window_start),
        ).fetchone()
        count_after = int(row["count"])
        allowed = cur.rowcount == 1
        self.conn.commit()
        return allowed, count_after

    def get_count(self, tenant_id: str, endpoint: str, window_start: int) -> int:
        row = self.conn.execute(
            "SELECT count FROM quota_counters WHERE tenant_id=? AND endpoint=? AND window_start=?",
            (tenant_id, endpoint, window_start),
        ).fetchone()
        return 0 if row is None else int(row["count"])


class IdempotencyRepository:
    """幂等记录状态机：processing -> completed，结果 JSON 整存整取。"""

    def __init__(self, conn: sqlite3.Connection, logger):
        self.conn = conn
        self.logger = logger

    def insert_processing(self, tenant_id: str, key: str, endpoint: str, request_hash: str) -> bool:
        """插入 processing 占位。返回 True=首次占位成功，False=同键已存在。"""
        now = time.time()
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO idempotency_records
                (tenant_id, idem_key, endpoint, request_hash, status,
                 status_code, response_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'processing', NULL, NULL, ?, ?)
            """,
            (tenant_id, key, endpoint, request_hash, now, now),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def finish(self, tenant_id: str, key: str, status_code: int, body_json: str) -> None:
        self.conn.execute(
            "UPDATE idempotency_records SET status='completed', status_code=?, response_json=?, updated_at=? "
            "WHERE tenant_id=? AND idem_key=?",
            (status_code, body_json, time.time(), tenant_id, key),
        )
        self.conn.commit()

    def get(self, tenant_id: str, key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM idempotency_records WHERE tenant_id=? AND idem_key=?",
            (tenant_id, key),
        ).fetchone()

    def recover_pending(self) -> int:
        """重启恢复：把没有结果的 processing 记录标记为 interrupted。

        这些是进程崩溃时“占位成功但未完成”的键，标记后允许同报文重新执行；
        已 completed 的记录原样保留，成功结果绝不被再次执行。
        """
        cur = self.conn.execute(
            "UPDATE idempotency_records SET status='interrupted', updated_at=? "
            "WHERE status='processing'",
            (time.time(),),
        )
        self.conn.commit()
        return cur.rowcount


class OrderRepository:
    def __init__(self, conn: sqlite3.Connection, logger):
        self.conn = conn
        self.logger = logger

    def create(self, tenant_id: str, item: str, quantity: int) -> dict:
        order_uid = f"ord_{uuid.uuid4().hex[:16]}"
        cur = self.conn.execute(
            "INSERT INTO orders(order_uid, tenant_id, item, quantity) VALUES (?, ?, ?, ?)",
            (order_uid, tenant_id, item, quantity),
        )
        self.conn.commit()
        return {
            "order_id": order_uid,
            "tenant_id": tenant_id,
            "item": item,
            "quantity": quantity,
        }

    def list_for_tenant(self, tenant_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT order_uid, tenant_id, item, quantity FROM orders WHERE tenant_id=? ORDER BY id",
            (tenant_id,),
        ).fetchall()
        return [
            {"order_id": r["order_uid"], "tenant_id": r["tenant_id"], "item": r["item"], "quantity": r["quantity"]}
            for r in rows
        ]

    def count_for_tenant(self, tenant_id: str) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM orders WHERE tenant_id=?", (tenant_id,)).fetchone()
        return int(row["c"])
