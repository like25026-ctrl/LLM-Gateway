# DeepSeek LLM Gateway — 多协议统一大模型网关

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

基于 FastAPI 构建的企业级大模型统一网关：用**一套 OpenAI Chat Completions 契约**屏蔽三类上游协议的差异，
并提供结构化输出、提示词版本管理、Token/延迟可观测、重试/熔断/限流等生产级韧性能力。

**配置驱动**：供应商、模型路由、价格、韧性参数全部集中在 [gateway.yaml](gateway.yaml)，改配置即可上线，无需改代码。

---

## 核心特性

| 能力 | 说明 |
|---|---|
| **OpenAI 兼容入口** | 新版 `/v1` 兼容层直接对齐全原生 OpenAI SDK：`POST /v1/chat/completions`（含 `stream:true` SSE）、`POST /v1/responses`、`GET /v1/models`；旧版 `/api/v1/*` 继续保留 |
| **可选调用方鉴权** | 配置 `api_keys` 即启用 `Authorization: Bearer` 校验；留空则**完全关闭**（内置 Web 界面零改动） |
| **多协议归一化** | 适配器模式封装 OpenAI Chat / OpenAI Responses / Anthropic Messages 三类协议，出口统一为 OpenAI Chat Completions 结构，前端零改动 |
| **路由能力声明** | 每条路由可用 `api: chat\|responses\|both` 显式声明支持的入口；未声明时按 `type` 自动推导，`/v1/responses` 只放行声明为 responses 的路由 |
| **配置驱动路由** | `providers`（供应商）与 `models`（逻辑模型 + 路由列表）解耦；支持 `priority` 故障转移与 `weighted_round_robin` 平滑加权轮询 |
| **环境变量插值** | 配置文件支持 `${VAR}` / `${VAR:-default}`，解析顺序：系统环境变量 → `.env` → 默认值 |
| **结构化输出** | `response_format` 三协议通吃：能原生支持的走 `text.format` / `json_object` 透传，不支持的用「提示词注入 + 本地 jsonschema 校验」模拟，校验失败可按 `structured_output_retries` 自修复 |
| **提示词版本管理** | 模板存储 + 版本自增 + 变量渲染；请求可按 `name`（取最新）或 `name+version` 引用；版本创建经唯一约束 + 冲突重试保证并发安全 |
| **韧性** | 统一错误码 + 指数退避重试（最大延迟封顶）+ 按路由熔断（半开探测）+ IP/模型双维度滑动窗口限流（超限 429） |
| **可观测** | 每次调用（成功/失败/限流/熔断）异步落库 `usage_events`（`asyncio.to_thread` 卸载工作线程，不阻塞事件循环）；分类统计 Token、费用（含缓存命中优惠）、总延迟与首 Token 延迟（TTFT）、重试/降级次数 |
| **结构化日志** | 基于 structlog，请求自动关联 `X-Request-ID` |
| **健康检查** | 三层探针（liveness / readiness / info），适配 Kubernetes |
| **内置 Web 界面 + Docker** | 自带对话测试页；提供 Dockerfile 与 docker-compose |

---

## 三类上游协议

| 协议 | 鉴权 | 结构化输出 | 流式 | 本项目适配器 |
|---|---|---|---|---|
| OpenAI Chat Completions | `Authorization: Bearer` | `json_object` 原生；`json_schema` 降级模拟 | SSE `data:` | `ChatAdapter` |
| OpenAI Responses API | `Authorization: Bearer` | `text.format` 原生 `json_schema` | `response.output_text.delta` 等事件 | `ResponsesAdapter` |
| Anthropic Messages API | `x-api-key`（附 Bearer 兼容） | 提示词注入模拟 | `content_block_delta` 等事件 | `AnthropicAdapter` |

> 关键设计：`BaseAdapter` 固定四个契约 `headers()` / `build_payload()` / `parse_response()` / `parse_stream_event()`，
> 上层编排层只依赖 `UnifiedRequest` / `UnifiedResponse` / `StreamDelta`，不感知具体协议。

---

## 技术栈

| 组件 | 用途 |
|---|---|
| FastAPI + Uvicorn | Web 框架与 ASGI 服务器 |
| httpx | 上游异步调用（连接复用） |
| SQLAlchemy 2.x + SQLite | ORM 与调用记录 / 模板持久化 |
| Pydantic Settings | 环境变量与 `.env` 配置 |
| structlog | 结构化日志 |
| PyYAML | `gateway.yaml` 解析 |
| jsonschema | 结构化输出本地校验 |
| tiktoken | 上下文预算检查 |

---

## 项目目录结构

