"""沙箱容器驱动（spec docs/sandbox-query.md §6-§8 + docs/opensandbox-migration.md v1.1）。

双驱动（spec v1.1 §11 迁移期开关，driver: opensandbox | docker-py）：

- **opensandbox（默认）**：backend 经 OpenSandbox SDK 调 opensandbox-server + 直连沙箱内
  execd 的 /command（argv 直传，SSE 流式）；沙箱来自会话级沙箱池（pool.py，spec §4），
  挂载为 Engine 侧 mirrors 根（只读，create 时固定），具体仓库由 working_directory 指向
  /repo/<owner>/<repo>；
- **docker-py（回滚路径）**：迁移前行为原样保留——每命令 runsc 临时容器跑完即焚，
  挂载为 Engine 侧仓库目录。

硬不变量（spec §2）：git_query/rg_query 的每次执行必须发生在 runsc(gVisor) 沙箱内——
server 不可达 / secure_runtime 非 gvisor / 挂载失败 / 执行层超时一律抛
SandboxUnavailableError，绝不回退宿主直跑。

超时预算链（spec §7）：git 命令 command_timeout_seconds(60，容器内 busybox timeout)
≤ execd 命令等待 OS_EXECD_WAIT_SECONDS(90) ≤ RepoSource fetch 120s ≤ research 300s。

opensandbox SDK 懒加载：依赖缺失/server 不可达只禁用沙箱查询（requires_opensandbox
语义，spec §10.3），不影响其他模块导入；探活用 SDK 同步入口（工厂函数是同步上下文，
不得起嵌套事件循环）。
"""

from __future__ import annotations

import asyncio
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)

SANDBOX_IMAGE = "yuan-sandbox-git"  # docker-py 回滚路径镜像（spec sandbox-query §6）
SANDBOX_RUNTIME = "runsc"  # gVisor，daemon.json 已注册（T08 产物）
SANDBOX_PIDS_LIMIT = 64  # spec sandbox-query §6
SANDBOX_TMPFS_SIZE = "128m"  # spec sandbox-query §6
DOCKER_WAIT_SECONDS = 90  # docker-py 路径 docker run 等待预算（spec §7 外层）
ENGINE_PROBE_TIMEOUT_SECONDS = 3.0  # docker-py 路径装配期探活预算
SANDBOX_MOUNT_TARGET = "/repo"  # docker-py 路径 mirror 只读挂载点（spec sandbox-query §6）
# 容器内环境（docker-py 路径）：HOME 指向 tmpfs，只读 git 查询绝不写 rootfs；
# 系统级 gitconfig 一律跳过；safe.directory 放行固定挂载点——9P（\\wsl$）clone 的
# 副本属主是 WSL 侧 uid，与容器 root 不一致，git ≥2.35.2 会拒读（dubious ownership）。
# 值为驱动常量，无注入面。
SANDBOX_ENV = {
    "HOME": "/tmp",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.directory",
    "GIT_CONFIG_VALUE_0": SANDBOX_MOUNT_TARGET,
}

# ---- OpenSandbox 驱动（spec v1.1） ----------------------------------------------

OS_IMAGE = "yuan-sandbox-exec"  # execd 基底 + git/rg（deploy/sandbox/Dockerfile.exec）
OS_MOUNT_TARGET = "/repo"  # 会话级挂载点：Engine 侧 mirrors 根（只读，create 时固定）
OS_READY_TIMEOUT_SECONDS = 60  # create → execd 就绪预算（含 execd 注入）
OS_EXECD_WAIT_SECONDS = 90  # execd /command 等待预算（spec §7 中层）
OS_UNAME_GVISOR_MARK = "gvisor"  # runsc 内核指纹（uname -r = 4.19.0-gvisor，实测；
# runc 报宿主内核 6.x——Q7 据此校验 secure_runtime，防漏配悄悄回退 runc）
OS_PROBE_NEGATIVE_TTL_SECONDS = 60.0  # 探活负缓存：server 恢复后 60s 内自动复探

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
    """沙箱不可用（server 不可达/非 gvisor/挂载失败/执行层超时）。code: sandbox_unavailable"""

    code = "sandbox_unavailable"
    status_code = 503


