"""幂等治理：同键并发串行化、同键异体冲突、结果回放。

前提：进程启动时先执行 recover_pending()，把上次崩溃残留的 processing
统一置为 interrupted。因此运行期 DB 中出现的 processing 必然属于本进程
正在执行的某个线程。

状态机（按 tenant_id + key 隔离）：
- 无记录        -> 占位 processing，调用方获得唯一执行权（proceed）
- processing    -> 本进程他线程正在执行：同报文则等待完成后回放；
                   不同报文立即 409；等待超时返回 409+retry_after
- interrupted   -> 上次崩溃残留：同报文允许接管重新执行；不同报文 409
- completed     -> 同报文回放原状态码与响应体；不同报文 409

成功结果一旦 completed 落盘，重启后也只回放、绝不再次执行业务。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

from app.errors import IdempotencyConflictError
from app.governance.quotas import GLOBAL_LOCK
from app.storage.repositories import IdempotencyRepository

# 内存门闩：记录本进程正在执行的键，并发同键请求据此等待首个完成。
_INFLIGHT: set[tuple[str, str]] = set()
_INFLIGHT_COND = threading.Condition()

POLL_INTERVAL = 0.01
WAIT_TIMEOUT = 2.0


def canonical_request_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyService:
    def __init__(self, repo: IdempotencyRepository, logger):
        self.repo = repo
        self.logger = logger

    # ---------- 内部工具 ----------
    @staticmethod
    def _replay_of(row) -> dict:
        return {"status_code": int(row["status_code"]), "body": json.loads(row["response_json"])}

    def _wait_completed(self, tenant_id: str, key: str, deadline: float):
        with _INFLIGHT_COND:
            while (tenant_id, key) in _INFLIGHT and time.time() < deadline:
                _INFLIGHT_COND.wait(timeout=min(POLL_INTERVAL, max(0.0, deadline - time.time())))
        row = self.repo.get(tenant_id, key)
        return row if row is not None and row["status"] == "completed" else None

    def _conflict(self, tenant_id: str, key: str, endpoint: str, existing_status: str) -> None:
        self.logger.warning(
            "idempotency_conflict",
            extra={"audit": {"event": "idempotency_conflict", "tenant_id": tenant_id,
                             "key": key, "endpoint": endpoint, "status": "conflict",
                             "existing_status": existing_status}},
        )
        raise IdempotencyConflictError(
            "idempotency key was already used with a different request payload"
        )

    def _take_over(self, tenant_id: str, key: str, endpoint: str, req_hash: str) -> None:
        self.repo.conn.execute(
            "UPDATE idempotency_records SET endpoint=?, request_hash=?, status='processing', "
            "status_code=NULL, response_json=NULL, updated_at=? WHERE tenant_id=? AND idem_key=?",
            (endpoint, req_hash, time.time(), tenant_id, key),
        )
        self.repo.conn.commit()

    # ---------- 对外主流程 ----------
    def begin_or_replay(self, tenant_id: str, key: str, endpoint: str, payload: dict) -> tuple[str, dict | None]:
        """返回 ("proceed", None) 或 ("replay", {"status_code","body"})；冲突直接抛异常。"""
        req_hash = canonical_request_hash(payload)
        with GLOBAL_LOCK:
            row = self.repo.get(tenant_id, key)
            if row is None:
                if not self.repo.insert_processing(tenant_id, key, endpoint, req_hash):
                    row = self.repo.get(tenant_id, key)  # 竞争落败，转入既有记录分支
                else:
                    self._mark_inflight(tenant_id, key)
                    self._log_begin(tenant_id, key, endpoint)
                    return "proceed", None

            assert row is not None
            status = row["status"]

            if status == "completed":
                if row["request_hash"] != req_hash:
                    self._conflict(tenant_id, key, endpoint, status)
                self._log_replay(tenant_id, key, endpoint)
                return "replay", self._replay_of(row)

            if status == "interrupted":
                # 崩溃残留：同报文才允许接管
                if row["request_hash"] != req_hash:
                    self._conflict(tenant_id, key, endpoint, status)
                self._take_over(tenant_id, key, endpoint, req_hash)
                self._mark_inflight(tenant_id, key)
                self._log_begin(tenant_id, key, endpoint, recovered=True)
                return "proceed", None

            # status == "processing"：本进程他线程正在执行
            if row["request_hash"] != req_hash:
                self._conflict(tenant_id, key, endpoint, status)

        # 锁外等待首个完成（不持全局锁，避免阻塞其他租户/接口）
        completed = self._wait_completed(tenant_id, key, time.time() + WAIT_TIMEOUT)
        if completed is None:
            self.logger.warning(
                "idempotency_inflight_timeout",
                extra={"audit": {"event": "idempotency_inflight_timeout", "tenant_id": tenant_id,
                                 "key": key, "endpoint": endpoint, "status": "retry"}},
            )
            raise IdempotencyConflictError(
                "a request with the same idempotency key is still being processed; retry shortly",
                retry_after=1,
            )
        self._log_replay(tenant_id, key, endpoint)
        return "replay", self._replay_of(completed)

    @staticmethod
    def _mark_inflight(tenant_id: str, key: str) -> None:
        with _INFLIGHT_COND:
            _INFLIGHT.add((tenant_id, key))

    def _log_begin(self, tenant_id: str, key: str, endpoint: str, *, recovered: bool = False) -> None:
        self.logger.info(
            "idempotency_begin",
            extra={"audit": {"event": "idempotency_begin", "tenant_id": tenant_id, "key": key,
                             "endpoint": endpoint, "status": "processing", "recovered": recovered}},
        )

    def _log_replay(self, tenant_id: str, key: str, endpoint: str) -> None:
        self.logger.info(
            "idempotency_replay",
            extra={"audit": {"event": "idempotency_replay", "tenant_id": tenant_id,
                             "key": key, "endpoint": endpoint, "status": "replayed"}},
        )

    def finish(self, tenant_id: str, key: str, status_code: int, body: dict) -> None:
        with GLOBAL_LOCK:
            self.repo.finish(tenant_id, key, status_code, json.dumps(body, ensure_ascii=False, sort_keys=True))
            with _INFLIGHT_COND:
                _INFLIGHT.discard((tenant_id, key))
                _INFLIGHT_COND.notify_all()
        self.logger.info(
            "idempotency_finish",
            extra={"audit": {"event": "idempotency_finish", "tenant_id": tenant_id,
                             "key": key, "http_status": status_code, "status": "completed"}},
        )

    def fail(self, tenant_id: str, key: str) -> None:
        """业务执行未成功：释放占位并置 interrupted，允许同报文安全重试。"""
        with GLOBAL_LOCK:
            self.repo.conn.execute(
                "UPDATE idempotency_records SET status='interrupted', updated_at=? "
                "WHERE tenant_id=? AND idem_key=? AND status='processing'",
                (time.time(), tenant_id, key),
            )
            self.repo.conn.commit()
            with _INFLIGHT_COND:
                _INFLIGHT.discard((tenant_id, key))
                _INFLIGHT_COND.notify_all()
        self.logger.warning(
            "idempotency_failed",
            extra={"audit": {"event": "idempotency_failed", "tenant_id": tenant_id,
                             "key": key, "status": "interrupted"}},
        )

    def recover_pending(self) -> int:
        with GLOBAL_LOCK:
            n = self.repo.recover_pending()
        if n:
            self.logger.warning(
                "idempotency_recover",
                extra={"audit": {"event": "idempotency_recover", "status": "interrupted", "recovered": n}},
            )
        return n