```
Agent_platform/
├── app/
│   ├── main.py                      # 应用入口：中间件 / 路由 / 错误处理器 / 建表
│   ├── config.py                    # Pydantic Settings（.env + 环境变量）
│   ├── database.py                  # SQLAlchemy 引擎与会话
│   ├── schemas.py                   # Pydantic 数据模型
│   ├── api/
│   │   ├── openai_compat.py         # /v1 兼容层：models / chat/completions / responses
│   │   ├── health.py                # /health/live|ready|info + 运维别名 /healthz /readyz
│   │   └── routes.py                # /api/v1/* 对话、模板、用量 + 别名 /admin/usage
│   ├── core/                        # 核心能力
│   │   ├── gateway_config.py        # gateway.yaml 加载 + 环境变量插值 + tuning + api_keys
│   │   ├── auth.py                  # 可选调用方鉴权依赖（Bearer / X-API-Key）
│   │   ├── circuit_breaker.py       # 按路由熔断状态机
│   │   ├── errors.py                # 统一错误码 + OpenAI 风格错误体 + 全局异常处理器
│   │   ├── rate_limit.py            # 滑动窗口限流（IP / 模型）
│   │   ├── security.py              # 敏感词过滤
│   │   └── logging_config.py        # structlog 配置
│   ├── middleware/                  # request_id / logging 中间件
│   ├── models/__init__.py           # PromptTemplate / UsageEventModel / ApiKey / ConversationLog
│   ├── services/                    # 业务编排
│   │   ├── upstream.py              # 三协议适配器 + 统一数据类型
│   │   ├── router.py                # 配置驱动的模型注册表与路由策略
│   │   ├── gateway.py               # 调用编排：故障转移 / 熔断 / 重试 / 限流 / 结构化 / 观测
│   │   ├── structured_output.py     # 输入 / 输出 / 校验 / 修复 四件套
│   │   ├── prompts.py               # 提示词模板与版本管理（异步接口，版本创建并发安全）
│   │   └── usage.py                 # 用量合并 / 计价 / UsageRepository（异步落库与查询）
│   └── static/index.html            # 内置 Web 对话界面
├── tests/
│   └── test_sync_generation.py      # 功能点验证脚本（重点覆盖同步生成）
├── gateway.yaml                     # 网关配置（供应商 / 模型 / 价格 / 韧性）
├── requirements.txt
├── Dockerfile
├── docker-compose*.yml
└── README.md
```

---

## 快速开始

### 1. 环境准备

```bash
# 创建并激活虚拟环境
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# 安装依赖（含 pyyaml / jsonschema / tiktoken）
pip install -r requirements.txt
```

### 2. 配置环境变量

项目默认读取 `.env.development`（可用 `ENV_FILE` 覆盖）：

```bash
# .env.development
ENVIRONMENT=development
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
# 调用方鉴权（留空 = 关闭；设置后 /v1 与 /api/v1 需携带 Bearer）
GATEWAY_API_KEY=replace-with-a-long-random-secret
DATABASE_URL=sqlite:///./dev.db
RATE_LIMIT=60            # 每 IP 每分钟最大请求数
MODEL_RATE_LIMIT=60      # 每模型每分钟最大请求数
MAX_TOKENS=4096
LOG_LEVEL=INFO
```

### 3. 启动服务

```bash
# 开发模式（热重载）
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 生产模式
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

启动后访问：

| 地址 | 说明 |
|---|---|
| <http://localhost:8000/docs> | OpenAPI 交互文档（仅 development） |
| <http://localhost:8000/> | 内置 Web 对话界面 |
| <http://localhost:8000/health/info> | 服务元信息（版本 / 环境 / 运行时长） |

> 提示：`gateway.yaml` 中的 `${DEEPSEEK_API_KEY:-}` 会自动读取 `.env.development` 或系统环境变量，无需重复配置。

### 4. 一键验证（覆盖全部功能点）

```bash
# 全量验证：离线单测 + 真实上游 E2E（同步生成 4 模型、结构化、故障转移、流式、观测…）
python tests/test_sync_generation.py

# 仅离线单测（不联网，无需 API Key）
python tests/test_sync_generation.py --offline
```

脚本用 `httpx.ASGITransport` 在进程内直接调用应用，**无需先起服务、不占用端口**；
未配置 `DEEPSEEK_API_KEY` 或设置 `GATEWAY_LIVE=0` 时，E2E 用例自动跳过。

---

## 配置说明（gateway.yaml）

```yaml
api_keys:                    # 调用方鉴权白名单（留空 = 关闭鉴权，开发模式）
  - ${GATEWAY_API_KEY:-}     # 可用逗号分隔多个，或设环境变量 API_KEYS

