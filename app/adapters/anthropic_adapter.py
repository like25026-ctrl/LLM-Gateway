"""Anthropic Messages API 适配器（deepseek-flash）

要点：
- endpoint: {base_url}/v1/messages，鉴权用 x-api-key（同时附带 Bearer 兼容 AUTH_TOKEN 方式）
- system 为顶层字段，messages 仅包含 user / assistant，且 max_tokens 必填
- 无原生结构化输出：通过向 system 注入「只输出 JSON」约束来模拟
- 流式：content_block_delta(text_delta) 取正文；message_start / message_delta 取用量
"""

from app.adapters.base import BaseAdapter, StreamDelta, UnifiedRequest, UnifiedResponse, Usage
from app.core.config import settings


class AnthropicAdapter(BaseAdapter):
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
