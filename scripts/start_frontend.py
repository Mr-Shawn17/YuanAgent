"""前端启动脚本：读取 config/agent.yaml 的 server 段，启动 Next.js 开发服务器。

用法：
    python scripts/start_frontend.py

启动参数全部来自 config/agent.yaml 的 server 段：
    frontend_port  前端 dev 服务器监听端口，传入 next dev -p。
    backend_port   后端 API 端口，注入环境变量 BACKEND_ORIGIN 供 Next.js rewrites 代理。

说明：
- 前端通过 Next.js rewrites 把 /v1/* 代理到后端（见 frontend/next.config.ts 的
  BACKEND_ORIGIN），因此这里必须把 backend_port 同步注入，否则代理指向错误端口。
- npm / node 需在 PATH 中；Windows 下用 npm.cmd。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = PROJECT_ROOT / "frontend"

try:
    import yaml
except ImportError:  # pragma: no cover
    print("缺少依赖 PyYAML，请先安装：pip install pyyaml", file=sys.stderr)
    sys.exit(1)


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


def main() -> int:
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

    print(f"前端启动中：端口 {frontend_port}，后端代理 {env['BACKEND_ORIGIN']}")
    print(" ".join(cmd))

    try:
        return subprocess.call(cmd, cwd=str(FRONTEND_DIR), env=env)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
