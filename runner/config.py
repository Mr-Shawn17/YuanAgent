"""runner 配置：%LOCALAPPDATA%\\YuanAgent\\runner.yaml。

优先级：显式 --config 参数 > 环境变量 > 默认路径。
密钥（设备令牌）不进配置文件，走 Windows 凭据管理器（auth.py）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from runner.command import CommandPolicy

APP_DIR_NAME = "YuanAgent"
CONFIG_NAME = "runner.yaml"
DEFAULT_SERVER_URL = "ws://127.0.0.1:8000/ws/runner"
DEFAULT_WORKSPACE = "~/YuanAgent-workspace"


def app_dir() -> Path:
    """配置根目录：%LOCALAPPDATA%\\YuanAgent（非 Windows 回退 ~）。

    注意：打包成 exe 后 sys.frozen 场景下 LOCALAPPDATA 依然可用（用户级目录）。
    """
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_DIR_NAME


def default_config_path() -> Path:
    return app_dir() / CONFIG_NAME


@dataclass(slots=True)
class RunnerConfig:
    """runner.yaml 的结构。"""

    server_url: str = DEFAULT_SERVER_URL
    # 工作目录沙箱根：支持 ~ 展开；相对路径按配置文件所在目录解析
    workspace: str = DEFAULT_WORKSPACE
    # 心跳间隔（秒）：每 interval 发一次 ping，连续 3 次未收 pong 断线重连
    heartbeat_seconds: float = 30.0
    # 重连退避：initial → max（秒），指数增长
    reconnect_initial: float = 1.0
    reconnect_max: float = 60.0
    # 命令执行策略（run_command / git_* / delegate_coding，见 runner/command.py）
    command: CommandPolicy = field(default_factory=CommandPolicy)
    extra: dict[str, str] = field(default_factory=dict)


def _expand(value: str, base: Path) -> str:
    text = value.strip()
    if text.startswith("~"):
        return str(Path(text).expanduser())
    if not Path(text).is_absolute():
        return str((base / text).resolve())
    return text


def load_config(path: Path | None = None) -> RunnerConfig:
    """加载配置；文件不存在时返回默认值（首次运行由用户或引导写入）。"""
    resolved = path or default_config_path()
    config = RunnerConfig()
    if not resolved.exists():
        return config
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return config
    base = resolved.parent
    if isinstance(raw.get("server_url"), str):
        config.server_url = raw["server_url"].strip() or DEFAULT_SERVER_URL
    if isinstance(raw.get("workspace"), str):
        config.workspace = _expand(raw["workspace"], base)
    if isinstance(raw.get("heartbeat_seconds"), (int, float)):
        config.heartbeat_seconds = max(5.0, float(raw["heartbeat_seconds"]))
    if isinstance(raw.get("reconnect_initial"), (int, float)):
        config.reconnect_initial = max(0.5, float(raw["reconnect_initial"]))
    if isinstance(raw.get("reconnect_max"), (int, float)):
        config.reconnect_max = max(config.reconnect_initial, float(raw["reconnect_max"]))
    raw_command = raw.get("command")
    if isinstance(raw_command, dict):
        _apply_command_section(config.command, raw_command)
    return config


def _apply_command_section(policy: CommandPolicy, section: dict[str, object]) -> None:
    """防御式解析 command 段（类型不符的字段忽略，不炸启动）。"""
    if isinstance(section.get("backend"), str):
        policy.backend = section["backend"].strip().lower() or "auto"
    if isinstance(section.get("shell"), str):
        policy.shell = section["shell"].strip().lower() or "cmd"
    if isinstance(section.get("allowed_commands"), list):
        names = [
            str(item).strip().lower()
            for item in section["allowed_commands"]
            if str(item).strip()
        ]
        if names:
            policy.allowed_commands = names
    if isinstance(section.get("blocked_patterns"), list):
        patterns = [
            str(item) for item in section["blocked_patterns"] if str(item).strip()
        ]
        if patterns:
            policy.blocked_patterns = patterns
    if isinstance(section.get("env_whitelist"), list):
        policy.env_whitelist = [
            str(item).strip() for item in section["env_whitelist"] if str(item).strip()
        ]
    if isinstance(section.get("timeout_seconds"), int):
        policy.timeout_seconds = max(5, int(section["timeout_seconds"]))
    if isinstance(section.get("max_timeout_seconds"), int):
        policy.max_timeout_seconds = max(
            policy.timeout_seconds, int(section["max_timeout_seconds"])
        )
    if isinstance(section.get("max_output_chars"), int):
        policy.max_output_chars = max(1000, min(int(section["max_output_chars"]), 200_000))
    if isinstance(section.get("sandbox_tools_dir"), str):
        policy.sandbox_tools_dir = _expand(section["sandbox_tools_dir"], app_dir()).strip()
    raw_git = section.get("git")
    if isinstance(raw_git, dict):
        if isinstance(raw_git.get("user_name"), str):
            policy.git_user_name = raw_git["user_name"].strip() or policy.git_user_name
        if isinstance(raw_git.get("user_email"), str):
            policy.git_user_email = raw_git["user_email"].strip() or policy.git_user_email
    if isinstance(section.get("codex_enabled"), bool):
        policy.codex_enabled = section["codex_enabled"]
    if isinstance(section.get("codex_path"), str):
        policy.codex_path = section["codex_path"].strip() or "codex"
    if isinstance(section.get("codex_timeout_seconds"), int):
        policy.codex_timeout_seconds = max(60, min(int(section["codex_timeout_seconds"]), 3600))


def write_default_config(path: Path | None = None, *, overwrite: bool = False) -> Path:
    """写入示例配置（首次安装引导用）。"""
    resolved = path or default_config_path()
    if resolved.exists() and not overwrite:
        return resolved
    resolved.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# YuanRunner 配置\n"
        f"server_url: {DEFAULT_SERVER_URL}\n"
        f"workspace: {DEFAULT_WORKSPACE}\n"
        "heartbeat_seconds: 30\n"
        "reconnect_initial: 1\n"
        "reconnect_max: 60\n"
        "\n"
        "# 命令执行策略（run_command / git_* / delegate_coding）\n"
        "# backend: auto | shell | windows_sandbox（auto=沙盒功能启用时用 VM 档）\n"
        "# 白名单/黑名单默认值见 runner/command.py，此处按需覆盖扩展\n"
        "command:\n"
        "  backend: auto\n"
        "  shell: cmd\n"
        "  timeout_seconds: 120\n"
        "  # allowed_commands: [git, python, node]\n"
        "  # blocked_patterns: []\n"
        "  git:\n"
        "    user_name: YuanAgent\n"
        "    user_email: yuanagent@local\n"
        "  codex_enabled: true\n"
    )
    resolved.write_text(content, encoding="utf-8")
    return resolved


def logs_dir() -> Path:
    return app_dir() / "logs"


__all__ = [
    "APP_DIR_NAME",
    "DEFAULT_SERVER_URL",
    "DEFAULT_WORKSPACE",
    "RunnerConfig",
    "app_dir",
    "default_config_path",
    "load_config",
    "logs_dir",
    "write_default_config",
]
