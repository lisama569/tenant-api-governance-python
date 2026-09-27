"""无状态 HMAC 租户凭证（token）签发与校验。

token 形如:  base64url(payload_json).base64url(hmac_sha256)
payload: {"tenant_id": str, "scopes": [str], "exp": int}

本地零外部服务；密钥来自配置（生产应由环境注入）。
签名伪造、结构损坏 -> InvalidCredentialError；过期 -> ExpiredCredentialError。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from app.errors import ExpiredCredentialError, InvalidCredentialError


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload_b64: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def issue_token(tenant_id: str, scopes: list[str], ttl_seconds: int, secret: str, *, now: int | None = None) -> str:
    now_ts = int(time.time()) if now is None else now
    payload = {"tenant_id": tenant_id, "scopes": list(scopes), "exp": now_ts + ttl_seconds}
    payload_b64 = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def parse_token(token: str, secret: str, *, now: int | None = None) -> dict:
    now_ts = int(time.time()) if now is None else now
    if not token or token.count(".") != 1:
        raise InvalidCredentialError("malformed credential")
    payload_b64, signature = token.split(".", 1)
    if not payload_b64 or not signature:
        raise InvalidCredentialError("malformed credential")
    expected = _sign(payload_b64, secret)
    if not hmac.compare_digest(expected, signature):
        raise InvalidCredentialError("credential signature verification failed")
    try:
        payload = json.loads(_b64decode(payload_b64))
    except Exception:
        raise InvalidCredentialError("credential payload is not valid json")
    for field in ("tenant_id", "scopes", "exp"):
        if field not in payload:
            raise InvalidCredentialError(f"credential missing field: {field}")
    if not isinstance(payload["scopes"], list):
        raise InvalidCredentialError("credential scopes must be a list")
    if int(payload["exp"]) < now_ts:
        raise ExpiredCredentialError("credential has expired")
    return payload
