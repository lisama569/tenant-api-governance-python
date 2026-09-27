"""幂等提交：同键并发/重试只生效一次、异体冲突、重启后不重执行。"""

import threading

from tests.conftest import AUTH


def post(client, key, item="widget"):
    return client.post("/orders", headers={**AUTH, "Idempotency-Key": key},
                       json={"item": item})


def test_missing_key_rejected(make_client):
    client = make_client()
    r = client.post("/orders", headers=AUTH, json={"item": "x"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "missing_idempotency_key"


def test_replay_returns_identical_result(make_client):
    client = make_client()
    r1 = post(client, "k-1")
    assert r1.status_code == 201
    r2 = post(client, "k-1")
    assert r2.status_code == 201
    assert r2.headers["X-Idempotent-Replay"] == "true"
    assert r2.json() == r1.json()  # 与首次结果一致
    assert r2.json()["order_id"] == "order-1"  # 只执行了一次


def test_same_key_different_body_conflicts(make_client):
    client = make_client()
    assert post(client, "k-2", item="a").status_code == 201
    r = post(client, "k-2", item="b")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"


def test_concurrent_same_key_executes_once(make_client):
    client = make_client()
    results = []

    def submit():
        r = post(client, "k-race")
        results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=submit) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 所有成功响应（201）的 order_id 必须一致：只生效一次
    order_ids = {body["order_id"] for status, body in results
                 if status == 201 and "order_id" in body}
    assert order_ids == {"order-1"}, results
    # 不允许出现两个不同的执行结果
    assert len(results) == 10


def test_restart_does_not_reexecute(make_client):
    client = make_client()
    r1 = post(client, "k-persist")
    assert r1.status_code == 201
    order_id = r1.json()["order_id"]
    # 模拟重启
    client2 = make_client()
    r2 = post(client2, "k-persist")
    assert r2.status_code == 201
    assert r2.headers["X-Idempotent-Replay"] == "true"
    assert r2.json()["order_id"] == order_id  # 未重新执行，仍是首次结果
    # 新键仍可正常执行，且序号未被重复消费
    r3 = post(client2, "k-new")
    assert r3.status_code == 201
    assert r3.json()["order_id"] != order_id


def test_different_keys_are_independent(make_client):
    client = make_client()
    assert post(client, "k-a").status_code == 201
    assert post(client, "k-b").status_code == 201
    assert post(client, "k-a").json() != post(client, "k-b").json()
