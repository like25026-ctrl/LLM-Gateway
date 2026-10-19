import time
from collections import defaultdict
from threading import Lock

import structlog

"""限流（rate_limit）：基于滑动窗口的 IP 限流 + 按模型独立限流

生产环境建议替换为 Redis 实现，当前使用内存字典适合单实例部署。
"""

logger = structlog.get_logger()

# 内存存储：{ip: [timestamp1, timestamp2, ...]}
_request_log: dict[str, list[float]] = defaultdict(list)
# 按模型限流：{"model:<id>": [timestamp1, timestamp2, ...]}
_model_request_log: dict[str, list[float]] = defaultdict(list)
_lock = Lock()


def _sliding_window_allow(key: str, store: dict[str, list[float]], limit: int, window: int) -> bool:
    """通用滑动窗口判断：True=放行，False=超限"""
    now = time.time()
    cutoff = now - window

    with _lock:
        timestamps = store[key]
        store[key] = [ts for ts in timestamps if ts > cutoff]

        if len(store[key]) >= limit:
            logger.warning(
                "rate_limit_exceeded",
                key=key,
                current_count=len(store[key]),
                limit=limit,
                window=window,
            )
            return False

        store[key].append(now)
        return True


def check_rate_limit(client_ip: str, limit: int = 60, window: int = 60) -> bool:
    """检查 IP 是否超过限流阈值

    使用滑动窗口算法：统计最近 window 秒内的请求数，超过 limit 则拒绝。

    Args:
        client_ip: 客户端 IP 地址
        limit: 每个窗口内最大请求数，默认 60
        window: 滑动窗口大小（秒），默认 60

    Returns:
        bool: True 表示允许请求，False 表示触发限流
    """
    return _sliding_window_allow(client_ip, _request_log, limit, window)


def check_model_rate_limit(model_id: str, limit: int = 60, window: int = 60) -> bool:
    """检查指定模型是否超过限流阈值（按模型独立限流）

    Args:
        model_id: 模型 ID
        limit: 每个窗口内最大请求数
        window: 滑动窗口大小（秒）

    Returns:
        bool: True 表示允许请求，False 表示触发限流（上层应返回 429）
    """
    return _sliding_window_allow(f"model:{model_id}", _model_request_log, limit, window)


def get_remaining_requests(client_ip: str, limit: int = 60, window: int = 60) -> int:
    """获取 IP 剩余可用请求数

    Args:
        client_ip: 客户端 IP 地址
        limit: 每个窗口内最大请求数
        window: 滑动窗口大小（秒）

    Returns:
        int: 剩余可用请求数
    """
    now = time.time()
    cutoff = now - window

    with _lock:
        timestamps = _request_log.get(client_ip, [])
        valid_count = sum(1 for ts in timestamps if ts > cutoff)
        return max(0, limit - valid_count)


def reset_rate_limit(client_ip: str | None = None):
    """重置限流记录

    Args:
        client_ip: 指定 IP 则只重置该 IP，None 则重置所有
    """
    with _lock:
        if client_ip:
            _request_log.pop(client_ip, None)
        else:
            _request_log.clear()
    logger.info("rate_limit_reset", client_ip=client_ip or "all")
