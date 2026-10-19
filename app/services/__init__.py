"""服务层模块

封装业务逻辑：
- gateway：LLM 网关编排（限流 / 重试 / 观测 / 归一化出口）
- prompts：提示词模板版本管理与渲染
- router：模型注册表与适配器路由
- structured_output：结构化输出校验
- upstream：上游协议适配（OpenAI Chat / Responses / Anthropic Messages）
- usage：用量统计、费用计算与调用日志
"""

from app.services.gateway import chat_completion, chat_completion_stream
from app.core.rate_limit import check_rate_limit, check_model_rate_limit, get_remaining_requests, reset_rate_limit

__all__ = [
    "chat_completion",
    "chat_completion_stream",
    "check_rate_limit",
    "check_model_rate_limit",
    "get_remaining_requests",
    "reset_rate_limit",
]
