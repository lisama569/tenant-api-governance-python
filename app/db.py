"""SQLite 持久化层：配额计数与幂等记录共用同一数据库文件。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_windows (
    tenant_id TEXT NOT NULL,
    route TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, route, window_start)
);
CREATE TABLE IF NOT EXISTS idempotency_records (
    tenant_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    response_status INTEGER,
    response_body TEXT,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, idempotency_key)
);
"""


class Database:
    """线程安全的 SQLite 访问封装。

    所有语句执行与结果读取都在同一把互斥锁内完成，并以普通 dict 返回，
    避免跨线程共享连接时游标/Row 被并发语句干扰。
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        if self._path != Path(":memory:"):
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple = ()) -> None:
        """执行写语句并立即提交（autocommit）。"""
        with self._lock:
            self._conn.execute(sql, params)

    def query_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        """执行查询并在锁内取出第一行，以 dict 返回。"""
        with self._lock:
            cur = self._conn.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
