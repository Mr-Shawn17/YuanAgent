"""WindowsSandboxBackend：VM 级隔离档（Windows Pro 内置沙盒，无需安装三方软件）。

架构（会话式沙箱）：
- 动态生成 .wsb：workspace（可写映射 C:\\ws）+ bridge 脚本目录（只读）+ 便携工具链（只读）
- 沙箱内 PowerShell bridge 常驻：轮询映射文件夹任务队列 json → cmd /c 执行
  （chcp 65001 + timeout + taskkill /T 杀树）→ 结果 json 写回
- 宿主侧：写任务 → 200ms 轮询结果 → 拼装 CommandResult
- 会话生命周期：懒启动（首次命令）、workspace 变化时重建、空闲 10 分钟自动回收

已知边界（docs/local-runner.md 详述）：
- 映射目录之外的宿主文件对沙箱不可见；命令产物落映射 workspace 持久保留
- 映射文件夹小文件 I/O 慢（node_modules 级操作会明显变慢）
- git 凭证经任务 json 在映射文件夹短暂明文落盘（用后即删；本机磁盘威胁模型之外）
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from runner.command import CommandPolicy, CommandResult
from runner.config import app_dir

VM_WORKSPACE = "C:\\ws"
VM_BRIDGE_DIR = "C:\\yuan\\bridge"
VM_TOOLS_DIR = "C:\\yuan\\tools"
WSB_EXE = Path(r"C:\Windows\System32\WindowsSandbox.exe")

SESSION_START_TIMEOUT = 180  # VM 启动 + bridge 就绪上限（秒）
IDLE_REAP_SECONDS = 600  # 空闲回收阈值
POLL_INTERVAL = 0.2

# 沙箱内 bridge：轮询任务队列 → cmd 执行（UTF-8 代码页 + 超时杀树）→ 结果写回
BRIDGE_PS1 = r"""
$ErrorActionPreference = 'Continue'
$ws = 'C:\ws'
$bridgeDir = Join-Path $ws '.yuan\bridge'
$queue = Join-Path $bridgeDir 'tasks'
$results = Join-Path $bridgeDir 'results'
New-Item -ItemType Directory -Force -Path $queue | Out-Null
New-Item -ItemType Directory -Force -Path $results | Out-Null
$env:PATH = 'C:\yuan\tools\git\cmd;C:\yuan\tools\python;C:\yuan\tools\node;' + $env:PATH
$env:PYTHONIOENCODING = 'utf-8'
$env:GIT_TERMINAL_PROMPT = '0'
Set-Content -Path (Join-Path $bridgeDir 'ready') -Value (Get-Date).ToString('o')
while ($true) {
  $task = Get-ChildItem -Path $queue -Filter '*.json' -ErrorAction SilentlyContinue |
    Sort-Object Name | Select-Object -First 1
  if ($null -eq $task) { Start-Sleep -Milliseconds 200; continue }
  try { $raw = Get-Content -Path $task.FullName -Raw -Encoding UTF8 } catch {
    Remove-Item -Force $task.FullName; continue }
  Remove-Item -Force $task.FullName
  try { $t = $raw | ConvertFrom-Json } catch { continue }
  $cwd = Join-Path $ws ([string]$t.cwd)
  if (-not (Test-Path $cwd)) { $cwd = $ws }
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = 'cmd.exe'
  $psi.Arguments = '/d /c chcp 65001>nul & ' + [string]$t.command
  $psi.WorkingDirectory = $cwd
  $psi.UseShellExecute = $false
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
  $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
  if ($t.env) {
    foreach ($p in $t.env.PSObject.Properties) {
      [Environment]::SetEnvironmentVariable($p.Name, [string]$p.Value)
    }
  }
  $timedOut = $false; $out = ''; $err = ''; $code = -1
  try {
    $proc = [System.Diagnostics.Process]::Start($psi)
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()
    if (-not $proc.WaitForExit([int]$t.timeout * 1000)) {
      $timedOut = $true
      & taskkill /PID $proc.Id /T /F 2>$null | Out-Null
      try { $proc.WaitForExit(5000) | Out-Null } catch {}
    }
    $code = $proc.ExitCode
    $out = $outTask.Result
    $err = $errTask.Result
  } catch { $err = $_.Exception.Message }
  @{ id = [string]$t.id; exit_code = $code; timed_out = $timedOut
     stdout = $out; stderr = $err } | ConvertTo-Json -Compress -Depth 3 |
    Set-Content -Path (Join-Path $results ([string]$t.id + '.json')) -Encoding UTF8
}
"""

_AVAILABLE: bool | None = None


def is_sandbox_available() -> bool:
    """Windows Sandbox 功能是否已启用（以 WindowsSandbox.exe 存在性为准，进程内缓存）。

    功能未启用时该可执行文件不存在；此检测无需管理员权限。
    """
    global _AVAILABLE
    if _AVAILABLE is None:
        _AVAILABLE = WSB_EXE.is_file()
    return _AVAILABLE


def _has_running_sandbox() -> bool:
    """检测是否已有 Windows Sandbox 实例在运行（vmmem/Client 进程名）。"""
    try:
        import psutil
    except ImportError:
        return False
    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        if name.startswith(("vmmemwindowssandbox", "windowssandboxclient")):
            return True
    return False


def _terminate_session_sync(session: _Session) -> None:
    """关闭一个沙箱会话（同步版，close_session 与 atexit 共用）。

    WindowsSandbox.exe 只是启动器（拉起 Client 后即自行退出），terminate 它无效；
    真正持有窗口/VM 的是 WindowsSandboxClient.exe——按名单杀之
    （沙盒单实例限制保证同一时刻最多一个 Client，不会误杀他人实例）。
    """
    try:
        import psutil
    except ImportError:
        psutil = None  # type: ignore[assignment]
    if psutil is not None:
        # 会话相关进程：Client（窗口）与 RemoteSession/Server（会话本体，占单实例名额）；
        # vmmem 受系统保护不可直接杀，RemoteSession 终止后由系统自动回收
        names = {
            "windowssandboxclient.exe",
            "windowssandboxremotesession.exe",
            "windowssandboxserver.exe",
        }
        targets = []
        for proc in psutil.process_iter(["name", "create_time"]):
            if (proc.info.get("name") or "").lower() not in names:
                continue
            # 只杀本会话启动之后创建的进程（5s 容差），不误杀用户自开的其他沙盒
            with contextlib.suppress(psutil.Error):
                if proc.info.get("create_time", 0) >= session.started_at - 5:
                    targets.append(proc)
        for proc in targets:
            with contextlib.suppress(psutil.Error):
                proc.terminate()
        if targets:
            psutil.wait_procs(targets, timeout=10)
    with contextlib.suppress(Exception):
        session.proc.terminate()  # 启动器一般已自行退出，兜底再杀一次
    with contextlib.suppress(OSError):
        session.wsb_file.unlink(missing_ok=True)


@dataclass(slots=True)
class _Session:
    """一个运行中的沙箱会话（绑定单一 workspace）。"""

    workspace: Path
    wsb_file: Path
    proc: asyncio.subprocess.Process
    # started_at 用墙钟（epoch 秒，可与 psutil create_time 比较）；
    # last_used 用 monotonic（空闲回收做差值）
    started_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.monotonic)

    @property
    def bridge_dir(self) -> Path:
        return self.workspace / ".yuan" / "bridge"


class WindowsSandboxBackend:
    """VM 档后端：命令经映射文件夹队列送入 Windows Sandbox 执行。"""

    name = "windows_sandbox"

    def __init__(self, policy: CommandPolicy) -> None:
        self._policy = policy
        self._session: _Session | None = None
        self._reaper: asyncio.Task[None] | None = None

    # -- 会话管理 -----------------------------------------------------------

    def _bridge_home(self) -> Path:
        home = app_dir() / "sandbox-bridge"
        home.mkdir(parents=True, exist_ok=True)
        return home

    def _deploy_bridge(self) -> Path:
        script = self._bridge_home() / "bridge.ps1"
        script.write_text(BRIDGE_PS1, encoding="utf-8")
        return self._bridge_home()

    def _write_wsb(self, workspace: Path, bridge_home: Path, tools: Path) -> Path:
        wsb = self._bridge_home() / "session.wsb"
        mapping = (
            f"<MappedFolder><HostFolder>{escape(str(workspace))}</HostFolder>"
            f"<SandboxFolder>{VM_WORKSPACE}</SandboxFolder><ReadOnly>false</ReadOnly>"
            "</MappedFolder>\n"
            f"<MappedFolder><HostFolder>{escape(str(bridge_home))}</HostFolder>"
            f"<SandboxFolder>{VM_BRIDGE_DIR}</SandboxFolder><ReadOnly>true</ReadOnly>"
            "</MappedFolder>\n"
            f"<MappedFolder><HostFolder>{escape(str(tools))}</HostFolder>"
            f"<SandboxFolder>{VM_TOOLS_DIR}</SandboxFolder><ReadOnly>true</ReadOnly>"
            "</MappedFolder>"
        )
        wsb.write_text(
            "<Configuration>\n"
            "  <Networking>Default</Networking>\n"
            f"  <MappedFolders>\n    {mapping}\n  </MappedFolders>\n"
            "  <LogonCommand><Command>powershell.exe -NoProfile "
            "-ExecutionPolicy Bypass -File "
            f"{VM_BRIDGE_DIR}\\bridge.ps1</Command></LogonCommand>\n"
            "</Configuration>\n",
            encoding="utf-8",
        )
        return wsb

    async def _ensure_session(self, workspace: Path) -> _Session:
        if (
            self._session is not None
            and self._session.workspace == workspace
            and self._session.proc.returncode is None
        ):
            self._session.last_used = time.monotonic()
            return self._session
        await self._close_session()
        from runner.sandbox_tools import ensure_tools

        tools = await asyncio.to_thread(ensure_tools, ("git",), self._policy.sandbox_tools_dir)
        bridge_home = await asyncio.to_thread(self._deploy_bridge)
        ready = workspace / ".yuan" / "bridge" / "ready"
        ready.unlink(missing_ok=True)  # 清上次会话残留
        wsb = await asyncio.to_thread(
            self._write_wsb, workspace, bridge_home, tools
        )
        proc = await asyncio.create_subprocess_exec(  # 固定系统路径 + 受控 wsb 内容
            str(WSB_EXE), str(wsb),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )
        session = _Session(workspace=workspace, wsb_file=wsb, proc=proc)
        self._session = session
        # Python 退出兜底：沙盒 VM 是独立进程不会随父进程结束，
        # 不清理会留下孤儿窗口且占住单实例名额（下次启动必失败）
        atexit.register(_terminate_session_sync, session)
        deadline = time.monotonic() + SESSION_START_TIMEOUT
        launcher_exited_at: float | None = None
        while time.monotonic() < deadline:
            if proc.returncode is not None and launcher_exited_at is None:
                # 启动器拉起 Client 后正常退出（code=0）是常态，不能立即判失败；
                # 记录时间点，继续等 bridge ready（Client 起来到 bridge 写 ready 需要数十秒）
                launcher_exited_at = time.monotonic()
            if ready.is_file():
                self._start_reaper()
                return session
            await asyncio.sleep(2.0)
        await self._close_session()
        if _has_running_sandbox():
            msg = (
                "Windows Sandbox 启动失败：已有一个沙箱实例在运行（单实例限制）。"
                "请关闭现有沙盒窗口后重试"
            )
        else:
            msg = (
                f"Windows Sandbox 会话启动超时（{SESSION_START_TIMEOUT}s）：bridge 未就绪"
                + (
                    f"（启动器已退出，code={proc.returncode}）"
                    if launcher_exited_at is not None
                    else ""
                )
                + "。请确认功能已启用且未被组策略/杀软拦截"
            )
        raise RuntimeError(msg)

    async def _close_session(self) -> None:
        session, self._session = self._session, None
        if session is None:
            return
        await asyncio.to_thread(_terminate_session_sync, session)

    def _start_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_when_idle())

    async def _reap_when_idle(self) -> None:
        while self._session is not None:
            await asyncio.sleep(60.0)
            session = self._session
            if session is None:
                return
            if time.monotonic() - session.last_used > IDLE_REAP_SECONDS:
                await self._close_session()
                return

    # -- 执行 ---------------------------------------------------------------

    def _to_vm_path(self, workspace: Path, host_path: Path) -> str:
        try:
            rel = host_path.resolve().relative_to(workspace.resolve())
        except ValueError:
            raise ValueError(
                f"VM 档要求注入文件位于 workspace 内：{host_path}"
            ) from None
        return f"{VM_WORKSPACE}\\{rel}"

    async def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: int,  # noqa: ASYNC109 — 整段执行的超时上限
        extra_env: dict[str, str] | None = None,
        workspace: Path | None = None,
    ) -> CommandResult:
        if workspace is None:
            workspace = cwd  # 兜底：无 workspace 语义时以 cwd 为会话根
        session = await self._ensure_session(workspace)
        try:
            cwd_rel = cwd.resolve().relative_to(workspace.resolve())  # noqa: ASYNC240 — 单次轻量解析
        except ValueError:
            cwd_rel = Path()
        task_id = uuid.uuid4().hex[:12]
        vm_env: dict[str, str] = {}
        for name, value in (extra_env or {}).items():
            if name == "GIT_ASKPASS":
                value = self._to_vm_path(workspace, Path(value))
            vm_env[name] = value
        payload = {
            "id": task_id,
            "command": command,
            "cwd": str(cwd_rel),
            "timeout": timeout,
            "env": vm_env,
        }
        tasks_dir = session.bridge_dir / "tasks"
        results_dir = session.bridge_dir / "results"
        task_file = tasks_dir / f"{task_id}.json"
        result_file = results_dir / f"{task_id}.json"

        def _write_task() -> None:
            tasks_dir.mkdir(parents=True, exist_ok=True)
            results_dir.mkdir(parents=True, exist_ok=True)
            task_file.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )

        await asyncio.to_thread(_write_task)
        deadline = time.monotonic() + timeout + 20.0
        while time.monotonic() < deadline:
            if result_file.is_file():
                break
            if session.proc.returncode is not None:
                self._session = None  # 沙箱中途退出
                return CommandResult(
                    exit_code=None, timed_out=False, stdout="",
                    stderr="Windows Sandbox 会话中途退出（命令未完成）",
                    backend=self.name,
                )
            await asyncio.sleep(POLL_INTERVAL)
        else:
            return CommandResult(
                exit_code=None, timed_out=True, stdout="",
                stderr=f"VM 内命令超过 {timeout}s 未返回结果（bridge 端应已杀树）",
                backend=self.name,
            )
        try:
            data = json.loads(result_file.read_text(encoding="utf-8-sig"))
        finally:
            result_file.unlink(missing_ok=True)
        session.last_used = time.monotonic()
        return CommandResult(
            exit_code=data.get("exit_code"),
            timed_out=bool(data.get("timed_out")),
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
            backend=self.name,
        )

__all__ = ["WindowsSandboxBackend", "is_sandbox_available"]
