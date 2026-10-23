"""提示词模板服务（prompts）：模板存储 / 版本管理 / 变量替换 / 版本引用

- 存储：SQLite（SQLAlchemy），表 prompt_templates，(name, version) 唯一
- 版本：同一 name 每次创建自增 version
- 并发安全：创建版本参照 BEGIN IMMEDIATE 语义——(name, version) 唯一约束兜底，
  冲突时重读最新版本重试，避免并发创建产生 500
- 引用：可按 name 取最新，或按 name + version 指定
- 变量：模板中 {var} 占位，缺失变量保留占位符不抛错（_SafeDict）

异步设计：服务接口全 async。同步 SQLAlchemy 会话经 asyncio.to_thread
卸载到工作线程（与 aiosqlite 同属「线程执行 + async 接口」），
请求热路径不阻塞事件循环。
"""

import asyncio
import json

from sqlalchemy.exc import IntegrityError

from app.core.errors import ErrorCode, LLMGatewayError
from app.database import SessionLocal
from app.models import PromptTemplate

# 版本创建冲突时的最大重试次数（并发创建同一 name 的场景极少，重试成本低）
_CREATE_MAX_ATTEMPTS = 3


class _SafeDict(dict):
    """format_map 用的安全字典：缺失键保留 {key} 原样"""

    def __missing__(self, key):
        return "{" + key + "}"


def _to_dict(row: PromptTemplate) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "version": row.version,
        "template": row.template,
        "variables": json.loads(row.variables or "[]"),
        "description": row.description or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def create_version(name: str, template: str, variables: list[str] | None = None, description: str = "") -> dict:
    """新建模板版本（同一 name 自增 version，非阻塞）"""
    return await asyncio.to_thread(_create_version_sync, name, template, variables, description)


def _create_version_sync(name: str, template: str, variables: list[str] | None, description: str) -> dict:
    """同步创建版本（在工作线程中执行）

    并发安全：SELECT MAX(version) 与 INSERT 之间的竞态由 (name, version)
    唯一约束兜底；冲突时回滚并重读最新版本重试（等效 BEGIN IMMEDIATE 的保证）。
    """
    last_exc: IntegrityError | None = None
    for _ in range(_CREATE_MAX_ATTEMPTS):
        with SessionLocal() as db:
            latest = (
                db.query(PromptTemplate)
                .filter(PromptTemplate.name == name)
                .order_by(PromptTemplate.version.desc())
                .first()
            )
            version = latest.version + 1 if latest else 1
            row = PromptTemplate(
                name=name,
                version=version,
                template=template,
                variables=json.dumps(variables or [], ensure_ascii=False),
                description=description or "",
            )
            db.add(row)
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                last_exc = exc
                continue  # 并发创建同版本冲突：重读 MAX(version) 重试
            db.refresh(row)
            return _to_dict(row)
    raise LLMGatewayError(
        ErrorCode.INVALID_REQUEST,
        f"提示词模板版本创建冲突，请重试: {name}",
        http_status=409,
        detail=str(last_exc)[:200] if last_exc else None,
    )


async def list_templates() -> list[dict]:
    """列出所有版本（按 name、version 倒序，非阻塞）"""
    return await asyncio.to_thread(_list_templates_sync)


def _list_templates_sync() -> list[dict]:
    with SessionLocal() as db:
        rows = (
            db.query(PromptTemplate)
            .order_by(PromptTemplate.name, PromptTemplate.version.desc())
            .all()
        )
        return [_to_dict(r) for r in rows]


async def get_template(name: str, version: int | None = None) -> dict:
    """获取模板元信息（非阻塞）；version 为空取最新版本"""
    return await asyncio.to_thread(_get_template_sync, name, version)


def _get_template_sync(name: str, version: int | None) -> dict:
    with SessionLocal() as db:
        query = db.query(PromptTemplate).filter(PromptTemplate.name == name)
        if version is None:
            row = query.order_by(PromptTemplate.version.desc()).first()
        else:
            row = query.filter(PromptTemplate.version == version).first()
        if not row:
            suffix = f"@{version}" if version is not None else ""
            raise LLMGatewayError(ErrorCode.PROMPT_NOT_FOUND, f"提示词模板不存在: {name}{suffix}")
        return _to_dict(row)


async def render(name: str, variables: dict | None = None, version: int | None = None) -> str:
    """渲染模板：替换 {var} 占位符

    仅 get_template 一次 DB 读（走工作线程）；format_map 为纯 CPU 微秒级操作，
    无需再卸载线程。
    """
    meta = await get_template(name, version)
    try:
        return meta["template"].format_map(_SafeDict(variables or {}))
    except (ValueError, IndexError) as exc:
        raise LLMGatewayError(ErrorCode.INVALID_REQUEST, f"模板渲染失败: {exc}")
