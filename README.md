# 多租户 API 治理（本地可复现）

基于 FastAPI + SQLite 的多租户 API 治理能力：**租户认证与作用域校验**、**按租户 × 接口的配额限流**、**幂等写提交**。所有状态持久化在本地 SQLite 文件中，无需任何外部服务，并发与重启场景下结论一致。

## 结构

```
app/
  config.py        # 配额规则与服务配置
  db.py            # SQLite 线程安全封装（单连接 + 互斥锁，锁内执行并取数）
  tenants.py       # 租户凭证、作用域、分类认证错误
  quota.py         # 固定窗口限流器（原子 SQL 计数，状态落库）
  idempotency.py   # 幂等记录（主键占位 + 结果落库）
  main.py          # FastAPI 应用工厂：装配以上组件与路由
main.py            # 本地启动入口（uvicorn）
tests/             # 认证 / 配额 / 幂等 / 日志测试
```

## 租户边界

- 请求需携带 `Authorization: Bearer <token>`。凭证校验失败按类别区分，响应体为 `{"error": {"code", "detail"}}`：
  - `missing_credential`（401）：未携带凭证
  - `invalid_credential`（401）：伪造/未知 token
  - `expired_credential`（401）：凭证已过期
  - `insufficient_scope`（403）：凭证有效但缺少接口所需作用域（如 `write`）
- 认证在配额与幂等**之前**执行：未认证/越权请求不消耗任何租户额度，也到不了幂等层，无法读取其他租户的幂等结果。
- 配额计数与幂等记录均以 `tenant_id` 为主键前缀，租户之间天然隔离。

## 配额与限流语义

- 维度：`租户 × 接口`，规则为 `limit 次 / window_seconds 秒`（固定窗口，窗口起点为 unix 时间向下取整）。
- 计数通过单条原子 SQL（`INSERT ... ON CONFLICT DO UPDATE ... WHERE count < limit RETURNING`）完成：突发并发下恰好放行 `limit` 个请求，其余拒绝。
- 超限返回 `429`，携带 `Retry-After` 头（秒）与 `quota_exceeded` 错误体，被拒请求**不会**进入业务逻辑。
- 窗口重置行为确定：进入下一窗口后计数从 0 重新开始；`Retry-After` 精确指向当前窗口结束时刻。
- 计数持久化在 `quota_windows` 表：服务重启后同一窗口的计数**不清零也不翻倍**。

## 幂等语义

- 写接口 `POST /orders` 必须携带 `Idempotency-Key` 头，否则 `400 missing_idempotency_key`。
- 请求报文以 SHA-256 摘要标识。同一租户对同一键：
  - 首次：占位（`in_progress`）→ 执行 → 结果落库（`completed`），返回 `201`；
  - 同键同报文重试/重放：返回与首次**完全一致**的状态码与报文，并带 `X-Idempotent-Replay: true`，不会再次执行；
  - 同键不同报文：`409 idempotency_conflict`；
  - 并发占位冲突（另一请求正在执行）：`409 idempotency_in_progress`，可稍后重试。
- 结果持久化在 `idempotency_records` 表：重启后已成功结果仍然只重放、不重新执行；执行序号取自持久层，重启后不会重复分配。

## 失败重试原则

| 场景 | 状态码 | 建议 |
| --- | --- | --- |
| 凭证缺失/伪造/过期 | 401 | 不要自动重试，先修复凭证 |
| 作用域不足 | 403 | 不要重试，申请作用域 |
| 配额超限 | 429 | 按 `Retry-After` 秒后重试 |
| 幂等冲突（同键异体） | 409 | 不要重试，换用新的幂等键 |
| 幂等处理中 | 409 | 稍后以**相同报文**重试 |
| 缺少幂等键 | 400 | 补上 `Idempotency-Key` 再试 |

## 可核对日志

`governance` logger 输出结构化事件，便于核对错误分类、计数与执行结果：
`auth_failed`、`quota_allowed`（含 remaining/window_start）、`quota_rejected`（含 retry_after）、`idempotency_replayed`、`idempotency_conflict`、`idempotency_in_progress`、`order_executed`（含 seq）。

## 本地验证

```bash
uv sync                 # 安装依赖（仅 fastapi/uvicorn + 测试用 pytest/httpx）
uv run pytest tests/ -q # 全部测试：伪造租户、作用域不足、窗口边界、并发抢额度、
                        # 同键并发重放、同键异体冲突、重启恢复、日志核对
uv run python main.py   # 启动服务（127.0.0.1:8000，状态写入 ./governance.db）
```

手动验证（内置演示租户：`demo-token-alpha` 有 read+write，`demo-token-beta` 仅 read）：

```bash
curl -H 'Authorization: Bearer demo-token-alpha' http://127.0.0.1:8000/data
curl -X POST -H 'Authorization: Bearer demo-token-alpha' \
     -H 'Idempotency-Key: order-001' -H 'Content-Type: application/json' \
     -d '{"item": "widget"}' http://127.0.0.1:8000/orders
# 重复上面这条 POST -> 返回相同结果并带 X-Idempotent-Replay: true
# 重启进程后再重复   -> 仍然只重放，不会再次执行
```
