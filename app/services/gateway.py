"""LLM 网关编排（gateway）：协议无关的调用编排

职责（协议差异交给 services/upstream，路由策略交给 services/router）：
- 多路由故障转移：按模型策略（priority / weighted_round_robin）得到候选路由，
  可重试类失败（网络/超时/5xx/上游429）自动切换下一路由，fallbacks 计数
- 熔断：按路由（provider:model）连续失败达阈值后冷却期内快速失败（CIRCUIT_OPEN / 503）
- 重试：指数退避（带最大延迟封顶），仅对 retry_statuses 中的状态码与网络错误生效
- 限流：IP 限流 + 按模型独立限流（可在 gateway.yaml 关闭）
- 结构化输出：入口归一化 -> 校验 -> 失败按 structured_output_retries 自修复
- 可观测：每次调用（成功/失败/限流/熔断）构造 UsageEvent 持久化 + 结构化日志
- 出口归一化为 OpenAI Chat Completions 结构（非流式）/ SSE（流式）
"""

import asyncio
import hashlib
import json
import random
import time

import httpx
import structlog

from app.config import settings
from app.core.circuit_breaker import circuit_breaker
from app.core.errors import ErrorCode, LLMGatewayError, StructuredOutputError
from app.core.gateway_config import tuning
from app.core.rate_limit import check_model_rate_limit, check_rate_limit
from app.services.router import Route, get_adapter_for_route, get_routes
from app.services.structured_output import (
    repair_instruction,
    schema_from_request,
    validate_response,
)
from app.services.upstream import StreamDelta, UnifiedRequest, Usage
from app.services.usage import UsageEvent, calculate_cost, merge_usage, usage_repository

logger = structlog.get_logger()

# 按 (base_url, timeout, api_key) 复用 httpx 异步客户端（连接复用）
_clients: dict[str, httpx.AsyncClient] = {}


def _get_client(base_url: str, headers: dict, timeout: float) -> httpx.AsyncClient:
    key = f"{base_url.rstrip('/')}|{timeout}|{hashlib.sha256((headers.get('Authorization') or '').encode()).hexdigest()[:12]}"
    client = _clients.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(base_url=key.split("|")[0], timeout=timeout, headers=headers)
        _clients[key] = client
    return client


def _enforce_rate_limits(model: str, client_ip: str | None) -> None:
    """IP 限流 + 按模型限流（gateway.yaml rate_limit.enabled 可关闭）"""
    if not tuning.rate_limit.enabled:
        return
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
    """按上游状态码映射统一错误码；retry_statuses / 5xx 视为可重试"""
    retryable = status in tuning.retry.retry_statuses or status >= 500
    if status == 429:
        return LLMGatewayError(ErrorCode.RATE_LIMITED, "上游限流", http_status=429, detail=body[:500], retryable=retryable)
    if status >= 500:
        return LLMGatewayError(ErrorCode.UPSTREAM_ERROR, f"上游错误({status})", http_status=502, detail=body[:500], retryable=retryable)
    return LLMGatewayError(ErrorCode.INVALID_REQUEST, f"上游拒绝请求({status})", http_status=status, detail=body[:500], retryable=retryable)


async def _post_raw(adapter, payload: dict) -> dict:
    """单次上游调用（不含重试），返回原始响应 JSON"""
    client = _get_client(adapter.base_url, adapter.headers(), adapter.timeout_seconds)
    resp = await client.post(adapter.endpoint_path, json=payload)
    if resp.status_code >= 400:
        raise _upstream_status_error(resp.status_code, resp.text)
    return resp.json()


def _backoff(attempt: int) -> float:
    """指数退避 + 抖动，带最大延迟封顶"""
    delay = tuning.retry.base_delay_seconds * (2 ** attempt) + random.uniform(0, 0.1)
    return min(delay, tuning.retry.max_delay_seconds)


async def _post_with_retries(adapter, payload: dict) -> tuple[dict, int]:
    """带指数退避的上游调用；返回 (原始响应, 重试次数)"""
    retries_used = 0
    for attempt in range(tuning.retry.max_attempts):
        try:
            return await _post_raw(adapter, payload), retries_used
        except (LLMGatewayError, httpx.HTTPError, ValueError) as exc:
            err = _as_gateway_error(exc)
            if not err.retryable or attempt == tuning.retry.max_attempts - 1:
                raise err
            retries_used += 1
            await asyncio.sleep(_backoff(attempt))
    raise LLMGatewayError(ErrorCode.UPSTREAM_ERROR, "上游调用失败")  # pragma: no cover


