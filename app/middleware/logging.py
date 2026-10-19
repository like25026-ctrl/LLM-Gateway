"""请求日志中间件：记录每个请求的方法、路径、状态码、耗时"""

import time
import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = structlog.get_logger()


class LoggingMiddleware(BaseHTTPMiddleware):
    """记录每个请求的日志，包含耗时和状态码"""

    async def dispatch(self, request: Request, call_next):
        start_time = time.perf_counter()
        request_id = getattr(request.state, "request_id", "unknown")

        # 记录请求开始
        logger.info(
            "request_started",
            method=request.method,
            path=request.url.path,
            request_id=request_id,
            client_ip=request.client.host if request.client else None,
        )

        try:
            response: Response = await call_next(request)
            elapsed_ms = (time.perf_counter() - start_time) * 1000

            # 记录请求完成
            logger.info(
                "request_completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                elapsed_ms=round(elapsed_ms, 2),
                request_id=request_id,
            )

            # 在响应头中追加耗时
            response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.2f}"
            return response

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "request_error",
                method=request.method,
                path=request.url.path,
                elapsed_ms=round(elapsed_ms, 2),
                request_id=request_id,
                error=str(exc),
                exc_info=True,
            )
            raise