@dataclass(slots=True)
class SandboxQueryResult:
    """一次沙箱 git 查询的结果（spec §8 返回结构，外加 exit_code/sandbox_id 供渲染/审计）。

    sandbox_id：opensandbox 驱动下为会话沙箱 ID（TC-01 复用验证走审计落库）；
    docker-py 回滚路径为空串（一次性容器无稳定 ID 语义）。
    """

    ok: bool
    subcommand: str
    stdout: str  # 已消毒：stdout + stderr 合并、封顶
    truncated: bool
    elapsed_ms: int
    mirror_ref: str
    exit_code: int
    sandbox_id: str = ""


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


def describe(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{exc.__class__.__name__}: {text}" if text else exc.__class__.__name__


def _parse_server_url(raw: str) -> tuple[str, str]:
    """把 opensandbox_server_url 拆成 SDK ConnectionConfig 的 (protocol, domain)。

    支持 http://host:port 与 host:port；端口缺失报错（SDK 默认域不可用）。
    """
    text = raw.strip().rstrip("/")
    protocol = "http"
    if "://" in text:
        protocol, text = text.split("://", 1)
        if protocol not in ("http", "https"):
            raise SandboxUnavailableError(
                f"opensandbox_server_url 协议非法：{protocol}（支持 http/https）"
            )
    if not text or ":" not in text:
        raise SandboxUnavailableError(
            f"opensandbox_server_url 缺端口：{raw}（如 http://172.28.146.229:8080）"
        )
    return protocol, text


def _os_connection_config(cfg: Any, sync: bool) -> Any:
    """SDK ConnectionConfig（opensandbox 驱动；sync=True 用同步入口的配置类）。"""
    protocol, domain = _parse_server_url(cfg.opensandbox_server_url)
    if sync:
        from opensandbox.config.connection_sync import ConnectionConfigSync

        return ConnectionConfigSync(
            protocol=protocol,
            domain=domain,
            api_key=cfg.opensandbox_api_key.strip() or None,
            request_timeout=timedelta(seconds=OS_EXECD_WAIT_SECONDS),
        )
    from opensandbox.config.connection import ConnectionConfig

    return ConnectionConfig(
        protocol=protocol,
        domain=domain,
        api_key=cfg.opensandbox_api_key.strip() or None,
        request_timeout=timedelta(seconds=OS_EXECD_WAIT_SECONDS),
    )


# ---- 装配期探活（requires_docker → requires_opensandbox 语义，spec §10.3 Q7 强化） ----

_probe_cache: dict[tuple[str, str], tuple[str | None, float | None]] = {}


def probe_sandbox_engine(settings: Settings) -> str | None:
    """装配期探活：可达返回 None，否则返回禁用原因（工具整体自禁，拒绝装配）。

    opensandbox 驱动（Q7 强化）：不能只 ping server 存活——用一次性探针沙箱跑
    uname -r 验证 secure_runtime 确为 gvisor（防漏配悄悄回退 runc），顺带验证
    控制面认证、镜像就绪与数据面。结果缓存：正向永久（进程内），负向 60s（server
    恢复后自动复探）。
    docker-py 驱动：沿用原 docker socket + runsc 注册检查。
    """
    if settings.sandbox_query.driver == "docker-py":
        return _probe_docker_py(settings)
    return _probe_opensandbox(settings)


def _probe_docker_py(settings: Settings) -> str | None:
    """docker-py 回滚路径探活：docker 库可导入、Engine 可达（ping）、runsc 已注册。"""
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


def _probe_opensandbox(settings: Settings) -> str | None:
    cfg = settings.sandbox_query
    url = cfg.opensandbox_server_url.strip()
    if not url:
        return (
            "opensandbox_server_url 未配置（opensandbox 驱动必填，如"
            " http://172.28.146.229:8080）；无沙箱不执行，工具拒绝装配（spec §2）"
        )
    mirror_root = cfg.mirror_engine_root.strip() or cfg.mirror_root.strip()
    if not mirror_root.startswith("/"):
        return (
            "opensandbox 驱动要求 mirror_engine_root 为 Engine 内绝对路径"
            f"（如 /var/lib/yuan-mirrors），当前为：{mirror_root or '（空）'}"
        )
    cache_key = (url, cfg.opensandbox_api_key)
    cached = _probe_cache.get(cache_key)
    now = time.monotonic()
    if cached is not None and (cached[1] is None or cached[1] > now):
        return cached[0]
    reason = _probe_opensandbox_live(cfg, url)
    _probe_cache[cache_key] = (
        (None, None) if reason is None else (reason, now + OS_PROBE_NEGATIVE_TTL_SECONDS)
    )
    return reason


def _probe_opensandbox_live(cfg: Any, url: str) -> str | None:
    """探针沙箱实测：create → uname -r → destroy（全同步，工厂上下文可安全调用）。"""
    try:
        from opensandbox import SandboxSync

        probe = SandboxSync.create(
            OS_IMAGE,
            timeout=timedelta(seconds=120),  # 探针自带绝对过期兜底（探活中断不残留）
            ready_timeout=timedelta(seconds=OS_READY_TIMEOUT_SECONDS),
            resource={"cpu": f"{int(float(cfg.cpus) * 1000)}m", "memory": cfg.memory},
            metadata={"managed_by": "yuan-backend", "purpose": "runtime-probe"},
            connection_config=_os_connection_config(cfg, sync=True),
        )
        try:
            release = probe.commands.run(["uname", "-r"]).text.strip()
        finally:
            with suppress(Exception):
                probe.destroy()
        if OS_UNAME_GVISOR_MARK not in release:
            return (
                f"opensandbox-server secure_runtime 非 gvisor（uname={release}）——"
                "防漏配回退 runc，无沙箱不执行，工具拒绝装配（spec §2/§10.3）"
            )
        return None
    except SandboxUnavailableError as exc:
        return str(exc)
    except Exception as exc:
        return f"opensandbox-server 不可达（{url}）：{describe(exc)}"


# ---- docker-py 回滚路径（迁移前行为原样保留，spec v1.1 §11） --------------------


def _exec_in_sandbox_docker_py(
    settings: Settings, engine_mount_src: str, command: list[str]
) -> tuple[int, str, str]:
    """在 runsc 一次性容器内执行一条白名单 argv（校验器已把关），返回 (退出码, stdout, stderr)。

    - 阻塞函数：驱动分发层经 asyncio.to_thread 调用；
    - 命令 = ["timeout", "<预算>", ...command]，容器内 busybox timeout 实现第一层
      超时（kill 挂死的进程）；
    - 跑完即焚：容器无论成败均手动 remove（等价 --rm，且异常路径也焚）；
    - 引擎级故障（不可达/挂载失败/等待超时）抛 SandboxUnavailableError——不回退宿主。
    """
    import docker
    import requests  # docker-py 的超时/连接异常以 requests 异常形态冒出
    from docker.types import Mount

    cfg = settings.sandbox_query
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
                command=["timeout", str(cfg.command_timeout_seconds), *command],
                runtime=SANDBOX_RUNTIME,
                network_mode="none",  # 断网（spec sandbox-query §6）
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
    return exit_code, stdout, stderr


# ---- OpenSandbox 驱动执行路径（spec v1.1 §4/§6/§8） -----------------------------


class _StreamCollector:
    """SSE 流式输出累积器（spec §8：封顶前置——边收边累计，超限即截断）。

    配合 skip_accumulation=True：SDK 不再自累积（防大输出撑爆内存），本类按共享
    预算收集 stdout/stderr，超限丢弃后续并置 truncated；消毒与最终封顶复用 cap_output。
    """

    def __init__(self, max_chars: int) -> None:
        self._max = max_chars
        self._size = 0
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self.truncated = False

    def _feed(self, sink: list[str], text: str) -> None:
        if self.truncated or not text:
            return
        self._size += len(text)
        if self._size > self._max:
            self.truncated = True
            return
        sink.append(text)

    async def on_stdout(self, msg: Any) -> None:
        self._feed(self._stdout, getattr(msg, "text", "") or "")

    async def on_stderr(self, msg: Any) -> None:
        self._feed(self._stderr, getattr(msg, "text", "") or "")

    def collected(self) -> tuple[str, str]:
        return "\n".join(self._stdout), "\n".join(self._stderr)


async def _exec_in_sandbox_opensandbox(
    settings: Settings,
    session_id: str,
    repo_ref: str,
    command: list[str],
) -> tuple[int, str, str, bool, str]:
    """在会话级 runsc 沙箱内执行一条白名单 argv（校验器已把关）。

    - argv 外层包 busybox timeout（第一层超时，沿用旧预算链），execd 侧
      OS_EXECD_WAIT_SECONDS 兜底；
    - 工作目录 = /repo/<owner>/<repo>（挂载为 mirrors 根），safe.directory 经命令级
      envs 指向该目录（dubious ownership 放行，值为 backend 计算常量，无注入面）；
    - 崩溃惰性重建（TC-04）：连接类失败 → pool.discard 丢映射 → acquire 重建 →
      重试一次；再失败抛 SandboxUnavailableError——绝不回退宿主（spec §2）；
    - 返回 (退出码, stdout, stderr, truncated, sandbox_id)。
    """
    from opensandbox.exceptions.sandbox import (
        SandboxApiException,
        SandboxConnectionException,
        SandboxException,
    )
    from opensandbox.models.execd import ExecutionHandlers, RunCommandOpts

    from app.sandbox.pool import get_pool

    cfg = settings.sandbox_query
    pool = get_pool(settings)
    workdir = f"{OS_MOUNT_TARGET}/{repo_ref}"
    envs = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": workdir,
    }
    argv = ["timeout", str(cfg.command_timeout_seconds), *command]
    last_error: Exception | None = None
    for attempt in (1, 2):
        sandbox = await pool.acquire(session_id)
        collector = _StreamCollector(cfg.output_max_chars)
        try:
            execution = await sandbox.commands.run(
                argv,
                opts=RunCommandOpts(
                    working_directory=workdir,
                    timeout=timedelta(seconds=OS_EXECD_WAIT_SECONDS),
                    envs=envs,
                ),
                handlers=ExecutionHandlers(
                    on_stdout=collector.on_stdout,
                    on_stderr=collector.on_stderr,
                    skip_accumulation=True,
                ),
            )
            exit_code = execution.exit_code if isinstance(execution.exit_code, int) else -1
            stdout, stderr = collector.collected()
            return exit_code, stdout, stderr, collector.truncated, sandbox.id
        except SandboxConnectionException as exc:
            # server 重启后端点未就绪/网络抖动 → 丢映射重建再试一次（TC-04）
            last_error = exc
            logger.warning(
                "sandbox_command_failed_rebuilding",
                session_id=session_id,
                sandbox_id=getattr(sandbox, "id", "?"),
                attempt=attempt,
                error=describe(exc),
            )
            await pool.discard(session_id)
        except SandboxApiException as exc:
            if getattr(exc, "status_code", None) == 404:
                # 沙箱已被外部销毁（TC-04 手动销毁分支）：端点解析 404 → 丢映射重建
                last_error = exc
                logger.warning(
                    "sandbox_command_failed_rebuilding",
                    session_id=session_id,
                    sandbox_id=getattr(sandbox, "id", "?"),
                    attempt=attempt,
                    error=describe(exc),
                )
                await pool.discard(session_id)
            else:
                # 控制面明确拒绝（认证失败/参数非法等）：重建无济于事，直接按沙箱不可用
                raise SandboxUnavailableError(describe(exc)) from exc
        except SandboxException as exc:
            # 其余 SDK 异常（如就绪超时）：重建无济于事，直接按沙箱不可用
            raise SandboxUnavailableError(describe(exc)) from exc
    raise SandboxUnavailableError(describe(last_error) if last_error else "execd 命令失败")


