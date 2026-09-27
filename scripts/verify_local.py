"""本地端到端验证：启动真实 uvicorn 进程，演示并校验全部治理语义。

运行:
    uv run python scripts/verify_local.py

会自动：重置本地 DB -> 起服务 -> 签发 token -> 验证认证/作用域/
配额窗口/幂等重放/同键异体冲突 -> 重启进程验证计数与结果持久化。
退出码 0 表示全部断言通过。
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port: int, db: Path, log: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "TENANT_GOV_DATABASE_PATH": str(db),
        "TENANT_GOV_LOG_PATH": str(log),
        "TENANT_GOV_TOKEN_SECRET": "local-verify-secret",
        "TENANT_GOV_WINDOW_SECONDS": "60",
        "TENANT_GOV_ORDER_CREATE_LIMIT": "100",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return proc
        except OSError:
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("server did not start")


def call(port: int, method: str, path: str, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    payload = json.dumps(body) if body is not None else None
    conn.request(method, path, body=payload, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8")
    conn.close()
    return resp.status, json.loads(data) if data else None


def issue_token(tenant_id: str) -> str:
    # 与服务相同配置下本地签发
    os.environ["TENANT_GOV_TOKEN_SECRET"] = "local-verify-secret"
    from importlib import reload
    import app.config as config
    reload(config)
    from app.api.container import build_container
    c = build_container(config.settings)
    try:
        return c.issue_token(tenant_id)
    finally:
        c.close()


def main() -> int:
    db = ROOT / "data" / "verify.db"
    log = ROOT / "logs" / "verify.log"
    for p in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm"), log):
        if p.exists():
            p.unlink()

    os.environ["TENANT_GOV_TOKEN_SECRET"] = "local-verify-secret"
    port = _free_port()
    proc = start_server(port, db, log)
    checks = 0

    def expect(cond: bool, msg: str):
        nonlocal checks
        assert cond, f"FAILED: {msg}"
        checks += 1
        print(f"  ✓ {msg}")

    try:
        acme = issue_token("tenant-acme")
        ro = issue_token("tenant-readonly")

        print("[1] 认证与作用域")
        s, b = call(port, "POST", "/v1/orders", {"Idempotency-Key": "k"}, {"item": "x", "quantity": 1})
        expect(s == 401 and b["error_code"] == "invalid_credential", "无凭证 -> invalid_credential")

        s, b = call(port, "POST", "/v1/orders",
                    {"Authorization": "Bearer garbage", "Idempotency-Key": "k"}, {"item": "x", "quantity": 1})
        expect(s == 401 and b["error_code"] == "invalid_credential", "伪造签名 -> invalid_credential")

        s, b = call(port, "POST", "/v1/orders",
                    {"Authorization": f"Bearer {ro}", "Idempotency-Key": "k"}, {"item": "x", "quantity": 1})
        expect(s == 403 and b["error_code"] == "scope_denied", "只读租户写 -> scope_denied")

        print("[2] 配额与限流（用读接口 order.list，窗口上限 10）")
        h = {"Authorization": f"Bearer {acme}"}
        codes = []
        for i in range(11):
            sx, bx = call(port, "GET", "/v1/orders", h)
            codes.append(sx)
        expect(codes == [200] * 10 + [429],
                f"前10成功第11限流: {codes.count(200)}x200, {codes.count(429)}x429")
        expect(isinstance(bx["retry_after"], int) and bx["retry_after"] >= 1,
               f"429 携带 retry_after={bx.get('retry_after')}")

        print("[3] 幂等重放与同键异体冲突（写接口窗口上限 100，互不干扰）")
        s, first = call(port, "POST", "/v1/orders", {**h, "Idempotency-Key": "idem-1"},
                        {"item": "book", "quantity": 2})
        expect(s == 201, "首次提交 201")
        s, replay = call(port, "POST", "/v1/orders", {**h, "Idempotency-Key": "idem-1"},
                         {"item": "book", "quantity": 2})
        expect(s == 201 and replay["order_id"] == first["order_id"], "重放返回相同 order_id")
        s, conflict = call(port, "POST", "/v1/orders", {**h, "Idempotency-Key": "idem-1"},
                           {"item": "different", "quantity": 2})
        expect(s == 409 and conflict["error_code"] == "idempotency_conflict", "同键异体 -> 409")

    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print("[4] 重启恢复（计数不丢、成功结果不重执行）")
    port2 = _free_port()
    proc2 = start_server(port2, db, log)
    try:
        from importlib import reload
        import app.config as config
        reload(config)
        acme = issue_token("tenant-acme")
        h = {"Authorization": f"Bearer {acme}"}

        s, replay = call(port2, "POST", "/v1/orders", {**h, "Idempotency-Key": "idem-1"},
                         {"item": "book", "quantity": 2})
        expect(s == 201 and replay["item"] == "book", "重启后同键仍回放原结果")

        # 直接核验 SQLite：读接口计数已落盘为 10，写接口在功能阶段成功过 1 次，
        # 重启后既不清零也不翻倍；订单副作用未因重放而增加。
        import sqlite3
        conn = sqlite3.connect(db)
        qrows = dict((r[0], r[1]) for r in conn.execute(
            "SELECT endpoint, count FROM quota_counters"))
        order_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        conn.close()
        expect(qrows.get("order.list") == 10, f"读窗口计数持久化=10（实际 {qrows.get('order.list')}）")
        expect(qrows.get("order.create") == 1, f"写窗口计数持久化=1（实际 {qrows.get('order.create')}）")
        expect(order_count == 1, f"成功结果未被重复执行，订单仍为 1（实际 {order_count}）")
    finally:
        proc2.terminate()
        proc2.wait(timeout=10)

    print(f"\n全部 {checks} 项本地验证通过 ✅  (审计日志: {log})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
