import os
import time
from typing import Dict, List

rate_limit_store: Dict[str, List[float]] = {}

# 限流配置
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "10"))  # 每分钟最大请求数


def check_rate_limit(client_ip: str) -> bool:
    """检查限流，返回 True 表示允许请求"""
    now = time.time()
    if client_ip not in rate_limit_store:
        rate_limit_store[client_ip] = []

    # 清理 60 秒前的记录
    rate_limit_store[client_ip] = [
        t for t in rate_limit_store[client_ip] if now - t < 60
    ]

    if len(rate_limit_store[client_ip]) >= RATE_LIMIT:
        return False

    rate_limit_store[client_ip].append(now)
    return True
