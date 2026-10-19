"""LLM 编排服务：协议无关的调用编排

职责（全部与上游协议无关，协议差异交给 app.adapters）：
- 限流：IP 限流 + 按模型独立限流（超限抛 RATE_LIMITED / HTTP 429）
- 重试：指数退避，最多 settings.RETRY_MAX_ATTEMPTS 次，仅对可重试错误生效
- 可观测：Token 分类统计、总延迟、流式首 Token 延迟（TTFT）
- 结构化输出校验
- 输出归一化为 OpenAI Chat Completions 结构（非流式）/ SSE（流式）
"""

import asyncio
import json
import random
import time

import httpx
import structlog

from app.adapters.base import StreamDelta, UnifiedRequest, Usage
from app.adapters.registry import get_adapter
from app.core.config import settings
from app.core.errors import ErrorCode, LLMGatewayError
from app.core.llm_config import calculate_cost
from app.services.rate_limit_service import check_model_rate_limit, check_rate_limit

logger = structlog.get_logger()

# 按 base_url 复用 httpx 异步客户端（连接复用）
_clients: dict[str, httpx.AsyncClient] = {}


def _get_client(base_url: str, headers: dict) -> httpx.AsyncClient:
    key = base_url.rstrip("/")
    client = _clients.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(base_url=key, timeout=settings.REQUEST_TIMEOUT, headers=headers)
        _clients[key] = client
    return client


def _enforce_rate_limits(model: str, client_ip: str | None) -> None:
    """IP 限流 + 按模型限流，超限抛 RATE_LIMITED"""
    if client_ip and not check_rate_limit(client_ip, limit=settings.RATE_LIMIT):
        raise LLMGatewayError(ErrorCode.RATE_LIMITED, "请求过于频繁，请稍后再试")
    if not check_model_rate_limit(model, limit=settings.MODEL_RATE_LIMIT):
        raise LLMGatewayError(ErrorCode.RATE_LIMITED, f"模型 {model} 请求过于频繁，请稍后再试")


