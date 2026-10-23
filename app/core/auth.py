"""调用方鉴权（auth）

网关层可选 Bearer 鉴权：
- 未配置 api_keys（gateway.yaml 的 api_keys 为空 / 环境变量 API_KEYS 未设置）时完全关闭，
  开发模式与内置 Web 界面零改动；
- 配置后，兼容层 /v1、业务层 /api/v1 与管理接口 /admin 均要求
  `Authorization: Bearer <key>`（同时兼容 `X-API-Key: <key>`）。

鉴权只校验网关自身颁发的 Key，不透传/不持有上游供应商密钥。
"""

from fastapi import Request

from app.core.errors import ErrorCode, LLMGatewayError
from app.core.gateway_config import get_api_keys

_BEARER_PREFIX = "bearer "


def _extract_key(request: Request) -> str | None:
    """从请求头提取调用方 Key（Bearer 优先，兼容 X-API-Key）"""
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith(_BEARER_PREFIX):
        token = auth[len(_BEARER_PREFIX):].strip()
        if token:
            return token
    key = (request.headers.get("x-api-key") or "").strip()
    return key or None


async def require_api_key(request: Request) -> None:
    """FastAPI 依赖：按配置校验调用方 Bearer Key

    - 未配置任何 Key -> 放行（开发模式）
    - 已配置 -> 缺失或不匹配抛 UNAUTHORIZED / 401（OpenAI authentication_error）
    """
    keys = get_api_keys()
    if not keys:
        return
    provided = _extract_key(request)
    if not provided or provided not in keys:
        raise LLMGatewayError(
            ErrorCode.UNAUTHORIZED,
            "缺少或无效的网关 API Key，请使用 Authorization: Bearer <key>",
            param="Authorization",
        )
