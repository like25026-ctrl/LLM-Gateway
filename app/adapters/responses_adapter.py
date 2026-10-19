"""OpenAI Responses API 适配器（deepseek-v4-pro）

要点：
- endpoint: {base_url}/responses
- system 消息提取为顶层 instructions，其余消息作为 input 项
- 结构化输出映射到 text.format
- 流式：response.output_text.delta 取正文；response.completed/incomplete/failed 携带 usage
"""

from app.adapters.base import BaseAdapter, StreamDelta, UnifiedRequest, UnifiedResponse, Usage


class ResponsesAdapter(BaseAdapter):
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
