"""认证、错误分类与租户边界。"""
from __future__ import annotations

from tests.conftest import auth, read_audit_log


def test_health_ok(env):
    r = env["client"].get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_missing_authorization_is_invalid_credential(env):
    r = env["client"].post("/v1/orders", json={"item": "a", "quantity": 1}, headers={"Idempotency-Key": "k"})
    assert r.status_code == 401
    assert r.json()["error_code"] == "invalid_credential"


def test_malformed_token_is_invalid_credential(env):
    r = env["client"].post(
        "/v1/orders", json={"item": "a", "quantity": 1},
        headers={"Authorization": "Bearer not-a-token", "Idempotency-Key": "k"},
    )
    assert r.status_code == 401
    assert r.json()["error_code"] == "invalid_credential"


def test_signature_forgery_is_rejected(env):
    # 用别的密钥签出的 token：签名校验失败
    from app.security.tokens import issue_token
    forged = issue_token("tenant-acme", ["order:write"], 3600, "wrong-secret")
    r = env["client"].post(
        "/v1/orders", json={"item": "a", "quantity": 1},
        headers={**auth(forged), "Idempotency-Key": "k"},
    )
    assert r.status_code == 401
    assert r.json()["error_code"] == "invalid_credential"


def test_expired_token_is_distinguishable(env):
    expired = env["container"].issue_token("tenant-acme", ttl_seconds=-10)
    r = env["client"].post(
        "/v1/orders", json={"item": "a", "quantity": 1},
        headers={**auth(expired), "Idempotency-Key": "k"},
    )
    assert r.status_code == 401
    assert r.json()["error_code"] == "expired_credential"


def test_forged_tenant_even_with_valid_signature_is_rejected(env):
    # token 签名合法但 tenant_id 不存在/已吊销
    ghost = env["container"].issue_token("tenant-forged", scopes=["order:write"])
    r = env["client"].post(
        "/v1/orders", json={"item": "a", "quantity": 1},
        headers={**auth(ghost), "Idempotency-Key": "k"},
    )
    assert r.status_code == 401
    assert r.json()["error_code"] == "tenant_not_found"


def test_readonly_tenant_cannot_write(env):
    token = env["container"].issue_token("tenant-readonly")
    r = env["client"].post(
        "/v1/orders", json={"item": "a", "quantity": 1},
        headers={**auth(token), "Idempotency-Key": "k1"},
    )
    assert r.status_code == 403
    body = r.json()
    assert body["error_code"] == "scope_denied"
    assert "order:write" in body["message"]


def test_unauthenticated_request_consumes_no_quota(env, clock):
    # 配额窗口冻结；连续非法请求后，合法额度应仍为满额（用 acme 成功下单 3 次验证）
    token = env["container"].issue_token("tenant-acme")
    for _ in range(5):
        env["client"].post(
            "/v1/orders", json={"item": "a", "quantity": 1},
            headers={"Authorization": "Bearer garbage", "Idempotency-Key": "k"},
        )
    for i in range(3):
        r = env["client"].post(
            "/v1/orders", json={"item": "a", "quantity": 1},
            headers={**auth(token), "Idempotency-Key": f"k{i}"},
        )
        assert r.status_code == 201, r.text
    # 第四单应被限流，证明此前未认证请求没有吃掉额度
    r = env["client"].post(
        "/v1/orders", json={"item": "a", "quantity": 1},
        headers={**auth(token), "Idempotency-Key": "overflow"},
    )
    assert r.status_code == 429


def test_unauthenticated_request_cannot_read_other_tenant_idempotency(env):
    # 无有效凭证根本无法以任何 tenant_id 查询幂等结果
    r = env["client"].post(
        "/v1/orders", json={"item": "a", "quantity": 1},
        headers={"Idempotency-Key": "shared-key"},  # no auth
    )
    assert r.status_code == 401
    rows = env["container"].idem_repo.conn.execute(
        "SELECT COUNT(*) AS c FROM idempotency_records"
    ).fetchone()
    assert rows["c"] == 0


def test_audit_log_classifies_auth_failures(env):
    env["client"].post("/v1/orders", json={"item": "a", "quantity": 1})
    ghost = env["container"].issue_token("tenant-forged", scopes=["order:write"])
    env["client"].post(
        "/v1/orders", json={"item": "a", "quantity": 1},
        headers={**auth(ghost), "Idempotency-Key": "k"},
    )
    events = [e for e in read_audit_log(env["settings"].log_path)]
    assert any(e["event"] == "auth_failed" and e["reason"] == "tenant_not_found" for e in events)
    # 未认证请求绝不产生配额/业务事件
    assert not any(e["event"] in {"quota_consumed", "quota_exceeded", "order_created"} for e in events)
