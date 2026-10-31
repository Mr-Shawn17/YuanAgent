"""前端启动脚本：读取 config/agent.yaml 的 server 段，启动 Next.js 开发服务器。

用法：
    python scripts/start_frontend.py
    python scripts/start_frontend.py --skip-check   # 跳过环境预检（不建议）

启动参数全部来自 config/agent.yaml 的 server 段：
    frontend_port  前端 dev 服务器监听端口，传入 next dev -p。
    backend_port   后端 API 端口，注入环境变量 BACKEND_ORIGIN 供 Next.js rewrites 代理。

日志：
- npm 子进程的全部输出（编译状态/报错堆栈）实时 tee 到项目根 log/frontend-日期.log，
  同时打印到终端；启动时自动清理超过保留期的旧日志（默认 7 天，可在 agent.yaml 的
  runtime.log_retention_days 调整）。

说明：
- 前端通过 Next.js rewrites 把 /v1/* 代理到后端（见 frontend/next.config.ts 的
  BACKEND_ORIGIN），因此这里必须把 backend_port 同步注入，否则代理指向错误端口。
- npm / node 需在 PATH 中；Windows 下用 npm.cmd。
- 启动前自动执行环境预检（scripts/preflight.py）：必须项（Node/npm/前端依赖等）
  任一失败立即退出；可选项（MCP Server）失败仅提示。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = PROJECT_ROOT / "frontend"
LOG_DIR = PROJECT_ROOT / "log"
LOG_PREFIX = "frontend-"
DEFAULT_RETENTION_DAYS = 7

try:
    import yaml
except ImportError:  # pragma: no cover
    print("缺少依赖 PyYAML，请先安装：pip install pyyaml", file=sys.stderr)
    sys.exit(1)


def run_preflight() -> None:
    """启动前环境预检；必须项失败时直接终止（错误信息由 preflight 打印）。"""
    preflight = Path(__file__).resolve().parent / "preflight.py"
    probe = subprocess.run(
        [sys.executable, str(preflight), "--scope", "frontend"],
        cwd=str(PROJECT_ROOT),
    )
    if probe.returncode != 0:
        print("环境预检未通过，终止启动（--skip-check 可跳过，不建议）。", file=sys.stderr)
        raise SystemExit(probe.returncode)


def _load_server_config() -> dict:
    path = PROJECT_ROOT / "config" / "agent.yaml"
    if not path.exists():
        print(f"警告：未找到配置文件 {path}，使用默认端口", file=sys.stderr)
        return {}
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    server = raw.get("server") or {}
    return server if isinstance(server, dict) else {}


def _npm_cmd() -> str:
    """Windows 用 npm.cmd，其余平台用 npm。"""
    return "npm.cmd" if os.name == "nt" else "npm"


def _load_runtime() -> dict:
    """读取 config/agent.yaml 的 runtime 段（日志保留天数等）。"""
    path = PROJECT_ROOT / "config" / "agent.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    runtime = raw.get("runtime") or {}
    return runtime if isinstance(runtime, dict) else {}


def _cleanup_old_frontend_logs(retention_days: int) -> None:
    """删除超过保留期的前端日志文件（按文件名日期字典序比较，失败静默）。"""
    if retention_days <= 0 or not LOG_DIR.exists():
        return
    cutoff = _dt.datetime.now() - _dt.timedelta(days=retention_days)
    cutoff_name = f"{LOG_PREFIX}{cutoff.strftime('%Y-%m-%d')}"
    try:
        for file in LOG_DIR.glob(f"{LOG_PREFIX}*.log"):
            if file.name <= cutoff_name:
                file.unlink(missing_ok=True)
    except OSError:
        pass


def _run_with_log(cmd: list[str], log_file: Path, env: dict) -> int:
    """跑子进程并把 stdout/stderr 实时 tee 到日志文件（同时打印终端）。"""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        cmd,
        cwd=str(FRONTEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    with log_file.open("a", encoding="utf-8", errors="replace") as log:
        log.write(f"\n===== 启动 {_dt.datetime.now().isoformat(timespec='seconds')} =====\n")
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace")
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
    return proc.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description="启动前端（Next.js dev）")
    parser.add_argument(
        "--skip-check",
        action="store_true",
        help="跳过启动前环境预检（不建议）",
    )
    args = parser.parse_args()

    if not args.skip_check:
        run_preflight()

    server = _load_server_config()
    frontend_port = int(server.get("frontend_port", 3000))
    backend_port = int(server.get("backend_port", 8000))

    if not (FRONTEND_DIR / "package.json").exists():
        print(f"错误：未找到前端工程 {FRONTEND_DIR}，请先确认 frontend/ 目录存在", file=sys.stderr)
        return 1

    npm = _npm_cmd()
    if shutil.which(npm) is None:
        print("错误：未在 PATH 中找到 npm，请先安装 Node.js", file=sys.stderr)
        return 1

    env = os.environ.copy()
    # Next.js rewrites 代理目标：后端地址（从配置的 backend_port 推导）
    env["BACKEND_ORIGIN"] = f"http://127.0.0.1:{backend_port}"

    cmd = [npm, "run", "dev", "--", "-p", str(frontend_port)]

    # 日志文件：log/frontend-日期.log，启动前清理过期文件
    retention = int(_load_runtime().get("log_retention_days", DEFAULT_RETENTION_DAYS))
    _cleanup_old_frontend_logs(retention)
    log_file = LOG_DIR / f"{LOG_PREFIX}{_dt.date.today().isoformat()}.log"

    print(f"前端启动中：端口 {frontend_port}，后端代理 {env['BACKEND_ORIGIN']}")
    print(f"日志文件：{log_file}")
    print(" ".join(cmd))

    try:
        return _run_with_log(cmd, log_file, env)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
