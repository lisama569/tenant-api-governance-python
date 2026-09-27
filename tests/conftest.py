from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import QuotaRule, Settings
from app.main import create_app
from app.tenants import TenantCredential

NOW = [1_000_000]  # 可变时钟，测试可推进


def now_fn() -> int:
    return NOW[0]


@pytest.fixture()
def clock():
    NOW[0] = 1_000_000
    return NOW


@pytest.fixture()
def tenants():
    return [
        TenantCredential("tok-alpha", "tenant-alpha", frozenset({"read", "write"}), None),
        TenantCredential("tok-beta", "tenant-beta", frozenset({"read"}), None),
        TenantCredential("tok-expired", "tenant-gamma", frozenset({"read", "write"}), 500_000),
    ]


@pytest.fixture()
def make_client(tmp_path, tenants):
    """工厂：每次调用基于同一 db 文件新建应用（模拟重启）。"""
    db_path = tmp_path / "governance.db"

    def _make(extra_route_quotas: dict[str, QuotaRule] | None = None) -> TestClient:
        settings = Settings(
            db_path=db_path,
            default_quota=QuotaRule(limit=100, window_seconds=60),
            route_quotas=extra_route_quotas or {},
        )
        return TestClient(create_app(settings, tenants=tenants, now_fn=now_fn))

    return _make


AUTH = {"Authorization": "Bearer tok-alpha"}