retry:                       # 上游重试：指数退避 + 最大延迟封顶
  max_attempts: 3
  base_delay_seconds: 0.5
  max_delay_seconds: 4.0
  retry_statuses: [408, 429, 500, 502, 503, 504]

circuit_breaker:             # 按路由（provider:model）熔断 → 冷却 → 半开探测
  enabled: true
  failure_threshold: 5
  cooldown_seconds: 30

rate_limit:
  enabled: true              # 关掉即整体禁用限流（阈值见 app/config.py）

structured_output_retries: 1 # 结构化校验失败后的自修复次数（0=不修复）

providers:                   # 供应商：endpoint/鉴权/超时与模型解耦
  deepseek:
    base_url: https://api.deepseek.com
    api_key: ${DEEPSEEK_API_KEY:-}
    timeout_seconds: 120

models:                      # 逻辑模型 → 路由列表
  deepseek-chat:
    display_name: DeepSeek Chat (V3)
    strategy: priority       # priority | weighted_round_robin
    routes:
      - {provider: deepseek, type: chat, model: deepseek-chat, api: chat}   # api: chat|responses|both
  deepseek-v4-pro:
    display_name: DeepSeek V4 Pro (Responses)
    strategy: priority
    routes:
      - {provider: deepseek, type: responses, model: deepseek-v4-pro, api: responses}
  smart:                     # 别名 + 故障转移演示（首路由不可达 → 自动切备用）
    display_name: Smart (failover demo)
    strategy: priority
    routes:
      - {provider: deepseek, type: chat, model: deepseek-chat, base_url: http://127.0.0.1:9, api: chat}
      - {provider: deepseek, type: chat, model: deepseek-chat, api: chat}

pricing:                     # 单位：元 / 1M tokens；cached_input 未配置时按 input 价
  deepseek-chat: {input_per_million: 1.0, output_per_million: 2.0, cached_input_per_million: 0.2}
```

新增供应商或模型只需在此追加配置，无需改动代码。

> **路由能力声明 `api`**：`chat` / `responses` / `both`，未声明时按 `type` 自动推导
> （`chat`/`anthropic` → `chat`，`responses` → `responses`）。`/v1/responses` 入口**只放行**声明为
> `responses` 的路由，避免把仅支持 Chat 的供应商伪装成 Responses API；`/api/v1/chat` 与
> `/v1/chat/completions` 不做此过滤以保持向后兼容。

---

## API 端点

### OpenAI 兼容层（`/v1`，可用原生 OpenAI SDK 调用）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/models` | 返回网关公开的模型别名列表（OpenAI list 结构） |
| POST | `/v1/chat/completions` | Chat Completions 兼容入口，支持 `stream:true` SSE，请求/响应结构与 OpenAI 一致 |
| POST | `/v1/responses` | Responses API 兼容入口（协议透明代理，仅放行 `api: responses` 的路由，不支持 `stream:true`） |

> 新版 `/v1` 与旧版 `/api/v1` 并存，二者共享同一套路由 / 韧性 / 观测能力。
> 配置了 `api_keys` 时，`/v1`、`/api/v1` 与 `/admin` 均需携带 `Authorization: Bearer <key>`；
> 探针 `/healthz`、`/readyz` 与 `/health/*` 不校验。

### 原生管理 / 对话层（`/api/v1`，保留兼容）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/models` | 支持的模型列表（仅 enabled） |
| POST | `/api/v1/chat` | 非流式对话（同步生成） |
| POST | `/api/v1/chat/stream` | 流式对话（SSE） |
| POST | `/api/v1/prompts` | 创建提示词模板版本 |
| GET | `/api/v1/prompts` | 列出所有模板版本 |
| GET | `/api/v1/prompts/{name}` | 取模板（默认最新版本） |
| GET | `/api/v1/prompts/{name}/{version}` | 取指定版本 |
| POST | `/api/v1/prompts/{name}/render` | 渲染模板（预览变量替换） |
| GET | `/api/v1/usage/recent?limit=100` | 最近调用记录（成功/失败/限流） |

### 运维别名

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | Liveness 探针（等价 `/health/live`） |
| GET | `/readyz` | Readiness 探针（等价 `/health/ready`） |
| GET | `/admin/usage?limit=100` | 用量账本（等价 `/api/v1/usage/recent`） |
| GET | `/health/live` `/health/ready` `/health/info` | 原生健康探针 |

统一错误响应体（OpenAI 风格，同时保留旧字段以保证兼容）：

```json
{
  "error": {
    "code": "model_not_found",
    "type": "invalid_request_error",
    "message": "不支持的模型: xxx",
    "param": "model",
    "detail": null
  }
}
```

错误码与 `type` 映射：`invalid_request` / `model_not_found` / `prompt_not_found` / `structured_output_failed`
→ `invalid_request_error`；`unauthorized` → `authentication_error`；`rate_limited` → `rate_limit_error`；
`upstream_error` / `upstream_timeout` / `network_error` / `circuit_open` → `api_error`。

---

## 使用 OpenAI SDK

调用方只需持有网关密钥（配置了 `api_keys` 时），无需任何上游供应商密钥即可使用原生 SDK：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="your-gateway-key",  # gateway.yaml api_keys 中的调用方密钥
)

