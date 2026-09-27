"""认证与作用域：伪造租户、过期凭证、作用域不足，且未认证不消耗额度。"""

from tests.conftest import AUTH


def test_missing_credential(make_client):
    client = make_client()
    r = client.get("/data")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "missing_credential"


def test_forged_token_rejected(make_client):
    client = make_client()
    r = client.get("/data", headers={"Authorization": "Bearer forged-token"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_credential"


def test_expired_token_rejected(make_client):
    client = make_client()
    r = client.get("/data", headers={"Authorization": "Bearer tok-expired"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "expired_credential"


def test_insufficient_scope_rejected(make_client):
    client = make_client()
    # beta 只有 read 作用域，不能写
    r = client.post("/orders", headers={
        "Authorization": "Bearer tok-beta", "Idempotency-Key": "k1",
    }, json={"item": "x"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "insufficient_scope"
    # 但读接口可以
    assert client.get("/data", headers={"Authorization": "Bearer tok-beta"}).status_code == 200


def test_unauthenticated_does_not_consume_quota(make_client):
    from app.config import QuotaRule
    client = make_client({"/data": QuotaRule(limit=2, window_seconds=60)})
    # 大量未认证请求
    for _ in range(10):
        assert client.get("/data").status_code == 401
    # 合法租户额度未被消耗
    assert client.get("/data", headers=AUTH).status_code == 200
    assert client.get("/data", headers=AUTH).status_code == 200


def test_cannot_read_other_tenant_idempotency(make_client):
    client = make_client()
    r = client.post("/orders", headers={**AUTH, "Idempotency-Key": "shared-key"},
                    json={"item": "a"})
    assert r.status_code == 201
    # beta 没有 write 作用域，根本到不了幂等层
    r2 = client.post("/orders", headers={
        "Authorization": "Bearer tok-beta", "Idempotency-Key": "shared-key",
    }, json={"item": "a"})
    assert r2.status_code == 403
