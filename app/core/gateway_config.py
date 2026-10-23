"""网关 YAML 配置加载（gateway.yaml）

- 支持 ${VAR} / ${VAR:-default} 环境变量插值；变量解析顺序：
  系统环境变量 -> app/config.py 的 Settings（.env 文件） -> 插值默认值
- 配置文件缺失时使用内置默认值（与出厂行为一致），路径可用 GATEWAY_CONFIG 覆盖
- 暴露三类信息：
  - GATEWAY_CONFIG：原始配置 dict（providers / models / pricing ...）
  - tuning：韧性调优参数（retry / circuit_breaker / rate_limit / structured_output_retries）
  - get_provider_config / get_pricing / get_api_keys：按名称查询
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import yaml

from app.config import settings

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "gateway.yaml"


def _resolve_var(var: str, default: str | None) -> str:
    """环境变量解析：os.environ -> settings(.env) -> 插值默认值"""
    val = os.getenv(var)
    if val:
        return val
    val = getattr(settings, var, None)
    if val:
        return str(val)
    return default or ""


def _interpolate(node):
    """递归插值 ${VAR} / ${VAR:-default}"""
    if isinstance(node, str):
        return _ENV_PATTERN.sub(
            lambda m: _resolve_var(m.group(1), m.group(2)),
            node,
        )
    if isinstance(node, dict):
        return {k: _interpolate(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_interpolate(v) for v in node]
    return node


# 内置默认配置：与 gateway.yaml 保持一致；配置文件缺失时使用
_DEFAULT_YAML = """
# 调用方鉴权：留空（[]）表示开发模式不校验；配置后所有兼容层/管理接口都需 Bearer
api_keys: []
retry:
  max_attempts: 3
  base_delay_seconds: 0.5
  max_delay_seconds: 4.0
  retry_statuses: [408, 429, 500, 502, 503, 504]
circuit_breaker:
  enabled: true
  failure_threshold: 5
  cooldown_seconds: 30
rate_limit:
  enabled: true
structured_output_retries: 1
providers:
  deepseek:
    base_url: https://api.deepseek.com
    api_key: ${DEEPSEEK_API_KEY:-}
    timeout_seconds: 120
models:
  deepseek-chat:
    display_name: DeepSeek Chat (V3)
    strategy: priority
    routes: [{provider: deepseek, type: chat, model: deepseek-chat}]
  deepseek-reasoner:
    display_name: DeepSeek Reasoner (R1)
    strategy: priority
    routes: [{provider: deepseek, type: chat, model: deepseek-reasoner}]
  deepseek-v4-pro:
    display_name: DeepSeek V4 Pro
    strategy: priority
    routes: [{provider: deepseek, type: responses, model: deepseek-v4-pro}]
  deepseek-flash:
    display_name: DeepSeek V4 Flash
    strategy: priority
    routes: [{provider: deepseek, type: anthropic, model: deepseek-flash}]
pricing:
  deepseek-chat: {input_per_million: 1.0, output_per_million: 2.0, cached_input_per_million: 0.2}
  deepseek-reasoner: {input_per_million: 1.0, output_per_million: 2.0}
  deepseek-v4-pro: {input_per_million: 9.0, output_per_million: 27.0}
  deepseek-flash: {input_per_million: 2.0, output_per_million: 8.0}
"""


def _load_raw() -> dict:
    path = Path(os.getenv("GATEWAY_CONFIG") or _DEFAULT_CONFIG_PATH)
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else _DEFAULT_YAML
    except OSError:
        text = _DEFAULT_YAML
    return _interpolate(yaml.safe_load(text) or {})


# ==================== 韧性调优参数 ====================

@dataclass
class RetryConfig:
    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 4.0
    retry_statuses: tuple[int, ...] = (408, 429, 500, 502, 503, 504)


@dataclass
class CircuitBreakerConfig:
    enabled: bool = True
    failure_threshold: int = 5
    cooldown_seconds: float = 30.0


@dataclass
class RateLimitConfig:
    enabled: bool = True


@dataclass
class GatewayTuning:
    retry: RetryConfig
    circuit_breaker: CircuitBreakerConfig
    rate_limit: RateLimitConfig
    structured_output_retries: int = 1


def _build_tuning(raw: dict) -> GatewayTuning:
    retry = raw.get("retry") or {}
    cb = raw.get("circuit_breaker") or {}
    rl = raw.get("rate_limit") or {}
    return GatewayTuning(
        retry=RetryConfig(
            max_attempts=int(retry.get("max_attempts", 3)),
            base_delay_seconds=float(retry.get("base_delay_seconds", 0.5)),
            max_delay_seconds=float(retry.get("max_delay_seconds", 4.0)),
            retry_statuses=tuple(int(s) for s in (retry.get("retry_statuses") or [408, 429, 500, 502, 503, 504])),
        ),
        circuit_breaker=CircuitBreakerConfig(
            enabled=bool(cb.get("enabled", True)),
            failure_threshold=int(cb.get("failure_threshold", 5)),
            cooldown_seconds=float(cb.get("cooldown_seconds", 30)),
        ),
        rate_limit=RateLimitConfig(enabled=bool(rl.get("enabled", True))),
        structured_output_retries=int(raw.get("structured_output_retries", 1)),
    )


# 模块级单例：进程启动时加载一次
GATEWAY_CONFIG: dict = _load_raw()
tuning: GatewayTuning = _build_tuning(GATEWAY_CONFIG)


def get_provider_config(name: str) -> dict | None:
    """查询供应商配置（base_url / api_key / timeout_seconds）"""
    return (GATEWAY_CONFIG.get("providers") or {}).get(name)


def get_pricing(model: str) -> dict | None:
    """查询上游模型定价（input/output/cached_input per million）"""
    return (GATEWAY_CONFIG.get("pricing") or {}).get(model)


def get_api_keys() -> list[str]:
    """调用方 API Key 白名单（用于网关层 Bearer 鉴权）

    来源优先级：gateway.yaml 的 api_keys（支持列表 / 逗号分隔字符串）→ 环境变量 API_KEYS。
    过滤空串：返回空列表即表示「未配置鉴权」，此时网关不校验调用方（开发模式）。
    """
    raw = GATEWAY_CONFIG.get("api_keys")
    if raw is None:
        raw = os.getenv("API_KEYS") or ""
    if isinstance(raw, str):
        raw = raw.split(",")
    return [str(k).strip() for k in (raw or []) if str(k).strip()]
