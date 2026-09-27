"""幂等提交：重放一致、并发只生效一次、同键异体冲突、重启不重复执行。"""
from __future__ import annotations

import threading
import time

from tests.conftest import auth, idem, read_audit_log

PAYLOAD = {"item": "widget", "quantity": 2}


def _create(client, token, key, payload=PAYLOAD):
    return client.post(
        "/v1/orders", json=payload, headers={**auth(token), **idem(key)}
    )


def test_repeated_submission_returns_identical_result(env, clock):
    token = env["container"].issue_token("tenant-acme")
    first = _create(env["client"], token, "key-1")
    assert first.status_code == 201
    order_id = first.json()["order_id"]

    for _ in range(3):
        r = _create(env["client"], token, "key-1")
        assert r.status_code == 201
        assert r.json()["order_id"] == order_id  # 与首次完全一致

    # 只真正执行了一次
    assert env["container"].order_repo.count_for_tenant("tenant-acme") == 1


def test_concurrent_same_key_executes_once(env, clock):
    token = env["container"].issue_token("tenant-acme")
    bodies: list[dict] = []
    statuses: list[int] = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def fire():
        barrier.wait()
        r = _create(env["client"], token, "hot-key")
        with lock:
            statuses.append(r.status_code)
            bodies.append(r.json())

    threads = [threading.Thread(target=fire) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(s == 201 for s in statuses), statuses
    order_ids = {b["order_id"] for b in bodies}
    assert len(order_ids) == 1, order_ids  # 8 个并发请求共享同一结果
    assert env["container"].order_repo.count_for_tenant("tenant-acme") == 1

    events = read_audit_log(env["settings"].log_path)
    assert sum(1 for e in events if e["event"] == "order_created") == 1
    assert sum(1 for e in events if e["event"] == "idempotency_replay") == 7


def test_same_key_different_payload_conflicts(env, clock):
    token = env["container"].issue_token("tenant-acme")
    r1 = _create(env["client"], token, "key-x", {"item": "a", "quantity": 1})
    assert r1.status_code == 201

    conflict = _create(env["client"], token, "key-x", {"item": "b", "quantity": 1})
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "idempotency_conflict"

    # 冲突请求不产生新副作用，且原结果仍可正常重放
    assert env["container"].order_repo.count_for_tenant("tenant-acme") == 1
    replay = _create(env["client"], token, "key-x", {"item": "a", "quantity": 1})
    assert replay.status_code == 201
    assert replay.json()["order_id"] == r1.json()["order_id"]


def test_completed_result_survives_restart_and_never_reexecutes(env, clock):
    from app.api.container import build_container
    token = env["container"].issue_token("tenant-acme")
    first = _create(env["client"], token, "persist-key")
    order_id = first.json()["order_id"]
    assert env["container"].order_repo.count_for_tenant("tenant-acme") == 1

    # 重启：关闭并以同一 DB 重建容器（启动恢复完成后）
    env["container"].close()
    container2 = build_container(env["settings"])
    env["client"].app.state.container = container2
    env["container"] = container2

    replay = _create(env["client"], token, "persist-key")
    assert replay.status_code == 201
    assert replay.json()["order_id"] == order_id  # 回放旧结果
    # 关键：没有再次执行业务
    assert container2.order_repo.count_for_tenant("tenant-acme") == 1
    events = read_audit_log(env["settings"].log_path)
    assert sum(1 for e in events if e["event"] == "order_created") == 1
    assert any(e["event"] == "idempotency_replay" for e in events)


def test_restart_marks_interrupted_pending_but_keeps_completed(env, clock):
    from app.api.container import build_container
    token = env["container"].issue_token("tenant-acme")
    _create(env["client"], token, "done-key")  # completed

    # 人为制造一个崩溃残留的 processing 行
    conn = env["container"].conn
    conn.execute(
        "INSERT INTO idempotency_records(tenant_id,idem_key,endpoint,request_hash,status,"
        "status_code,response_json,created_at,updated_at) "
        "VALUES ('tenant-acme','stuck-key','order.create','h','processing',NULL,NULL,1,1)"
    )
    conn.commit()

    env["container"].close()
    container2 = build_container(env["settings"])  # 启动恢复
    env["client"].app.state.container = container2
    env["container"] = container2

    stuck = container2.idem_repo.get("tenant-acme", "stuck-key")
    assert stuck["status"] == "interrupted"  # 残留被识别
    done = container2.idem_repo.get("tenant-acme", "done-key")
    assert done["status"] == "completed"  # 成功结果保留


def test_idempotency_isolated_per_tenant(env, clock):
    acme = env["container"].issue_token("tenant-acme")
    # 用容器直接为一个“其他租户”播种并造一条 completed
    env["container"].tenant_repo.seed(
        [{"tenant_id": "tenant-bravo", "name": "Bravo", "scopes": ["order:read", "order:write"]}]
    )
    bravo = env["container"].issue_token("tenant-bravo")

    r1 = _create(env["client"], acme, "shared-key")
    assert r1.status_code == 201
    r2 = _create(env["client"], bravo, "shared-key")  # 同键不同租户
    assert r2.status_code == 201
    assert r1.json()["order_id"] != r2.json()["order_id"]

    # 双方各自只重放自己的结果，互不可见
    assert _create(env["client"], acme, "shared-key").json()["order_id"] == r1.json()["order_id"]
    assert _create(env["client"], bravo, "shared-key").json()["order_id"] == r2.json()["order_id"]
    assert env["container"].order_repo.count_for_tenant("tenant-acme") == 1
    assert env["container"].order_repo.count_for_tenant("tenant-bravo") == 1


def test_write_without_idempotency_key_rejected_before_business(env, clock):
    token = env["container"].issue_token("tenant-acme")
    r = env["client"].post("/v1/orders", json=PAYLOAD, headers=auth(token))
    assert r.status_code == 401
    # 缺少幂等键归类为可区分错误码（invalid_credential: 写请求凭证不完整）
    assert r.json()["error_code"] == "invalid_credential"
    assert env["container"].order_repo.count_for_tenant("tenant-acme") == 0


def test_replay_does_not_consume_quota_again(env, clock):
    """首次消耗 1 个额度；重放同键不再扣额度（配额 3，可再新建 2 个不同键）。"""
    token = env["container"].issue_token("tenant-acme")
    _create(env["client"], token, "q0")
    for _ in range(4):  # 多次重放
        _create(env["client"], token, "q0")
    assert _create(env["client"], token, "q1").status_code == 201
    assert _create(env["client"], token, "q2").status_code == 201
    r = _create(env["client"], token, "q3")
    assert r.status_code == 429  # 只有 3 次真实占用


def test_conflict_logged_with_existing_status(env, clock):
    token = env["container"].issue_token("tenant-acme")
    _create(env["client"], token, "lk", {"item": "a", "quantity": 1})
    _create(env["client"], token, "lk", {"item": "zzz", "quantity": 1})
    events = read_audit_log(env["settings"].log_path)
    evt = next(e for e in events if e["event"] == "idempotency_conflict")
    assert evt["status"] == "conflict" and evt["existing_status"] == "completed"
