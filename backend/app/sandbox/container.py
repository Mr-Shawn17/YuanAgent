"""沙箱容器驱动（spec docs/sandbox-query.md §6/§7/§8）：docker-py 驱动 runsc 容器。

硬不变量（spec §2）：git_query 的每次执行必须发生在 runsc 容器内——Engine 不可达 /
runsc 缺失 / 挂载失败 / 引擎级超时一律抛 SandboxUnavailableError，绝不回退宿主直跑。

容器规格（spec §6，全部固定值，不走配置面）：
    镜像 yuan-sandbox-git（alpine + git，deploy/sandbox/Dockerfile）｜runtime runsc
    --network=none ｜ rootfs 只读 ｜ mirror bind 挂载 /repo (ro)
    --memory 512m --cpus 0.5 --pids-limit 64 ｜ tmpfs /tmp 128m
    每命令临时容器，跑完即焚（auto_remove 语义由本层手动 remove 实现）

超时预算链（spec §7）：git 命令 command_timeout_seconds(60，容器内 busybox timeout)
≤ docker 等待 DOCKER_WAIT_SECONDS(90，本层) ≤ RepoSource fetch 120s ≤ 子 Agent 300s。

docker 库懒加载 import：依赖缺失/引擎不可达只应禁用 git_query 自身（requires_docker
语义，spec §12），不影响其他工具与模块导入。
"""

from __future__ import annotations

import re
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)

SANDBOX_IMAGE = "yuan-sandbox-git"  # spec §6：Engine 内预构建
SANDBOX_RUNTIME = "runsc"  # spec §6：gVisor，daemon.json 已注册（T08 产物）
SANDBOX_PIDS_LIMIT = 64  # spec §6
SANDBOX_TMPFS_SIZE = "128m"  # spec §6
SANDBOX_MOUNT_TARGET = "/repo"  # spec §6：mirror 只读挂载点
DOCKER_WAIT_SECONDS = 90  # docker run 等待预算（spec §7 外层）
ENGINE_PROBE_TIMEOUT_SECONDS = 3.0  # 装配期 requires_docker 探活预算
# 容器内环境：HOME 指向 tmpfs，只读 git 查询绝不写 rootfs；系统级 gitconfig 一律跳过；
# safe.directory 放行固定挂载点——9P（\\wsl$）clone 的副本属主是 WSL 侧 uid，
# 与容器 root 不一致，git ≥2.35.2 会拒读（dubious ownership）。值为驱动常量，无注入面。
SANDBOX_ENV = {
    "HOME": "/tmp",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.directory",
    "GIT_CONFIG_VALUE_0": SANDBOX_MOUNT_TARGET,
}

# 输出消毒模式：与 runner/procs.py 保持一致（spec §8「复用剥离正则」）。
# backend 不 import runner（wheel 只打包 backend/app），此处同步实现，改动须两边同步。
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"yrt_[A-Za-z0-9_\-]{20,}"),
)
_URL_CREDS_RE = re.compile(r"(https?://)[^\s/@:]+:[^\s/@]+@")

TRUNCATED_MARKER = "[truncated]"  # spec §8：截断标记


class SandboxUnavailableError(AppError):
    """沙箱不可用（引擎不可达/runsc 缺失/挂载失败/引擎级超时）。code: sandbox_unavailable"""

    code = "sandbox_unavailable"
    status_code = 503


@dataclass(slots=True)
class SandboxQueryResult:
    """一次沙箱 git 查询的结果（spec §8 返回结构，外加 exit_code 供渲染/审计）。"""

    ok: bool
    subcommand: str
    stdout: str  # 已消毒：stdout + stderr 合并、封顶
    truncated: bool
    elapsed_ms: int
    mirror_ref: str
    exit_code: int


def sanitize_output(text: str) -> str:
    """剥离显式凭证模式（与 runner/procs.py 同步）：沙箱无凭证，消毒不改语义，防御纵深。"""
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub("***", text)
    return _URL_CREDS_RE.sub(r"\1***@", text)


def cap_output(stdout: str, stderr: str, max_chars: int) -> tuple[str, bool]:
    """合并 stdout/stderr、消毒、按上限封顶（spec §8）：stderr 计入同一封顶。"""
    merged = sanitize_output(stdout)
    if stderr.strip():
        merged += f"\n[stderr]\n{sanitize_output(stderr)}"
    if len(merged) > max_chars:
        return merged[:max_chars] + f"\n…{TRUNCATED_MARKER}", True
    return merged, False


