"""上游协议适配层（upstream）

把三类上游协议的差异全部封装在内部，向外暴露协议无关的统一契约：
- OpenAI Chat Completions（deepseek-chat / deepseek-reasoner）
- OpenAI Responses API（deepseek-v4-pro）
- Anthropic Messages API（deepseek-flash）

BaseAdapter 统一契约：
- headers()            -> 鉴权差异（Bearer / x-api-key）
- build_payload()      -> 请求体结构差异
- parse_response()     -> 非流式响应体差异
- parse_stream_event() -> 流式 SSE 事件差异

上层（services/gateway.py）只依赖 UnifiedRequest / UnifiedResponse / StreamDelta，
不感知具体是哪一种上游 API。
"""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.config import settings


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


class ChatAdapter(BaseAdapter):
    """OpenAI Chat Completions 适配器（deepseek-chat / deepseek-reasoner）"""

    api_type = "chat"
    endpoint_path = "/chat/completions"
    supports_structured_output = True

    def build_payload(self, req: UnifiedRequest) -> dict:
        payload: dict = {
            "model": self.upstream_model,
            "messages": req.messages,
            "stream": req.stream,
        }
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.max_tokens is not None:
            payload["max_tokens"] = req.max_tokens
        if req.response_format is not None:
            payload["response_format"] = req.response_format
        if req.user_id:
            payload["user_id"] = req.user_id
        return payload

    def parse_response(self, raw: dict) -> UnifiedResponse:
        choice = (raw.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        u = raw.get("usage") or {}
        usage = Usage(
            prompt_tokens=u.get("prompt_tokens", 0),
            completion_tokens=u.get("completion_tokens", 0),
            reasoning_tokens=(u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
            cached_tokens=(u.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            total_tokens=u.get("total_tokens", 0),
        )
        return UnifiedResponse(
            text=text,
            finish_reason=choice.get("finish_reason"),
            usage=usage,
            raw=raw,
        )

    def parse_stream_event(self, data: str) -> StreamDelta | None:
        if data.strip() == "[DONE]":
            return StreamDelta(done=True)

        chunk = self._loads(data)
        if chunk is None:
            return None

        usage = None
        u = chunk.get("usage")
        if u:
            usage = Usage(
                prompt_tokens=u.get("prompt_tokens", 0),
                completion_tokens=u.get("completion_tokens", 0),
                total_tokens=u.get("total_tokens", 0),
            )

        delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
        text = delta.get("content") or None
        reasoning = delta.get("reasoning_content") or None
        if text is None and reasoning is None and usage is None:
            return None
        return StreamDelta(text=text, reasoning=reasoning, usage=usage)


class ResponsesAdapter(BaseAdapter):
    """OpenAI Responses API 适配器（deepseek-v4-pro）

    要点：
    - system 消息提取为顶层 instructions，其余消息作为 input 项
    - 结构化输出映射到 text.format
    - 流式：response.output_text.delta 取正文；completed/incomplete/failed 携带 usage
    """

    api_type = "responses"
    endpoint_path = "/responses"
    supports_structured_output = True

    def build_payload(self, req: UnifiedRequest) -> dict:
        instructions: list[str] = []
        input_items: list[dict] = []
        for msg in req.messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                instructions.append(content)
            else:
                input_items.append({"role": role, "content": content})

        payload: dict = {
            "model": self.upstream_model,
            "input": input_items,
            "stream": req.stream,
        }
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.max_tokens is not None:
            payload["max_output_tokens"] = req.max_tokens
        if req.response_format is not None:
            payload["text"] = {"format": self._map_format(req.response_format)}
        if req.user_id:
            payload["user"] = req.user_id
        return payload

    @staticmethod
    def _map_format(response_format: dict) -> dict:
        """OpenAI 风格 response_format -> Responses API text.format"""
        fmt_type = response_format.get("type")
        if fmt_type == "json_object":
            return {"type": "json_object"}
        if fmt_type == "json_schema":
            schema = response_format.get("json_schema") or {}
            return {
                "type": "json_schema",
                "name": schema.get("name", "response"),
                "schema": schema.get("schema", {}),
            }
        return {"type": "text"}

    def parse_response(self, raw: dict) -> UnifiedResponse:
        texts: list[str] = []
        for item in raw.get("output") or []:
            if item.get("type") != "message":
                continue
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    texts.append(part.get("text", ""))

        u = raw.get("usage") or {}
        usage = Usage(
            prompt_tokens=u.get("input_tokens", 0),
            completion_tokens=u.get("output_tokens", 0),
            reasoning_tokens=(u.get("output_tokens_details") or {}).get("reasoning_tokens", 0),
            cached_tokens=(u.get("input_tokens_details") or {}).get("cached_tokens", 0),
            total_tokens=u.get("total_tokens", 0),
        )
        return UnifiedResponse(
            text="".join(texts),
            finish_reason=raw.get("status"),
            usage=usage,
            raw=raw,
        )

    def parse_stream_event(self, data: str) -> StreamDelta | None:
        data = data.strip()
        if not data:
            return None
        if data == "[DONE]":
            return StreamDelta(done=True)

        event = self._loads(data)
        if event is None:
            return None

        event_type = event.get("type")
        if event_type == "response.output_text.delta":
            return StreamDelta(text=event.get("delta", ""))
        if event_type in ("response.completed", "response.incomplete", "response.failed"):
            u = (event.get("response") or {}).get("usage") or {}
            usage = Usage(
                prompt_tokens=u.get("input_tokens", 0),
                completion_tokens=u.get("output_tokens", 0),
                total_tokens=u.get("total_tokens", 0),
            )
            return StreamDelta(usage=usage, done=True)
        return None


class AnthropicAdapter(BaseAdapter):
    """Anthropic Messages API 适配器（deepseek-flash）

    要点：
    - 鉴权用 x-api-key（同时附带 Bearer 兼容）
    - system 为顶层字段，messages 仅包含 user / assistant，且 max_tokens 必填
    - 无原生结构化输出：通过向 system 注入「只输出 JSON」约束来模拟
    - 流式：content_block_delta(text_delta) 取正文；message_start / message_delta 取用量
    """

    api_type = "anthropic"
    endpoint_path = "/v1/messages"
    supports_structured_output = False  # 原生不支持，采用提示词注入模拟

    def headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "x-api-key": settings.DEEPSEEK_API_KEY,
            "anthropic-version": "2023-06-01",
            "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
        }

    def build_payload(self, req: UnifiedRequest) -> dict:
        system_parts: list[str] = []
        conversation: list[dict] = []
        for msg in req.messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                system_parts.append(content)
            elif role in ("user", "assistant"):
                conversation.append({"role": role, "content": content})

        # 结构化输出模拟：注入 JSON 约束指令
        if req.response_format is not None:
            system_parts.append(self._structured_hint(req.response_format))

        payload: dict = {
            "model": self.upstream_model,
            "messages": conversation,
            "max_tokens": req.max_tokens or settings.MAX_TOKENS,
            "stream": req.stream,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.user_id:
            payload["metadata"] = {"user_id": req.user_id}
        return payload

    @staticmethod
    def _structured_hint(response_format: dict) -> str:
        fmt_type = response_format.get("type")
        base = (
            "You must respond with a single valid JSON value and nothing else. "
            "Do not wrap it in markdown code fences or add any explanation."
        )
        if fmt_type == "json_schema":
            schema = response_format.get("json_schema") or {}
            return base + " The JSON must conform to this schema: " + str(schema.get("schema", {}))
        return base

    def parse_response(self, raw: dict) -> UnifiedResponse:
        texts = [
            block.get("text", "")
            for block in (raw.get("content") or [])
            if block.get("type") == "text"
        ]
        u = raw.get("usage") or {}
        prompt_tokens = u.get("input_tokens", 0)
        completion_tokens = u.get("output_tokens", 0)
        usage = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )
        return UnifiedResponse(
            text="".join(texts),
            finish_reason=raw.get("stop_reason"),
            usage=usage,
            raw=raw,
        )

    def parse_stream_event(self, data: str) -> StreamDelta | None:
        data = data.strip()
        if not data:
            return None
        if data == "[DONE]":
            return StreamDelta(done=True)

        event = self._loads(data)
        if event is None:
            return None

        event_type = event.get("type")
        if event_type == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                return StreamDelta(text=delta.get("text", ""))
            if delta.get("type") == "thinking_delta":
                return StreamDelta(reasoning=delta.get("thinking", ""))
            return None
        if event_type == "message_start":
            u = (event.get("message") or {}).get("usage") or {}
            return StreamDelta(
                usage=Usage(
                    prompt_tokens=u.get("input_tokens", 0),
                    completion_tokens=u.get("output_tokens", 0),
                )
            )
        if event_type == "message_delta":
            u = event.get("usage") or {}
            return StreamDelta(
                usage=Usage(completion_tokens=u.get("output_tokens", 0)),
                finish_reason=(event.get("delta") or {}).get("stop_reason"),
            )
        if event_type == "message_stop":
            return StreamDelta(done=True)
        return None
