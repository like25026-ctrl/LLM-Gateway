"""OpenAI Chat Completions 适配器（deepseek-chat / deepseek-reasoner）"""

from app.adapters.base import BaseAdapter, StreamDelta, UnifiedRequest, UnifiedResponse, Usage


class ChatAdapter(BaseAdapter):
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
