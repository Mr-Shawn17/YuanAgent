"""local 模式转发助手：文件三件套与开发工具（git/依赖/codex 委派）共用。

与 hub（LocalTaskHub.dispatch）的对接细节集中在此：同步工具函数内经
asyncio.run 桥接异步 router。同步工具在 Agent 的 ainvoke 下走
run_in_executor 线程池，本函数在工作线程执行，没有运行中的事件循环，
asyncio.run 安全；CLI 直接 invoke 亦同。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.tools.base import ToolContext, ToolEntryConfig

LOCAL_MODE = "local"


def wants_local(entry: ToolEntryConfig, context: ToolContext) -> bool:
    """是否启用本地执行器模式：显式 local 且注入了可用 router 才转发。

    CLI 场景不注入 router：文件工具退化为直读本机沙箱（语义等价）；
    开发工具（git/依赖/codex）无本进程实现，由各工厂返回可读提示。
    """
    return entry.mode.strip().lower() == LOCAL_MODE and context.local_router is not None


def dispatch_local(
    context: ToolContext,
    *,
    tool_name: str,
    args: dict[str, Any],
    timeout: float | None = None,
) -> str:
    """把工具任务转发到用户执行器；router 保证不抛异常，返回可读文本。

    本函数运行在 worker 线程（同步工具经 run_in_executor 调用）。hub 场景必须
    经 dispatch_threadsafe 投递回服务主循环执行——在线程内 asyncio.run 新建
    循环直接 await 会造成跨循环唤醒不可靠（websocket/future 循环亲和性），
    概率性永久挂起。无 threadsafe 能力的 router（测试替身）回退直跑。
    """
    router: Any = context.local_router
    if router is None:
        return "[local_runner_offline] 本地执行器未接入，无法执行本工具"
    coro = router.dispatch(
        user_id=context.user_id,
        tool=tool_name,
        args=args,
        workspace=context.workspace,
        timeout=timeout,
    )
    threadsafe = getattr(router, "dispatch_threadsafe", None)
    if callable(threadsafe):
        return str(threadsafe(coro, timeout))
    return str(asyncio.run(coro))


__all__ = ["LOCAL_MODE", "dispatch_local", "wants_local"]
