"""DeepSeek API 调用服务（兼容门面）

历史上本模块直接实现上游调用；现已重构为「适配器 + 编排层」：
- 协议差异封装在 app/adapters/
- 限流 / 重试 / 观测 / 归一化在 app/services/llm_service.py

本模块仅保留原有函数名作为兼容入口，委托给 llm_service，
以保证既有 import（app.services.__init__、app.main 等）不破裂。
"""

from app.services.llm_service import chat_completion, chat_completion_stream


async def call_deepseek_nonstream(model: str, messages: list, **kwargs) -> dict:
    """非流式调用（兼容原接口）"""
    return await chat_completion(model, messages, **kwargs)


def call_deepseek_stream(model: str, messages: list, **kwargs):
    """流式调用（兼容原接口），返回 SSE 异步生成器"""
    return chat_completion_stream(model, messages, **kwargs)
