"""模型路由（router）

- SUPPORTED_MODELS：模型注册表（协议类型、上游地址、显示名、定价）
- get_model_config：查询模型配置
- get_adapter：按模型 type 动态路由到对应上游适配器
"""

from typing import Dict, Optional

from app.core.errors import ErrorCode, LLMGatewayError
from app.services.upstream import AnthropicAdapter, BaseAdapter, ChatAdapter, ResponsesAdapter

SUPPORTED_MODELS = {
    "deepseek-chat": {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "type": "chat",
        "display_name": "DeepSeek Chat (V3)",
        "input_price": 1.0,       # 元 / 百万 tokens（缓存未命中）
        "output_price": 2.0,
    },
    "deepseek-reasoner": {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "type": "reasoning",
        "display_name": "DeepSeek Reasoner (R1)",
        "input_price": 1.0,
        "output_price": 2.0,
    },
    "deepseek-v4-pro": {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "type": "responses",
        "display_name": "DeepSeek V4 Pro",
        "model": "deepseek-v4-pro",
        "input_price": 9.0,
        "output_price": 27.0,
        "price_note": "高峰时段 缓存未命中；空闲时段减半（input 4.5 / output 13.5）"
    },
    "deepseek-flash": {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/anthropic",
        "type": "anthropic",
        "display_name": "DeepSeek V4 Flash",
        "model": "deepseek-flash",
        "input_price": 2.0,
        "output_price": 8.0,
        "price_note": "高峰时段 缓存未命中；空闲时段减半（input 1.0 / output 4.0）"
    },
}


def get_model_config(model_id: str) -> Optional[Dict]:
    """获取指定模型配置"""
    return SUPPORTED_MODELS.get(model_id)


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
