"""delegate_coding：把编程任务整段委派给本机 codex CLI（用户自己的账号）。

token 经济分层：编程类重任务走用户本机 codex 自己的账号，平台服务端 key
只负责轻量编排（docs/codex-integration.md）。

调用方式：codex exec --json --ephemeral --skip-git-repo-check
          -s workspace-write -C <workspace> -o <last_message_file> <task>
- codex 自带的 workspace-write 沙箱管它内部循环的命令；平台安全支柱是
  结构化收口 + danger 审批 + 审计，不依赖该沙箱（Windows 强制力未验证）
- --ephemeral：不在 ~/.codex 留 session 文件；--skip-git-repo-check：允许在
  任意目录跑（我们的 workspace 不一定是 git 根）
- -o：最终 agent 消息落文件，取回截断后回传；JSONL 事件流不回传（太长）
- stdin=DEVNULL：codex 会尝试读 stdin，必须堵死，否则可能挂起
"""

from __future__ import annotations

import contextlib
import shutil
import uuid
from pathlib import Path

from runner.config import RunnerConfig
from runner.procs import MAX_OUTPUT_CHARS, format_result, run_argv

MAX_TASK_CHARS = 8000

_NOT_INSTALLED_TEXT = (
    "[codex_not_installed] 本机未检测到 codex CLI。安装并登录后重试：\n"
    "  npm install -g @openai/codex\n"
    "  codex login\n"
    "（也可在 runner.yaml 的 codex.path 指定路径；或在 codex.enabled: false 关闭本工具）"
)


def detect_codex(config: RunnerConfig) -> str | None:
    """返回 codex 可执行文件路径；未启用或未安装返回 None。"""
    if not config.codex.enabled:
        return None
    return shutil.which(config.codex.path)


def run_codex_task(args: dict[str, object], workspace: Path, config: RunnerConfig) -> str:
    """执行一次 codex 委派。永不抛异常，返回给模型的文本。"""
    task = str(args.get("task", "")).strip()
    if not task:
        return "[参数无效] task 不能为空"
    if len(task) > MAX_TASK_CHARS:
        return f"[参数无效] task 过长（>{MAX_TASK_CHARS} 字符），请拆分任务"
    codex = detect_codex(config)
    if codex is None:
        if not config.codex.enabled:
            return "[codex_not_installed] codex 委派已在 runner.yaml 中关闭（codex.enabled: false）"
        return _NOT_INSTALLED_TEXT

    out_dir = workspace / ".yuan" / "codex"
    out_dir.mkdir(parents=True, exist_ok=True)
    last_message = out_dir / f"last_{uuid.uuid4().hex[:8]}.md"
    timeout = max(60, min(config.codex.timeout_seconds, 3600))

    argv = [
        codex, "exec",
        "--json", "--ephemeral", "--skip-git-repo-check",
        "-s", "workspace-write",
        "-C", str(workspace),
        "-o", str(last_message),
        task,
    ]
    # codex 的 HTTP 客户端会跟随系统代理（Clash 等常设 127.0.0.1:789x），但不识别
    # ProxyOverride 的 127.* 豁免——发往本地 provider（如 cc-switch 127.0.0.1:15721）
    # 的请求被代理转发到远端节点后 502。显式豁免回环地址即可；其他目标仍正常走代理。
    result = run_argv(argv, cwd=workspace, timeout=timeout, extra_env={
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    })

    message = ""
    if last_message.is_file():
        message = last_message.read_text(encoding="utf-8", errors="replace").strip()
        with contextlib.suppress(OSError):
            last_message.unlink(missing_ok=True)
    if not message and not result.timed_out:
        message = (result.stdout or "")[-2000:].strip()
    if result.timed_out:
        return (
            f"[执行超时] codex 任务超过 {timeout} 秒被终止（进程树已结束）。"
            f"可在 runner.yaml 的 codex.timeout_seconds 调大上限。\n{format_result(result)}"
        )
    if result.exit_code != 0:
        err_tail = (result.stderr or "")[-500:].strip()
        hint = f"\n[stderr 尾部]\n{err_tail}" if err_tail else ""
        return f"[codex 退出码 {result.exit_code}]\n{message[:MAX_OUTPUT_CHARS]}{hint}"
    if not message:
        return "（codex 无输出，exit 0）"
    if len(message) > MAX_OUTPUT_CHARS:
        message = message[:MAX_OUTPUT_CHARS] + f"\n…（输出超长，已截断至 {MAX_OUTPUT_CHARS} 字符）"
    return message


__all__ = ["MAX_TASK_CHARS", "detect_codex", "run_codex_task"]
