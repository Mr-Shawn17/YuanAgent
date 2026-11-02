"""skill 脚本执行器：独立进程 + 超时 + 输出封顶 + 路径钉死 + 白名单。

安全模型（非容器级隔离，多层防护 + 人工信任）：
1. 路径钉死：脚本必须位于该 skill 的 scripts/ 子目录内（resolve 后包含检查）
2. 白名单前置：frontmatter allowed_scripts 之外的脚本直接拒绝
3. 独立进程：subprocess + 当前解释器，超时 kill 整个进程树
4. 输出封顶：stdout/stderr 合计截断，防撑爆模型上下文
5. danger 档审批由 ToolPermissionMiddleware 在工具层强制（本项目首个 danger 成员）
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys
from pathlib import Path

from app.core.errors import PermissionDeniedError
from app.core.logging import get_logger
from app.skills.schema import SkillRecord

logger = get_logger(__name__)


def resolve_script_path(record: SkillRecord, script_name: str) -> Path:
    """把脚本名解析到 skill 的 scripts/ 目录内，拒绝任何越界。

    与 tools/base.resolve_in_sandbox 同思路：resolve 后做包含检查，
    `../`、绝对路径、Windows 盘符路径全部被拦下。
    """
    raw = script_name.strip()
    if not raw:
        msg = "脚本名不能为空"
        raise PermissionDeniedError(msg)
    if Path(raw).name != raw:
        msg = f"脚本名不能包含路径分隔符：{script_name}"
        raise PermissionDeniedError(msg)
    root = record.scripts_dir.resolve()
    candidate = (root / raw).resolve()
    if not candidate.is_relative_to(root):
        msg = f"禁止访问技能脚本目录之外的路径：{script_name}"
        raise PermissionDeniedError(msg)
    return candidate


async def run_skill_script(
    record: SkillRecord,
    *,
    script_name: str,
    args: str,
    timeout_seconds: int,
    max_output_chars: int,
) -> str:
    """校验 → 独立进程执行 → 封顶输出。任何失败都返回可读错误文本（不抛异常）。"""
    script = resolve_script_path(record, script_name)
    if not record.script_allowed(script.name):
        allowed = "、".join(record.allowed_scripts) or "（全部 .py）"
        return (
            f"[permission_denied] 脚本 {script.name} 不在技能 {record.name} "
            f"的白名单内（允许：{allowed}）"
        )
    if not script.is_file():
        return f"[permission_denied] 脚本不存在：{record.name}/scripts/{script.name}"

    argv = [sys.executable, str(script)]
    for token in shlex.split(args or ""):
        argv.append(token)

    logger.info(
        "skill_script_run",
        skill=record.name,
        script=script.name,
        timeout=timeout_seconds,
    )
    try:
        completed = await asyncio.to_thread(  # 阻塞调用放线程池，不卡事件循环
            subprocess.run,
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            cwd=str(script.parent),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            f"[脚本执行超时] {record.name}/scripts/{script.name} 超过 {timeout_seconds} 秒被终止；"
            "可在 frontmatter 调大 script_timeout 或优化脚本"
        )
    except OSError as exc:
        return f"[工具执行失败] 无法启动脚本进程：{exc}"

    merged = (completed.stdout or "") + (completed.stderr or "")
    if len(merged) > max_output_chars:
        merged = merged[:max_output_chars] + f"\n…（输出超长，已截断至 {max_output_chars} 字符）"
    status = "exit 0" if completed.returncode == 0 else f"exit {completed.returncode}"
    if not merged.strip():
        return f"（脚本无输出，{status}）"
    return f"[{status}]\n{merged}"


__all__ = ["resolve_script_path", "run_skill_script"]
