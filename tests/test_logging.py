"""治理日志：错误分类、配额计数、执行结果均可核对。"""

import logging

from app.config import QuotaRule
from tests.conftest import AUTH


def test_auth_failure_logged(make_client, caplog):
    client = make_client()
    with caplog.at_level(logging.WARNING, logger="governance"):
        client.get("/data", headers={"Authorization": "Bearer forged"})
    assert any("auth_failed" in r.message and "invalid_credential" in r.message
               for r in caplog.records)


def test_quota_and_execution_logged(make_client, caplog):
    client = make_client({"/data": QuotaRule(limit=1, window_seconds=60)})
    with caplog.at_level(logging.INFO, logger="governance"):
        client.get("/data", headers=AUTH)
        client.get("/data", headers=AUTH)
        client.post("/orders", headers={**AUTH, "Idempotency-Key": "k"}, json={"item": "x"})
        client.post("/orders", headers={**AUTH, "Idempotency-Key": "k"}, json={"item": "x"})
    messages = [r.message for r in caplog.records]
    assert any("quota_allowed" in m and "remaining=0" in m for m in messages)
    assert any("quota_rejected" in m and "retry_after=" in m for m in messages)
    assert any("order_executed" in m and "seq=1" in m for m in messages)
    assert any("idempotency_replayed" in m for m in messages)
