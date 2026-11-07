"""API 运行时装配容器：一次组装（lifespan），每个请求轻量派生 agent。

设计对齐 cli.py 的 _chat_loop：checkpointer/store/MCP/模型是进程级共享资源，
而 EventBus、ToolContext、中间件与编译后的图按请求新建（成本低、无跨会话串扰）。
HITL 审批从 CLI 的同步询问换成 LangGraph 的 interrupt()/Command(resume=...)：
审批回调里 interrupt 挂起图，前端 POST /v1/chat/{run_id}/approve 后恢复执行。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.types import interrupt

from app.agents.builder import build_agent
from app.agents.subagents import apply_routing, load_routing_config, routing_note
from app.api.local_hub import LocalTaskHub
from app.core.config import AgentFileConfig, Settings
from app.core.events import EventBus
from app.core.logging import get_logger
from app.db.engine import Database
from app.db.session_repo import SessionRepo
from app.llm.factory import get_chat_model
from app.memory import MemoryExtractor
from app.middleware.guardrail import GuardrailMiddleware
from app.middleware.memory_recall import MemoryRecallMiddleware
from app.middleware.persona import PersonaMiddleware
from app.middleware.skill import make_skill_middleware
from app.middleware.telemetry import TelemetryMiddleware
from app.middleware.tool_permission import ToolAuditWriter, ToolPermissionMiddleware
from app.skills import load_skills
from app.tools.base import ToolContext
from app.tools.permissions import ApprovalCallback, PermissionTier, ToolCallRequest
from app.tools.registry import build_tools

logger = get_logger(__name__)

# 等待在途记忆抽取任务的最长秒数（与 CLI 的 MEMORY_FLUSH_TIMEOUT 一致）
EXTRACTION_FLUSH_TIMEOUT = 30.0
# 待审批 run 的存活时间：超时后 approve 返回 404（图仍挂在 checkpoint 里，不丢数据）
PENDING_RUN_TTL = 3600.0


def make_web_approval() -> ApprovalCallback:
    """Web 场景的审批回调：interrupt 挂起图，等待 Command(resume=布尔) 恢复。

    interrupt 之前的代码在 resume 后会重新执行（LangGraph 语义），
    因此 ToolStart/ApprovalRequired 事件可能重发一次，前端按 call_id 幂等处理。
    """

    async def _approve(request: ToolCallRequest) -> bool:
        decision = interrupt(
            {
                "kind": "tool_approval",
                "tool_name": request.tool_name,
                "tier": request.tier,
                "summary": request.summary(),
            }
        )
        return bool(decision)

    return _approve


@dataclass(slots=True)
class PendingRun:
    """一个等待审批恢复的 run。"""

    run_id: str
    thread_id: str
    user_id: str
    created_at: float


class RunRegistry:
    """进程内 run 注册表：活跃线程互斥 + 待审批 run 的 run_id 映射。

    只做单进程内存态：服务重启后待审批 run_id 失效（返回 404），
    但图的挂起状态在 MySQL checkpoint 里，数据不丢。
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingRun] = {}
        self._active_threads: set[str] = set()

    def acquire(self, thread_id: str) -> bool:
        """占住一个线程的执行权；已有活跃 run 时返回 False（调用方回 409）。"""
        self._purge_expired()
        if thread_id in self._active_threads:
            return False
        self._active_threads.add(thread_id)
        return True

    def release(self, thread_id: str) -> None:
        self._active_threads.discard(thread_id)

    def register_pending(self, thread_id: str, user_id: str) -> str:
        """图被 interrupt 挂起后登记 run_id，供 approve 端点换取 thread_id。"""
        run_id = uuid.uuid4().hex
        self._pending[run_id] = PendingRun(
            run_id=run_id,
            thread_id=thread_id,
            user_id=user_id,
            created_at=time.time(),
        )
        return run_id

    def pop_pending(self, run_id: str) -> PendingRun | None:
        self._purge_expired()
        return self._pending.pop(run_id, None)

    def _purge_expired(self) -> None:
        deadline = time.time() - PENDING_RUN_TTL
        expired = [key for key, run in self._pending.items() if run.created_at < deadline]
        for key in expired:
            del self._pending[key]


def make_audit_writer(database: Database | None) -> ToolAuditWriter | None:
    """把审计写入桥接到 MySQL（与 CLI 的 _make_audit_writer 相同策略）。"""
    if database is None:
        return None
    db = database

    async def _write(
        *,
        session_id: str,
        tool_name: str,
        tier: str,
        status: str,
        args_redacted: dict[str, Any],
        approved_by_user: bool,
        latency_ms: int,
        error_message: str | None,
    ) -> None:
        async with db.session() as session:
            await SessionRepo(session).record_tool_audit(
                session_id=session_id or "unknown",
                tool_name=tool_name,
                tier=tier,
                status=status,
                args_redacted=args_redacted,
                approved_by_user=approved_by_user,
                latency_ms=latency_ms,
                error_message=error_message,
            )

    return _write


