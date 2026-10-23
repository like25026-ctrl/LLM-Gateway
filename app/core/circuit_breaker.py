"""熔断器（circuit_breaker）：按路由统计连续失败，超过阈值后冷却期内快速失败

- 状态机：closed（正常）-> open（失败达阈值，冷却期内拒绝）-> half-open（冷却结束放行探测）
- 探测成功重置计数；探测失败立即重新 open
- 单实例内存实现，多实例部署可替换为 Redis
"""

import threading
import time
from dataclasses import dataclass

from app.core.gateway_config import tuning


@dataclass
class _State:
    failures: int = 0
    opened_at: float = 0.0


class CircuitBreaker:
    """按 route key（provider:model）维度的简单熔断器"""

    def __init__(self, config):
        self._enabled = config.enabled
        self._threshold = config.failure_threshold
        self._cooldown = config.cooldown_seconds
        self._states: dict[str, _State] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """是否放行该路由：open 且冷却未结束 -> False；冷却结束 -> True（半开探测）"""
        if not self._enabled:
            return True
        with self._lock:
            state = self._states.get(key)
            if not state or not state.opened_at:
                return True
            return (time.time() - state.opened_at) >= self._cooldown

    def record_success(self, key: str) -> None:
        """成功：重置状态"""
        with self._lock:
            self._states.pop(key, None)

    def record_failure(self, key: str) -> None:
        """失败：累计计数，达到阈值则熔断"""
        with self._lock:
            state = self._states.setdefault(key, _State())
            state.failures += 1
            if state.failures >= self._threshold:
                state.opened_at = time.time()


# 模块级单例
circuit_breaker = CircuitBreaker(tuning.circuit_breaker)
