import os
from typing import Literal, List
from pydantic import field_validator, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

"""
Pydantic Settings 统一配置管理
所有配置项集中在此，支持 .env 文件 + 环境变量覆盖
"""

class Settings(BaseSettings):

    # ===== 基础 =====
    APP_NAME: str = "DeepSeek LLM Gateway"
    DEBUG: bool = False
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    APP_DESCRIPTION: str = "DeepSeek LLM 网关服务"

    # DeepSeek
    DEEPSEEK_API_KEY: str =""
    DEEPSEEK_API_BASE: str = "https://api.deepseek.com/v1"

    # Database
    DATABASE_URL: str = "sqlite:///./app.db"

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WORKERS: int = 1
    RELOAD: bool = False


    # ====限流配置-安全与限流 =====
    SECRET_KEY: str = "change-me-in-production"
    LOG_LEVEL: str = "INFO"
    RATE_LIMIT: int = Field(default=10, ge=1, description="每个IP每分钟最大请求数")
    MAX_TOKENS:int = 4096 # 单次请求最大输出 tokens

    # ===== 韧性 / 可观测 =====
    REQUEST_TIMEOUT: float = Field(default=60.0, gt=0, description="上游请求超时（秒）")
    RETRY_MAX_ATTEMPTS: int = Field(default=3, ge=1, description="上游调用最大尝试次数（含首次）")
    RETRY_BASE_DELAY: float = Field(default=0.5, ge=0, description="指数退避基础延迟（秒）")
    MODEL_RATE_LIMIT: int = Field(default=60, ge=1, description="每个模型每分钟最大请求数（按模型独立限流）")

    # ===== CORS =====
    ALLOWED_ORIGINS: List[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://localhost:5173",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:5173",
        ],
        description="允许的跨域来源",
    )

    # ===== 日志 =====
    LOG_LEVEL: str = "INFO"

    # ===== Docs 开关 =====
    DOCS_URL: str | None = "/docs"
    REDOC_URL: str | None = "/redoc"
    OPENAPI_URL: str | None = "/openapi.json"

    @model_validator(mode='after')
    def check_api_key(self) -> 'Settings':
        """在所有字段加载完成后校验 API Key"""
        if (not self.DEEPSEEK_API_KEY or self.DEEPSEEK_API_KEY.startswith("sk-你的")):
            # 使用 self.ENVIRONMENT（已从 .env 或环境变量中加载的值）
            if self.ENVIRONMENT != "development":
                raise ValueError("请配置有效的 DEEPSEEK_API_KEY")
        return self

    # ===== Pydantic Settings 配置 =====
    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", ".env.development"),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore"
    )


    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid:
            raise ValueError(f"LOG_LEVEL 必须是 {valid} 之一")
        return v.upper()

    @property
    def is_prod(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def is_dev(self) -> bool:
        return self.ENVIRONMENT == "development"

# 全局单例，其他模块直接 from app.config import settings
settings = Settings()