def probe_sandbox_engine(settings: Settings) -> str | None:
    """装配期探活（requires_docker 语义，spec §12）：可达返回 None，否则返回禁用原因。

    检查三件事：docker 库可导入、Engine 可达（ping）、runsc 运行时已注册。
    """
    try:
        import docker
    except ImportError as exc:
        return f"docker 库不可用（需 docker>=7）：{exc}"
    cfg = settings.sandbox_query
    base_url = cfg.docker_host.strip() or None
    label = base_url or "默认 socket"
    try:
        client = docker.DockerClient(base_url=base_url, timeout=ENGINE_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:  # 引擎连接构造失败（如 npipe 缺失）
        return f"Docker Engine 不可达（{label}）：{describe(exc)}"
    try:
        client.ping()
        runtimes = client.info().get("Runtimes") or {}
        if SANDBOX_RUNTIME not in runtimes:
            return (
                f"Docker Engine（{label}）未注册 {SANDBOX_RUNTIME} 运行时"
                "（无沙箱不执行，工具拒绝装配；见 spec §2）"
            )
        return None
    except Exception as exc:
        return f"Docker Engine 不可达（{label}）：{describe(exc)}"
    finally:
        with suppress(Exception):
            client.close()


def describe(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{exc.__class__.__name__}: {text}" if text else exc.__class__.__name__


def run_git_in_sandbox(
    settings: Settings, engine_mount_src: str, subcommand: str, argv: list[str]
) -> SandboxQueryResult:
    """在 runsc 沙箱容器内对只读副本执行一条白名单 git 命令（spec §6）。

    - 阻塞函数：工具层经 asyncio.to_thread 调用；
    - 命令 = ["timeout", "<预算>", "git", <sub>, ...argv]，容器内 busybox timeout
      实现第一层超时（kill 挂死的 git）；
    - 跑完即焚：容器无论成败均手动 remove（等价 --rm，且异常路径也焚）；
    - 引擎级故障（不可达/挂载失败/等待超时）抛 SandboxUnavailableError——不回退宿主。
    """
    import docker
    import requests  # docker-py 的超时/连接异常以 requests 异常形态冒出
    from docker.types import Mount

    cfg = settings.sandbox_query
    started = time.perf_counter()
    client = docker.DockerClient(
        base_url=cfg.docker_host.strip() or None, timeout=DOCKER_WAIT_SECONDS
    )
    container: Any = None
    exit_code = -1
    stdout_raw, stderr_raw = b"", b""
    try:
        try:
            container = client.containers.run(
                SANDBOX_IMAGE,
                command=[
                    "timeout",
                    str(cfg.command_timeout_seconds),
                    "git",
                    subcommand,
                    *argv,
                ],
                runtime=SANDBOX_RUNTIME,
                network_mode="none",  # 断网（spec §6）
                read_only=True,  # rootfs 只读
                mounts=[
                    Mount(
                        target=SANDBOX_MOUNT_TARGET,
                        source=engine_mount_src,
                        type="bind",
                        read_only=True,
                    )
                ],
                working_dir=SANDBOX_MOUNT_TARGET,
                environment=SANDBOX_ENV,
                mem_limit=cfg.memory,
                nano_cpus=int(float(cfg.cpus) * 1_000_000_000),
                pids_limit=SANDBOX_PIDS_LIMIT,
                tmpfs={"/tmp": f"size={SANDBOX_TMPFS_SIZE}"},
                detach=True,
                auto_remove=False,  # 手动焚（异常路径也要保证回收）
            )
            wait_result = container.wait(timeout=DOCKER_WAIT_SECONDS)
            exit_code = int(wait_result.get("StatusCode", -1))
            # 一次性容器分两次取流（Container.logs 不支持 demux）：
            # stdout 与 stderr 各自完整拿回，由 cap_output 合并封顶
            stdout_raw = container.logs(stdout=True, stderr=False) or b""
            stderr_raw = container.logs(stdout=False, stderr=True) or b""
        except (docker.errors.DockerException, requests.RequestException, OSError) as exc:
            # 引擎不可达 / 镜像缺失 / runsc 拒绝 / bind 源不存在 / 等待超时 → 沙箱不可用
            # （其余异常原样冒出，由工具错误边界如实报告，避免掩盖真实缺陷）
            raise SandboxUnavailableError(describe(exc)) from exc
    finally:
        if container is not None:
            with suppress(Exception):
                container.remove(force=True)
        with suppress(Exception):
            client.close()

    stdout = stdout_raw.decode("utf-8", errors="replace") if stdout_raw else ""
    stderr = stderr_raw.decode("utf-8", errors="replace") if stderr_raw else ""
    capped, truncated = cap_output(stdout, stderr, cfg.output_max_chars)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return SandboxQueryResult(
        ok=exit_code == 0,
        subcommand=subcommand,
        stdout=capped,
        truncated=truncated,
        elapsed_ms=elapsed_ms,
        mirror_ref=engine_mount_src,
        exit_code=exit_code,
    )


__all__ = [
    "DOCKER_WAIT_SECONDS",
    "SANDBOX_IMAGE",
    "SANDBOX_RUNTIME",
    "TRUNCATED_MARKER",
    "SandboxQueryResult",
    "SandboxUnavailableError",
    "cap_output",
    "probe_sandbox_engine",
    "run_git_in_sandbox",
    "sanitize_output",
]
