"""OpenAI 兼容入口（openai_compat）

对上提供与 OpenAI 一致的入口，便于使用官方 SDK / 既有客户端零改动接入网关：
- GET  /v1/models            返回网关公开的模型别名（OpenAI 列表结构）
- POST /v1/chat/completions  Chat Completions 兼容入口（支持 stream / response_format）
- POST /v1/responses         Responses API 兼容入口（仅路由到声明 responses 能力的路由，按协议透明代理）

复用 services/gateway 的编排（限流 / 重试 / 熔断 / 故障转移 / 结构化输出 / 观测），
本层仅做入口形状适配与协议能力校验：
- 不把仅支持 Chat Completions 的供应商伪装成 Responses API（由路由 api 能力声明把关）
- 调用方只持有网关 Key，不需要持有任何上游供应商密钥
"""

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.routes import _apply_prompt, _call_ctx, _client_ip, _validate_request
from app.core.errors import ErrorCode, LLMGatewayError
from app.services.gateway import chat_completion, chat_completion_stream
from app.services.router import SUPPORTED_MODELS

router = APIRouter(tags=["OpenAI Compatible"])


# ==================== 模型列表 ====================

@router.get("/models")
async def list_models():
    """OpenAI 风格模型列表（`/v1/models`），仅返回 enabled 的模型别名"""
    data = [
        {
            "id": entry.id,
            "object": "model",
            "created": 0,
            "owned_by": entry.provider or "gateway",
            "display_name": entry.display_name,
            "type": entry.api_type,
            "strategy": entry.strategy,
        }
        for entry in SUPPORTED_MODELS.values()
        if entry.enabled
    ]
    return {"object": "list", "data": data}


# ==================== Chat Completions ====================

def _incoming_messages(body: dict) -> list[dict]:
    """提取消息列表；兼容网关侧 gateway_prompt 别名"""
    if "gateway_prompt" in body and "prompt" not in body:
        body["prompt"] = body["gateway_prompt"]
    return body.get("messages") or []


@router.post("/chat/completions")
async def chat_completions(request: Request):
    """OpenAI Chat Completions 兼容入口（非流式返回归一化结构，流式返回 SSE）"""
    body = await request.json()
    model = body.get("model", "deepseek-chat")
    messages = _incoming_messages(body)

    _validate_request(model, messages)
    messages, resolved_version = await _apply_prompt(body, messages)

    kwargs = {
        "response_format": body.get("response_format"),
        "temperature": body.get("temperature"),
        "max_tokens": body.get("max_tokens"),
        "user_id": body.get("user"),
        "client_ip": _client_ip(request),
        "prompt_version": resolved_version,
        **_call_ctx(request, body),
    }

    if body.get("stream"):
        return StreamingResponse(
            chat_completion_stream(model, messages, **kwargs),
            media_type="text/event-stream",
        )
    return await chat_completion(model, messages, **kwargs)


# ==================== Responses API ====================

def _messages_from_input(body: dict) -> list[dict]:
    """把 Responses API 的 input / instructions 适配为统一消息列表"""
    messages: list[dict] = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                content = item.get("content", "")
                if isinstance(content, list):  # Responses 内容部件 -> 拼接文本
                    content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
                messages.append({"role": item.get("role", "user"), "content": content})
    if not messages:
        raise LLMGatewayError(ErrorCode.INVALID_REQUEST, "input 不能为空", param="input")
    return messages


def _format_from_text(text_cfg) -> dict | None:
    """Responses 的 text.format -> 网关规范形 response_format"""
    if not isinstance(text_cfg, dict):
        return None
    fmt = text_cfg.get("format")
    if not isinstance(fmt, dict):
        return None
    fmt_type = fmt.get("type")
    if fmt_type == "json_object":
        return {"type": "json_object"}
    if fmt_type == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {"name": fmt.get("name", "response"), "schema": fmt.get("schema") or {}},
        }
    return None


@router.post("/responses")
async def responses(request: Request):
    """Responses API 兼容入口（非流式；按协议透明代理，仅限声明 responses 能力的路由）"""
    body = await request.json()
    model = body.get("model", "deepseek-chat")
    if body.get("stream"):
        raise LLMGatewayError(
            ErrorCode.INVALID_REQUEST,
            "/v1/responses 暂不支持 stream=true，流式请使用 /v1/chat/completions",
            param="stream",
        )

    messages = _messages_from_input(body)
    _validate_request(model, messages)

    return await chat_completion(
        model,
        messages,
        response_format=_format_from_text(body.get("text")),
        temperature=body.get("temperature"),
        max_tokens=body.get("max_output_tokens"),
        user_id=body.get("user"),
        client_ip=_client_ip(request),
        require_api="responses",
        raw_response=True,
        **_call_ctx(request, body),
    )