@dataclass
class AgentRuntime:
    """进程级共享资源 + 按请求派生 agent 的工厂。"""

    settings: Settings
    agent_config: AgentFileConfig
    checkpointer: Any
    store: Any
    model: BaseChatModel
    fast_model: BaseChatModel
    database: Database | None = None
    mcp_tools: list[BaseTool] = field(default_factory=list)
    mcp_tiers: dict[str, PermissionTier] = field(default_factory=dict)
    runs: RunRegistry = field(default_factory=RunRegistry)
    extract_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    extractor: MemoryExtractor | None = None
    local_hub: LocalTaskHub = field(default_factory=LocalTaskHub)

    def __post_init__(self) -> None:
        if self.extractor is None:
            self.extractor = MemoryExtractor(
                store=self.store,
                user_id=self.settings.default_user_id,
                model=self.fast_model,
            )
        # 对齐 settings 的下发超时配置（hub 默认 120s，可用 local_dispatch_timeout 覆盖）
        self.local_hub.set_dispatch_timeout(self.settings.local_dispatch_timeout)

    @property
    def user_id(self) -> str:
        return self.settings.default_user_id

    def build_run_agent(
        self,
        *,
        session_id: str,
        event_bus: EventBus,
        approval: ApprovalCallback | None,
        workspace: str = "",
    ) -> tuple[Any, ToolPermissionMiddleware]:
        """按请求装配一个 agent（中间件顺序与 CLI 完全一致），返回 (agent, 权限中间件)。"""
        user_id = self.user_id
        # 技能目录：进程级加载一次（启动到重启之间技能集合不变），空目录不装配
        skill_catalog = load_skills()
        tool_context = ToolContext(
            settings=self.settings,
            user_id=user_id,
            session_id=session_id,
            store=self.store,
            event_bus=event_bus,
            skill_catalog=skill_catalog,
            workspace=workspace,
        )
        # 文件工具 local 模式：任务经 hub 转发到该用户的本地执行器。
        # 必须在 build_tools 之前注入——文件工具工厂在装配期就据
        # context.local_router 判定 use_local，之后注入会让 mode=local 退化为 server 直连。
        if self.settings.local_runner_enabled:
            tool_context.local_router = self.local_hub
        registry = build_tools(tool_context)
        registry.tools.extend(self.mcp_tools)
        registry.tiers.update(self.mcp_tiers)

        # P6' 子 Agent 分流：装配 delegate 工具并收缩主 Agent 工具集（未启用时返回 None）
        routing_config = load_routing_config()
        routing = apply_routing(
            registry=registry,
            config=routing_config,
            model_for=self._model_for(),
            event_bus=event_bus,
            audit_writer=make_audit_writer(self.database),
        )
        routing_notes = ""
        if routing is not None:
            registry.tools = routing.main_tools
            registry.tiers.update(routing.delegate_tiers)
            routing_notes = routing_note(routing, routing_config)

        limits = self.agent_config.limits
        tool_limit_mw = ToolCallLimitMiddleware(
            run_limit=limits.max_tool_calls_per_turn,
            exit_behavior="end",
        )
        model_limit_mw = ModelCallLimitMiddleware(
            run_limit=limits.max_model_calls_per_turn,
            exit_behavior="end",
        )
        permission_mw = ToolPermissionMiddleware(
            tiers=registry.tiers,
            approval=approval,
            audit_writer=make_audit_writer(self.database),
            event_bus=event_bus,
        )
        persona = self.agent_config.persona
        persona_mw = PersonaMiddleware(
            persona_name=persona.name,
            persona_role=persona.role,
            persona_style=persona.style,
            persona_boundaries=persona.boundaries,
        )
        recall_mw = MemoryRecallMiddleware(store=self.store, user_id=user_id, event_bus=event_bus)
        skill_mw = make_skill_middleware(skill_catalog.records)
        telemetry_mw = TelemetryMiddleware(user_id=user_id, session_id=session_id)
        guardrail_mw = GuardrailMiddleware()
        summarization_mw = SummarizationMiddleware(
            self.fast_model,
            trigger=("tokens", self.agent_config.history.summarize_trigger_tokens),
            keep=("messages", self.agent_config.history.keep_messages),
        )
        agent = build_agent(
            model=self.model,
            tools=registry.tools,
            middleware=[
                tool_limit_mw,
                model_limit_mw,
                permission_mw,
                persona_mw,
                recall_mw,
                skill_mw,
                telemetry_mw,
                guardrail_mw,
                summarization_mw,
            ],
            settings=self.settings,
            agent_config=self.agent_config,
            checkpointer=self.checkpointer,
            store=self.store,
            user_id=user_id,
            notes=routing_notes,
        )
        return agent, permission_mw

    def _model_for(self) -> Callable[[str], BaseChatModel]:
        """子 Agent 的模型工厂：fast 档关流式（子 Agent 不需要 token 流）。"""

        def factory(tier: str) -> BaseChatModel:
            if tier == "fast":
                return get_chat_model("fast", settings=self.settings, streaming=False)
            return get_chat_model(tier, settings=self.settings)

        return factory

    def schedule_extraction(self, messages: list[Any]) -> None:
        """轮结束后 fire-and-forget 调度记忆抽取（与 CLI 的 _schedule_extraction 一致）。"""
        extractor = self.extractor
        if extractor is None or not messages:
            return

        async def _run() -> None:
            try:
                written = await extractor.extract_and_merge(messages)
            except Exception:
                logger.warning("memory_extract_failed", exc_info=True)
                return
            if written:
                logger.info("memory_extracted", written=written)

        task = asyncio.create_task(_run())
        self.extract_tasks.add(task)
        task.add_done_callback(self.extract_tasks.discard)

    async def shutdown(self) -> None:
        """服务关停前等待在途抽取任务，超时则取消（消息在 checkpoint 里，不丢）。"""
        await self.local_hub.aclose()
        pending = {task for task in self.extract_tasks if not task.done()}
        if pending:
            _, unfinished = await asyncio.wait(pending, timeout=EXTRACTION_FLUSH_TIMEOUT)
            for task in unfinished:
                task.cancel()


__all__ = [
    "EXTRACTION_FLUSH_TIMEOUT",
    "PENDING_RUN_TTL",
    "AgentRuntime",
    "PendingRun",
    "RunRegistry",
    "make_audit_writer",
    "make_web_approval",
]
