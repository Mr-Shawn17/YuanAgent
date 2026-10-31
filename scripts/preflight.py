"""服务启动前环境预检（start_backend.py / start_frontend.py 共用）。

检查项分两档：
- 必须项（required）：任一失败 → 红字 ERROR + 立即退出（服务不值得启动）
- 可选项（optional）：失败 → 黄字 WARN 提示，不阻断启动（运行期有隔离机制兜底）

必须项：
  R1 config/agent.yaml 存在
  R2 server.python_env 解释器存在（留空回退当前解释器）
  R3 核心依赖已安装（uvicorn / fastmcp / langgraph / fastapi 抽样 import，用配置解释器执行）
  R4 MySQL 可连接（用配置的 host/port/user/password 真实试连）
  R5 LLM API Key 环境变量已设置（按 runtime.llm_api_key_env 指向的变量或回退链）
  R6 仅前端：Node.js / npm 在 PATH 且 frontend/node_modules 已 install

可选项（单 server 失败会被 McpManager 隔离，故只提示）：
  O1 每个 enabled 的 MCP Server 真实握手（stdio：拉起探测；streamable_http：HTTP 连接）

深度探测（R3/R4/O1）为避免当前解释器缺依赖，统一以「配置的解释器」跑子进程 probe。
用法：
    python scripts/preflight.py --scope backend          # 后端启动前全套检查
    python scripts/preflight.py --scope frontend         # 前端启动前全套检查
    python scripts/preflight.py --probe-deps <python>    # 子进程：依赖导入探测
    python scripts/preflight.py --probe-mysql            # 子进程：MySQL 试连（读 agent.yaml）
    python scripts/preflight.py --probe-mcp              # 子进程：MCP 全部 enabled server 握手
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "agent.yaml"
MCP_CONFIG_PATH = PROJECT_ROOT / "config" / "mcp.yaml"

try:
    import yaml
except ImportError:  # pragma: no cover
    print("缺少依赖 PyYAML，请先安装：pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# ============ ANSI 颜色（Windows 10+ 终端原生支持；不支持时自动降级） ============


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.name == "nt":
        return True  # Windows Terminal / VSCode / PowerShell 7 均支持 ANSI
    return sys.stdout.isatty()


_COLOR_OK = _supports_color()

RESET = "\033[0m" if _COLOR_OK else ""
RED = "\033[31m" if _COLOR_OK else ""
GREEN = "\033[32m" if _COLOR_OK else ""
YELLOW = "\033[33m" if _COLOR_OK else ""
CYAN = "\033[36m" if _COLOR_OK else ""
BOLD = "\033[1m" if _COLOR_OK else ""


def ok(msg: str) -> str:
    return f"{GREEN}{msg}{RESET}"


def warn(msg: str) -> str:
    return f"{YELLOW}{msg}{RESET}"


def err(msg: str) -> str:
    return f"{RED}{msg}{RESET}"


# ============ 检查结果模型 ============


@dataclass
class CheckResult:
    name: str  # 检查项标识，如 "MySQL 连接"
    required: bool  # True=必须项，False=可选项
    passed: bool
    detail: str  # 成功时的摘要 / 失败时的原因
    fix_hint: str = ""  # 失败时的修复建议


class PreflightError(Exception):
    """必须项未通过：调用方捕获后应以非零码退出。"""


# ============ 配置读取 ============


def load_agent_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise PreflightError(
            f"配置文件不存在：{CONFIG_PATH}\n"
            f"  修复：cp config/agent.yaml.example config/agent.yaml 后按需填写"
        )
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise PreflightError(f"配置文件格式错误（顶层不是映射）：{CONFIG_PATH}")
    return raw


def load_mcp_config() -> dict[str, Any]:
    if not MCP_CONFIG_PATH.exists():
        return {}
    with MCP_CONFIG_PATH.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return raw if isinstance(raw, dict) else {}


def resolve_python(cfg: dict[str, Any]) -> str:
    """server.python_env → 平台相关解释器绝对路径；留空回退当前解释器。"""
    env_root = str((cfg.get("server") or {}).get("python_env", "") or "").strip()
    if not env_root:
        return sys.executable
    candidate = (
        Path(env_root) / "Scripts" / "python.exe"
        if os.name == "nt"
        else Path(env_root) / "bin" / "python"
    )
    return str(candidate) if candidate.exists() else ""


# ============ 必须项检查 ============


def check_config_exists() -> CheckResult:
    """R1：agent.yaml 存在。"""
    if CONFIG_PATH.exists():
        return CheckResult("配置文件 agent.yaml", True, True, str(CONFIG_PATH))
    return CheckResult(
        "配置文件 agent.yaml",
        True,
        False,
        f"未找到 {CONFIG_PATH}",
        "cp config/agent.yaml.example config/agent.yaml，然后填写 mysql_password 与模型配置",
    )


def check_python_interpreter(cfg: dict[str, Any]) -> tuple[CheckResult, str]:
    """R2：python_env 解释器存在。返回 (结果, 解释器路径)。"""
    env_root = str((cfg.get("server") or {}).get("python_env", "") or "").strip()
    if not env_root:
        return (
            CheckResult(
                "Python 解释器（server.python_env 留空，回退当前）",
                True,
                True,
                sys.executable,
            ),
            sys.executable,
        )
    candidate = (
        Path(env_root) / "Scripts" / "python.exe"
        if os.name == "nt"
        else Path(env_root) / "bin" / "python"
    )
    if candidate.exists():
        return (
            CheckResult("Python 解释器（server.python_env）", True, True, str(candidate)),
            str(candidate),
        )
    return (
        CheckResult(
            "Python 解释器（server.python_env）",
            True,
            False,
            f"解释器不存在：{candidate}",
            "检查 config/agent.yaml 的 server.python_env 是否指向有效的虚拟环境根目录",
        ),
        "",
    )


def _run_probe(
    cmd: list[str],
    *,
    timeout: int,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """跑子进程探测，容错解码：优先 UTF-8，失败回退 GBK（Windows 中文环境报错常为 GBK）。"""
    env = {**os.environ, **(extra_env or {})}
    raw = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        cwd=str(PROJECT_ROOT),
        env=env,
    )

    def _decode(data: bytes) -> str:
        for encoding in ("utf-8", "gbk", "latin-1"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")  # pragma: no cover

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=raw.returncode,
        stdout=_decode(raw.stdout or b""),
        stderr=_decode(raw.stderr or b""),
    )


def check_core_deps(python: str) -> CheckResult:
    """R3：核心依赖抽样 import（uvicorn/fastmcp/langchain/fastapi/langmem）。"""
    probe = _run_probe([python, __file__, "--probe-deps", python], timeout=120)
    if probe.returncode == 0:
        missing = probe.stdout.strip()
        detail = "核心依赖均可导入" if not missing else f"已装（缺可选依赖：{missing}）"
        return CheckResult("核心依赖（uvicorn/fastmcp/langgraph/…）", True, True, detail)
    first_line = (probe.stderr.strip().splitlines() or ["未知错误"])[0]
    return CheckResult(
        "核心依赖（uvicorn/fastmcp/langgraph/…）",
        True,
        False,
        first_line,
        f'{python} -m pip install -e ".[dev,tools,memory]"',
    )


def check_mysql(python: str, cfg: dict[str, Any]) -> CheckResult:
    """R4：MySQL 真实试连（子进程，用配置解释器，读 runtime 段的连接参数）。"""
    runtime = cfg.get("runtime") or {}
    host = str(runtime.get("mysql_host", "127.0.0.1"))
    port = int(runtime.get("mysql_port", 3306))
    # 先做廉价 TCP 探测（快速失败，避免 asyncio 连接超时挂太久）
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
    except OSError as exc:
        return CheckResult(
            f"MySQL 连接（{host}:{port}）",
            True,
            False,
            f"TCP 不可达：{exc}",
            "确认 MySQL 服务已启动，且 agent.yaml 的 mysql_host/mysql_port 正确",
        )
    # TCP 通了再做真实认证连接
    probe = _run_probe(
        [python, __file__, "--probe-mysql"],
        timeout=30,
        extra_env={"PYTHONPATH": str(PROJECT_ROOT / "backend")},
    )
    if probe.returncode == 0:
        version = probe.stdout.strip().splitlines()[0] if probe.stdout.strip() else ""
        return CheckResult(f"MySQL 连接（{host}:{port}）", True, True, f"认证成功 {version}")
    first_line = (probe.stderr.strip().splitlines() or ["未知错误"])[0]
    return CheckResult(
        f"MySQL 连接（{host}:{port}）",
        True,
        False,
        first_line,
        "检查 agent.yaml 的 mysql_user/mysql_password；"
        "或 mysql_password 留空时确认环境变量 MYSQL_PASSWORD 已设置",
    )


def check_llm_key(cfg: dict[str, Any]) -> CheckResult:
    """R5：LLM API Key 环境变量已设置。"""
    runtime = cfg.get("runtime") or {}
    env_field = str(runtime.get("llm_api_key_env", "") or "").strip()
    if env_field:
        value = os.environ.get(env_field, "").strip()
        if value:
            return CheckResult(
                "LLM API Key 环境变量", True, True, f"{env_field} 已设置（{len(value)} 字符）"
            )
        model = str(runtime.get("llm_model", ""))
        return CheckResult(
            "LLM API Key 环境变量",
            True,
            False,
            f"环境变量 {env_field} 未设置或为空（llm_api_key_env 指向它）",
            f"设置环境变量 {env_field}=sk-你的密钥（模型 {model}）",
        )
    # 留空走回退链：DASHSCOPE_API_KEY → OPENAI_API_KEY → GLM_API_KEY
    fallbacks = ("DASHSCOPE_API_KEY", "OPENAI_API_KEY", "GLM_API_KEY")
    for name in fallbacks:
        if os.environ.get(name, "").strip():
            return CheckResult(
                "LLM API Key 环境变量（回退链）", True, True, f"{name} 已设置"
            )
    return CheckResult(
        "LLM API Key 环境变量",
        True,
        False,
        f"llm_api_key_env 未配置，且回退链 {fallbacks} 均未设置",
        "在 agent.yaml 的 runtime.llm_api_key_env 填环境变量名，或直接设置上述任一环境变量",
    )


def check_node_frontend() -> list[CheckResult]:
    """R6：仅前端 —— Node/npm 在 PATH、node_modules 已 install。"""
    results = []
    node = shutil.which("node")
    results.append(
        CheckResult(
            "Node.js（PATH）",
            True,
            node is not None,
            node or "未找到 node 命令",
            "安装 Node.js 18+ 并加入 PATH",
        )
    )
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    results.append(
        CheckResult(
            "npm（PATH）",
            True,
            npm is not None,
            npm or "未找到 npm 命令",
            "Node.js 自带 npm，检查 PATH",
        )
    )
    node_modules = PROJECT_ROOT / "frontend" / "node_modules"
    results.append(
        CheckResult(
            "前端依赖（frontend/node_modules）",
            True,
            node_modules.exists(),
            "已安装" if node_modules.exists() else "不存在",
            "cd frontend && npm install",
        )
    )
    return results


# ============ 可选项检查（MCP 握手探测） ============


def check_mcp_servers(python: str) -> list[CheckResult]:
    """O1：全部 enabled MCP Server 真实握手（子进程）。"""
    mcp_cfg = load_mcp_config()
    servers = mcp_cfg.get("servers") or []
    enabled = [s for s in servers if isinstance(s, dict) and s.get("enabled")]
    if not enabled:
        return [CheckResult("MCP Servers", False, True, "无启用的 MCP Server")]
    probe = _run_probe(
        [python, __file__, "--probe-mcp"],
        timeout=180,
        extra_env={"PYTHONPATH": str(PROJECT_ROOT / "backend")},
    )
    statuses: dict[str, str] = {}
    try:
        statuses = json.loads(probe.stdout) if probe.stdout.strip() else {}
        if not isinstance(statuses, dict):
            statuses = {}
    except json.JSONDecodeError:
        statuses = {}
    results = []
    for server in enabled:
        name = str(server.get("name", "?"))
        # 状态值：ok:工具数 / fail:原因 / unknown:探测异常
        status = statuses.get(name, f"unknown:探测无返回（exit={probe.returncode}）")
        passed = status.startswith("ok")
        results.append(
            CheckResult(
                f"MCP Server「{name}」",
                False,
                passed,
                status.split(":", 1)[1] if ":" in status else status,
                "" if passed else "该 Server 启动后会被隔离，不影响其余功能；需要时修复后重启",
            )
        )
    return results


# ============ 子进程探测入口 ============


def probe_deps() -> int:
    """以当前解释器 import 核心依赖；stdout 输出缺失列表（可缺的可选项）。"""
    required = ["uvicorn", "fastmcp", "langchain", "langgraph", "fastapi", "yaml", "pydantic"]
    optional = {"langmem": "memory 可选组"}
    missing_required = []
    for mod in required:
        try:
            __import__(mod)
        except ImportError:
            missing_required.append(mod)
    if missing_required:
        print(f"缺少必需依赖：{', '.join(missing_required)}", file=sys.stderr)
        return 1
    missing_optional = []
    for mod in optional:
        try:
            __import__(mod)
        except ImportError:
            missing_optional.append(f"{mod}({optional[mod]})")
    print(", ".join(missing_optional))
    return 0


def probe_mysql() -> int:
    """以当前解释器真实连接 MySQL（连接参数读 agent.yaml，密钥回退 MYSQL_PASSWORD）。"""
    import asyncio

    with CONFIG_PATH.open(encoding="utf-8") as handle:
        runtime = (yaml.safe_load(handle) or {}).get("runtime") or {}
    host = str(runtime.get("mysql_host", "127.0.0.1"))
    port = int(runtime.get("mysql_port", 3306))
    user = str(runtime.get("mysql_user", "root"))
    password = str(runtime.get("mysql_password", "") or "") or os.environ.get(
        "MYSQL_PASSWORD", ""
    )
    database = str(runtime.get("mysql_database", "agent"))

    async def _connect() -> str:
        import asyncmy

        conn = await asyncmy.connect(
            host=host, port=port, user=user, password=password, db=database
        )
        try:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT VERSION()")
                row = await cursor.fetchone()
                return str(row[0]) if row else "?"
        finally:
            conn.close()

    try:
        version = asyncio.run(_connect())
    except ImportError as exc:
        print(f"缺少依赖 asyncmy：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # 统一转成失败原因文本
        print(f"认证/连接失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"MySQL {version} @ {host}:{port}/{database}")
    return 0


def probe_mcp() -> int:
    """以当前解释器对全部 enabled MCP Server 做真实握手，stdout 输出 JSON 结果。"""
    import asyncio

    async def _handshake(server: dict[str, Any]) -> tuple[str, str]:
        name = str(server.get("name", "?"))
        transport = str(server.get("transport", "stdio"))
        try:
            if transport == "streamable_http":
                from fastmcp import Client
                from fastmcp.client.transports import StreamableHttpTransport

                t: StreamableHttpTransport | StdioTransport = StreamableHttpTransport(
                    url=str(server.get("url", "")),
                    headers=server.get("headers") or None,
                )
                timeout_s = float(server.get("timeout_seconds", 30))
                async with Client(t) as client:
                    tools = await asyncio.wait_for(client.list_tools(), timeout=timeout_s)
                    return name, f"ok:{len(tools)} 个工具"
            # stdio：本地子进程（如 mock）
            from fastmcp import Client
            from fastmcp.client.transports import StdioTransport

            command = str(server.get("command", "python"))
            if command.lower() in {"python", "python3", "python.exe", "py"}:
                command = sys.executable
            args = [str(a) for a in (server.get("args") or [])]
            resolved_args = []
            for item in args:
                candidate = Path(item)
                is_script = candidate.suffix in {".py", ".js", ".mjs", ".cjs", ".ts"}
                if is_script and not candidate.is_absolute():
                    item = str((PROJECT_ROOT / candidate).resolve())
                resolved_args.append(item)
            t = StdioTransport(
                command=command,
                args=resolved_args,
                cwd=str(PROJECT_ROOT),
                env=server.get("env") or None,
            )
            async with Client(t) as client:
                tools = await asyncio.wait_for(client.list_tools(), timeout=30)
                return name, f"ok:{len(tools)} 个工具"
        except Exception as exc:
            return name, f"fail:{type(exc).__name__}: {str(exc)[:120]}"

    async def _run() -> int:
        mcp_cfg = load_mcp_config()
        servers = [
            s
            for s in (mcp_cfg.get("servers") or [])
            if isinstance(s, dict) and s.get("enabled")
        ]
        statuses: dict[str, str] = {}
        # 串行探测：避免并发握手对同一远端的连接数压力；单个失败继续下一个
        for server in servers:
            name, status = await _handshake(server)
            statuses[name] = status
        print(json.dumps(statuses, ensure_ascii=False))
        return 0

    return asyncio.run(_run())


# ============ 主流程：跑检查 + 打印表格 ============


def run_preflight(scope: str) -> None:
    """scope: backend / frontend。必须项失败抛 PreflightFailure。"""
    started = time.time()
    print(f"{BOLD}{'=' * 62}{RESET}")
    print(f"{BOLD} 环境预检（scope={scope}）{RESET}")
    print(f"{BOLD}{'=' * 62}{RESET}")

    results: list[CheckResult] = []

    # R1 配置文件
    result = check_config_exists()
    results.append(result)
    cfg: dict[str, Any] = {}
    if result.passed:
        try:
            cfg = load_agent_config()
        except PreflightError as exc:
            results.append(CheckResult("配置文件解析", True, False, str(exc)))

    # R2 解释器
    python = sys.executable
    if cfg:
        result, python = check_python_interpreter(cfg)
        results.append(result)
        # R3 核心依赖（需要解释器存在才有意义）
        if result.passed:
            results.append(check_core_deps(python))
            # R4 MySQL（后端必须；前端不直接依赖，但代理目标挂着也查，便于排障）
            results.append(check_mysql(python, cfg))
            # O1 MCP 握手（可选，只在依赖可用后探测）
            results.extend(check_mcp_servers(python))

    # R5 LLM Key
    if cfg:
        results.append(check_llm_key(cfg))

    # R6 前端专属
    if scope == "frontend":
        results.extend(check_node_frontend())

    # ---- 打印 ----
    required_failed: list[CheckResult] = []
    print()
    for r in results:
        icon = "✔" if r.passed else ("✘" if r.required else "⚠")
        color = GREEN if r.passed else (RED if r.required else YELLOW)
        line = f" {color}{icon}{RESET} [{('必须' if r.required else '可选')}] {r.name}：{r.detail}"
        print(line)
        if not r.passed and r.fix_hint:
            print(f"      ↳ 修复建议：{r.fix_hint}")
        if not r.passed and r.required:
            required_failed.append(r)

    elapsed = time.time() - started
    print()
    if required_failed:
        msg = (
            f"✘ 预检失败：{len(required_failed)} 个必须项未通过"
            f"（耗时 {elapsed:.1f}s），终止启动。"
        )
        print(err(msg))
        print(err("   请按上方「修复建议」逐项处理后重试。"))
        raise PreflightError("required checks failed")
    optional_failed = [r for r in results if not r.passed]
    if optional_failed:
        print(
            warn(
                f"⚠ {len(optional_failed)} 个可选项未就绪（不影响启动，运行期已隔离）："
                f"{'、'.join(r.name for r in optional_failed)}"
            )
        )
    print(ok(f"✔ 预检通过（耗时 {elapsed:.1f}s），继续启动 {scope}…"))
    print(f"{BOLD}{'=' * 62}{RESET}")


def main() -> int:
    parser = argparse.ArgumentParser(description="服务启动前环境预检")
    parser.add_argument("--scope", choices=["backend", "frontend"], help="预检范围")
    parser.add_argument("--probe-deps", metavar="PYTHON", help=argparse.SUPPRESS)
    parser.add_argument("--probe-mysql", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--probe-mcp", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.probe_deps:
        return probe_deps()
    if args.probe_mysql:
        return probe_mysql()
    if args.probe_mcp:
        return probe_mcp()
    if args.scope:
        try:
            run_preflight(args.scope)
        except PreflightError:
            return 1
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
