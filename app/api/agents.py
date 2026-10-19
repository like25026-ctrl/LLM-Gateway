"""对话 API 路由

端点：
- GET  /models                 支持的模型列表
- POST /chat                   非流式对话（支持 response_format / prompt 模板引用）
- POST /chat/stream            流式对话（SSE）
- POST /prompts                创建提示词模板版本
- GET  /prompts                列出所有模板版本
- GET  /prompts/{name}         获取模板（默认最新版本）
- GET  /prompts/{name}/{version} 获取指定版本
- POST /prompts/{name}/render  渲染模板（预览变量替换结果）

协议差异与限流/重试/观测由 app.adapters 与 app.services.llm_service 负责，
本层只做请求校验、模板渲染与参数透传。
"""

from typing import Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.core.errors import ErrorCode, LLMGatewayError
from app.core.llm_config import SUPPORTED_MODELS
from app.core.safety import check_sensitive_words
from app.services import prompt_service
from app.services.llm_service import chat_completion, chat_completion_stream

router = APIRouter(tags=["Agents"])


# ==================== 共用校验逻辑 ====================

def _validate_request(model: str, messages: List[Dict]) -> None:
    """统一请求校验：模型白名单 + 消息非空 + 敏感词

    注：IP 限流与按模型限流已下沉到 llm_service，此处不再重复计数。
    """
    if model not in SUPPORTED_MODELS:
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


def _apply_prompt(body: dict, messages: List[Dict]) -> List[Dict]:
    """若请求携带 prompt 模板引用，则渲染并作为 system 消息前插"""
    prompt = body.get("prompt")
    if not prompt:
        return messages
    name = prompt.get("name")
    if not name:
        raise LLMGatewayError(ErrorCode.INVALID_REQUEST, "prompt.name 不能为空")
    rendered = prompt_service.render(name, prompt.get("variables") or {}, prompt.get("version"))
    return [{"role": "system", "content": rendered}] + list(messages)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# ==================== 模型列表 ====================

@router.get("/models")
async def list_models():
    """获取支持的模型列表"""
    return {
        "models": [
            {
                "id": model_id,
                "display_name": config["display_name"],
                "type": config["type"],
                "provider": config["provider"],
            }
            for model_id, config in SUPPORTED_MODELS.items()
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
    messages = _apply_prompt(body, messages)

    return await chat_completion(
        model,
        messages,
        response_format=body.get("response_format"),
        temperature=body.get("temperature"),
        max_tokens=body.get("max_tokens"),
        user_id=body.get("user_id"),
        client_ip=_client_ip(request),
    )


@router.post("/chat/stream")
async def chat_stream(request: Request):
    """流式对话接口（SSE）"""
    body = await request.json()
    model = body.get("model", "deepseek-chat")
    messages = body.get("messages", [])

    _validate_request(model, messages)
    messages = _apply_prompt(body, messages)

    return StreamingResponse(
        chat_completion_stream(
            model,
            messages,
            response_format=body.get("response_format"),
            temperature=body.get("temperature"),
            max_tokens=body.get("max_tokens"),
            user_id=body.get("user_id"),
            client_ip=_client_ip(request),
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
    return prompt_service.create_version(
        name=name,
        template=template,
        variables=body.get("variables"),
        description=body.get("description", ""),
    )


@router.get("/prompts")
async def list_prompts():
    """列出所有模板版本"""
    return {"prompts": prompt_service.list_templates()}


@router.get("/prompts/{name}")
async def get_prompt(name: str):
    """获取模板（默认最新版本）"""
    return prompt_service.get_template(name)


@router.get("/prompts/{name}/{version}")
async def get_prompt_version(name: str, version: int):
    """获取指定版本模板"""
    return prompt_service.get_template(name, version)


@router.post("/prompts/{name}/render")
async def render_prompt(name: str, request: Request):
    """渲染模板：预览变量替换结果"""
    body = await request.json()
    rendered = prompt_service.render(name, body.get("variables") or {}, body.get("version"))
    return {"name": name, "rendered": rendered}
