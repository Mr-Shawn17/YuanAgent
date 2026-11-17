"""会话级沙箱池（spec docs/opensandbox-migration.md §4，Q8/Q9 决策）。

生命周期（每会话一个常驻沙箱，会话内 git_query/rg_query 反复复用）：

    create（首条命令触发，秒级：镜像就绪 + execd 注入 + 就绪探测）
      → 会话内反复 execd /command（毫秒级，acquire 只做映射查询 + touch）
      → 空闲 idle_ttl_seconds（sweeper 定时扫描）或会话结束（destroy_session，
        硬规则不可配）或进程退出（destroy_all）→ destroy

关键设计：

- **不用 opensandbox 的 timeout 绝对过期**（Q9：它是「创建后 N 秒」而非「空闲 N 秒」），
  create 时 timeout=None，空闲回收由本模块 sweeper 实现；
- **崩溃惰性重建**（TC-04）：server 重启/沙箱被外部销毁后，执行层命令失败即调
  discard 丢映射，下次 acquire 重新 create——本模块不做主动健康检查（命令失败即
  真相，不为每条命令加一次往返）；
- **挂载形态**（实施期定版）：沙箱挂 Engine 侧 mirrors 根（只读，create 时固定），
  会话内换仓库无需重建沙箱，具体仓库由执行层用 working_directory 指向
  /repo/<owner>/<repo>（safe.directory 经命令级 envs 注入）；
- opensandbox SDK 懒加载：依赖缺失只禁用沙箱查询（requires_opensandbox 语义），
  不影响其他模块导入；
- **降级阈值防误配**：mirror_engine_root 未配成 Engine 侧绝对路径时 create 直接报
  sandbox_unavailable（Windows dev 的 UNC 路径不是 Engine 路径）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.sandbox.container import (
    OS_IMAGE,
    OS_MOUNT_TARGET,
    OS_READY_TIMEOUT_SECONDS,
    SandboxUnavailableError,
    _os_connection_config,
    describe,
)

logger = get_logger(__name__)

SWEEP_INTERVAL_SECONDS = 15  # 空闲扫描周期（TTL 判定精度上界）


@dataclass
class _PoolEntry:
    """一个会话的沙箱持有者：sandbox 实例 + 最后使用时间。"""

    sandbox: Any  # opensandbox.Sandbox（SDK 实例，类型懒加载不标注）
    created_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)


class SandboxPool:
    """session_id → sandbox 映射 + 空闲回收 + 会话销毁 + 惰性重建映射清理。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._entries: dict[str, _PoolEntry] = {}
        self._lock = asyncio.Lock()
        self._sweeper_task: asyncio.Task[None] | None = None

    # ---- 对执行层（container.py）的三个入口 --------------------------------

    async def acquire(self, session_id: str) -> Any:
        """取会话沙箱：复用或新建；同时 touch 最后使用时间并确保 sweeper 存活。"""
        async with self._lock:
            entry = self._entries.get(session_id)
            if entry is None:
                entry = _PoolEntry(sandbox=await self._create_sandbox(session_id))
                self._entries[session_id] = entry
                logger.info(
                    "sandbox_pool_created",
                    session_id=session_id,
                    sandbox_id=entry.sandbox.id,
                )
            entry.last_used = time.monotonic()
            self._ensure_sweeper()
            return entry.sandbox

    async def discard(self, session_id: str) -> None:
        """丢映射（崩溃惰性重建的第一半）：尽量销毁残留沙箱，下次 acquire 重建。"""
        async with self._lock:
            entry = self._entries.pop(session_id, None)
        if entry is not None:
            await self._destroy_sandbox(entry.sandbox, session_id, "discard")

    async def destroy_session(self, session_id: str) -> bool:
        """会话结束强制销毁（硬规则，spec §4.3）：无论是否达到空闲 TTL。"""
        async with self._lock:
            entry = self._entries.pop(session_id, None)
        if entry is None:
            return False
        await self._destroy_sandbox(entry.sandbox, session_id, "session_end")
        return True

    async def destroy_all(self) -> None:
        """进程退出兜底：销毁全部会话沙箱并停 sweeper。"""
        self._stop_sweeper()
        async with self._lock:
            entries = list(self._entries.items())
            self._entries.clear()
        for session_id, entry in entries:
            await self._destroy_sandbox(entry.sandbox, session_id, "shutdown")

    def live_session_ids(self) -> list[str]:
        """当前存活会话（观测/测试用）。"""
        return list(self._entries)

    # ---- 内部 ----------------------------------------------------------------

    def _connection_config(self) -> Any:
        """SDK ConnectionConfig（懒加载；url/api_key 来自配置面，实现在 container）。"""
        return _os_connection_config(self._settings.sandbox_query, sync=False)

    def _engine_mirror_root(self) -> str:
        """Engine 侧 mirrors 根（沙箱只读挂载源）。opensandbox 驱动下必须是
        Engine 内绝对路径——Windows dev 的 UNC 路径不是 Engine 路径，直接拒绝。"""
        cfg = self._settings.sandbox_query
        root = cfg.mirror_engine_root.strip() or cfg.mirror_root.strip()
        if not root.startswith("/"):
            raise SandboxUnavailableError(
                "opensandbox 驱动要求 mirror_engine_root 为 Engine 内绝对路径"
                f"（如 /var/lib/yuan-mirrors），当前为：{root or '（空）'}"
            )
        return root.rstrip("/")

    async def _create_sandbox(self, session_id: str) -> Any:
        """SDK create：会话级形态全量参数（spec §4.1/§7）。SDK 异常统一翻译成
        SandboxUnavailableError——绝不回退宿主直跑（硬不变量，spec §2）。"""
        try:
            from datetime import timedelta

            from opensandbox import Sandbox
            from opensandbox.models.sandboxes import Host, Volume

            cfg = self._settings.sandbox_query
            sandbox = await Sandbox.create(
                OS_IMAGE,
                timeout=None,  # Q9：不用绝对过期，空闲回收由 sweeper 负责
                ready_timeout=timedelta(seconds=OS_READY_TIMEOUT_SECONDS),
                resource={"cpu": f"{int(float(cfg.cpus) * 1000)}m", "memory": cfg.memory},
                env={"HOME": "/tmp", "GIT_CONFIG_NOSYSTEM": "1"},
                metadata={"managed_by": "yuan-backend", "session_id": session_id},
                volumes=[
                    Volume(
                        name="mirror",
                        host=Host(path=self._engine_mirror_root()),
                        mount_path=OS_MOUNT_TARGET,
                        read_only=True,
                    )
                ],
                connection_config=self._connection_config(),
            )
            return sandbox
        except SandboxUnavailableError:
            raise
        except Exception as exc:
            raise SandboxUnavailableError(describe(exc)) from exc

    async def _destroy_sandbox(self, sandbox: Any, session_id: str, reason: str) -> None:
        try:
            await sandbox.destroy()
            logger.info(
                "sandbox_pool_destroyed",
                session_id=session_id,
                sandbox_id=sandbox.id,
                reason=reason,
            )
        except Exception as exc:
            # 销毁失败不阻断调用方（沙箱可能已不存在）；server 侧绝对过期兜底不存在，
            # 残留沙箱依赖 server 重启或手动清理，故这里留 warning 观测
            logger.warning(
                "sandbox_pool_destroy_failed",
                session_id=session_id,
                sandbox_id=getattr(sandbox, "id", "?"),
                reason=reason,
                error=describe(exc),
            )

    # ---- 空闲回收（Q9：backend 自建） ----------------------------------------

    def _idle_ttl(self) -> float:
        """读配置面而非构造期快照：测试可动态调小 TTL。"""
        return float(self._settings.sandbox_query.idle_ttl_seconds)

    def _ensure_sweeper(self) -> None:
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        self._sweeper_task = asyncio.create_task(self._sweep_loop(), name="sandbox-pool-sweeper")

    def _stop_sweeper(self) -> None:
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            self._sweeper_task = None

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            await self._sweep_once()

    async def _sweep_once(self) -> None:
        """单轮空闲扫描（拆出便于测试）：销毁超过 idle_ttl 未使用的会话沙箱。"""
        now = time.monotonic()
        ttl = self._idle_ttl()
        async with self._lock:
            expired = [
                session_id
                for session_id, entry in self._entries.items()
                if now - entry.last_used > ttl
            ]
            victims = [self._entries.pop(session_id) for session_id in expired]
        for session_id, entry in zip(expired, victims, strict=True):
            await self._destroy_sandbox(entry.sandbox, session_id, "idle_ttl")


# ---- 进程级单例（tools 装配与执行层共用；测试用 reset_pool 隔离） --------------

_pool: SandboxPool | None = None
_pool_settings_id: int | None = None


def get_pool(settings: Settings) -> SandboxPool:
    """按 settings 取进程级池（settings 变化即重建——配置热改不串池）。"""
    global _pool, _pool_settings_id
    marker = id(settings)
    if _pool is None or _pool_settings_id != marker:
        _pool = SandboxPool(settings)
        _pool_settings_id = marker
    return _pool


def reset_pool() -> None:
    """测试隔离：丢弃单例（不销毁沙箱——测试用替身沙箱）。"""
    global _pool, _pool_settings_id
    if _pool is not None:
        _pool._stop_sweeper()
    _pool = None
    _pool_settings_id = None


__all__ = [
    "SWEEP_INTERVAL_SECONDS",
    "SandboxPool",
    "get_pool",
    "reset_pool",
]
