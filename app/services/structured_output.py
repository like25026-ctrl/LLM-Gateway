"""结构化输出（structured_output）

覆盖结构化输出的完整生命周期：
- 输入：schema_from_request(api, body)           从请求体提取并归一化 response_format
- 输出：content_from_response(api, payload)      按协议从上游响应中提取文本内容
- 校验：validate_response(api, payload, schema)  去代码围栏 + JSON 解析 + jsonschema 校验
- 修复：repair_instruction(error, schema)        校验失败后生成修复指令（回传模型自修复）

不合法时抛 StructuredOutputError（统一错误码 STRUCTURED_OUTPUT_FAILED）。
"""

import json
import re
from typing import Any

from jsonschema import ValidationError, validate

from app.core.errors import ErrorCode, LLMGatewayError, StructuredOutputError

# 匹配 ```json ... ``` 代码围栏
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


# ==================== 输入：从请求体提取结构化输出定义 ====================

def schema_from_request(api: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """从请求体提取并归一化 response_format（结构化输出定义）

    归一化结果（协议无关的规范形）：
    - {"type": "json_object"}
    - {"type": "json_schema", "name": ..., "schema": {...}}
    - None：未要求结构化输出 / type=text

    兼容 OpenAI 风格入参 {"type": "json_schema", "json_schema": {...}}。

    Raises:
        LLMGatewayError(INVALID_REQUEST): response_format 形状不合法
    """
    fmt = (body or {}).get("response_format")
    if fmt is None:
        return None
    if not isinstance(fmt, dict):
        raise LLMGatewayError(ErrorCode.INVALID_REQUEST, "response_format 必须是对象")

    fmt_type = fmt.get("type", "text")
    if fmt_type in (None, "text"):
        return None
    if fmt_type == "json_object":
        return {"type": "json_object"}
    if fmt_type == "json_schema":
        js = fmt.get("json_schema") or {}
        schema = js.get("schema") or fmt.get("schema")
        if not isinstance(schema, dict) or not schema:
            raise LLMGatewayError(ErrorCode.INVALID_REQUEST, "json_schema 缺少 schema 定义")
        return {
            "type": "json_schema",
            "name": js.get("name") or fmt.get("name") or "response",
            "schema": schema,
        }
    raise LLMGatewayError(ErrorCode.INVALID_REQUEST, f"不支持的 response_format.type: {fmt_type}")


# ==================== 输出：按协议提取响应文本 ====================

def content_from_response(api: str, payload: dict[str, Any]) -> str:
    """按上游协议从响应 payload 中提取文本内容

    - responses：output[].content[].text（output_text）
    - anthropic：content[].text
    - chat（默认）：choices[0].message.content
    """
    if api == "responses":
        texts: list[str] = []
        for item in payload.get("output") or []:
            if item.get("type") != "message":
                continue
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    texts.append(part.get("text", ""))
        return "".join(texts)
    if api == "anthropic":
        return "".join(
            block.get("text", "")
            for block in payload.get("content") or []
            if block.get("type") == "text"
        )
    # 默认按 OpenAI Chat Completions 处理
    choice = (payload.get("choices") or [{}])[0]
    return (choice.get("message") or {}).get("content") or ""


# ==================== 校验：去围栏 + JSON 解析 + jsonschema 校验 ====================

def strip_fences(text: str) -> str:
    """去除 markdown 代码围栏（```json ... ```）与首尾空白"""
    match = _FENCE.match(text or "")
    return (match.group(1) if match else (text or "")).strip()


def validate_response(api: str, payload: dict[str, Any], schema: dict[str, Any] | None) -> str:
    """校验上游响应是否满足结构化输出定义

    流程：content_from_response 提取文本 -> 去代码围栏 -> JSON 解析
          -> json_schema 类型时用 jsonschema 库做完整 schema 校验

    Returns:
        校验通过的 JSON 文本（去围栏、去首尾空白）

    Raises:
        StructuredOutputError: 不是合法 JSON，或不符合 schema
    """
    text = strip_fences(content_from_response(api, payload))
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise StructuredOutputError("模型未返回合法 JSON", detail=text[:500])

    if schema and schema.get("type") == "json_schema":
        try:
            validate(instance=data, schema=schema["schema"])
        except ValidationError as exc:
            raise StructuredOutputError(
                f"结构化输出不符合 schema: {exc.message}", detail=text[:500]
            )
    return text


# ==================== 修复：校验失败后的修复指令 ====================

def json_schema_instruction(schema: dict[str, Any]) -> str:
    """生成「按 schema 输出 JSON」的提示词指令

    供不支持原生 json_schema 的协议（chat / anthropic）注入 system 使用。
    """
    return (
        "You must respond with a single valid JSON value and nothing else. "
        "Do not wrap it in markdown code fences or add any explanation. "
        "The JSON must conform to this schema: "
        + json.dumps(schema, ensure_ascii=False)
    )


def repair_instruction(error: StructuredOutputError, schema: dict[str, Any] | None) -> str:
    """根据校验错误与 schema 生成修复指令

    用法：校验失败后把该指令作为 system 消息追加，重新调用模型自修复一次。
    """
    parts = [
        "你上一次的输出未通过结构化校验。请严格重新输出："
        "只输出一个合法的 JSON 值，不要包含任何解释，也不要使用 markdown 代码围栏。"
    ]
    if error.message:
        parts.append(f"校验错误：{error.message}")
    if schema and schema.get("type") == "json_schema":
        parts.append(f"目标 JSON Schema：{json.dumps(schema['schema'], ensure_ascii=False)}")
    return "\n".join(parts)
