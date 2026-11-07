"""delegate_coding：把编程任务整段委派给本机 codex CLI（用户自己的账号）。

token 经济分层：编程类重任务走用户本机 codex 自己的账号，
平台服务端 key 只负责轻量编排（见 docs/roadmap.md）。

调用方式：codex exec --json --ephemeral --skip-git-repo-check
          -s workspace-write -C <workspace> -o <last_message_file> <task>
- codex 自带沙箱管它内部循环的命令，与平台 VM 档互不依赖
- --ephemeral：不在 ~/.codex 留 session 文件
- -o：最终 agent 消息落文件，取回截断后回传；JSONL 事件流不回传（太长）
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import uuid
from pathlib import Path

from runner.command import CommandPolicy, build_sandbox_env, kill_process_tree

_NOT_INSTALLED_TEXT = (
    "[codex_not_installed] 本机未检测到 codex CLI。安装并登录后重试：\n"
    "  npm install -g @openai/codex\n"
    "  codex login\n"
    "（也可在 runner.yaml 的 command.codex_path 指定路径；"
    "或在 command.codex_enabled: false 关闭本工具）"
)

_MAX_TASK_CHARS = 8000


def detect_codex(policy: CommandPolicy) -> str | None:
    """返回 codex 可执行文件绝对路径；未启用或未安装返回 None。"""
    if not policy.codex_enabled:
        return None
    return shutil.which(policy.codex_path)


async def run_codex_task(
    args: dict[str, object], workspace: Path, policy: CommandPolicy
) -> str:
    """执行一次 codex 委派。永不抛异常，返回给模型的文本。"""
    task = str(args.get("task", "")).strip()
    if not task:
        return "[工具执行失败] task 不能为空"
    if len(task) > _MAX_TASK_CHARS:
        return f"[工具执行失败] task 过长（>{_MAX_TASK_CHARS} 字符），请拆分任务"
    codex = detect_codex(policy)
    if codex is None:
        if not policy.codex_enabled:
            return ("[codex_not_installed] codex 委派已在 runner.yaml 中关闭 "
                    "（command.codex_enabled: false）")
        return _NOT_INSTALLED_TEXT

    out_dir = workspace / ".yuan" / "codex"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = uuid.uuid4().hex[:8]
    last_message = out_dir / f"last_{stamp}.md"
    timeout = max(60, min(int(policy.codex_timeout_seconds), 3600))

    argv = (
        codex, "exec",
        "--json", "--ephemeral", "--skip-git-repo-check",
        "-s", "workspace-write",
        "-C", str(workspace),
        "-o", str(last_message),
        task,
    )
    env = build_sandbox_env(policy)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(workspace),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        return f"[工具执行失败] 无法启动 codex：{exc}"

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        await asyncio.to_thread(kill_process_tree, proc.pid)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.communicate()
        return (
            f"[执行超时] codex 任务超过 {timeout} 秒被终止（进程树已结束）。"
            "可在 runner.yaml 的 command.codex_timeout_seconds 调大上限"
        )

    message = ""
    if last_message.is_file():
        message = last_message.read_text(encoding="utf-8", errors="replace").strip()
        with contextlib.suppress(OSError):
            last_message.unlink(missing_ok=True)
    if not message:
        message = (stdout or b"").decode("utf-8", errors="replace")[-2000:].strip()
    if proc.returncode != 0:
        err_tail = (stderr or b"").decode("utf-8", errors="replace")[-500:].strip()
        hint = f"\n[stderr 尾部]\n{err_tail}" if err_tail else ""
        return f"[codex 退出码 {proc.returncode}]\n{message[:policy.max_output_chars]}{hint}"
    if not message:
        return "（codex 无输出，exit 0）"
    if len(message) > policy.max_output_chars:
        message = message[: policy.max_output_chars] + (
            f"\n…（输出超长，已截断至 {policy.max_output_chars} 字符）"
        )
    return message


__all__ = ["detect_codex", "run_codex_task"]
