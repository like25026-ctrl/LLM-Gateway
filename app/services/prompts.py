"""提示词模板服务（prompts）：模板存储 / 版本管理 / 变量替换 / 版本引用

- 存储：SQLite（SQLAlchemy），表 prompt_templates，(name, version) 唯一
- 版本：同一 name 每次创建自增 version
- 引用：可按 name 取最新，或按 name + version 指定
- 变量：模板中 {var} 占位，缺失变量保留占位符不抛错（_SafeDict）
"""

import json

from app.core.errors import ErrorCode, LLMGatewayError
from app.database import SessionLocal
from app.models import PromptTemplate


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


def create_version(name: str, template: str, variables: list[str] | None = None, description: str = "") -> dict:
    """新建模板版本（同一 name 自增 version）"""
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
        db.commit()
        db.refresh(row)
        return _to_dict(row)


def list_templates() -> list[dict]:
    """列出所有版本（按 name、version 倒序）"""
    with SessionLocal() as db:
        rows = (
            db.query(PromptTemplate)
            .order_by(PromptTemplate.name, PromptTemplate.version.desc())
            .all()
        )
        return [_to_dict(r) for r in rows]


def get_template(name: str, version: int | None = None) -> dict:
    """获取模板元信息；version 为空取最新版本"""
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


def render(name: str, variables: dict | None = None, version: int | None = None) -> str:
    """渲染模板：替换 {var} 占位符"""
    meta = get_template(name, version)
    try:
        return meta["template"].format_map(_SafeDict(variables or {}))
    except (ValueError, IndexError) as exc:
        raise LLMGatewayError(ErrorCode.INVALID_REQUEST, f"模板渲染失败: {exc}")
