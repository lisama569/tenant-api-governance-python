"""pytest 公共夹具：每个用例使用独立临时 DB 与日志文件，互不污染。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.container import build_container
from app.config import Settings
from app.main import create_app


@pytest.fixture()
def env(tmp_path: Path) -> dict:
    settings = Settings(
        database_path=tmp_path / "gov.db",
        log_path=tmp_path / "gov.log",
        token_secret="test-secret",
        token_ttl_seconds=3600,
        order_create_limit=3,
        order_list_limit=10,
        window_seconds=60,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield {
            "client": client,
            "container": app.state.container,
            "settings": settings,
            "tmp": tmp_path,
        }


@pytest.fixture()
def clock(monkeypatch) -> dict:
    """可控时钟：替换全局 now_ts，初始固定在一个窗口起点（整 60）。"""
    state = {"now": 1_000_079}  # 落在窗口 [1000020, 1000080) 的最后一秒
    import app.clock as clock_mod
    monkeypatch.setattr(clock_mod, "now_ts", lambda: state["now"])
    return state


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def idem(key: str) -> dict:
    return {"Idempotency-Key": key}


def read_audit_log(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
