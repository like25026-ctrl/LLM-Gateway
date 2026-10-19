"""结构化输出（structured_output）

校验模型返回内容满足 response_format 约束：
- json_object：必须是合法 JSON
- json_schema：合法 JSON 且包含 schema.required 的必填字段

不合法时抛 STRUCTURED_OUTPUT_FAILED（统一错误码）。
"""

import json

from app.core.errors import ErrorCode, LLMGatewayError


def validate_structured(model: str, text: str, response_format: dict) -> str:
    """校验结构化输出：返回合法 JSON 文本，否则抛 STRUCTURED_OUTPUT_FAILED"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise LLMGatewayError(
            ErrorCode.STRUCTURED_OUTPUT_FAILED,
            f"模型 {model} 未返回合法 JSON",
            detail=text[:500],
        )

    if response_format.get("type") == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema") or {}
        required = schema.get("required") or []
        obj = data if isinstance(data, dict) else {}
        missing = [k for k in required if k not in obj]
        if missing:
            raise LLMGatewayError(
                ErrorCode.STRUCTURED_OUTPUT_FAILED,
                f"结构化输出缺少必填字段: {missing}",
                detail=text[:500],
            )
    return text
