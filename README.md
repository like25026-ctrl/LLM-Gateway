# DeepSeek Proxy - 企业级 DeepSeek API 代理服务

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

基于 FastAPI 构建的企业级 DeepSeek API 代理服务，提供请求日志追踪、IP 限流、敏感词过滤、流式输出、健康检查等生产级能力。

---

##  核心特性

| 特性 | 说明 |
|---|---|
|  **API Key 管理** | 通过环境变量配置 DeepSeek API Key，支持多模型 |
|  **结构化日志** | 基于 structlog，每个请求自动关联唯一 Request ID |
| ️ **请求耗时追踪** | 自动记录每个请求的耗时，并写入响应头 `X-Process-Time-Ms` |
| ️ **IP 限流** | 滑动窗口算法，默认 60 次/分钟，可自定义 |
|  **敏感词过滤** | 可配置的敏感词列表，拦截违规内容 |
|  **流式输出** | 支持 SSE 流式对话，实时返回 AI 响应 |
|  **健康检查** | 三层探针（liveness / readiness / info），适配 Kubernetes |
|  **费用计算** | 自动统计 Token 用量并估算费用（人民币） |
|  **Docker 部署** | 提供 Dockerfile 和 docker-compose.yml，一键部署 |
| ️ **内置 Web 界面** | 提供简洁的对话测试页面，无需额外前端 |

---


## 技术栈

| 组件 | 版本 | 用途 |
|---|---|---|
| Python | 3.10+ | 运行时 |
| FastAPI | 0.100+ | Web 框架 |
| Uvicorn | 0.27+ | ASGI 服务器 |
| httpx | 0.27+ | 异步 HTTP 客户端 |
| structlog | 24.1+ | 结构化日志 |
| SQLAlchemy | 2.0+ | ORM（预留扩展） |
| Pydantic | 2.5+ | 数据校验 |
| python-dotenv | 1.0+ | 环境变量加载 |


---

##  项目目录结构
deepseek-proxy/
├── app/ # 应用核心代码
│ ├── init.py # 包标识
│ ├── main.py # FastAPI 应用入口
│ ├── database.py # 数据库连接配置
│ │
│ ├── core/ # 核心配置与工具
│ │ ├── init.py
│ │ ├── config.py # 全局配置（环境变量读取）
│ │ ├── llm_config.py # 模型配置与费用计算
│ │ ├── safety.py # 敏感词过滤
│ │ └── logging_config.py # 结构化日志配置
│ │
│ ├── api/ # API 路由
│ │ ├── init.py
│ │ ├── health.py # 健康检查路由
│ │ └── agents.py # 对话 API 路由
│ │
│ ├── services/ # 业务服务层
│ │ ├── init.py
│ │ ├── agent_service.py # DeepSeek API 调用封装
│ │ └── rate_limit_service.py # IP 限流服务
│ │
│ ├── middleware/ # 中间件
│ │ ├── init.py
│ │ ├── request_id.py # 请求 ID 生成与注入
│ │ └── logging.py # 请求日志记录
│ │
│ ├── models/ # 数据库模型（预留扩展）
│ │ └── init.py
│ │
│ └── schemas/ # Pydantic 数据模型
│ └── init.py
│
├── static/ # 静态资源
│ └── index.html # 内置 Web 对话界面
│
├── requirements.txt # Python 依赖
├── .env # 环境变量配置（不提交到 Git）
├── .env.example # 环境变量模板
├── Dockerfile # Docker 镜像构建
├── docker-compose.yml # Docker Compose 编排
├── nginx.conf # Nginx 反向代理配置
└── README.md # 项目说明文档


---

##  快速开始

### 1. 环境准备

```bash
# 克隆项目
git clone https://github.com/your-org/deepseek-proxy.git
cd deepseek-proxy

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# 安装依赖
pip install -r requirements.txt

### 2.配置环境变量
# 复制环境变量模板
cp .env.example .env

# 编辑 .env 文件，填入你的 DeepSeek API Key
vi .env

### 3.启动服务
# 开发模式（热重载）
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 生产模式
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4

#### 访问以下地址：
 - API 文档：http://localhost:8000/docs
 - Web 对话界面：http://localhost:8000/
 - 健康检查：http://localhost:8000/health/info

### Docker 部署
** 使用 Docker Compose（推荐） **
# 编辑 .env 文件后，一键启动
docker-compose up -d

# 查看日志
docker-compose logs -f

# 停止服务
docker-compose down

**单独构建Docker镜像 **
docker build -t deepseek-proxy:latest .
docker run -d \
  --name deepseek-proxy \
  -p 8000:8000 \
  --env-file .env \
  deepseek-proxy:latest

### 健康检查
| 端点 | 用途 |
|---|---|
| `GET /health/live` | Kubernetes livenessProbe |
| `GET /health/ready` | Kubernetes readinessProbe |
| `GET /health/info` | 服务元信息（版本、环境、运行时长） |




### 模块依赖关系图
main.py (应用入口)
│
├── 中间件层（按注册顺序执行）
│   ├── middleware/request_id.py      ← 生成 UUID，注入 request.state 和响应头
│   └── middleware/logging.py         ← 读取 request_id，记录请求开始/完成/异常日志
│
├── 路由层
│   ├── api/health.py                 ← /health/live, /health/ready, /health/info
│   └── api/agents.py                 ← /api/v1/models, /api/v1/chat, /api/v1/chat/stream
│       │
│       ├── 统一校验逻辑 _validate_request()
│       │   ├── services/rate_limit_service.py   ← IP 限流检查
│       │   ├── core/llm_config.py               ← 模型白名单验证
│       │   └── core/safety.py                   ← 敏感词过滤
│       │
│       └── 业务调用
│           └── services/agent_service.py        ← DeepSeek API 调用（流式/非流式）
│               ├── core/llm_config.py           ← 获取模型配置、计算费用
│               └── core/config.py               ← 读取 DEEPSEEK_API_KEY
│
├── 核心配置层
│   ├── core/config.py                ← 全局配置（环境变量读取）
│   ├── core/llm_config.py            ← 模型配置、Token 费用计算
│   ├── core/safety.py                ← 敏感词加载与检查
│   └── core/logging_config.py        ← structlog 结构化日志配置
│
├── 数据层（预留扩展）
│   ├── database.py                   ← SQLAlchemy 异步引擎
│   ├── models/__init__.py            ← ORM 模型定义（对话日志、API Key）
│   └── schemas/__init__.py           ← Pydantic 请求/响应模型
│
└── 静态资源
    └── static/index.html             ← 内置 Web 对话界面


### 请求流程图

客户端请求
    │
    ▼
┌─────────────────────────┐
│  RequestIDMiddleware    │  生成/透传 X-Request-ID
└───────────┬─────────────┘
            ▼
┌─────────────────────────┐
│  LoggingMiddleware      │  记录请求开始日志
└───────────┬─────────────┘
            ▼
┌─────────────────────────┐
│  路由匹配               │  /api/v1/chat 或 /api/v1/chat/stream
└───────────┬─────────────┘
            ▼
┌─────────────────────────┐
│  _validate_request()    │  1. IP 限流检查
│                         │  2. 模型白名单验证
│                         │  3. 敏感词过滤
└───────────┬─────────────┘
            ▼
┌─────────────────────────┐
│  agent_service          │  调用 DeepSeek API
│                         │  - 非流式：直接返回 JSON
│                         │  - 流式：SSE 逐块返回
└───────────┬─────────────┘
            ▼
┌─────────────────────────┐
│  LoggingMiddleware      │  记录请求完成日志（含耗时）
└───────────┬─────────────┘
            ▼
      客户端响应
