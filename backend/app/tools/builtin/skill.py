"""skill 工具：load_skill（读指南全文）+ execute_skill_script（受控执行脚本）。

两个工具的档位刻意分开：
- load_skill           read    免审批（读本地文件，无副作用）
- execute_skill_script danger  每次执行必须人工审批（项目 danger 档首个成员）

技能目录（SkillCatalog）在装配时经 ToolContext 注入，工具闭包持有引用；
catalog 为 None 时两个工厂都返回 None（registry 记禁用原因）。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from app.skills.runner import run_skill_script
from app.skills.schema import SkillCatalog
from app.tools.base import ToolContext, ToolEntryConfig, tool_error_boundary

# 单次 load_skill 返回内容的上限（防超长 SKILL.md 撑爆上下文）
MAX_SKILL_BODY_CHARS = 60_000


def _catalog_from(context: ToolContext) -> SkillCatalog | None:
    """从 ToolContext 拿技能目录；未装配（功能关闭）时返回 None。"""
    return getattr(context, "skill_catalog", None)


def make_load_skill_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool | None:
    catalog = _catalog_from(context)
    if catalog is None or not catalog.records:
        return None

    @tool("load_skill")
    @tool_error_boundary
    def load_skill(skill_name: str) -> str:
        """读取一个技能的完整操作指南。当系统提示中的技能清单与当前任务匹配时调用。

        Args:
            skill_name: 技能名（清单中列出的名称）。
        """
        record = catalog.find(skill_name)
        if record is None:
            available = "、".join(catalog.names) or "（无）"
            return f"未找到技能：{skill_name}。可用技能：{available}"
        body = record.markdown
        if len(body) > MAX_SKILL_BODY_CHARS:
            body = body[:MAX_SKILL_BODY_CHARS] + "\n\n…（指南过长已截断）"
        header = f"# 技能指南：{record.name}\n"
        extras = ""
        if record.scripts:
            allowed = "、".join(record.allowed_scripts) if record.allowed_scripts else "全部"
            extras = (
                f"\n\n（本技能附带脚本：{'、'.join(record.scripts)}；"
                f"白名单：{allowed}；需要时用 execute_skill_script 运行）"
            )
        return header + body + extras

    return load_skill


def make_execute_skill_script_tool(
    context: ToolContext, entry: ToolEntryConfig
) -> BaseTool | None:
    catalog = _catalog_from(context)
    if catalog is None or not catalog.records:
        return None
    if not catalog.script_config.enabled:
        return None

    @tool("execute_skill_script")
    @tool_error_boundary
    async def execute_skill_script(
        skill_name: str, script_name: str, script_args: str = ""
    ) -> str:
        """运行技能自带的 Python 脚本（会请求用户审批，批准后执行）。

        只能运行技能 scripts/ 目录内、白名单允许的脚本；
        输出为 stdout/stderr 合并文本（超长截断）。

        Args:
            skill_name: 技能名（清单中列出的名称）。
            script_name: 脚本文件名（如 gen_skeleton.py）。
            script_args: 传给脚本的命令行参数（空格分隔，可留空）。
        """
        record = catalog.find(skill_name)
        if record is None:
            available = "、".join(catalog.names) or "（无）"
            return f"未找到技能：{skill_name}。可用技能：{available}"
        return await run_skill_script(
            record,
            script_name=script_name,
            args=script_args,
            timeout_seconds=record.script_timeout or catalog.script_config.timeout_seconds,
            max_output_chars=catalog.script_config.max_output_chars,
        )

    return execute_skill_script


__all__ = ["make_execute_skill_script_tool", "make_load_skill_tool"]
