"""配额与限流：窗口边界、突发并发、重启后计数不清零不翻倍、可重试信息。"""

import threading

from app.config import QuotaRule
from tests.conftest import AUTH, NOW

RULE = {"/data": QuotaRule(limit=3, window_seconds=60)}


def statuses(client, n, headers=AUTH):
    return [client.get("/data", headers=headers).status_code for _ in range(n)]


def test_limit_and_retry_info(make_client):
    client = make_client(RULE)
    assert statuses(client, 3) == [200, 200, 200]
    r = client.get("/data", headers=AUTH)
    assert r.status_code == 429
    body = r.json()["error"]
    assert body["code"] == "quota_exceeded"
    assert "retry after" in body["detail"]
    assert r.headers["Retry-After"].isdigit()


def test_window_boundary_resets(make_client, clock):
    client = make_client(RULE)
    # 当前窗口起点 = 1000000 // 60 * 60
    assert statuses(client, 3) == [200, 200, 200]
    assert statuses(client, 1) == [429]
    # 推进到下一窗口，额度重置
    clock[0] += 60
    assert statuses(client, 3) == [200, 200, 200]
    assert statuses(client, 1) == [429]


def test_retry_after_points_to_window_end(make_client, clock):
    client = make_client(RULE)
    clock[0] = 1_000_010  # 窗口 [999960+60k...]；取整后窗口末 - now 即 retry_after
    statuses(client, 3)
    r = client.get("/data", headers=AUTH)
    window_start = (clock[0] // 60) * 60
    assert int(r.headers["Retry-After"]) == window_start + 60 - clock[0]


def test_concurrent_burst_exact_limit(make_client):
    client = make_client({"/data": QuotaRule(limit=5, window_seconds=60)})
    results = []

    def hit():
        results.append(client.get("/data", headers=AUTH).status_code)

    threads = [threading.Thread(target=hit) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(200) == 5
    assert results.count(429) == 15


def test_restart_preserves_count(make_client):
    client = make_client(RULE)
    assert statuses(client, 3) == [200, 200, 200]
    # 模拟重启：同一 db 文件新建应用
    client2 = make_client(RULE)
    # 计数不清零（仍拒绝）也不翻倍（不会重新获得 3 个额度）
    assert statuses(client2, 2) == [429, 429]


def test_tenant_isolation(make_client):
    client = make_client(RULE)
    assert statuses(client, 3) == [200, 200, 200]
    assert statuses(client, 1) == [429]
    # 其他租户不受影响
    beta = {"Authorization": "Bearer tok-beta"}
    assert statuses(client, 3, beta) == [200, 200, 200]
