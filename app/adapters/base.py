"""适配器抽象基类与统一数据模型

适配器的职责：把「上游协议的差异」全部封装在内部，向外暴露协议无关的统一契约：
- headers()          -> 鉴权差异（Bearer / x-api-key）
- build_payload()    -> 请求体结构差异
- parse_response()   -> 非流式响应体差异
- parse_stream_event()-> 流式 SSE 事件差异

上层（services/llm_service.py）只依赖 UnifiedRequest / UnifiedResponse / StreamDelta，
不感知具体是哪一种上游 API。
"""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.core.config import settings


@dataclass
class Usage:
    """Token 用量（分类统计）"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0


@dataclass
class UnifiedRequest:
    """统一入参（协议无关）"""

    model: str
    messages: list
    stream: bool = False
    response_format: dict | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    user_id: str | None = None


@dataclass
class UnifiedResponse:
    """统一出参（协议无关）"""

    text: str = ""
    finish_reason: str | None = None
    usage: Usage = field(default_factory=Usage)
    raw: dict = field(default_factory=dict)


@dataclass
class StreamDelta:
    """统一流式增量"""

    text: str | None = None           # 正文增量
    reasoning: str | None = None      # 思维链增量
    usage: Usage | None = None        # 用量（通常出现在流末尾）
    finish_reason: str | None = None  # 结束原因
    done: bool = False                # 是否为终止事件


class BaseAdapter(ABC):
    """协议适配器抽象基类"""

    api_type: str = ""
    endpoint_path: str = ""
    # 是否原生支持结构化输出（False 表示需要提示词注入模拟）
    supports_structured_output: bool = True

    def __init__(self, model_id: str, config: dict):
        self.model_id = model_id
        self.config = config

    @property
    def upstream_model(self) -> str:
        """上游实际使用的模型名（部分协议与网关模型 ID 不同）"""
        return self.config.get("model", self.model_id)

    @property
    def base_url(self) -> str:
        return self.config["base_url"]

    def headers(self) -> dict:
        """默认鉴权头：OpenAI 风格 Bearer"""
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
        }

    @staticmethod
    def _loads(data: str) -> dict | None:
        """安全解析 SSE 的 data 载荷"""
        try:
            return json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return None

    @abstractmethod
    def build_payload(self, req: UnifiedRequest) -> dict:
        """构造上游请求体"""

    @abstractmethod
    def parse_response(self, raw: dict) -> UnifiedResponse:
        """解析非流式响应"""

    @abstractmethod
    def parse_stream_event(self, data: str) -> StreamDelta | None:
        """解析单个 SSE 事件（入参为 data: 之后的原始字符串）"""
