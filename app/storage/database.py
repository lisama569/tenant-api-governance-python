"""SQLite 持久化：配额计数与幂等结果跨重启不丢、不翻倍。

关键设计：
- 配额表对 (tenant_id, endpoint, window_start) 做 UNIQUE，扣减走
  INSERT ... ON CONFLICT DO UPDATE，同一窗口只有一行，重启后继续累加，
  不会“清零”；UPDATE 只在 count < limit 时生效，靠 WHERE 保证不超额。
- 幂等表对 (tenant_id, idem_key) 做 UNIQUE PRIMARY KEY，状态机
  processing -> completed，结果整体 JSON 落库，重启后直接回放。
- 写操作开启 IMMEDIATE 事务，配合线程锁把并发抢额度/同键并发串行化。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    scopes    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quota_counters (
    tenant_id    TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    count        INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, endpoint, window_start)
);

CREATE TABLE IF NOT EXISTS idempotency_records (
    tenant_id    TEXT NOT NULL,
    idem_key     TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status       TEXT NOT NULL,
    status_code  INTEGER,
    response_json TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (tenant_id, idem_key)
);

CREATE TABLE IF NOT EXISTS orders (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    order_uid TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    item      TEXT NOT NULL,
    quantity  INTEGER NOT NULL
);
"""


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(database_path), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()
