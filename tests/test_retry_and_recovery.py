"""失败重试原则：限流可安全重试、重试不重复执行、跨进程重启结论一致。"""
from __future__ import annotations

from tests.conftest import auth, idem, read_audit_log


def _create(client, token, key, payload=None):
    return client.post(
        "/v1/orders", json=payload or {"item": "w", "quantity": 1},
        headers={**auth(token), **idem(key)},
    )


def test_rate_limited_first_attempt_succeeds_in_next_window_with_same_key(env, clock):
    """同键同报文：本窗口额度耗尽 -> 429 可重试；下窗口同键只执行一次。"""
    token = env["container"].issue_token("tenant-acme")
    for i in range(3):
        assert _create(env["client"], token, f"occupy{i}").status_code == 201

    r = _create(env["client"], token, "retry-key", {"item": "late", "quantity": 5})
    assert r.status_code == 429
    assert r.json()["retry_after"] == 1

    # 此时尚未产生该键的订单
    before = env["container"].order_repo.count_for_tenant("tenant-acme")
    assert before == 3

    # 下一窗口用同一幂等键、同一报文重试：成功且只执行一次
    clock["now"] = 1_000_080
    r2 = _create(env["client"], token, "retry-key", {"item": "late", "quantity": 5})
    assert r2.status_code == 201, r2.text
    order_id = r2.json()["order_id"]

    r3 = _create(env["client"], token, "retry-key", {"item": "late", "quantity": 5})
    assert r3.status_code == 201 and r3.json()["order_id"] == order_id
    assert env["container"].order_repo.count_for_tenant("tenant-acme") == 4


def test_429_is_actionable_and_logged(env, clock):
    token = env["container"].issue_token("tenant-acme")
    for i in range(3):
        _create(env["client"], token, f"k{i}")
    r = _create(env["client"], token, "boom")
    assert r.status_code == 429
    body = r.json()
    assert body["error_code"] == "quota_exceeded"
    assert isinstance(body["retry_after"], int) and body["retry_after"] >= 1
    events = read_audit_log(env["settings"].log_path)
    rej = [e for e in events if e["event"] == "quota_exceeded"]
    assert rej and rej[-1]["retry_after"] == body["retry_after"]


def test_restart_preserves_quota_and_idempotency_together(env, clock):
    """综合重启恢复：同一窗口内重启，配额计数与已完成幂等结果都保持一致。"""
    from app.api.container import build_container
    token = env["container"].issue_token("tenant-acme")
    _create(env["client"], token, "x0")
    _create(env["client"], token, "x1")
    env["container"].close()

    container2 = build_container(env["settings"])
    env["client"].app.state.container = container2
    env["container"] = container2

    # 已完成键：重放，不扣新额度
    replay = _create(env["client"], token, "x0")
    assert replay.status_code == 201
    # 本窗口只剩 1 个真实额度（已用 2）
    assert _create(env["client"], token, "x2").status_code == 201
    assert _create(env["client"], token, "x3").status_code == 429
    # 副作用恰好 3 个订单
    assert container2.order_repo.count_for_tenant("tenant-acme") == 3


def test_orders_listing_is_tenant_scoped(env, clock):
    acme = env["container"].issue_token("tenant-acme")
    env["container"].tenant_repo.seed(
        [{"tenant_id": "tenant-bravo", "name": "Bravo", "scopes": ["order:read", "order:write"]}]
    )
    bravo = env["container"].issue_token("tenant-bravo")
    _create(env["client"], acme, "a1", {"item": "a", "quantity": 1})
    _create(env["client"], bravo, "b1", {"item": "b", "quantity": 1})

    ra = env["client"].get("/v1/orders", headers=auth(acme)).json()
    rb = env["client"].get("/v1/orders", headers=auth(bravo)).json()
    assert {o["order_id"] for o in ra["orders"]} == {
        o["order_id"] for o in env["container"].orders.list_orders("tenant-acme")
    }
    assert {o["order_id"] for o in rb["orders"]} == {
        o["order_id"] for o in env["container"].orders.list_orders("tenant-bravo")
    }
    assert ra["tenant_id"] == "tenant-acme" and rb["tenant_id"] == "tenant-bravo"
    assert {o["order_id"] for o in ra["orders"]}.isdisjoint({o["order_id"] for o in rb["orders"]})
