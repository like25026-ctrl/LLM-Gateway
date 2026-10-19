# app/core/safety.py

"""
敏感词过滤工具
- SENSITIVE_WORDS：敏感词库（后续可升级为读取外部数据库/文件）
- check_sensitive_words：检查文本是否包含敏感词
- filter_sensitive_words：过滤敏感词，替换为 ***
注：原 deepseek_llm_gateway_full.py 中的 SENSITIVE_WORDS 及相关函数
  已迁移至此文件，其他模块从此处导入，避免重复定义。
"""

# 敏感词库（后续可升级为读取外部数据库/文件）
SENSITIVE_WORDS = ["暴力", "色情", "赌博", "毒品", "诈骗", "违规"]

def check_sensitive_words(text: str) -> tuple[bool, list[str]]:
    """检查是否包含敏感词，返回 (是否存在, 敏感词列表)"""
    found = [word for word in SENSITIVE_WORDS if word in text]
    return len(found) > 0, found

def filter_sensitive_words(text: str) -> str:
    """过滤敏感词，替换为 ***"""
    for word in SENSITIVE_WORDS:
        text = text.replace(word, "***")
    return text