def _as_gateway_error(exc: Exception) -> LLMGatewayError:
    if isinstance(exc, LLMGatewayError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return LLMGatewayError(ErrorCode.UPSTREAM_TIMEOUT, f"上游请求超时: {exc}", retryable=True)
    if isinstance(exc, httpx.RequestError):
        return LLMGatewayError(ErrorCode.NETWORK_ERROR, f"网络请求失败: {exc}", retryable=True)
    return LLMGatewayError(ErrorCode.UPSTREAM_ERROR, f"上游响应异常: {exc}")


def _upstream_status_error(status: int, body: str) -> LLMGatewayError:
    """按上游状态码映射统一错误码与可重试性"""
    if status == 429:
        return LLMGatewayError(ErrorCode.RATE_LIMITED, "上游限流", http_status=429, detail=body[:500], retryable=True)
    if status >= 500:
        return LLMGatewayError(ErrorCode.UPSTREAM_ERROR, f"上游错误({status})", http_status=502, detail=body[:500], retryable=True)
    return LLMGatewayError(ErrorCode.INVALID_REQUEST, f"上游拒绝请求({status})", http_status=status, detail=body[:500], retryable=False)


def _merge_usage(target: Usage, part: Usage) -> None:
    """合并分片用量（流式中用量分散在多个事件里）"""
    if part.prompt_tokens:
        target.prompt_tokens = part.prompt_tokens
    if part.completion_tokens:
        target.completion_tokens = part.completion_tokens
    if part.reasoning_tokens:
        target.reasoning_tokens = part.reasoning_tokens
    if part.cached_tokens:
        target.cached_tokens = part.cached_tokens
    if part.total_tokens:
        target.total_tokens = part.total_tokens


def _validate_structured(model: str, text: str, response_format: dict) -> str:
    """结构化输出校验：返回合法 JSON 文本，否则抛 STRUCTURED_OUTPUT_FAILED"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise LLMGatewayError(
            ErrorCode.STRUCTURED_OUTPUT_FAILED,
            f"模型 {model} 未返回合法 JSON",
            detail=text[:500],
        )

    if response_format.get("type") == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema") or {}
        required = schema.get("required") or []
        obj = data if isinstance(data, dict) else {}
        missing = [k for k in required if k not in obj]
        if missing:
            raise LLMGatewayError(
                ErrorCode.STRUCTURED_OUTPUT_FAILED,
                f"结构化输出缺少必填字段: {missing}",
                detail=text[:500],
            )
    return text


def _to_chat_completion(model: str, unified, text: str) -> dict:
    """归一化为 OpenAI Chat Completions 结构"""
    usage = unified.usage
    return {
        "id": unified.raw.get("id"),
        "object": "chat.completion",
        "model": unified.raw.get("model", model),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": unified.finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens or (usage.prompt_tokens + usage.completion_tokens),
        },
    }


def _sse_chunk(model: str, content: str, reasoning: bool = False) -> str:
    delta = {"reasoning_content": content} if reasoning else {"content": content}
    chunk = {
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _log_completion(
    model: str,
    api_type: str,
    stream: bool,
    usage: Usage,
    latency_ms: float,
    first_token_ms: float | None = None,
    error: str | None = None,
) -> None:
    """可观测性：Token 分类统计 + 延迟 + 首 Token 延迟"""
    cost = calculate_cost(model, usage.prompt_tokens, usage.completion_tokens)
    logger.info(
        "llm_call_completed",
        model=model,
        api_type=api_type,
        stream=stream,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        cached_tokens=usage.cached_tokens,
        total_tokens=usage.total_tokens or (usage.prompt_tokens + usage.completion_tokens),
        cost_cny=cost,
        latency_ms=round(latency_ms, 2),
        first_token_latency_ms=round(first_token_ms, 2) if first_token_ms is not None else None,
        error=error,
    )


async def chat_completion(
    model: str,
    messages: list,
    *,
    response_format: dict | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    user_id: str | None = None,
    client_ip: str | None = None,
) -> dict:
    """非流式统一调用，返回 OpenAI Chat Completions 结构"""
    adapter = get_adapter(model)
    _enforce_rate_limits(model, client_ip)

    req = UnifiedRequest(
        model=model,
        messages=messages,
        stream=False,
        response_format=response_format,
        temperature=temperature,
        max_tokens=max_tokens,
        user_id=user_id,
    )
    payload = adapter.build_payload(req)
    client = _get_client(adapter.base_url, adapter.headers())

    start = time.perf_counter()
    raw = None
    for attempt in range(settings.RETRY_MAX_ATTEMPTS):
        try:
            resp = await client.post(adapter.endpoint_path, json=payload)
            if resp.status_code >= 400:
                raise _upstream_status_error(resp.status_code, resp.text)
            raw = resp.json()
            break
        except (LLMGatewayError, httpx.HTTPError, ValueError) as exc:
            err = _as_gateway_error(exc)
            if not err.retryable or attempt == settings.RETRY_MAX_ATTEMPTS - 1:
                logger.warning("llm_call_failed", model=model, code=err.code.value, attempts=attempt + 1, error=err.message)
                raise err
            await asyncio.sleep(settings.RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.1))
    else:
        raise LLMGatewayError(ErrorCode.UPSTREAM_ERROR, "上游调用失败")

    latency_ms = (time.perf_counter() - start) * 1000
    unified = adapter.parse_response(raw)
    text = unified.text
    if response_format is not None:
        text = _validate_structured(model, text, response_format)

    _log_completion(model, adapter.api_type, False, unified.usage, latency_ms)
    return _to_chat_completion(model, unified, text)


async def chat_completion_stream(
    model: str,
    messages: list,
    *,
    response_format: dict | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    user_id: str | None = None,
    client_ip: str | None = None,
):
    """流式统一调用，产出 OpenAI Chat Completions 结构的 SSE 数据块

    仅在该次尝试「尚未产出任何增量」时才重试；已开始输出后不再重试。
    """
    adapter = get_adapter(model)
    _enforce_rate_limits(model, client_ip)

    req = UnifiedRequest(
        model=model,
        messages=messages,
        stream=True,
        response_format=response_format,
        temperature=temperature,
        max_tokens=max_tokens,
        user_id=user_id,
    )
    payload = adapter.build_payload(req)
    client = _get_client(adapter.base_url, adapter.headers())

    start = time.perf_counter()
    first_token_ms: float | None = None
    usage = Usage()
    emitted = False
    error_to_emit: LLMGatewayError | None = None

    for attempt in range(settings.RETRY_MAX_ATTEMPTS):
        try:
            async with client.stream("POST", adapter.endpoint_path, json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "ignore")
                    raise _upstream_status_error(resp.status_code, body)

                async for line in resp.aiter_lines():
                    line = line.strip()
                    # 忽略空行与 keep-alive（: keep-alive）
                    if not line or not line.startswith("data:"):
                        continue
                    delta: StreamDelta | None = adapter.parse_stream_event(line[5:].strip())
                    if delta is None:
                        continue
                    if delta.usage:
                        _merge_usage(usage, delta.usage)
                    if delta.reasoning:
                        if first_token_ms is None:
                            first_token_ms = (time.perf_counter() - start) * 1000
                        emitted = True
                        yield _sse_chunk(model, delta.reasoning, reasoning=True)
                    if delta.text:
                        if first_token_ms is None:
                            first_token_ms = (time.perf_counter() - start) * 1000
                        emitted = True
                        yield _sse_chunk(model, delta.text)
                    if delta.done:
                        break
            break
        except (LLMGatewayError, httpx.HTTPError, ValueError) as exc:
            err = _as_gateway_error(exc)
            error_to_emit = err
            if emitted or not err.retryable or attempt == settings.RETRY_MAX_ATTEMPTS - 1:
                break
            await asyncio.sleep(settings.RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.1))
            error_to_emit = None

    latency_ms = (time.perf_counter() - start) * 1000
    if error_to_emit is not None:
        logger.warning("llm_stream_failed", model=model, code=error_to_emit.code.value, error=error_to_emit.message)
        err_payload = {"error": {"code": error_to_emit.code.value, "message": error_to_emit.message}}
        yield f"data: {json.dumps(err_payload, ensure_ascii=False)}\n\n"

    _log_completion(
        model,
        adapter.api_type,
        True,
        usage,
        latency_ms,
        first_token_ms,
        error=error_to_emit.code.value if error_to_emit else None,
    )
    yield "data: [DONE]\n\n"
