"""YuanRunner 入口：python -m runner [run|set-token|status|init-config]。

run 为主命令（也是无参默认）：单实例锁 → 日志 → 配置 → 连接循环。
打包成 exe 后双击即执行 run（无窗口，日志落文件）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from runner import __version__
from runner.auth import clear_token, set_token, token_status
from runner.config import RunnerConfig, default_config_path, load_config, write_default_config
from runner.connection import run_forever
from runner.gitops import delete_git_cred, get_git_cred, set_git_cred
from runner.logging_setup import setup_logging
from runner.singleton import SingleInstanceError, SingleInstanceLock


def _configure_stdio() -> None:
    """把 stdout/stderr 切到 UTF-8，避免 Windows 控制台 GBK 编码导致中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _cmd_run(args: argparse.Namespace) -> int:
    logger = setup_logging(debug=args.debug)
    lock = SingleInstanceLock()
    try:
        lock.acquire()
    except SingleInstanceError as exc:
        logger.warning("another_instance_running: %s", exc)
        print(str(exc), file=sys.stderr)
        return 0  # 非错误退出：双击场景不弹崩溃对话框
    try:
        config = load_config(args.config)
        try:
            asyncio.run(run_forever(config))
        except KeyboardInterrupt:
            logger.info("stopped_by_user")
        return 0
    finally:
        lock.close()


def _cmd_set_token(args: argparse.Namespace) -> int:
    token = args.token.strip()
    if not token:
        print("用法：python -m runner set-token <设备令牌>", file=sys.stderr)
        return 1
    set_token(token)
    print("✓ 令牌已写入系统凭据库")
    return 0


def _cmd_status(_: argparse.Namespace) -> int:
    config: RunnerConfig = load_config(None)
    print(f"版本：{__version__}")
    print(f"配置文件：{default_config_path()}")
    print(f"服务地址：{config.server_url}")
    print(f"工作目录：{config.workspace}")
    print(f"令牌来源：{token_status()}")
    print(f"命令后端：{config.command.backend}（shell={config.command.shell}）")
    return 0


def _cmd_set_git_cred(args: argparse.Namespace) -> int:
    import getpass

    host = args.host.strip().lower()
    if not host:
        print("用法：python -m runner set-git-cred <host>（如 github.com）", file=sys.stderr)
        return 1
    username = (args.username or input(f"用户名（{host}）：")).strip()
    pat = getpass.getpass(f"Personal Access Token（{host}，输入不回显）：").strip()
    try:
        set_git_cred(host, username, pat)
    except Exception as exc:
        print(f"✗ 保存失败：{exc}", file=sys.stderr)
        return 1
    print(f"✓ 已保存 {host} 的 git 凭证到系统凭据库（仅本机，不会上传服务器）")
    return 0


def _cmd_clear_git_cred(args: argparse.Namespace) -> int:
    host = args.host.strip().lower()
    if get_git_cred(host) is None:
        print(f"（{host} 无已存凭证）")
        return 0
    print("✓ 已删除凭证" if delete_git_cred(host) else f"✗ 删除失败（{host}）")
    return 0


def _cmd_init_config(args: argparse.Namespace) -> int:
    path = write_default_config(args.config)
    print(f"✓ 已生成示例配置：{path}")
    print("  请按需修改 server_url 与 workspace 后再启动。")
    return 0


def _cmd_clear_token(_: argparse.Namespace) -> int:
    print("✓ 已清除令牌" if clear_token() else "（无已存令牌）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runner", description="YuanAgent 本地执行器（YuanRunner）"
    )
    parser.add_argument("--version", action="version", version=f"yuan-runner {__version__}")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="启动执行器（默认命令，双击 exe 即此）")
    run_p.add_argument("--config", type=str, default=None, help="配置文件路径（默认 LOCALAPPDATA）")
    run_p.add_argument("--debug", action="store_true", help="调试日志级别")
    run_p.set_defaults(func=_cmd_run)

    token_p = sub.add_parser("set-token", help="保存设备令牌到系统凭据库")
    token_p.add_argument("token", help="app device create 签发的令牌（yrt_ 开头）")
    token_p.set_defaults(func=_cmd_set_token)

    gitcred_p = sub.add_parser(
        "set-git-cred", help="保存某 git host 的 HTTPS 凭证（用户名+PAT，私有仓库 clone/push 用）"
    )
    gitcred_p.add_argument("host", help="git 主机名，如 github.com / gitlab.com")
    gitcred_p.add_argument("--username", "-u", default="", help="用户名（不传则交互输入）")
    gitcred_p.set_defaults(func=_cmd_set_git_cred)

    gitclear_p = sub.add_parser("clear-git-cred", help="删除某 git host 的已存凭证")
    gitclear_p.add_argument("host", help="git 主机名")
    gitclear_p.set_defaults(func=_cmd_clear_git_cred)

    sub.add_parser("clear-token", help="删除已保存的令牌").set_defaults(func=_cmd_clear_token)
    sub.add_parser("status", help="显示配置与令牌状态").set_defaults(func=_cmd_status)

    init_p = sub.add_parser("init-config", help="生成示例配置文件")
    init_p.add_argument("--config", type=str, default=None)
    init_p.set_defaults(func=_cmd_init_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        # 无子命令时默认 run（双击 exe 场景）
        args = parser.parse_args([*(argv or []), "run"] if argv else ["run"])
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