# 非流式
resp = client.chat.completions.create(
    model="smart",  # gateway.yaml 中的公开别名
    messages=[{"role": "user", "content": "用一句话说明什么是 Agent Loop"}],
    temperature=0.2,
    max_tokens=256,
)
print(resp.choices[0].message.content)

# 流式（SSE 逐块转发）
stream = client.chat.completions.create(
    model="deepseek-flash",
    messages=[{"role": "user", "content": "写一个 FastAPI SSE 示例"}],
    stream=True,
)
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

> 若网关未配置 `api_keys`（开发模式），`api_key` 可随意填写，服务端不校验。

---

## curl 示例

> 以下示例假设服务运行在 `http://localhost:8000`。

### 1. 模型列表

```bash
curl -s http://localhost:8000/api/v1/models
```

### 2. 同步生成（非流式对话）

```bash
curl -s -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "用一句话回答：1+1=?"}],
    "temperature": 0.3,
    "max_tokens": 256
  }'
```

返回归一化后的 OpenAI Chat Completions 结构：

```json
{
  "id": "...",
  "object": "chat.completion",
  "model": "deepseek-chat",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "1+1=2。"}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
}
```

### 3. 结构化输出（JSON Schema 约束）

```bash
curl -s -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "计算 3+5，只返回 JSON"}],
    "response_format": {
      "type": "json_schema",
      "name": "calc",
      "schema": {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"]
      }
    }
  }'
```

`choices[0].message.content` 即合法 JSON 文本（`{"answer": 8}`）。也兼容 OpenAI 风格
`{"type":"json_schema","json_schema":{"name":"calc","schema":{...}}}`，以及 `{"type":"json_object"}`。

### 4. 提示词模板：创建 / 查询 / 渲染 / 引用

```bash
# 创建模板（同一 name 重复创建会自增版本）
curl -s -X POST http://localhost:8000/api/v1/prompts \
  -H "Content-Type: application/json" \
  -d '{"name": "assistant", "template": "你是{role}，请用{lang}回答。", "variables": ["role", "lang"]}'

# 查看最新版本 / 指定版本
curl -s http://localhost:8000/api/v1/prompts/assistant
curl -s http://localhost:8000/api/v1/prompts/assistant/1

# 预览渲染结果
curl -s -X POST http://localhost:8000/api/v1/prompts/assistant/render \
  -H "Content-Type: application/json" \
  -d '{"variables": {"role": "技术助手", "lang": "中文"}, "version": 1}'

# 在对话中引用模板（作为 system 消息前插，并记录 prompt_version）
curl -s -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "prompt": {"name": "assistant", "version": 1, "variables": {"role": "技术助手", "lang": "中文"}},
    "messages": [{"role": "user", "content": "什么是 SSE？"}]
  }'
```

### 5. 流式输出（SSE）

```bash
curl -N -X POST http://localhost:8000/api/v1/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"model": "deepseek-flash", "messages": [{"role": "user", "content": "用一句话说明什么是 SSE"}]}'
```

逐块返回 `data: {...}`，以 `data: [DONE]` 结束；与 OpenAI 流式结构一致。

### 6. 故障转移（multi-route failover）

```bash
# smart 的首选路由指向不可达地址，请求会自动切到备用路由并成功返回
curl -s -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"model": "smart", "messages": [{"role": "user", "content": "hi"}]}'
```

对应调用记录中 `fallbacks >= 1`。

### 7. 调用记录（可观测）

```bash
curl -s "http://localhost:8000/api/v1/usage/recent?limit=20"
```

每条记录含：`requested_model` / `provider` / `upstream_model` / `stream` / `status` /
`input_tokens` / `output_tokens` / `cached_tokens` / `cost_usd`（元） / `latency_ms` /
`first_token_ms`（TTFT） / `retries` / `fallbacks` / `error_type` / `prompt_version` / `metadata`。

