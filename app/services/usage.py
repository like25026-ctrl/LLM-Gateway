"""用量统计与可观测（usage）

- merge_usage：合并流式分片用量
- calculate_cost：按模型定价计算费用（元）
- usage_from_payload：从上游响应提取 (input, output, cached) tokens
- UsageEvent：调用事件数据类（决策调用记录，含重试/降级/错误信息）
- UsageRepository：调用事件异步持久化（usage_events 表）+ 查询 + 结构化日志
- check_context_fit：上下文窗口预算检查（工具函数）

异步设计：仓储接口全 async。SQLite 驱动本身是同步的（aiosqlite 同样是
「线程执行 + async 接口」的封装），因此这里用 asyncio.to_thread 把
SQLAlchemy 同步会话卸载到工作线程，获得同等的非阻塞效果，同时保留
单一 ORM 层与既有表结构，避免双写路径与 schema 漂移。
"""

import asyncio
from dataclasses import dataclass
from typing import Any

import structlog
import tiktoken

from app.core.gateway_config import get_pricing
from app.database import SessionLocal
from app.models import UsageEventModel
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


def calculate_cost(
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
) -> float:
    """计算请求费用（元），按 gateway.yaml pricing 段（单位：元 / 1M tokens）

    cached_input_per_million 未配置时缓存命中部分按 input 价计。
    """
    pricing = get_pricing(model_id)
    if not pricing:
        return 0.0
    cached = max(0, min(int(cached_tokens or 0), prompt_tokens))
    uncached = prompt_tokens - cached
    input_price = float(pricing.get("input_per_million", 0.0))
    cached_price = float(pricing.get("cached_input_per_million", input_price))
    output_price = float(pricing.get("output_per_million", 0.0))
    cost = (
        uncached / 1_000_000 * input_price
        + cached / 1_000_000 * cached_price
        + completion_tokens / 1_000_000 * output_price
    )
    return round(cost, 6)


def usage_from_payload(payload: dict[str, Any]) -> tuple[int, int, int]:
    """从上游响应 payload 提取 (input_tokens, output_tokens, cached_tokens)

    兼容 OpenAI Chat / Responses / Anthropic 三种用量字段命名。
    """
    u = payload.get("usage") or {}
    input_tokens = u.get("prompt_tokens", u.get("input_tokens", 0)) or 0
    output_tokens = u.get("completion_tokens", u.get("output_tokens", 0)) or 0
    cached_tokens = ((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                     or (u.get("input_tokens_details") or {}).get("cached_tokens", 0)) or 0
    return int(input_tokens), int(output_tokens), int(cached_tokens)


@dataclass
class UsageEvent:
    """决策调用记录事件（每次网关调用一条，成功与失败都记录）"""

    request_id: str
    api_key_hash: str
    endpoint: str
    requested_model: str
    provider: str | None = None
    upstream_model: str | None = None
    stream: bool = False
    status: str = "success"
    status_code: int = 200
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0
    latency_ms: float = 0
    first_token_ms: float | None = None
    retries: int = 0
    fallbacks: int = 0
    error_type: str | None = None
    error_message: str | None = None
    prompt_id: str | None = None
    prompt_version: int | None = None
    metadata: dict[str, Any] | None = None


def _event_to_row(event: UsageEvent) -> dict:
    """UsageEvent -> usage_events 表行"""
    return {
        "request_id": event.request_id,
        "api_key_hash": event.api_key_hash,
        "endpoint": event.endpoint,
        "requested_model": event.requested_model,
        "provider": event.provider,
        "upstream_model": event.upstream_model,
        "stream": event.stream,
        "status": event.status,
        "status_code": event.status_code,
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "cached_tokens": event.cached_tokens,
        "cost_usd": event.cost_usd,
        "latency_ms": event.latency_ms,
        "first_token_ms": event.first_token_ms,
        "retries": event.retries,
        "fallbacks": event.fallbacks,
        "error_type": event.error_type,
        "error_message": (event.error_message or "")[:512] or None,
        "prompt_id": event.prompt_id,
        "prompt_version": event.prompt_version,
        "event_metadata": event.metadata,
    }


def _row_to_dict(row: UsageEventModel) -> dict:
    return {
        "id": row.id,
        "request_id": row.request_id,
        "api_key_hash": row.api_key_hash,
        "endpoint": row.endpoint,
        "requested_model": row.requested_model,
        "provider": row.provider,
        "upstream_model": row.upstream_model,
        "stream": row.stream,
        "status": row.status,
        "status_code": row.status_code,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "cached_tokens": row.cached_tokens,
        "cost_usd": row.cost_usd,
        "latency_ms": row.latency_ms,
        "first_token_ms": row.first_token_ms,
        "retries": row.retries,
        "fallbacks": row.fallbacks,
        "error_type": row.error_type,
        "error_message": row.error_message,
        "prompt_id": row.prompt_id,
        "prompt_version": row.prompt_version,
        "metadata": row.event_metadata,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


class UsageRepository:
    """调用事件仓储：异步持久化 + 查询（DB 异常只记日志，不影响主流程）

    record/recent 为 async 接口，同步 SQLAlchemy 操作经 asyncio.to_thread
    卸载到工作线程执行，请求热路径不阻塞事件循环。
    """

    async def record(self, event: UsageEvent) -> None:
        """持久化调用事件 + 输出结构化日志（非阻塞）"""
        await asyncio.to_thread(self._persist, event)

        logger.info(
            "llm_call_completed",
            request_id=event.request_id,
            endpoint=event.endpoint,
            model=event.requested_model,
            provider=event.provider,
            upstream_model=event.upstream_model,
            stream=event.stream,
            status=event.status,
            status_code=event.status_code,
            prompt_tokens=event.input_tokens,
            completion_tokens=event.output_tokens,
            cached_tokens=event.cached_tokens,
            total_tokens=event.input_tokens + event.output_tokens,
            cost_cny=event.cost_usd,
            latency_ms=round(event.latency_ms, 2),
            first_token_latency_ms=round(event.first_token_ms, 2) if event.first_token_ms is not None else None,
            retries=event.retries,
            error_type=event.error_type,
        )

    def _persist(self, event: UsageEvent) -> None:
        """同步落库（在工作线程中执行）；失败仅告警，不阻断业务"""
        try:
            with SessionLocal() as db:
                db.add(UsageEventModel(**_event_to_row(event)))
                db.commit()
        except Exception as exc:  # noqa: BLE001 观测失败不阻断业务
            logger.warning("usage_event_persist_failed", error=str(exc))

    async def recent(self, limit: int = 100) -> list[dict]:
        """查询最近的调用记录（按 id 倒序，非阻塞）"""
        return await asyncio.to_thread(self._recent_sync, limit)

    def _recent_sync(self, limit: int) -> list[dict]:
        """同步查询（在工作线程中执行）"""
        with SessionLocal() as db:
            rows = (
                db.query(UsageEventModel)
                .order_by(UsageEventModel.id.desc())
                .limit(max(1, min(limit, 1000)))
                .all()
            )
            return [_row_to_dict(r) for r in rows]


# 模块级单例，gateway 直接使用
usage_repository = UsageRepository()


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
