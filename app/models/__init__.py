"""数据库模型定义

当前项目使用 SQLite + SQLAlchemy ORM，模型层预留扩展空间。
后续如需持久化对话记录、用户信息等，可在此添加模型类。
"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Float, Boolean, UniqueConstraint
from app.database import Base


class ConversationLog(Base):
    """对话记录表（预留，按需启用）

    如需记录历史对话，取消 main.py 中 Base.metadata.create_all 的注释即可自动建表。
    """
    __tablename__ = "conversation_logs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    request_id = Column(String(64), index=True, comment="请求 ID")
    client_ip = Column(String(45), index=True, comment="客户端 IP")
    model = Column(String(64), nullable=False, comment="使用的模型")
    prompt_tokens = Column(Integer, default=0, comment="输入 tokens")
    completion_tokens = Column(Integer, default=0, comment="输出 tokens")
    cost_cny = Column(Float, default=0.0, comment="费用（元）")
    status_code = Column(Integer, default=200, comment="响应状态码")
    created_at = Column(DateTime, default=datetime.utcnow, comment="创建时间")

    def __repr__(self):
        return f"<ConversationLog id={self.id} model={self.model} cost={self.cost_cny}>"


class ApiKey(Base):
    """API Key 管理表（预留，按需启用）

    如需实现 API Key 鉴权，可在此扩展。
    """
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    key = Column(String(128), unique=True, nullable=False, index=True, comment="API Key")
    name = Column(String(64), comment="Key 名称/备注")
    is_active = Column(Boolean, default=True, comment="是否激活")
    rate_limit = Column(Integer, default=60, comment="每分钟限流数")
    created_at = Column(DateTime, default=datetime.utcnow, comment="创建时间")
    last_used_at = Column(DateTime, nullable=True, comment="最后使用时间")

    def __repr__(self):
        return f"<ApiKey id={self.id} name={self.name} active={self.is_active}>"


class PromptTemplate(Base):
    """提示词模板表：支持版本管理与版本引用

    同一个 name 下每次创建自增 version；(name, version) 唯一。
    """
    __tablename__ = "prompt_templates"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_prompt_name_version"),)

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(128), index=True, nullable=False, comment="模板名称")
    version = Column(Integer, nullable=False, default=1, comment="版本号（自增）")
    template = Column(Text, nullable=False, comment="模板内容，含 {var} 占位符")
    variables = Column(Text, default="[]", comment="变量名列表（JSON 数组）")
    description = Column(String(255), default="", comment="模板说明")
    created_at = Column(DateTime, default=datetime.utcnow, comment="创建时间")

    def __repr__(self):
        return f"<PromptTemplate name={self.name} v{self.version}>"