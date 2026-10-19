"""适配器注册表：按模型的 type 动态路由到对应适配器"""

from app.adapters.anthropic_adapter import AnthropicAdapter
from app.adapters.base import BaseAdapter
from app.adapters.chat_adapter import ChatAdapter
from app.adapters.responses_adapter import ResponsesAdapter
from app.core.errors import ErrorCode, LLMGatewayError
from app.core.llm_config import get_model_config

# 模型 type -> 适配器类
_ADAPTER_CLASSES: dict[str, type[BaseAdapter]] = {
    "chat": ChatAdapter,
    "reasoning": ChatAdapter,
    "responses": ResponsesAdapter,
    "anthropic": AnthropicAdapter,
}


def get_adapter(model_id: str) -> BaseAdapter:
    """根据模型 ID 返回适配器实例

    Raises:
        LLMGatewayError(MODEL_NOT_FOUND): 未知模型
    """
    config = get_model_config(model_id)
    if not config:
        raise LLMGatewayError(
            ErrorCode.MODEL_NOT_FOUND,
            f"不支持的模型: {model_id}",
        )
    adapter_cls = _ADAPTER_CLASSES.get(config.get("type"), ChatAdapter)
    return adapter_cls(model_id, config)