def _finalize(
    settings: Settings,
    label: str,
    started: float,
    exit_code: int,
    stdout: str,
    stderr: str,
    mirror_ref: str,
    *,
    truncated_extra: bool = False,
    sandbox_id: str = "",
) -> SandboxQueryResult:
    """消毒 + 封顶 + 组装结果（spec §8）。

    truncated_extra：上游流式封顶已触发（collector 边收边截）。此时 cap_output 收到的
    内容已 ≤ 上限、不会再切，标记必须在这里补齐——模型可见文本的 [truncated] 与
    artifact.truncated 必须一致。
    """
    capped, truncated = cap_output(stdout, stderr, settings.sandbox_query.output_max_chars)
    if truncated_extra and not truncated:
        capped += f"\n…{TRUNCATED_MARKER}"
        truncated = True
    return SandboxQueryResult(
        ok=exit_code == 0,
        subcommand=label,
        stdout=capped,
        truncated=truncated,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        mirror_ref=mirror_ref,
        exit_code=exit_code,
        sandbox_id=sandbox_id,
    )


async def run_git_in_sandbox(
    settings: Settings,
    session_id: str,
    mirror_ref: str,
    repo_ref: str,
    subcommand: str,
    argv: list[str],
) -> SandboxQueryResult:
    """在 runsc 沙箱内对只读副本执行一条白名单 git 命令（按驱动分发，spec §2 硬不变量）。

    mirror_ref 为 Engine 侧仓库目录（审计/展示用）；opensandbox 驱动下沙箱挂载为
    mirrors 根，工作目录按 repo_ref 推导；docker-py 回滚路径挂载为仓库目录本身。
    """
    started = time.perf_counter()
    command = ["git", subcommand, *argv]
    if settings.sandbox_query.driver == "docker-py":
        exit_code, stdout, stderr = await asyncio.to_thread(
            _exec_in_sandbox_docker_py, settings, mirror_ref, command
        )
        return _finalize(settings, subcommand, started, exit_code, stdout, stderr, mirror_ref)
    exit_code, stdout, stderr, truncated, sandbox_id = await _exec_in_sandbox_opensandbox(
        settings, session_id, repo_ref, command
    )
    return _finalize(
        settings,
        subcommand,
        started,
        exit_code,
        stdout,
        stderr,
        mirror_ref,
        truncated_extra=truncated,
        sandbox_id=sandbox_id,
    )


