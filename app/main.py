import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import structlog
from contextvars import ContextVar
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import HTMLResponse
from starlette.staticfiles import StaticFiles

from app.core.logging_config import setup_logging
from app.core.errors import LLMGatewayError, llm_gateway_error_handler
from app.core.auth import require_api_key
from app.api import health, openai_compat, routes
import app.models  # noqa: F401  注册 ORM 模型（含 PromptTemplate），供 create_all 建表
from app.database import Base, engine
from app.middleware import RequestIDMiddleware, LoggingMiddleware
from app.config import settings

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

logger = structlog.get_logger()

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.ENVIRONMENT)
    # 创建数据库表（同步 DDL 卸载到工作线程，不阻塞事件循环）
    await asyncio.to_thread(Base.metadata.create_all, bind=engine)
    yield


def create_app() -> FastAPI:
    #setup_logging(settings.ENVIRONMENT)

    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        debug=settings.DEBUG,
        description=settings.APP_DESCRIPTION,
        docs_url="/docs" if settings.ENVIRONMENT == "development" else None,
        redoc_url="/redoc" if settings.ENVIRONMENT == "development" else None,
        lifespan=lifespan,
    )

    # 区分环境的 CORS 设置
    if settings.ENVIRONMENT == "development":
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    logger.info("Development mode: CORS & Docs enabled")

    # 注册中间件
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(LoggingMiddleware)

    # 注册统一错误处理器
    app.add_exception_handler(LLMGatewayError, llm_gateway_error_handler)

    # register Routers
    # 探针不加鉴权；兼容层 /v1、业务层 /api/v1 与管理接口按需 Bearer 鉴权
    # （require_api_key 在未配置 api_keys 时自动放行，不影响开发模式与内置 Web 界面）
    app.include_router(health.router)
    app.include_router(health.ops_router)
    app.include_router(openai_compat.router, prefix="/v1", dependencies=[Depends(require_api_key)])
    app.include_router(routes.router, prefix="/api/v1", dependencies=[Depends(require_api_key)])
    app.include_router(routes.admin_router, dependencies=[Depends(require_api_key)])

    # 挂载静态前端
    index_path  = os.path.join(os.path.dirname(__file__), "static", "index.html")
    static_dir = os.path.dirname(index_path)  # 获取 static 文件夹路径

    logger.info(f"Static directory: {static_dir}")
    logger.info(f"Index file path: {index_path}")

    # 2. 挂载静态文件目录
    if os.path.exists(index_path):
        app.mount("/app/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", response_class=HTMLResponse)
    def read_root():
        if os.path.exists(index_path):
            return FileResponse(index_path)
        return {"detail": "找不到 index.html 文件，请检查 static 目录"}


    return app


app = create_app()


# ==================== 入口 容易出现坑的 一般会遗忘 ====================
if __name__ == "__main__":
    import uvicorn
    ## 启动 uvicorn 服务器
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
        reload=True,
    )