"""remember 工具：把用户要求长期记住的信息写入三段式命名空间的长期记忆。

P4' 起改为 ("memories", user_id, kind) 三段式，并带 kind/keywords 冗余字段，
供召回侧（app.memory.recall）做结构化过滤与关键词匹配。
kind 由模型自选（参数），非法/缺省时用正则启发式兜底，最终回退 event。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from langchain_core.tools import BaseTool, tool

from app.db import uuid7
from app.memory.schema import (
    KIND_EVENT,
    KIND_LABELS,
    KIND_PREFERENCE,
    KIND_PROFILE,
    build_record,
    memory_namespace,
    normalize_kind,
)
from app.tools.base import ToolContext, ToolEntryConfig, tool_error_boundary

PREVIEW_LIMIT = 60
MAX_KEYWORDS = 8

# 启发式兜底：模型没给 kind 或给了非法值时，按内容特征猜一个
_PREFERENCE_PATTERN = re.compile(r"(喜欢|偏好|讨厌|不吃|爱吃|习惯|口味|兴趣|最爱|不喜欢|不喜欢吃)")
_PROFILE_PATTERN = re.compile(r"(我是|我叫|我的名字|我的职业|我住在|我在.{0,12}(工作|上班|上学))")
# 关键词字符串形态的分隔符：中英文逗号、顿号、分号、斜杠与空白
_KEYWORD_SPLIT_PATTERN = re.compile(r"[,，、;；/\s]+")
_KEYWORD_QUOTE_CHARS = "\"'“”‘’"


def infer_kind(content: str) -> str:
    """按内容特征推断 kind：偏好 > 身份 > 事件（默认）。"""
    if _PREFERENCE_PATTERN.search(content):
        return KIND_PREFERENCE
    if _PROFILE_PATTERN.search(content):
        return KIND_PROFILE
    return KIND_EVENT


def normalize_keywords(raw: Any) -> list[str]:
    """把模型给的关键词归一成去重的 list[str]。

    LLM 实际会传出三种形态：真数组、逗号/顿号分隔的字符串、以及被字符串化的
    JSON 数组（'["生日","3月12日"]'）。若签名只写 list[str]，后两种会被工具入参
    校验直接拒掉（模型只能反复重试），因此这里统一宽容解析，失败即视为无关键词。
    """
    if raw is None:
        return []
    items: list[Any]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        parsed: Any = None
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
        items = parsed if isinstance(parsed, list) else _KEYWORD_SPLIT_PATTERN.split(text)
    elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        items = list(raw)
    else:
        items = [raw]

    words: list[str] = []
    seen: set[str] = set()
    for item in items:
        word = str(item).strip().strip(_KEYWORD_QUOTE_CHARS).strip()
        if not word or word in seen:
            continue
        seen.add(word)
        words.append(word)
        if len(words) >= MAX_KEYWORDS:
            break
    return words


def make_remember_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    @tool("remember")
    @tool_error_boundary
    async def remember(
        content: str, kind: str = "", keywords: list[str] | str | None = None
    ) -> str:
        """把用户要求长期记住的重要信息（偏好、约定、事实、待办）写入长期记忆。

        Args:
            content: 要记住的内容，应当是一句脱离上下文也能看懂的完整描述。
            kind: 记忆类别，profile（身份档案）/ preference（偏好）/ event（事件）；拿不准留空。
            keywords: 3~5 个便于日后检索的关键词（中文或英文），可留空。
                数组或逗号分隔的字符串都接受。
        """
        text = content.strip()
        if not text:
            return "没有需要记住的内容：content 为空"
        store = context.store
        if store is None:
            return "记忆存储未初始化，暂时无法记住"

        resolved = normalize_kind(kind) if kind.strip() else infer_kind(text)
        words = normalize_keywords(keywords)
        record = build_record(
            kind=resolved,
            content=text,
            keywords=words,
            session_id=context.session_id,
        )
        await store.aput(memory_namespace(context.user_id, resolved), uuid7(), dict(record))

        preview = text if len(text) <= PREVIEW_LIMIT else text[:PREVIEW_LIMIT] + "…"
        return f"已记住（{KIND_LABELS.get(resolved, KIND_EVENT)}）：{preview}"

    return remember


__all__ = ["MAX_KEYWORDS", "infer_kind", "make_remember_tool", "normalize_keywords"]
