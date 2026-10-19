"""Pydantic 数据校验模型

定义 API 的请求体和响应体结构，用于 FastAPI 自动校验和文档生成。
"""

from typing import List, Optional, Literal, Dict
from pydantic import BaseModel, Field, field_validator


# ==================== 对话相关 ====================

class Message(BaseModel):
    """单条消息"""
    role: Literal["system", "user", "assistant", "tool"] = Field(..., description="角色")
    content: str = Field(..., min_length=1, description="消息内容")


class ChatRequest(BaseModel):
    """对话请求体"""
    model: str = Field(default="deepseek-chat", description="模型 ID")
    messages: List[Message] = Field(..., min_length=1, description="消息列表")
    temperature: Optional[float] = Field(default=0.7, ge=0, le=2, description="温度参数")
    max_tokens: Optional[int] = Field(default=None, gt=0, description="最大输出 tokens")
    stream: bool = Field(default=False, description="是否流式输出")
    response_format: Optional[dict] = Field(default=None, description="结构化输出约束，如 {'type': 'json_object'}")
    prompt: Optional[dict] = Field(default=None, description="提示词模板引用，如 {'name': ..., 'version': ..., 'variables': {...}}")

    @field_validator("model")
    @classmethod
    def validate_model(cls, v: str) -> str:
        from app.services.router import SUPPORTED_MODELS
        if v not in SUPPORTED_MODELS:
            raise ValueError(f"不支持的模型: {v}，支持的模型: {list(SUPPORTED_MODELS.keys())}")
        return v


class ChatResponseChoice(BaseModel):
    """对话响应中的单个选项"""
    index: int
    message: Message
    finish_reason: Optional[str] = None


class ChatUsage(BaseModel):
    """Token 使用量"""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatResponse(BaseModel):
    """对话响应体（兼容 OpenAI 格式）"""
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatResponseChoice]
    usage: ChatUsage


# ==================== 模型列表相关 ====================

class ModelInfo(BaseModel):
    """模型信息"""
    id: str
    display_name: str
    type: str
    provider: str


class ModelListResponse(BaseModel):
    """模型列表响应"""
    models: List[ModelInfo]


# ==================== 健康检查相关 ====================

class HealthStatus(BaseModel):
    """健康状态"""
    status: str


class ServiceInfo(BaseModel):
    """服务信息"""
    service: str
    version: str
    environment: str
    uptime: str
    timestamp: str


# ==================== 错误响应 ====================

class ErrorResponse(BaseModel):
    """统一错误响应"""
    detail: str
    status_code: int


# ==================== 提示词模板相关 ====================

class PromptRef(BaseModel):
    """提示词模板引用"""
    name: str = Field(..., description="模板名称")
    version: Optional[int] = Field(default=None, description="版本号，缺省取最新")
    variables: Dict[str, str] = Field(default_factory=dict, description="变量替换")


class PromptCreateRequest(BaseModel):
    """创建模板版本请求"""
    name: str = Field(..., min_length=1, max_length=128, description="模板名称")
    template: str = Field(..., min_length=1, description="模板内容，含 {var} 占位符")
    variables: List[str] = Field(default_factory=list, description="变量名列表")
    description: str = Field(default="", description="模板说明")


class PromptRenderRequest(BaseModel):
    """模板渲染请求"""
    variables: Dict[str, str] = Field(default_factory=dict, description="变量替换")
    version: Optional[int] = Field(default=None, description="版本号，缺省取最新")


class PromptTemplateInfo(BaseModel):
    """模板信息"""
    id: int
    name: str
    version: int
    template: str
    variables: List[str] = Field(default_factory=list)
    description: str = ""
    created_at: Optional[str] = None
