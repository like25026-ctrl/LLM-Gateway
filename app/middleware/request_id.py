"""请求 ID 中间件：为每个请求生成唯一追踪 ID

将 request_id 注入到 request.state 和响应头中，
便于日志关联和分布式追踪。
"""

import uuid
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware


class RequestIDMiddleware(BaseHTTPMiddleware):
    """为每个请求生成唯一 ID，并注入到 request.state 和响应头"""

    async def dispatch(self, request: Request, call_next):
        # 优先使用客户端传入的 X-Request-ID，否则生成新的
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

        # 注入到 request.state，供后续中间件和路由使用
        request.state.request_id = request_id

        # 处理请求
        response = await call_next(request)

        # 将 request_id 写入响应头
        response.headers["X-Request-ID"] = request_id

        return response