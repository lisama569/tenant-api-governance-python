# 多租户 API 治理（FastAPI · 本地零外部依赖）

一套可在本地完整复现的多租户 API 治理能力：**租户身份与作用域校验**、
**按租户 × 接口的固定窗口配额/限流**、**带幂等键的写请求去重**。
所有状态保存在本地 SQLite，结论在**突发并发**与**进程重启**下保持一致；
无任何外部服务（无需 Redis / 消息队列 / 独立数据库）。

## 1. 能力总览

| 能力 | 保证 |
| --- | --- |
| 租户凭证 | HMAC-SHA256 无状态 token；签名伪造/损坏、过期、租户不存在、作用域不足分别返回**可区分**错误码 |
| 租户边界 | 认证先于一切；未认证请求**不消耗额度、不触碰幂等记录**；数据/幂等结果按 `tenant_id` 严格隔离 |
| 配额限流 | 固定窗口，按 `(租户, 接口, 窗口起点)` 计数；窗口对齐时间轴，重启后**不清零、不翻倍** |
| 并发抢额度 | 「读-判-写」在同一临界区 + 原子 SQL 内完成，突发并发**绝不超额** |
| 幂等提交 | 同 `(租户, 键)` 并发/重试只生效一次，重复请求返回与首次**完全一致**的状态码与响应体 |
| 同键异体 | 同键但报文不同 -> `409 idempotency_conflict`，绝不执行第二次 |
| 重启安全 | 成功结果落盘，重启后只回放、**不再次执行**；崩溃残留的处理中记录被标记为 `interrupted`，同报文可接管重试 |

## 2. 错误分类（`error_code` 可被客户端程序化处理）

| HTTP | error_code | 含义 | 是否可重试 |
| --- | --- | --- | --- |
| 401 | `invalid_credential` | 缺少/格式错/签名校验失败的凭证 | 否（修正凭证） |
| 401 | `expired_credential` | 凭证已过期 | 否（换新凭证） |
| 401 | `tenant_not_found` | 凭证合法但租户不存在或已吊销 | 否 |
| 403 | `scope_denied` | 作用域不足 | 否（申请作用域） |
| 429 | `quota_exceeded` | 当前窗口配额用尽，响应含 `retry_after`（秒） | 是，等待 `retry_after` |
| 409 | `idempotency_conflict` | 同键异体冲突，或同键请求仍在处理中 | 异体不可重试；处理中可稍后重试 |
| 422 | `invalid_request` | 请求体未通过校验 | 否（修正报文） |

## 3. HTTP 接口

### `GET /health`
存活探针，无需认证。

### `POST /v1/orders`（写，需要 `order:write`）
- 请求头：`Authorization: Bearer <token>`、`Idempotency-Key: <客户端生成的唯一键>`
- 请求体：`{"item": str, "quantity": int}`
- 成功：`201`，响应 `{"order_id", "tenant_id", "item", "quantity"}`

治理顺序（每一步都有确定拒绝点）：
1. **认证 + 作用域**：失败立即终止，不产生任何额度/副作用；
2. **幂等键校验 + 幂等判定**：命中既有成功结果则原样回放（**不重复扣额度、不重复执行**）；同键异体在此 `409`；
3. **配额扣减**：仅「首次执行」占用一个额度；超限返回 `429 + retry_after`，并释放幂等占位，允许下窗口用同键同报文安全重试；
4. **业务执行一次**并落盘，结果写入幂等记录供后续重放。

### `GET /v1/orders`（读，需要 `order:read`）
认证后按 `order.list` 独立配额计数，只返回本租户订单。

## 4. 配额与窗口语义

- 固定窗口大小 `TENANT_GOV_WINDOW_SECONDS`（默认 60s），窗口起点
  `window_start = floor(now / T) * T`，与进程启停无关，因此重启后窗口划分一致。
- 计数行唯一键 `(tenant_id, endpoint, window_start)`，一行表示一个窗口；
  扣减为条件更新 `... WHERE count < limit`，达上限后计数**不再增加**。
- 默认每窗口限额：写 `order.create=3`、读 `order.list=10`（可用环境变量调整）。
- 超限请求**不执行业务**，响应体给出 `retry_after`（距下一窗口起点的秒数）。
- 不同租户、不同接口的计数彼此独立。

## 5. 幂等语义

- 作用域为 `(tenant_id, idempotency_key)`，**天然按租户隔离**：A 租户无法读取/命中 B 租户的同键结果。
- 报文指纹：对请求体做 `json.dumps(sort_keys=True)` 后的 SHA-256；键相同而指纹不同即冲突。
- 状态机：`processing -> completed`；崩溃残留由启动恢复置为 `interrupted`。

```
无记录       -> 占位 processing，获得唯一执行权
processing   -> 本进程他线程在执行：同报文等待后回放；异体立即 409；超时 409+retry_after
interrupted  -> 上次崩溃残留：同报文允许接管重试；异体 409
completed    -> 同报文回放原状态码与响应体；异体 409
```

