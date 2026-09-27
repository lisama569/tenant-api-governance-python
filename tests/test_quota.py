"""按租户 + 接口维度的配额：窗口边界、突发并发、重置、重启恢复。"""
from __future__ import annotations

import threading

from tests.conftest import auth, idem, read_audit_log


def _create(client, token, key, item="widget", qty=1):
    return client.post(
        "/v1/orders", json={"item": item, "quantity": qty},
        headers={**auth(token), **idem(key)},
    )


def test_exactly_limit_requests_succeed_then_rejected(env, clock):
    token = env["container"].issue_token("tenant-acme")
    statuses = [_create(env["client"], token, f"k{i}").status_code for i in range(5)]
    # 窗口起点附近：前 3 成功，后 2 被限流
    assert statuses == [201, 201, 201, 429, 429], statuses


def test_rejected_request_has_retry_after_and_no_side_effect(env, clock):
    token = env["container"].issue_token("tenant-acme")
    for i in range(3):
        _create(env["client"], token, f"k{i}")
    r = _create(env["client"], token, "k-overflow")
    assert r.status_code == 429
    body = r.json()
    assert body["error_code"] == "quota_exceeded"
    # 当前窗口 1000020~1000079，边界前 1 秒 -> retry_after=1
    assert body["retry_after"] == 1
    # 被限流的请求没有产生订单
    assert env["container"].order_repo.count_for_tenant("tenant-acme") == 3


def test_window_boundary_resets_quota_deterministically(env, clock):
    token = env["container"].issue_token("tenant-acme")
    for i in range(3):
        assert _create(env["client"], token, f"a{i}").status_code == 201
    assert _create(env["client"], token, "a-overflow").status_code == 429

    # 推进到下一窗口起点，配额必须重新可用
    clock["now"] = 1_000_080
    r = _create(env["client"], token, "b0")
    assert r.status_code == 201, r.text


def test_concurrent_burst_never_exceeds_limit(env, clock):
    token = env["container"].issue_token("tenant-acme")
    results: list[int] = []
    lock = threading.Lock()

    def fire(i):
        r = _create(env["client"], token, f"c{i}")
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results).count(201) == 3, results
    assert sorted(results).count(429) == 17, results
    # 真实副作用 == 成功数，绝不超额执行
    assert env["container"].order_repo.count_for_tenant("tenant-acme") == 3


def test_quotas_isolated_per_tenant_and_endpoint(env, clock):
    acme = env["container"].issue_token("tenant-acme")
    ro = env["container"].issue_token("tenant-readonly")
    # acme 写额度打满
    for i in range(3):
        assert _create(env["client"], acme, f"a{i}").status_code == 201
    assert _create(env["client"], acme, "a-over").status_code == 429
    # readonly 没有写作用域（403 先于配额），但其读额度独立可用
    r = env["client"].get("/v1/orders", headers=auth(ro))
    assert r.status_code == 200
    # acme 的读额度也不受写额度影响
    r = env["client"].get("/v1/orders", headers=auth(acme))
    assert r.status_code == 200


def test_quota_counter_persists_across_restart(env, clock):
    """重启后计数不丢、不翻倍：用掉 2 个额度 -> 重建容器 -> 本窗口只剩 1 个。"""
    token = env["container"].issue_token("tenant-acme")
    assert _create(env["client"], token, "r0").status_code == 201
    assert _create(env["client"], token, "r1").status_code == 201

    # 模拟进程重启：同一 DB 文件重新构建容器
    from app.api.container import build_container
    env["container"].close()
    container2 = build_container(env["settings"])
    env["app"] if False else None
    env["client"].app.state.container = container2
    env["container"] = container2

    assert _create(env["client"], token, "r2").status_code == 201
    r = _create(env["client"], token, "r-overflow")
    assert r.status_code == 429
    # 窗口内 DB 计数严格为 3（不翻倍、不清零）
    count = container2.quota_repo.get_count("tenant-acme", "order.create", 1_000_020)
    assert count == 3


def test_quota_events_logged_with_counts(env, clock):
    token = env["container"].issue_token("tenant-acme")
    for i in range(4):
        _create(env["client"], token, f"l{i}")
    events = read_audit_log(env["settings"].log_path)
    consumed = [e for e in events if e["event"] == "quota_consumed"]
    rejected = [e for e in events if e["event"] == "quota_exceeded"]
    assert [e["used"] for e in consumed] == [1, 2, 3]
    assert rejected and rejected[0]["used"] == 3 and rejected[0]["status"] == "rejected"
