"""runner 配置：%LOCALAPPDATA%\\YuanAgent\\runner.yaml。

优先级：显式 --config 参数 > 环境变量 > 默认路径。
密钥（设备令牌）不进配置文件，走 Windows 凭据管理器（auth.py）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

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
class GitConfig:
    """git 结构化工具（git_clone / git_commit_push）的配置。"""

    # repo_url 主机白名单；空列表表示不限制（不建议）
    allowed_hosts: list[str] = field(default_factory=lambda: ["github.com"])
    # 浅克隆深度；0 = 完整克隆
    clone_depth: int = 0
    # git 命令超时（秒）
    timeout_seconds: int = 300
    # 兜底提交身份：仅当仓库/全局 git 未配置 user.name 时注入
    user_name: str = "YuanAgent"
    user_email: str = "yuanagent@local"


@dataclass(slots=True)
class DepsConfig:
    """依赖安装（install_deps）的配置。"""

    # 指定 Python 解释器绝对路径；空 = 自动探测 workspace 下 .venv，探测不到报错
    python: str = ""
    # 安装命令超时（秒）；npm install 大项目可能较慢
    timeout_seconds: int = 900


@dataclass(slots=True)
class CodexConfig:
    """codex 委派（delegate_coding）的配置。"""

    enabled: bool = True
    # codex 可执行文件；默认按 PATH 解析（npm 全局安装的 codex.cmd）
    path: str = "codex"
    # 单次委派超时（秒），上限 3600
    timeout_seconds: int = 600


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
    # 开发工具段（docs/codex-integration.md）
    git: GitConfig = field(default_factory=GitConfig)
    deps: DepsConfig = field(default_factory=DepsConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    extra: dict[str, str] = field(default_factory=dict)


def _expand(value: str, base: Path) -> str:
    text = value.strip()
    if text.startswith("~"):
        return str(Path(text).expanduser())
    if not Path(text).is_absolute():
        return str((base / text).resolve())
    return text


def _int_value(raw: object, minimum: int, default: int) -> int:
    """读取配置里的正整数（排除 bool），带下限钳制。"""
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return max(minimum, raw)
    return default


def _load_devtool_sections(config: RunnerConfig, raw: dict[str, object]) -> None:
    """解析 git / deps / codex 三段（缺省保持默认值）。"""
    git = raw.get("git")
    if isinstance(git, dict):
        hosts = git.get("allowed_hosts")
        if isinstance(hosts, list):
            config.git.allowed_hosts = [str(item).strip() for item in hosts if str(item).strip()]
        config.git.clone_depth = _int_value(git.get("clone_depth"), 0, config.git.clone_depth)
        config.git.timeout_seconds = _int_value(
            git.get("timeout_seconds"), 10, config.git.timeout_seconds
        )
        if isinstance(git.get("user_name"), str) and git["user_name"].strip():
            config.git.user_name = git["user_name"].strip()
        if isinstance(git.get("user_email"), str) and git["user_email"].strip():
            config.git.user_email = git["user_email"].strip()

    deps = raw.get("deps")
    if isinstance(deps, dict):
        if isinstance(deps.get("python"), str) and deps["python"].strip():
            config.deps.python = deps["python"].strip()
        config.deps.timeout_seconds = _int_value(
            deps.get("timeout_seconds"), 10, config.deps.timeout_seconds
        )

    codex = raw.get("codex")
    if isinstance(codex, dict):
        if isinstance(codex.get("enabled"), bool):
            config.codex.enabled = codex["enabled"]
        if isinstance(codex.get("path"), str) and codex["path"].strip():
            config.codex.path = codex["path"].strip()
        config.codex.timeout_seconds = _int_value(
            codex.get("timeout_seconds"), 60, config.codex.timeout_seconds
        )


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
    _load_devtool_sections(config, raw)
    return config


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
        "# 开发工具段（详见 docs/codex-integration.md）\n"
        "git:\n"
        "  allowed_hosts: [github.com]   # git_clone 允许的仓库主机\n"
        "  clone_depth: 0                # 0=完整克隆；>0 浅克隆\n"
        "deps:\n"
        "  python: \"\"                    # 空=自动探测工作目录下 .venv\n"
        "codex:\n"
        "  enabled: true\n"
        "  path: codex                   # npm 全局安装的 codex.cmd\n"
        "  timeout_seconds: 600\n"
    )
    resolved.write_text(content, encoding="utf-8")
    return resolved


def logs_dir() -> Path:
    return app_dir() / "logs"


__all__ = [
    "APP_DIR_NAME",
    "DEFAULT_SERVER_URL",
    "DEFAULT_WORKSPACE",
    "CodexConfig",
    "DepsConfig",
    "GitConfig",
    "RunnerConfig",
    "app_dir",
    "default_config_path",
    "load_config",
    "logs_dir",
    "write_default_config",
]
