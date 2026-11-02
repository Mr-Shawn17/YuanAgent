# ruff: noqa: E501  # 本文件主体是 prompt 文本模板，长行属正常内容
"""把 system 主提示词初始化/更新到 Langfuse（Prompt 版本化试点）。

用法：
    python scripts/init_langfuse_prompts.py
行为：
- 读取 config/agent.yaml 的 langfuse 段（须已启用），连自托管 Langfuse；
- 把 system 主提示词（扁平变量版，见 SYSTEM_PROMPT_TEMPLATE）作为新版本写入
  prompt「agent-system」，并把 label "production" 指向最新版本；
- 未启用 Langfuse 时直接退出（不报错）。

版本化语义：
- 每次运行会 mint 一个新版本（不可变），label 重指向新版本即完成「发布」；
- 回滚 = 在 Langfuse 面板把 production label 指回旧版本即可，无需改代码/重新部署。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.logging import get_logger
from app.observability import get_langfuse, is_enabled

logger = get_logger(__name__)

PROMPT_NAME = "agent-system"
PROMPT_LABEL = "production"

# 扁平变量版 system 提示词（与原 backend/app/agents/prompts/system.j2 等价，
# 但把 persona.* 嵌套访问展开为扁平变量，适配 Langfuse 的 compile(**kwargs)）。
# Langfuse 文本 prompt 用 Mustache 风格 {{ var }}（不支持 Jinja2 的 {% if %} 或 or 表达式），
# 因此 notes 为空时的「## 补充说明」整段由 Python 端决定是否追加（见 builder.py），
# 模板里只保留 notes 占位（空串时该行自然为空，不影响整体）。
# 变量：name / role / style / boundaries / timezone / language / now / session_id / notes
SYSTEM_PROMPT_TEMPLATE = """你是「{{ name }}」，{{ role }}。

## 说话方式
{{ style }}

## 边界
{{ boundaries }}

## 长期记忆
- 用户要求你记住的信息，调用 remember 工具写入长期记忆，并按内容选好 kind（profile 身份档案 / preference 偏好习惯 / event 事件约定）；
- 系统会在下文自动注入你已记住的信息，跨会话依然有效，回答时自然运用；
- 用户说"忘掉/不要再记"某条信息时，礼貌说明当前暂不支持删除，并在后续回答中不再引用该信息。

## 当前上下文
- 现在时间：{{ now }}（时区 {{ timezone }}）
- 默认回复语言：{{ language }}
- 会话 ID：{{ session_id }}
{{ notes }}
"""


def main() -> None:
    if not is_enabled():
        logger.info("langfuse 未启用，跳过 prompt 初始化")
        return
    client = get_langfuse()
    prompt = client.create_prompt(
        name=PROMPT_NAME,
        prompt=SYSTEM_PROMPT_TEMPLATE,
        labels=[PROMPT_LABEL],
        type="text",
        commit_message="init: system prompt 扁平变量版",
    )
    logger.info(
        "prompt 已创建/更新",
        name=PROMPT_NAME,
        version=getattr(prompt, "version", None),
        label=PROMPT_LABEL,
    )
    client.flush()


if __name__ == "__main__":
    main()
