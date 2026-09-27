"""幂等提交：同一租户同一键只执行一次，结果持久化，重启后仍生效。"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from .db import Database


class IdempotencyOutcome(enum.Enum):
    EXECUTED = "executed"      # 首次执行
    REPLAYED = "replayed"      # 同键同报文，返回首次结果
    CONFLICT = "conflict"      # 同键不同报文
    IN_PROGRESS = "in_progress"  # 并发下另一请求正在执行


@dataclass(frozen=True)
class IdempotencyRecord:
    tenant_id: str
    key: str
    request_hash: str
    status: str  # "in_progress" | "completed"
    response_status: int | None
    response_body: str | None


@dataclass(frozen=True)
class IdempotencyDecision:
    outcome: IdempotencyOutcome
    record: IdempotencyRecord | None  # REPLAYED/CONFLICT 时为已存记录


class IdempotencyStore:
    """基于 SQLite 主键的原子占位 + 结果落库。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def _load(self, tenant_id: str, key: str) -> IdempotencyRecord | None:
        row = self._db.query_one(
            "SELECT * FROM idempotency_records WHERE tenant_id=? AND idempotency_key=?",
            (tenant_id, key),
        )
        if row is None:
            return None
        return IdempotencyRecord(
            tenant_id=row["tenant_id"],
            key=row["idempotency_key"],
            request_hash=row["request_hash"],
            status=row["status"],
            response_status=row["response_status"],
            response_body=row["response_body"],
        )

    def begin(self, tenant_id: str, key: str, request_hash: str, now: int) -> IdempotencyDecision:
        """尝试占位；同键同体 -> REPLAYED，同键异体 -> CONFLICT，占位冲突 -> IN_PROGRESS。"""
        try:
            self._db.execute(
                "INSERT INTO idempotency_records "
                "(tenant_id, idempotency_key, request_hash, status, created_at) "
                "VALUES (?, ?, ?, 'in_progress', ?)",
                (tenant_id, key, request_hash, now),
            )
            return IdempotencyDecision(outcome=IdempotencyOutcome.EXECUTED, record=None)
        except Exception:
            existing = self._load(tenant_id, key)
            assert existing is not None
            if existing.request_hash != request_hash:
                return IdempotencyDecision(IdempotencyOutcome.CONFLICT, existing)
            if existing.status == "completed":
                return IdempotencyDecision(IdempotencyOutcome.REPLAYED, existing)
            return IdempotencyDecision(IdempotencyOutcome.IN_PROGRESS, existing)

    def execution_sequence(self, tenant_id: str) -> int:
        """当前租户已占位的键数量，可用作重启后仍稳定的执行序号。"""
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM idempotency_records WHERE tenant_id=?",
            (tenant_id,),
        )
        return row["n"]

    def complete(self, tenant_id: str, key: str, response_status: int, response_body: str) -> None:
        """将首次执行结果写入记录。"""
        self._db.execute(
            "UPDATE idempotency_records SET status='completed', response_status=?, "
            "response_body=? WHERE tenant_id=? AND idempotency_key=?",
            (response_status, response_body, tenant_id, key),
        )
