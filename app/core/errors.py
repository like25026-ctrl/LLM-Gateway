"""统一错误码与异常封装

- ErrorCode：请求生命周期内所有可预期错误的统一编码
- LLMGatewayError：携带错误码 / HTTP 状态 / 是否可重试 的统一异常
- llm_gateway_error_handler：FastAPI 异常处理器，统一错误响应体

响应体统一为 OpenAI 风格（同时保留旧字段，向后兼容）：
    {"error": {"code": "...", "type": "...", "message": "...", "param": null, "detail": "..."}}

- code：网关内部稳定错误码（如 model_not_found），调用方可据此分支
- type：OpenAI 语义分类（invalid_request_error / authentication_error /
  rate_limit_error / api_error），便于 OpenAI SDK 客户端解析
- param：出错的具体入参字段（如 model / Authorization），无则为 null
"""

from enum import Enum

from fastapi import Request
from fastapi.responses import JSONResponse


class ErrorCode(str, Enum):
    """统一错误码"""

    INVALID_REQUEST = "invalid_request"                 # 请求参数不合法
    UNAUTHORIZED = "unauthorized"                       # 调用方 API Key 缺失或无效
    MODEL_NOT_FOUND = "model_not_found"                 # 未知 / 不支持的模型
    STRUCTURED_OUTPUT_FAILED = "structured_output_failed"  # 结构化输出不合法
    RATE_LIMITED = "rate_limited"                       # 触发限流
    PROMPT_NOT_FOUND = "prompt_not_found"               # 提示词模板不存在
    UPSTREAM_ERROR = "upstream_error"                   # 上游返回错误
    UPSTREAM_TIMEOUT = "upstream_timeout"               # 上游超时
    NETWORK_ERROR = "network_error"                     # 网络连接失败
    CIRCUIT_OPEN = "circuit_open"                       # 熔断器打开（冷却期内快速失败）


# 错误码 -> 默认 HTTP 状态码
_DEFAULT_STATUS = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.MODEL_NOT_FOUND: 400,
    ErrorCode.STRUCTURED_OUTPUT_FAILED: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.PROMPT_NOT_FOUND: 404,
    ErrorCode.UPSTREAM_ERROR: 502,
    ErrorCode.UPSTREAM_TIMEOUT: 504,
    ErrorCode.NETWORK_ERROR: 502,
    ErrorCode.CIRCUIT_OPEN: 503,
}

# 错误码 -> OpenAI 错误 type 分类（供 OpenAI SDK / SDK-like 客户端识别）
_ERROR_TYPE = {
    ErrorCode.INVALID_REQUEST: "invalid_request_error",
    ErrorCode.UNAUTHORIZED: "authentication_error",
    ErrorCode.MODEL_NOT_FOUND: "invalid_request_error",
    ErrorCode.PROMPT_NOT_FOUND: "invalid_request_error",
    ErrorCode.STRUCTURED_OUTPUT_FAILED: "invalid_request_error",
    ErrorCode.RATE_LIMITED: "rate_limit_error",
    ErrorCode.UPSTREAM_ERROR: "api_error",
    ErrorCode.UPSTREAM_TIMEOUT: "api_error",
    ErrorCode.NETWORK_ERROR: "api_error",
    ErrorCode.CIRCUIT_OPEN: "api_error",
}


class LLMGatewayError(Exception):
    """统一网关异常

    Args:
        code: ErrorCode
        message: 面向调用方的简要信息
        http_status: 覆盖默认 HTTP 状态码
        detail: 原始错误细节（如上游响应体）
        retryable: 显式指定是否可重试；None 时由调用方按错误类型判断
        param: 出错的具体入参字段（如 model / Authorization）
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        http_status: int | None = None,
        detail: str | None = None,
        retryable: bool | None = None,
        param: str | None = None,
    ):
        self.code = code
        self.message = message
        self.http_status = http_status or _DEFAULT_STATUS.get(code, 500)
        self.detail = detail
        self.retryable = retryable
        self.param = param
        super().__init__(message)

    def to_dict(self) -> dict:
        """序列化为统一错误体（OpenAI 风格 + 保留旧字段）"""
        return {
            "error": {
                "code": self.code.value,
                "type": _ERROR_TYPE.get(self.code, "api_error"),
                "message": self.message,
                "param": self.param,
                "detail": self.detail,
            }
        }


class StructuredOutputError(LLMGatewayError):
    """结构化输出校验失败（code=STRUCTURED_OUTPUT_FAILED）

    由 services/structured_output.validate_response 在以下情况抛出：
    - 模型输出不是合法 JSON
    - 输出不符合 response_format 中的 JSON Schema
    """

    def __init__(self, message: str, detail: str | None = None):
        super().__init__(ErrorCode.STRUCTURED_OUTPUT_FAILED, message, detail=detail)


async def llm_gateway_error_handler(request: Request, exc: LLMGatewayError) -> JSONResponse:
    """LLMGatewayError 全局处理器：统一错误响应体与状态码"""
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())
