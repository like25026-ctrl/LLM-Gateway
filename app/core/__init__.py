"""core 包：配置、错误码、限流、安全、日志

兼容 re-export：旧代码 `from app.core import settings` 等写法仍可用；
推荐直接从具体模块导入（app.config / app.core.errors / app.core.rate_limit / app.core.security）。
"""
from app.config import settings
from app.core.security import SENSITIVE_WORDS, check_sensitive_words, filter_sensitive_words
from app.services.router import SUPPORTED_MODELS, get_model_config
from app.services.usage import calculate_cost, check_context_fit

__all__ = [
    "settings",
    "SUPPORTED_MODELS",
    "get_model_config",
    "calculate_cost",
    "SENSITIVE_WORDS",
    "check_sensitive_words",
    "filter_sensitive_words",
    "check_context_fit",
]
