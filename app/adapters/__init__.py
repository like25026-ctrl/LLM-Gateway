"""协议适配器包

把 OpenAI Chat Completions / OpenAI Responses / Anthropic Messages 三类上游协议
的差异（鉴权、请求体、响应体、SSE 事件）统一封装在适配器内部，
向上提供协议无关的统一调用契约。
"""

from app.adapters.base import (
    BaseAdapter,
    StreamDelta,
    UnifiedRequest,
    UnifiedResponse,
    Usage,
)
from app.adapters.registry import get_adapter

__all__ = [
    "BaseAdapter",
    "StreamDelta",
    "UnifiedRequest",
    "UnifiedResponse",
    "Usage",
    "get_adapter",
]