def _api_key_hash(api_key: str | None = None) -> str:
    """API Key 哈希（sha256 前 16 位，避免明文落库）"""
    key = api_key or settings.DEEPSEEK_API_KEY
    if not key:
        return ""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _new_event(
    model: str,
    *,
    request_id: str | None = None,
    endpoint: str | None = None,
    prompt_name: str | None = None,
    prompt_version: int | None = None,
    provider: str | None = None,
    upstream_model: str | None = None,
    api_key: str | None = None,
    stream: bool = False,
    status: str = "success",
    status_code: int = 200,
    usage: Usage | None = None,
    latency_ms: float = 0.0,
    first_token_ms: float | None = None,
    retries: int = 0,
    fallbacks: int = 0,
    error_type: str | None = None,
    error_message: str | None = None,
    user_id: str | None = None,
    response_format: dict | None = None,
) -> UsageEvent:
    """构造调用事件（费用按上游模型定价计算，含缓存命中优惠）"""
    usage = usage or Usage()
    meta: dict = {"cost_currency": "CNY"}
    if user_id:
        meta["user_id"] = user_id
    if response_format:
        meta["response_format_type"] = response_format.get("type")
    if fallbacks:
        meta["fallbacks"] = fallbacks

    return UsageEvent(
        request_id=request_id or "",
        api_key_hash=_api_key_hash(api_key),
        endpoint=endpoint or "",
        requested_model=model,
        provider=provider,
        upstream_model=upstream_model,
        stream=stream,
        status=status,
        status_code=status_code,
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        cached_tokens=usage.cached_tokens,
        cost_usd=calculate_cost(upstream_model or model, usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens),
        latency_ms=round(latency_ms, 2),
        first_token_ms=round(first_token_ms, 2) if first_token_ms is not None else None,
        retries=retries,
        fallbacks=fallbacks,
        error_type=error_type,
        error_message=error_message,
        prompt_id=prompt_name,
        prompt_version=prompt_version,
        metadata=meta,
    )


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


def _pick_candidates(model: str, require_api: str | None = None) -> list[Route]:
    """候选路由：按策略排序 -> 可选按入口能力过滤 -> 过滤熔断中的路由

    require_api 用于入口协议能力校验（如 /v1/responses 要求路由声明 responses 能力），
    为 None 时不做能力过滤（兼容既有 /api/v1/chat 入口：三类协议出口均归一为 chat）。
    """
    routes = get_routes(model)
    if require_api:
        routes = [r for r in routes if r.supports_api(require_api)]
        if not routes:
            raise LLMGatewayError(
                ErrorCode.MODEL_NOT_FOUND,
                f"模型 {model} 未声明 {require_api} 入口能力，无法用于该接口",
            )
    usable = [r for r in routes if circuit_breaker.allow(r.key)]
    if not usable:
        raise LLMGatewayError(ErrorCode.CIRCUIT_OPEN, f"模型 {model} 的所有路由处于熔断冷却期，请稍后重试")
    return usable


