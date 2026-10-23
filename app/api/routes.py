"""API 路由（routes）

端点：
- GET  /models                 支持的模型列表
- POST /chat                   非流式对话（支持 response_format / prompt 模板引用）
- POST /chat/stream            流式对话（SSE）
- POST /prompts                创建提示词模板版本
- GET  /prompts                列出所有模板版本
- GET  /prompts/{name}         获取模板（默认最新版本）
- GET  /prompts/{name}/{version} 获取指定版本
- POST /prompts/{name}/render  渲染模板（预览变量替换结果）

协议差异与限流/重试/观测由 services/upstream 与 services/gateway 负责，
本层只做请求校验、模板渲染与参数透传。
"""

from typing import Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.core.errors import ErrorCode, LLMGatewayError
from app.core.security import check_sensitive_words
from app.services import prompts
from app.services.gateway import chat_completion, chat_completion_stream
from app.services.router import SUPPORTED_MODELS
from app.services.usage import usage_repository

router = APIRouter(tags=["Agents"])
# 运维别名（无前缀）：/admin/usage，与 /api/v1/usage/recent 等价
admin_router = APIRouter(tags=["Admin"])


# ==================== 共用校验逻辑 ====================

def _validate_request(model: str, messages: List[Dict]) -> None:
    """统一请求校验：模型白名单 + 消息非空 + 敏感词

    注：IP 限流与按模型限流已下沉到 gateway，此处不再重复计数。
    """
    entry = SUPPORTED_MODELS.get(model)
    if not entry or not entry.enabled:
        raise LLMGatewayError(
            ErrorCode.MODEL_NOT_FOUND,
            f"不支持的模型: {model}，支持的模型: {list(SUPPORTED_MODELS.keys())}",
        )
    if not messages:
        raise LLMGatewayError(ErrorCode.INVALID_REQUEST, "messages 不能为空")

    for msg in messages:
        content = msg.get("content", "")
        if not content:
            continue
        has_sensitive, words = check_sensitive_words(content)
        if has_sensitive:
            raise LLMGatewayError(ErrorCode.INVALID_REQUEST, f"内容包含敏感词: {', '.join(words)}")


async def _apply_prompt(body: dict, messages: List[Dict]) -> tuple[List[Dict], int | None]:
    """若请求携带 prompt 模板引用，则渲染并作为 system 消息前插

    Returns:
        (消息列表, 实际解析到的模板版本号；未引用模板时为 None)
    """
    prompt = body.get("prompt")
    if not prompt:
        return messages, None
    name = prompt.get("name")
    if not name:
        raise LLMGatewayError(ErrorCode.INVALID_REQUEST, "prompt.name 不能为空")
    meta = await prompts.get_template(name, prompt.get("version"))
    rendered = await prompts.render(name, prompt.get("variables") or {}, meta["version"])
    return [{"role": "system", "content": rendered}] + list(messages), meta["version"]


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _call_ctx(request: Request, body: dict) -> dict:
    """提取调用上下文：request_id / endpoint / 提示词模板名（用于 UsageEvent 落库）"""
    prompt = body.get("prompt") or {}
    return {
        "request_id": getattr(request.state, "request_id", None),
        "endpoint": request.url.path,
        "prompt_name": prompt.get("name"),
    }


# ==================== 模型列表 ====================

@router.get("/models")
async def list_models():
    """获取支持的模型列表（仅返回 enabled 的模型）"""
    return {
        "models": [
            {
                "id": entry.id,
                "display_name": entry.display_name,
                "type": entry.api_type,
                "provider": entry.provider,
                "strategy": entry.strategy,
            }
            for entry in SUPPORTED_MODELS.values()
            if entry.enabled
        ]
    }


# ==================== 对话 ====================

@router.post("/chat")
async def chat(request: Request):
    """非流式对话接口"""
    body = await request.json()
    model = body.get("model", "deepseek-chat")
    messages = body.get("messages", [])

    _validate_request(model, messages)
    messages, resolved_version = await _apply_prompt(body, messages)

    return await chat_completion(
        model,
        messages,
        response_format=body.get("response_format"),
        temperature=body.get("temperature"),
        max_tokens=body.get("max_tokens"),
        user_id=body.get("user_id"),
        client_ip=_client_ip(request),
        prompt_version=resolved_version,
        **_call_ctx(request, body),
    )


@router.post("/chat/stream")
async def chat_stream(request: Request):
    """流式对话接口（SSE）"""
    body = await request.json()
    model = body.get("model", "deepseek-chat")
    messages = body.get("messages", [])

    _validate_request(model, messages)
    messages, resolved_version = await _apply_prompt(body, messages)

    return StreamingResponse(
        chat_completion_stream(
            model,
            messages,
            response_format=body.get("response_format"),
            temperature=body.get("temperature"),
            max_tokens=body.get("max_tokens"),
            user_id=body.get("user_id"),
            client_ip=_client_ip(request),
            prompt_version=resolved_version,
            **_call_ctx(request, body),
        ),
        media_type="text/event-stream",
    )


# ==================== 提示词模板管理 ====================

@router.post("/prompts")
async def create_prompt(request: Request):
    """创建提示词模板版本（同一 name 自增 version）"""
    body = await request.json()
    name = body.get("name")
    template = body.get("template")
    if not name or not template:
        raise LLMGatewayError(ErrorCode.INVALID_REQUEST, "name 与 template 不能为空")
    return await prompts.create_version(
        name=name,
        template=template,
        variables=body.get("variables"),
        description=body.get("description", ""),
    )


@router.get("/prompts")
async def list_prompts():
    """列出所有模板版本"""
    return {"prompts": await prompts.list_templates()}


@router.get("/prompts/{name}")
async def get_prompt(name: str):
    """获取模板（默认最新版本）"""
    return await prompts.get_template(name)


@router.get("/prompts/{name}/{version}")
async def get_prompt_version(name: str, version: int):
    """获取指定版本模板"""
    return await prompts.get_template(name, version)


@router.post("/prompts/{name}/render")
async def render_prompt(name: str, request: Request):
    """渲染模板：预览变量替换结果"""
    body = await request.json()
    rendered = await prompts.render(name, body.get("variables") or {}, body.get("version"))
    return {"name": name, "rendered": rendered}


# ==================== 调用记录查询 ====================

@router.get("/usage/recent")
async def recent_usage(limit: int = 100):
    """查询最近的调用记录（UsageEvent，含成功/失败/限流，按时间倒序）"""
    return {"events": await usage_repository.recent(limit)}


@admin_router.get("/admin/usage")
async def admin_usage(limit: int = 100):
    """运维别名：最近调用记录（等价 /api/v1/usage/recent）"""
    return {"events": await usage_repository.recent(limit)}
