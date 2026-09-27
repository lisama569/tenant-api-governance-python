"""按租户 + 接口维度的固定窗口配额/限流，状态持久化到 SQLite。"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from .config import QuotaRule
from .db import Database


class QuotaOutcome(enum.Enum):
    ALLOWED = "allowed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class QuotaDecision:
    outcome: QuotaOutcome
    limit: int
    remaining: int
    window_start: int
    retry_after_seconds: int  # 被拒时距离窗口重置的秒数；放行时为 0


class QuotaLimiter:
    """固定窗口计数器；窗口起点持久化，重启后不会清零或翻倍。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def acquire(self, tenant_id: str, route: str, rule: QuotaRule, now: int) -> QuotaDecision:
        """原子地占用一个额度；超限时不执行且返回可重试信息。"""
        window_start = (now // rule.window_seconds) * rule.window_seconds
        # 单条原子语句：不存在则插入 1，存在且未超限才 +1；并发与重启都安全。
        row = self._db.query_one(
            """
            INSERT INTO quota_windows (tenant_id, route, window_start, count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT (tenant_id, route, window_start)
            DO UPDATE SET count = count + 1
            WHERE quota_windows.count < ?
            RETURNING count
            """,
            (tenant_id, route, window_start, rule.limit),
        )
        retry_after = window_start + rule.window_seconds - now
        if row is None:
            return QuotaDecision(
                outcome=QuotaOutcome.REJECTED,
                limit=rule.limit,
                remaining=0,
                window_start=window_start,
                retry_after_seconds=max(retry_after, 1),
            )
        return QuotaDecision(
            outcome=QuotaOutcome.ALLOWED,
            limit=rule.limit,
            remaining=rule.limit - row["count"],
            window_start=window_start,
            retry_after_seconds=0,
        )
