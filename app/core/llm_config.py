# app/core/llm_config.py

"""
大模型业务配置
- SUPPORTED_MODELS：支持的模型字典（仅保留 DeepSeek）
- calculate_cost：费用计算函数
注：原 deepseek_llm_gateway_full.py 中的 SUPPORTED_MODELS 和 calculate_cost
  已迁移至此文件，其他模块从此处导入，避免重复定义。
"""
from typing import Optional, Dict

import structlog
import tiktoken

logger = structlog.get_logger()

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

def get_model_config(model_id: str) ->Optional[Dict]:
    """获取指定模型配置"""
    return SUPPORTED_MODELS.get(model_id)

def calculate_cost(model_id: str, prompt_tokens: int, completion_tokens: int) -> float:
    """计算请求费用（元）"""
    config = get_model_config(model_id)
    if not config:
        return 0.0
    input_cost = (prompt_tokens / 1_000_000) * config["input_price"]
    output_cost = (completion_tokens / 1_000_000) * config["output_price"]

    return round(input_cost + output_cost, 6)


def check_context_fit(system_prompt: str, messages: list[str], max_tokens: int = 128000,model_name:str = 'gpt-4o'):
    """检查所有内容是否在上下文窗口内 20260920"""
    enc = tiktoken.encoding_for_model(model_name)
    total = len(enc.encode(system_prompt))
    for msg in messages:
        total += len(enc.encode(msg))

    usage_pct = total / max_tokens * 100

    logger.info(f"System Prompt: {len(enc.encode(system_prompt))} tokens")
    logger.info(f"对话历史: {len(enc.encode(''.join(messages)))} tokens")
    logger.info(f"总计: {total} / {max_tokens} tokens ({usage_pct:.1f}%)")

    if total > max_tokens:
        raise ValueError(f"⚠️ 超出上限！请缩短内容或使用上下文压缩")
    else:
        raise ValueError(f"✅ 安全范围内")