async def run_rg_in_sandbox(
    settings: Settings,
    session_id: str,
    mirror_ref: str,
    repo_ref: str,
    pattern: str,
    path: str,
    argv: list[str],
) -> SandboxQueryResult:
    """在 runsc 沙箱内对只读副本执行 rg 内容检索（按驱动分发，spec §5 v1.1）。

    --color=never 等固定旗标由本层拼装（不经模型）；``--`` 分隔符防止以 ``-``
    开头的模式/路径被解析为选项。退出码语义：0=有命中，1=无命中（合法结果，
    由工具层区分呈现），2=出错。
    """
    command = ["rg", "--color=never", *argv, "--", pattern]
    if path:
        command.append(path)
    started = time.perf_counter()
    if settings.sandbox_query.driver == "docker-py":
        exit_code, stdout, stderr = await asyncio.to_thread(
            _exec_in_sandbox_docker_py, settings, mirror_ref, command
        )
        return _finalize(settings, "rg", started, exit_code, stdout, stderr, mirror_ref)
    exit_code, stdout, stderr, truncated, sandbox_id = await _exec_in_sandbox_opensandbox(
        settings, session_id, repo_ref, command
    )
    return _finalize(
        settings,
        "rg",
        started,
        exit_code,
        stdout,
        stderr,
        mirror_ref,
        truncated_extra=truncated,
        sandbox_id=sandbox_id,
    )


__all__ = [
    "DOCKER_WAIT_SECONDS",
    "OS_EXECD_WAIT_SECONDS",
    "OS_IMAGE",
    "OS_MOUNT_TARGET",
    "OS_READY_TIMEOUT_SECONDS",
    "OS_UNAME_GVISOR_MARK",
    "SANDBOX_IMAGE",
    "SANDBOX_RUNTIME",
    "TRUNCATED_MARKER",
    "SandboxQueryResult",
    "SandboxUnavailableError",
    "cap_output",
    "probe_sandbox_engine",
    "run_git_in_sandbox",
    "run_rg_in_sandbox",
    "sanitize_output",
]
