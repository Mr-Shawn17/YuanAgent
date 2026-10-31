"""后端启动脚本：读取 config/agent.yaml 的 server 段，用配置的 Python 环境启动 uvicorn。

用法：
    python scripts/start_backend.py            # 默认热重载（--reload），适合开发
    python scripts/start_backend.py --no-reload  # 生产模式（不热重载）
    python scripts/start_backend.py --skip-check  # 跳过环境预检（不建议）

启动参数全部来自 config/agent.yaml 的 server 段：
    python_env     Python 环境根目录（如 D:/software/miniconda3/envs/py312），
                   脚本按平台拼接解释器路径；换机器/换 Linux 只需改这一处。
    backend_port   后端 API 监听端口，传入 uvicorn --port。

说明：
- 本脚本用配置的 Python 环境重新拉起 uvicorn 子进程（而非当前解释器），
  保证与 Makefile 里 `PY ?= .venv312/Scripts/python.exe` 的语义一致。
- 若 python_env 留空，回退到当前解释器（sys.executable）。
- 启动前自动执行环境预检（scripts/preflight.py）：必须项（配置/解释器/依赖/
  MySQL/LLM Key）任一失败立即退出；可选项（MCP Server）失败仅提示。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 不依赖 backend 包也能读配置：直接解析 YAML（仅取 server 段，避免引入完整依赖链）
try:
    import yaml
except ImportError:  # pragma: no cover - 解释器缺 pyyaml 时给出友好提示
    print("缺少依赖 PyYAML，请先安装：pip install pyyaml", file=sys.stderr)
    sys.exit(1)


def run_preflight() -> None:
    """启动前环境预检；必须项失败时直接终止（错误信息由 preflight 打印）。"""
    preflight = Path(__file__).resolve().parent / "preflight.py"
    probe = subprocess.run(
        [sys.executable, str(preflight), "--scope", "backend"],
        cwd=str(PROJECT_ROOT),
    )
    if probe.returncode != 0:
        print("环境预检未通过，终止启动（--skip-check 可跳过，不建议）。", file=sys.stderr)
        raise SystemExit(probe.returncode)


def _load_server_config() -> dict:
    """读取 config/agent.yaml 的 server 段，缺失时用默认值。"""
    path = PROJECT_ROOT / "config" / "agent.yaml"
    if not path.exists():
        print(f"警告：未找到配置文件 {path}，使用默认端口", file=sys.stderr)
        return {}
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    server = raw.get("server") or {}
    return server if isinstance(server, dict) else {}


def _resolve_python(python_env: str) -> str:
    """按平台拼接 Python 解释器路径；python_env 为空时回退当前解释器。"""
    if not python_env.strip():
        return sys.executable
    env_root = Path(python_env)
    if os.name == "nt":  # Windows
        candidate = env_root / "Scripts" / "python.exe"
    else:  # Linux / macOS
        candidate = env_root / "bin" / "python"
    if not candidate.exists():
        print(
            f"警告：Python 环境解释器不存在 {candidate}，回退到当前解释器 {sys.executable}",
            file=sys.stderr,
        )
        return sys.executable
    return str(candidate)


def main() -> int:
    parser = argparse.ArgumentParser(description="启动后端 API（uvicorn）")
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="关闭热重载（默认开启 --reload，适合开发）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument(
        "--skip-check",
        action="store_true",
        help="跳过启动前环境预检（不建议）",
    )
    args = parser.parse_args()

    if not args.skip_check:
        run_preflight()

    server = _load_server_config()
    python_env = str(server.get("python_env", "") or "")
    backend_port = int(server.get("backend_port", 8000))

    python = _resolve_python(python_env)
    cmd = [
        python,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        args.host,
        "--port",
        str(backend_port),
    ]
    if not args.no_reload:
        cmd.append("--reload")

    print(f"后端启动中：端口 {backend_port}，解释器 {python}")
    print(" ".join(cmd))

    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT / "backend")
    try:
        return subprocess.call(cmd, cwd=str(PROJECT_ROOT), env=env)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
