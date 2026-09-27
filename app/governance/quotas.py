"""固定窗口配额治理：窗口判定、扣减、重置时间。

- 窗口按 UTC 时间轴对齐：window_start = (now // size) * size，
  因此重启后窗口划分完全一致，计数继续累加，不清零、不翻倍。
- 所有“读计数-判定-写计数”在同一把全局锁 + SQLite 提交内完成，
  突发并发抢额度时谁先进入临界区谁拿额度，结论确定。
- 超限请求同样计入窗口计数（占一次拒绝），但绝不执行业务，
  并返回 retry_after 告知下个窗口的等待秒数。
"""
from __future__ import annotations

import threading

from app import clock
from app.errors import QuotaExceededError
from app.storage.repositories import QuotaRepository

# endpoint 逻辑名 -> settings 中对应限额
ENDPOINT_LIMIT_KEYS = {
    "order.create": "order_create_limit",
    "order.list": "order_list_limit",
}


def window_start_for(now_ts: int, window_seconds: int) -> int:
    return (now_ts // window_seconds) * window_seconds


def retry_after_seconds(now_ts: int, window_seconds: int) -> int:
    return window_seconds - (now_ts % window_seconds)


# 进程级全局临界区：覆盖所有租户/接口的配额与幂等占位，保证突发并发结论确定。
GLOBAL_LOCK = threading.RLock()


class QuotaService:
    def __init__(self, repo: QuotaRepository, limits: dict[str, int], window_seconds: int, logger):
        self.repo = repo
        self.limits = limits
        self.window_seconds = window_seconds
        self.logger = logger

    def check_and_consume(self, tenant_id: str, endpoint: str, *, now_ts: int | None = None) -> dict:
        now_ts = clock.now_ts() if now_ts is None else now_ts
        limit = int(self.limits[endpoint])
        ws = window_start_for(now_ts, self.window_seconds)
        with GLOBAL_LOCK:
            allowed, count_after = self.repo.consume(tenant_id, endpoint, ws, limit)
        reset_at = ws + self.window_seconds
        result = {
            "allowed": allowed,
            "limit": limit,
            "used": count_after,
            "window_start": ws,
            "reset_at": reset_at,
            "retry_after": max(1, reset_at - now_ts),
        }
        if allowed:
            self.logger.info(
                "quota_consumed",
                extra={"audit": {"event": "quota_consumed", "tenant_id": tenant_id, "endpoint": endpoint,
                                 "status": "allowed", "used": count_after, "limit": limit, "window_start": ws}},
            )
        else:
            self.logger.warning(
                "quota_exceeded",
                extra={"audit": {"event": "quota_exceeded", "tenant_id": tenant_id, "endpoint": endpoint,
                                 "status": "rejected", "used": count_after, "limit": limit,
                                 "window_start": ws, "retry_after": result["retry_after"]}},
            )
            raise QuotaExceededError(
                f"quota exceeded for {endpoint}: {count_after}/{limit} in window {ws}",
                retry_after=result["retry_after"],
            )
        return result
