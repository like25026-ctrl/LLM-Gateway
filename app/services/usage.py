"""用量统计与可观测（usage）

- merge_usage：合并流式分片用量
- calculate_cost：按模型定价计算费用（元）
- log_call：结构化调用日志（Token 分类统计 + 延迟 + 首 Token 延迟）
- check_context_fit：上下文窗口预算检查（工具函数）
"""

import structlog
import tiktoken

from app.services.router import get_model_config
from app.services.upstream import Usage

logger = structlog.get_logger()


def merge_usage(target: Usage, part: Usage) -> None:
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


def calculate_cost(model_id: str, prompt_tokens: int, completion_tokens: int) -> float:
    """计算请求费用（元）"""
    config = get_model_config(model_id)
    if not config:
        return 0.0
    input_cost = (prompt_tokens / 1_000_000) * config["input_price"]
    output_cost = (completion_tokens / 1_000_000) * config["output_price"]

    return round(input_cost + output_cost, 6)


def log_call(
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


def check_context_fit(system_prompt: str, messages: list[str], max_tokens: int = 128000, model_name: str = 'gpt-4o'):
    """检查所有内容是否在上下文窗口内 20260920"""
    enc = tiktoken.encoding_for_model(model_name)
    total = len(enc.encode(system_prompt))
    for msg in messages:
        total += len(enc.encode(msg))

    usage_pct = total / max_tokens * 100

    logger.info(f"System Prompt: {len(enc.encode(system_prompt))} tokens")
    logger.info(f"对话历史: {len(enc.encode(''.join(messages)))} tokens")
    logger.info(f"总计: {total} / {max_tokens} tokens ({usage_pct:.1f}%)")

    if total > max_tokens:
        raise ValueError(f"⚠️ 超出上限！请缩短内容或使用上下文压缩")
    else:
        raise ValueError(f"✅ 安全范围内")
