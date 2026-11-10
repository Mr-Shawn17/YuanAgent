from __future__ import annotations

from app.rag.kb_tool import make_kb_tool
from app.tools.base import BuiltinToolSpec
from app.tools.builtin.calculator import make_calculator_tool
from app.tools.builtin.coding import make_delegate_coding_tool
from app.tools.builtin.deps import make_install_deps_tool
from app.tools.builtin.filesystem import (
    make_list_directory_tool,
    make_read_file_tool,
    make_write_file_tool,
)
from app.tools.builtin.gitops import make_git_clone_tool, make_git_commit_push_tool
from app.tools.builtin.memory import make_remember_tool
from app.tools.builtin.sandbox_query import make_git_query_tool
from app.tools.builtin.skill import make_execute_skill_script_tool, make_load_skill_tool
from app.tools.builtin.time_tool import make_time_tool
from app.tools.builtin.web_search import make_web_search_tool

# 内置工具注册表：名称 → 工厂 + 默认档位 + 默认依赖密钥。
# search_knowledge_base 的工厂来自 rag 模块（mode=stub 时产出，mode=mcp 时返回
# None 由 registry 记禁用原因，P2' 后由 MCP 管理器提供同名工具）。
# load_skill / execute_skill_script 依赖 ToolContext.skill_catalog：
# 未注入（skills 功能关闭）或无技能时工厂返回 None，由 registry 记禁用原因。
BUILTIN_TOOL_SPECS: dict[str, BuiltinToolSpec] = {
    "get_current_time": BuiltinToolSpec(make_time_tool, "read"),
    "calculator": BuiltinToolSpec(make_calculator_tool, "read"),
    "web_search": BuiltinToolSpec(make_web_search_tool, "read", "tavily_api_key"),
    "list_directory": BuiltinToolSpec(make_list_directory_tool, "read"),
    "read_file": BuiltinToolSpec(make_read_file_tool, "read"),
    "write_file": BuiltinToolSpec(make_write_file_tool, "write"),
    "remember": BuiltinToolSpec(make_remember_tool, "write"),
    "search_knowledge_base": BuiltinToolSpec(make_kb_tool, "read"),
    "load_skill": BuiltinToolSpec(make_load_skill_tool, "read"),
    "execute_skill_script": BuiltinToolSpec(make_execute_skill_script_tool, "danger"),
    # 开发工作流四件套（docs/codex-integration.md）：命令在用户本机 runner 固定拼装
    "git_clone": BuiltinToolSpec(make_git_clone_tool, "danger"),
    "git_commit_push": BuiltinToolSpec(make_git_commit_push_tool, "danger"),
    "install_deps": BuiltinToolSpec(make_install_deps_tool, "danger"),
    "delegate_coding": BuiltinToolSpec(make_delegate_coding_tool, "danger"),
    # 服务端沙箱化只读 git 查询（docs/sandbox-query.md）：read 档免审批的前提是
    # 每次执行都在 runsc 沙箱内（无沙箱不执行）；Engine 不可达时工厂返回 None 自禁
    "git_query": BuiltinToolSpec(make_git_query_tool, "read"),
}

__all__ = [
    "BUILTIN_TOOL_SPECS",
    "make_calculator_tool",
    "make_delegate_coding_tool",
    "make_execute_skill_script_tool",
    "make_git_clone_tool",
    "make_git_commit_push_tool",
    "make_install_deps_tool",
    "make_kb_tool",
    "make_list_directory_tool",
    "make_load_skill_tool",
    "make_read_file_tool",
    "make_remember_tool",
    "make_time_tool",
    "make_web_search_tool",
    "make_write_file_tool",
]