- 仅 `completed` 被视为「成功结果」，重启后永不再次执行业务；
  业务异常或限流导致未完成时记录回到 `interrupted`，客户端可用同键同报文安全重试。

## 6. 失败重试原则

1. **401/403/422 不要盲目重试**：先修正凭证/作用域/报文。
2. **429 按 `retry_after` 退避重试**；携带**相同** `Idempotency-Key` 与相同报文，成功后服务端只执行一次。
3. **网络超时/5xx**：用相同键、相同报文重试；若首次其实已成功，服务端回放原结果，不会重复下单。
4. **409 冲突分两类**：`同键异体` 属客户端用键错误，必须更换键或报文；`处理中` 可稍后重试。
5. 幂等键建议用 UUID 等在客户端全局唯一、且与「一次业务意图」绑定的值。

## 7. 审计日志

每条治理决策以**一行 JSON** 写入 `logs/governance.log`（同时输出到控制台），
字段稳定、可逐行核对，关键事件包括：

`authenticated` / `auth_failed` / `quota_consumed` / `quota_exceeded` /
`idempotency_begin` / `idempotency_replay` / `idempotency_conflict` /
`idempotency_finish` / `idempotency_recover` / `order_created`。

例如限流与重放：

```json
{"event":"quota_exceeded","tenant_id":"tenant-acme","endpoint":"order.create","status":"rejected","used":3,"limit":3,"window_start":1000020,"retry_after":1}
{"event":"idempotency_replay","tenant_id":"tenant-acme","key":"k1","endpoint":"order.create","status":"replayed"}
```

## 8. 本地验证

依赖只有 FastAPI（含 uvicorn/httpx）与开发依赖 pytest，均已在 `pyproject.toml` 声明。

```bash
# 方式一：自动化测试（30 个用例，覆盖全部要求场景）
uv run pytest -q

# 方式二：真实进程端到端验证（自动起停 uvicorn，并做一次重启恢复演练）
uv run python scripts/verify_local.py

# 方式三：手工演练
uv run python scripts/dev.py reset                       # 清空本地 DB/日志
uv run python scripts/dev.py token tenant-acme           # 签发写权限 token
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 # 启动服务
# 新开终端：
curl -s localhost:8000/health
curl -s -X POST localhost:8000/v1/orders \
  -H "Authorization: Bearer <token>" -H "Idempotency-Key: demo-1" \
  -H "Content-Type: application/json" -d '{"item":"book","quantity":2}'
# 重复执行上一条：返回相同 order_id；把 item 改掉：返回 409
tail -f logs/governance.log                              # 核对审计日志
```

测试覆盖：伪造租户、伪造/过期凭证、作用域不足、窗口边界、突发并发抢额度、
同键并发重放、同键异体冲突、限流后跨窗口安全重试、跨重启的计数与幂等结果恢复。

## 9. 配置（环境变量，前缀 `TENANT_GOV_`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `data/governance.db` | SQLite 文件路径 |
| `LOG_PATH` | `logs/governance.log` | 审计日志路径 |
| `TOKEN_SECRET` | 本地开发固定值 | HMAC 密钥，生产必须由环境注入 |
| `TOKEN_TTL_SECONDS` | `3600` | token 有效期 |
| `WINDOW_SECONDS` | `60` | 配额窗口大小 |
| `ORDER_CREATE_LIMIT` | `3` | 每租户每窗口写次数 |
| `ORDER_LIST_LIMIT` | `10` | 每租户每窗口读次数 |

预置本地租户（仅用于本地验证）：

| tenant_id | 作用域 |
| --- | --- |
| `tenant-acme` | `order:read`, `order:write` |
| `tenant-readonly` | `order:read`（用于作用域不足用例） |

> 安全说明：本地演示使用 HMAC 无状态 token 与固定密钥，目的是零外部依赖复现；
> 接入生产时应通过环境变量注入强密钥，并由租户管理系统提供租户与作用域。

## 10. 项目结构

```
app/
  config.py                 # 配置（pydantic-settings）
  clock.py                  # 可替换时钟（测试冻结时间）
  errors.py                 # 统一错误分类
  logging_setup.py          # JSON 审计日志
  api/
    container.py            # 组装 DB/仓储/服务、播种、启动恢复
    routes.py               # 认证->幂等->配额->业务 全链路编排
    errors.py               # 异常到 JSON 的映射
  security/
    tokens.py               # HMAC token 签发/校验
    dependencies.py         # 认证与作用域依赖
  governance/
    quotas.py               # 固定窗口配额（临界区 + 重试时间）
    idempotency.py          # 幂等状态机
  storage/
    database.py             # SQLite 连接与建表
    repositories.py         # 租户/配额/幂等/订单 仓储
  services/orders.py        # 订单业务（一次性副作用）
  models/schemas.py         # Pydantic 模型
scripts/
  dev.py                    # 签发 token / 重置数据
  verify_local.py           # 真实进程端到端 + 重启验证
tests/                      # pytest 用例（30）
```