### 8. 健康检查与运维别名

```bash
curl -s http://localhost:8000/health/live    # 或 /healthz
curl -s http://localhost:8000/health/ready   # 或 /readyz
curl -s http://localhost:8000/health/info
```

### 9. OpenAI 兼容入口（`/v1`）

```bash
# 模型别名列表
curl -s http://localhost:8000/v1/models -H "Authorization: Bearer your-gateway-key"

# Chat Completions 非流式
curl -s -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer your-gateway-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"1+1=?"}],"max_tokens":64}'

# Chat Completions 流式（SSE）
curl -N -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer your-gateway-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-flash","messages":[{"role":"user","content":"说一句话"}],"stream":true}'

# Responses API（仅 api: responses 的路由，如 deepseek-v4-pro）
curl -s -X POST http://localhost:8000/v1/responses \
  -H "Authorization: Bearer your-gateway-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-pro","input":"用三点解释模型路由"}'
```

> `Authorization` 只在配置了 `api_keys` 时必需；开发模式可省略。
> `/v1/responses` 不支持 `stream:true`，且请求未声明 `api: responses` 的模型会返回 `model_not_found`。

---

## 请求处理流程

```
客户端
  │
  ▼  RequestIDMiddleware（生成/透传 X-Request-ID）
  ▼  LoggingMiddleware（记录请求耗时）
  ▼  鉴权（可选）：/v1、/api/v1、/admin → require_api_key（Bearer / X-API-Key）
  ▼  路由匹配：/v1/chat/completions | /v1/responses | /api/v1/chat|stream
  ▼  _validate_request（模型白名单 + 消息非空 + 敏感词）
  ▼  _apply_prompt（可选：模板渲染前插 system 消息）
  ▼  gateway.chat_completion / chat_completion_stream
        ├─ require_api：/v1/responses 只放行 api: responses 的路由
        ├─ 限流：IP + 按模型（滑动窗口，超限 429）
        ├─ 路由：按策略取候选路由 → 过滤熔断中的路由（全熔断 503）
        ├─ 上游调用：重试（指数退避+封顶）→ 失败自动切换下一路由（fallbacks）
        ├─ 结构化：schema 归一化 → 三协议适配 → 校验 → 按需自修复
        └─ 观测：UsageEvent 异步落库（to_thread 不阻塞事件循环）+ llm_call_completed 结构化日志
  ▼  归一化出口（Chat Completions / Responses / SSE）
客户端
```

---

## Docker 部署

```bash
# 使用 docker-compose（推荐）
docker-compose up -d
docker-compose logs -f
docker-compose down

# 或单独构建镜像
docker build -t deepseek-gateway:latest .
docker run -d --name deepseek-gateway -p 8000:8000 --env-file .env.development deepseek-gateway:latest
```

---

## 验证脚本覆盖范围

`tests/test_sync_generation.py` 共 32 项用例，覆盖：

| 分组 | 覆盖点 |
|---|---|
| 配置层 | `${VAR}` / `${VAR:-default}` 插值、tuning 参数、`api_keys` 解析（空值过滤）、pricing 计价（含缓存命中） |
| 路由层 | 注册表字段、`priority`、`weighted_round_robin`（3:1 轮询）、未知/禁用模型、适配器选择、`api` 能力声明与非法值拒绝 |
| 韧性 | 熔断状态机、熔断开快速失败（503）、限流快速失败（429）、滑动窗口、退避封顶、状态码映射 |
| 结构化输出 | `schema_from_request` / `content_from_response` / `validate_response` / `repair_instruction` / 围栏剥离 |
| 适配器 | 三协议 payload、鉴权头、响应归一化、流式事件解析 |
| 鉴权 | `get_api_keys()` 解析、可选 Bearer 依赖（未配置放行 / 配置后 Bearer、X-API-Key、缺失或错误 401） |
| 可观测 | 用量合并、三协议用量字段、UsageEvent 落库与查询 |
| 提示词 | 版本自增、指定版本引用、变量渲染、缺失变量保留、未找到报错、并发创建版本号唯一连续 |
| 错误体 | OpenAI 风格错误对象（`code` / `type` / `message` / `param` / `detail`） |
| HTTP E2E | 同步生成 4 模型、结构化（chat/responses/json_object）、故障转移、错误码、模板引用、流式 SSE、用量查询、健康检查、`/healthz`、`/readyz`、`/admin/usage`、`/v1/models`、`/v1/chat/completions`（含 SSE）、`/v1/responses`（能力过滤 + 参数校验） |

---

## License

MIT
