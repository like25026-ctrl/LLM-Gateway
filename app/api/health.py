import time
from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from sqlalchemy import text
from starlette import status

from app.database import engine, get_db
from app.core.config import settings

router = APIRouter(prefix="/health", tags=["Health"])
_start_time = time.time()

@router.get("/live", status_code=status.HTTP_200_OK, include_in_schema=False)
async def health_live():
    """存活探针：仅检查进程是否存活，供 Kubernetes livenessProbe 使用"""
    return {"status": "alive"}

@router.get("/ready", status_code=status.HTTP_200_OK, include_in_schema=False)
async def health_ready(db = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ready", "database": "connected"}
    except Exception:
        return {"status": "not ready", "database": "disconnected"}

@router.get("/")
async def health_full():
    return {
        "status": "healthy",
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "uptime_seconds": time.time() - _start_time
    }

@router.get("/info", status_code=status.HTTP_200_OK, include_in_schema=False)
async def info_check():
    """服务信息端点：返回服务版本、环境、运行时长等元信息"""
    uptime_seconds = time.time() - _start_time
    hours, remainder = divmod(int(uptime_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)

    return {
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "uptime": f"{hours:02d}:{minutes:02d}:{seconds:02d}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }