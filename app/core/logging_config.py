import structlog


from structlog.stdlib import add_log_level
from app.core.config import settings

"""
结构化日志配置 - structlog
根据环境自动切换控制台彩色输出 / JSON文件输出
"""

def setup_logging(environment: str = "production"):
    """
    配置全局结构化日志。
    Args:
        environment: 运行环境 (development / production)
    """
    # 渲染器配置：开发环境用控制台，生产环境用 JSON 格式

    if environment == "development":
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    # 2. 配置 processors（注意顺序和大小写）
    processors = [
        #structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        renderer,
    ]

    structlog.configure(
        processors=processors,
        logger_factory=structlog.PrintLoggerFactory(),
        context_class=dict,
        wrapper_class=structlog.make_filtering_bound_logger(settings.LOG_LEVEL),
        cache_logger_on_first_use=True,
    )

    # 模块导入时自动初始化 坑 ???
    #setup_logging(environment)

    # 导出 logger 实例，供其他模块直接 import 使用
    #logger = structlog.get_logger("llm-gateway")