async def chat_completion(
    model: str,
    messages: list,
    *,
    response_format: dict | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    user_id: str | None = None,
    client_ip: str | None = None,
    request_id: str | None = None,
    endpoint: str | None = None,
    prompt_name: str | None = None,
    prompt_version: int | None = None,
    require_api: str | None = None,
    raw_response: bool = False,
) -> dict:
    """非流式统一调用，返回 OpenAI Chat Completions 结构

    候选路由按序尝试：可重试类失败自动故障转移（fallbacks++），
    结构化输出校验失败按 structured_output_retries 自修复。

    Args:
        require_api: 入口能力过滤（如 "responses"）；None 表示不过滤（兼容既有 chat 入口）
        raw_response: True 时不做归一化，直接返回上游原始响应体（用于 Responses API 透明代理）
    """
    start = time.perf_counter()
    retries = 0
    fallbacks = 0
    usage = Usage()
    chosen: tuple[Route, object] | None = None
    try:
        usable = _pick_candidates(model, require_api)
        _enforce_rate_limits(model, client_ip)
        response_format = schema_from_request("chat", {"response_format": response_format})

        last_err: LLMGatewayError | None = None
        for i, route in enumerate(usable):
            adapter = get_adapter_for_route(model, route)
            req = UnifiedRequest(
                model=model,
                messages=messages,
                stream=False,
                response_format=response_format,
                temperature=temperature,
                max_tokens=max_tokens,
                user_id=user_id,
            )
            try:
                raw, used = await _post_with_retries(adapter, adapter.build_payload(req))
                retries += used
                unified = adapter.parse_response(raw)
                usage = unified.usage
                text = unified.text

                # 结构化输出：校验 + 自修复（structured_output_retries 次）
                if response_format is not None:
                    retries_box = [retries]
                    text, raw, unified, usage = await _validate_with_repair(
                        adapter, messages, req, raw, unified, response_format,
                        model=model, retries_box=retries_box,
                    )
                    retries = retries_box[0]
                circuit_breaker.record_success(route.key)
                chosen = (route, adapter)
                latency_ms = (time.perf_counter() - start) * 1000
                await usage_repository.record(
                    _new_event(
                        model,
                        request_id=request_id,
                        endpoint=endpoint,
                        prompt_name=prompt_name,
                        prompt_version=prompt_version,
                        provider=route.provider,
                        upstream_model=route.model,
                        api_key=route.api_key or None,
                        status="success",
                        status_code=200,
                        usage=usage,
                        latency_ms=latency_ms,
                        retries=retries,
                        fallbacks=fallbacks,
                        user_id=user_id,
                        response_format=response_format,
                    )
                )
                return unified.raw if raw_response else _to_chat_completion(model, unified, text)
            except LLMGatewayError as err:
                last_err = err
                if err.retryable:
                    circuit_breaker.record_failure(route.key)
                if not err.retryable or i == len(usable) - 1:
                    raise
                fallbacks += 1
                logger.info(
                    "llm_failover",
                    model=model,
                    failed_route=route.key,
                    next_route=usable[i + 1].key,
                    error=err.code.value,
                )
        raise last_err or LLMGatewayError(ErrorCode.UPSTREAM_ERROR, "上游调用失败")
    except LLMGatewayError as err:
        route, adapter = chosen if chosen else (None, None)
        await usage_repository.record(
            _new_event(
                model,
                request_id=request_id,
                endpoint=endpoint,
                prompt_name=prompt_name,
                prompt_version=prompt_version,
                provider=route.provider if route else None,
                upstream_model=route.model if route else None,
                api_key=(route.api_key or None) if route else None,
                status="error",
                status_code=err.http_status,
                usage=usage,
                latency_ms=(time.perf_counter() - start) * 1000,
                retries=retries,
                fallbacks=fallbacks,
                error_type=err.code.value,
                error_message=err.message,
                user_id=user_id,
                response_format=response_format,
            )
        )
        raise


async def _validate_with_repair(adapter, messages, req, raw, unified, response_format, *, model, retries_box):
    """结构化输出校验 + 按 structured_output_retries 自修复

    Returns:
        (text, raw, unified, usage)（修复成功时更新为修复调用的结果）
    """
    text = unified.text
    current_raw = raw
    max_repairs = max(0, tuning.structured_output_retries)
    last_exc: StructuredOutputError | None = None

    for round_ in range(max_repairs + 1):
        try:
            text = validate_response(adapter.api_type, current_raw, response_format)
            return text, current_raw, unified, unified.usage
        except StructuredOutputError as exc:
            last_exc = exc
            if round_ == max_repairs:
                break
            repair_messages = list(messages) + [
                {"role": "system", "content": repair_instruction(exc, response_format)}
            ]
            repair_req = UnifiedRequest(
                model=req.model,
                messages=repair_messages,
                stream=False,
                response_format=response_format,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                user_id=req.user_id,
            )
            try:
                current_raw, used = await _post_with_retries(adapter, adapter.build_payload(repair_req))
                retries_box[0] += used
                unified = adapter.parse_response(current_raw)
                logger.info("llm_structured_repaired", model=model, attempt=round_ + 1)
            except (LLMGatewayError, httpx.HTTPError, ValueError):
                break  # 修复调用失败 -> 报原始校验错误
    raise last_exc  # type: ignore[misc]


async def chat_completion_stream(
    model: str,
    messages: list,
    *,
    response_format: dict | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    user_id: str | None = None,
    client_ip: str | None = None,
    request_id: str | None = None,
    endpoint: str | None = None,
    prompt_name: str | None = None,
    prompt_version: int | None = None,
):
    """流式统一调用，产出 OpenAI Chat Completions 结构的 SSE 数据块

    首个增量输出前可重试 / 可故障转移；一旦开始输出则不再切换。
    结束时（成功或失败）持久化 UsageEvent（含 TTFT）。
    """
    start = time.perf_counter()
    retries = 0
    fallbacks = 0
    usage = Usage()
    first_token_ms: float | None = None
    error_to_emit: LLMGatewayError | None = None
    try:
        usable = _pick_candidates(model)
        _enforce_rate_limits(model, client_ip)
        response_format = schema_from_request("chat", {"response_format": response_format})

        emitted = False
        chosen: tuple[Route, object] | None = None
        for i, route in enumerate(usable):
            adapter = get_adapter_for_route(model, route)
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
            client = _get_client(adapter.base_url, adapter.headers(), adapter.timeout_seconds)
            error_to_emit = None

            for attempt in range(tuning.retry.max_attempts):
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
                                merge_usage(usage, delta.usage)
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
                    chosen = (route, adapter)
                    break
                except (LLMGatewayError, httpx.HTTPError, ValueError) as exc:
                    err = _as_gateway_error(exc)
                    error_to_emit = err
                    if emitted or not err.retryable or attempt == tuning.retry.max_attempts - 1:
                        break
                    retries += 1
                    await asyncio.sleep(_backoff(attempt))
                    error_to_emit = None

            if chosen is not None:
                circuit_breaker.record_success(route.key)
                break
            # 候选路由失败：可重试类错误尝试下一路由（未开始输出时）
            if error_to_emit is not None and error_to_emit.retryable:
                circuit_breaker.record_failure(route.key)
                if not emitted and i < len(usable) - 1:
                    fallbacks += 1
                    logger.info(
                        "llm_failover",
                        model=model,
                        failed_route=route.key,
                        next_route=usable[i + 1].key,
                        error=error_to_emit.code.value,
                        stream=True,
                    )
                    continue
            break

        latency_ms = (time.perf_counter() - start) * 1000
        if error_to_emit is not None and chosen is None:
            logger.warning("llm_stream_failed", model=model, code=error_to_emit.code.value, error=error_to_emit.message)
            err_payload = {"error": {"code": error_to_emit.code.value, "message": error_to_emit.message}}
            yield f"data: {json.dumps(err_payload, ensure_ascii=False)}\n\n"

        route = chosen[0] if chosen else None
        await usage_repository.record(
            _new_event(
                model,
                request_id=request_id,
                endpoint=endpoint,
                prompt_name=prompt_name,
                prompt_version=prompt_version,
                provider=route.provider if route else None,
                upstream_model=route.model if route else None,
                api_key=(route.api_key or None) if route else None,
                stream=True,
                status="success" if chosen is not None else "error",
                status_code=200 if chosen is not None else (error_to_emit.http_status if error_to_emit else 500),
                usage=usage,
                latency_ms=latency_ms,
                first_token_ms=first_token_ms,
                retries=retries,
                fallbacks=fallbacks,
                error_type=error_to_emit.code.value if (error_to_emit and chosen is None) else None,
                error_message=error_to_emit.message if (error_to_emit and chosen is None) else None,
                user_id=user_id,
                response_format=response_format,
            )
        )
        yield "data: [DONE]\n\n"
    except LLMGatewayError as err:
        # 进入生成器主体前后的失败（未知模型、限流、全路由熔断等）
        await usage_repository.record(
            _new_event(
                model,
                request_id=request_id,
                endpoint=endpoint,
                prompt_name=prompt_name,
                prompt_version=prompt_version,
                stream=True,
                status="error",
                status_code=err.http_status,
                usage=usage,
                latency_ms=(time.perf_counter() - start) * 1000,
                first_token_ms=first_token_ms,
                retries=retries,
                fallbacks=fallbacks,
                error_type=err.code.value,
                error_message=err.message,
                user_id=user_id,
                response_format=response_format,
            )
        )
        raise
