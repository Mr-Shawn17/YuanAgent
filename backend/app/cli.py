from __future__ import annotations

import asyncio
import logging
import signal
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import typer
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from app import __version__
from app.agents.builder import (
    build_agent,
    list_sessions,
    mysql_checkpointer,
    mysql_store,
    thread_config,
)
from app.agents.state import initial_state_fields
from app.agents.subagents import apply_routing, load_routing_config, routing_note
from app.core.config import Settings, get_agent_config, get_settings
from app.core.errors import AppError
from app.core.events import (
    AgentEvent,
    ApprovalDecidedEvent,
    CitationsEvent,
    EventBus,
    MemoryRecalledEvent,
    ToolEndEvent,
    ToolStartEvent,
    make_event_logger,
)
from app.core.logging import get_logger, setup_logging
from app.db.device_repo import DeviceRepo, IssuedToken
from app.db.engine import Database, build_database, database_scope
from app.db.message_sync import persist_new_messages
from app.db.models import Device
from app.db.session_repo import SessionRepo
from app.llm.factory import get_chat_model, ping_model
from app.mcp import McpManager
from app.memory import MemoryExtractor
from app.middleware.guardrail import GuardrailMiddleware
from app.middleware.memory_recall import MemoryRecallMiddleware
from app.middleware.persona import PersonaMiddleware
from app.middleware.skill import make_skill_middleware
from app.middleware.telemetry import TelemetryMiddleware
from app.middleware.tool_permission import ToolAuditWriter, ToolPermissionMiddleware
from app.observability import make_callbacks
from app.skills import SkillCatalog, load_skills
from app.tools.base import ToolContext
from app.tools.permissions import ToolCallRequest
from app.tools.registry import build_tools

cli_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="日常聊天 Agent（Tools + RAG + MCP）命令行入口",
)
console = Console()
logger = get_logger(__name__)

# 退出时等待在途记忆抽取任务的最长秒数（抽取要跑一次 fast 模型，比审计慢）
MEMORY_FLUSH_TIMEOUT = 30.0

SLASH_HELP = """可用斜杠命令：
  /new            新建一个会话
  /sessions       列出历史会话（可恢复）
  /use <id前缀>   切换到指定会话
  /model          显示当前模型配置
  /skills         列出已加载的技能（Skills）
  /help           显示本帮助
  /exit           退出
Ctrl+C：生成过程中中断本轮，输入过程中退出程序。"""


class _InterruptGuard:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task[Any] | None = None
        self._previous: Any = None

    def install(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._handle)

    def restore(self) -> None:
        if self._previous is not None:
            signal.signal(signal.SIGINT, self._previous)
            self._previous = None

    def _handle(self, signum: int, frame: Any) -> None:
        task = self.task
        loop = self.loop
        if task is not None and not task.done() and loop is not None:
            loop.call_soon_threadsafe(task.cancel)
            return
        raise KeyboardInterrupt


def _chunk_text(chunk: Any) -> str:
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (bytes, str)):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return ""


async def _collect_sessions(checkpointer: Any, *, limit: int = 400) -> list[dict[str, Any]]:
    # 会话列举逻辑已抽到 agents/builder.list_sessions，供 CLI 与 API 共用
    return await list_sessions(checkpointer, limit=limit)


def _render_sessions(sessions: list[dict[str, Any]]) -> None:
    if not sessions:
        console.print("[dim]还没有任何历史会话。[/]")
        return
    table = Table(title="历史会话", show_lines=False, expand=True)
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("会话 ID", style="cyan", no_wrap=True)
    table.add_column("更新时间", style="green", no_wrap=True)
    table.add_column("消息", justify="right", width=6)
    table.add_column("首条提问")
    for index, row in enumerate(sessions, start=1):
        table.add_row(
            str(index),
            row["thread_id"][:12],
            row["updated_at"],
            str(row["message_count"]),
            row["title"],
        )
    console.print(table)


TIER_LABELS = {"read": "只读", "write": "写操作", "danger": "危险操作"}


def _render_event(event: AgentEvent) -> None:
    """把事件总线上的工具轨迹/引用渲染成终端输出。

    Rich Live 激活时 console.print 会自动把内容打在流式区上方，不会撕裂画面；
    approval_required 不在这里渲染——审批面板由 _cli_approval 自己画。
    """
    if isinstance(event, ToolStartEvent):
        label = TIER_LABELS.get(event.tier, event.tier)
        origin = "" if event.origin == "builtin" else f"[{event.origin}] "
        console.print(f"[dim]🔧 {origin}调用 {event.tool_name} · {label}档[/]")
    elif isinstance(event, ApprovalDecidedEvent):
        if event.approved:
            console.print(f"[green]✓ 已批准执行 {event.tool_name}[/]")
        else:
            console.print(f"[red]✗ 已拒绝执行 {event.tool_name}[/]")
    elif isinstance(event, ToolEndEvent):
        if event.ok:
            console.print(f"[dim]✓ {event.tool_name} 完成 · {event.latency_ms}ms[/]")
        else:
            preview = f" · {event.preview}" if event.preview else ""
            console.print(f"[red]✗ {event.tool_name} 失败 · {event.latency_ms}ms{preview}[/]")
    elif isinstance(event, MemoryRecalledEvent) and event.count:
        console.print(f"[dim]🧠 已召回 {event.count} 条长期记忆[/]")
    elif isinstance(event, CitationsEvent) and event.items:
        lines: list[str] = []
        for index, item in enumerate(event.items, start=1):
            source = item.get("source") or {}
            title = source.get("title") or source.get("doc_id") or "未知来源"
            lines.append(f"[{index}] {title}")
        console.print("[dim]引用：" + "  ".join(lines) + "[/]")


async def _generate_turn(
    agent: Any,
    payload: dict[str, Any],
    config: dict[str, Any],
    *,
    assistant_name: str,
    bus: EventBus | None = None,
) -> str:
    console.print()
    console.print(f"[bold magenta]{assistant_name} ›[/]")
    parts: list[str] = []
    unsubscribe = bus.subscribe(_render_event) if bus is not None else None
    # 事件日志：工具调用 / 子 Agent 拆分等关键事件落运行日志（每轮订阅，finally 退订）
    session_id = str((config.get("configurable") or {}).get("thread_id", ""))
    unsubscribe_event_log = (
        bus.subscribe(make_event_logger(session_id=session_id)) if bus is not None else None
    )
    try:
        with Live(
            Text(""),
            console=console,
            refresh_per_second=15,
            vertical_overflow="visible",
        ) as live:
            async for item in agent.astream(payload, config, stream_mode="messages"):
                chunk = item[0] if isinstance(item, tuple) else item
                if not isinstance(chunk, AIMessageChunk):
                    continue
                delta = _chunk_text(chunk)
                if not delta:
                    continue
                parts.append(delta)
                live.update(Markdown("".join(parts)))
    finally:
        if unsubscribe is not None:
            unsubscribe()
        if unsubscribe_event_log is not None:
            unsubscribe_event_log()
    return "".join(parts)


async def _run_extraction(extractor: MemoryExtractor, messages: list[Any]) -> None:
    """跑一次后台记忆抽取；失败只记日志，绝不影响对话主链路。"""
    try:
        written = await extractor.extract_and_merge(messages)
    except Exception:
        logger.warning("memory_extract_failed", exc_info=True)
        return
    if written:
        console.print(f"[dim]🧠 后台整理记忆：新增/更新 {written} 条[/]")


def _schedule_extraction(
    extractor: MemoryExtractor,
    messages: list[Any],
    tasks: set[asyncio.Task[None]],
) -> None:
    """fire-and-forget 调度抽取任务；退出前由 _flush_extractions 兜底等待。"""
    if not messages:
        return
    task = asyncio.create_task(_run_extraction(extractor, list(messages)))
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _flush_extractions(tasks: set[asyncio.Task[None]]) -> None:
    """退出前等待在途抽取任务，超时则取消（记忆晚点再抽也不丢，消息在 checkpoint 里）。"""
    pending = {task for task in tasks if not task.done()}
    if not pending:
        return
    console.print(f"[dim]等待 {len(pending)} 个记忆整理任务…[/]")
    _, unfinished = await asyncio.wait(pending, timeout=MEMORY_FLUSH_TIMEOUT)
    for task in unfinished:
        task.cancel()


def _make_audit_writer(database: Database) -> ToolAuditWriter:
    """把审计写入桥接到 MySQL：每次调用独立事务，失败由中间件降级为日志。"""

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
        async with database.session() as session:
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


async def _cli_approval(request: ToolCallRequest) -> bool:
    """CLI 审批回调：write/danger 工具执行前在终端同步询问（P5' Web 换成 interrupt）。"""
    tier_label = "危险操作" if request.tier == "danger" else "写操作"
    border = "red" if request.tier == "danger" else "yellow"
    console.print(
        Panel(
            f"[bold]{request.tool_name}[/] · {tier_label}\n[dim]{request.summary()}[/]",
            title="工具执行确认",
            border_style=border,
            expand=False,
        )
    )
    try:
        answer = await asyncio.to_thread(console.input, "允许执行吗？[y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in {"y", "yes"}


def _model_for(settings: Settings) -> Callable[[str], BaseChatModel]:
    """子 Agent 的模型工厂：fast 档关流式（子 Agent 不需要 token 流）。"""

    def factory(tier: str) -> BaseChatModel:
        if tier == "fast":
            return get_chat_model("fast", settings=settings, streaming=False)
        return get_chat_model(tier, settings=settings)

    return factory


async def _chat_loop(
    *,
    settings: Settings,
    session_id: str,
    check: bool,
) -> None:
    agent_config = get_agent_config()
    assistant_name = agent_config.persona.name

    if check:
        with console.status("[dim]正在校验模型配置…[/]", spinner="dots"):
            await ping_model("main", settings=settings)
        console.print("[green]✓[/] 模型连通正常")

    settings.ensure_dirs()
    database = build_database(settings)
    try:
        async with (
            mysql_checkpointer(settings) as checkpointer,
            mysql_store(settings) as store,
            McpManager() as mcp_manager,
        ):
            model = get_chat_model("main", settings=settings)
            # fast 模型复用：会话摘要压缩 + 后台记忆抽取
            fast_model = get_chat_model("fast", settings=settings, streaming=False)
            bus = EventBus()
            user_id = settings.default_user_id
            # 技能目录：进程级加载一次，空目录不装配（功能静默关闭）
            skill_catalog = load_skills()
            tool_context = ToolContext(
                settings=settings,
                user_id=user_id,
                session_id=session_id,
                store=store,
                event_bus=bus,
                skill_catalog=skill_catalog,
            )
            registry = build_tools(tool_context)
            # MCP 工具并入注册表：命名空间前缀与档位已在 McpManager 内处理
            registry.tools.extend(mcp_manager.tools)
            registry.tiers.update(mcp_manager.tiers)
            # P6' 子 Agent 分流：装配 delegate 工具并收缩主 Agent 工具集（未启用时返回 None）
            routing_config = load_routing_config()
            routing = apply_routing(
                registry=registry,
                config=routing_config,
                model_for=_model_for(settings),
                event_bus=bus,
                audit_writer=_make_audit_writer(database),
            )
            routing_notes = ""
            if routing is not None:
                registry.tools = routing.main_tools
                registry.tiers.update(routing.delegate_tiers)
                routing_notes = routing_note(routing, routing_config)
            permission_mw = ToolPermissionMiddleware(
                tiers=registry.tiers,
                approval=_cli_approval,
                audit_writer=_make_audit_writer(database),
                event_bus=bus,
            )
            limits = agent_config.limits
            tool_limit_mw = ToolCallLimitMiddleware(
                run_limit=limits.max_tool_calls_per_turn,
                exit_behavior="end",
            )
            model_limit_mw = ModelCallLimitMiddleware(
                run_limit=limits.max_model_calls_per_turn,
                exit_behavior="end",
            )
            persona = agent_config.persona
            persona_mw = PersonaMiddleware(
                persona_name=persona.name,
                persona_role=persona.role,
                persona_style=persona.style,
                persona_boundaries=persona.boundaries,
            )
            recall_mw = MemoryRecallMiddleware(store=store, user_id=user_id, event_bus=bus)
            skill_mw = make_skill_middleware(skill_catalog.records)
            telemetry_mw = TelemetryMiddleware(user_id=user_id)
            guardrail_mw = GuardrailMiddleware()
            summarization_mw = SummarizationMiddleware(
                fast_model,
                trigger=("tokens", agent_config.history.summarize_trigger_tokens),
                keep=("messages", agent_config.history.keep_messages),
            )
            extractor = MemoryExtractor(store=store, user_id=user_id, model=fast_model)
            extract_tasks: set[asyncio.Task[None]] = set()
            agent = build_agent(
                model=model,
                tools=registry.tools,
                # 顺序即包裹顺序：guardrail 最贴近工具执行，summarization 最贴近模型调用
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
                settings=settings,
                agent_config=agent_config,
                checkpointer=checkpointer,
                store=store,
                user_id=user_id,
                notes=routing_notes,
            )

            guard = _InterruptGuard()
            guard.install()
            header = (
                f"[bold]{assistant_name}[/] · 模型 [cyan]{settings.llm_model}[/] "
                f"· 会话 [cyan]{session_id[:12]}[/]\n"
                f"[dim]工具：{', '.join(registry.names) or '(无)'}[/]\n[dim]{SLASH_HELP}[/]"
            )
            if registry.disabled:
                header += f"\n[dim]已禁用：{', '.join(sorted(registry.disabled))}[/]"
            if routing is not None:
                summary = "、".join(
                    f"{name}（{len(tools)} 工具）"
                    for name, tools in routing.sub_agent_tools.items()
                )
                header += f"\n[dim]子 Agent：{summary}[/]"
            if skill_catalog.records:
                header += f"\n[dim]技能：{', '.join(skill_catalog.names)}[/]"
            if skill_catalog.disabled:
                header += (
                    f"\n[dim]技能未加载：{', '.join(sorted(skill_catalog.disabled))}"
                    "（/skills 查看原因）[/]"
                )
            if mcp_manager.connected:
                header += f"\n[dim]MCP 已连接：{', '.join(mcp_manager.connected)}[/]"
            if mcp_manager.failed:
                header += f"\n[dim]MCP 连接失败：{', '.join(sorted(mcp_manager.failed))}[/]"
            console.print(
                Panel(
                    header,
                    title=f"日常聊天 Agent v{__version__}",
                    border_style="blue",
                    expand=False,
                )
            )
            try:
                while True:
                    try:
                        user_input = await asyncio.to_thread(console.input, "[bold cyan]你 ›[/] ")
                    except (EOFError, KeyboardInterrupt):
                        break
                    text = user_input.strip()
                    if not text:
                        continue

                    if text.startswith("/"):
                        action = await _handle_slash(
                            text,
                            checkpointer=checkpointer,
                            settings=settings,
                            session_id=session_id,
                            skill_catalog=skill_catalog,
                        )
                        if action.get("exit"):
                            break
                        if isinstance(action.get("session_id"), str):
                            session_id = action["session_id"]
                            console.print(f"[green]✓[/] 已切换到会话 [cyan]{session_id[:12]}[/]")
                        continue

                    payload = {
                        "messages": [HumanMessage(content=text)],
                        **initial_state_fields(
                            session_id=session_id,
                            user_id=settings.default_user_id,
                        ),
                    }
                    config = thread_config(session_id)
                    langfuse_callbacks = make_callbacks(
                        session_id=session_id, user_id=settings.default_user_id
                    )
                    if langfuse_callbacks:
                        config = {**config, "callbacks": langfuse_callbacks}
                    # 用户问题入口日志（截断防超长），供排查完整调用链
                    logger.info(
                        "chat_request",
                        session_id=session_id,
                        user_id=settings.default_user_id,
                        message=" ".join(text.split())[:200],
                    )
                    # 本轮开始前的消息快照：用于轮结束后 diff 新增消息写入业务表
                    before_messages: list[Any] = []
                    try:
                        before_state = await agent.aget_state(cast(RunnableConfig, config))
                        before_messages = list(
                            (getattr(before_state, "values", None) or {}).get("messages") or []
                        )
                    except Exception:
                        logger.warning("pre_run_state_failed", exc_info=True)
                    task: asyncio.Task[str] = asyncio.create_task(
                        _generate_turn(
                            agent, payload, config, assistant_name=assistant_name, bus=bus
                        )
                    )
                    guard.task = task
                    try:
                        await asyncio.wait({task})
                        if task.cancelled():
                            console.print("\n[yellow]⏹ 已中断本轮生成[/]")
                            continue
                        error = task.exception()
                        if error is not None:
                            _report_error(error)
                            continue
                        console.print()
                        # 本轮成功结束后做后台记忆抽取（fire-and-forget，退出前统一等待）
                        state = await agent.aget_state(cast(RunnableConfig, config))
                        turn_messages = list(
                            (getattr(state, "values", None) or {}).get("messages") or []
                        )
                        _schedule_extraction(extractor, turn_messages, extract_tasks)
                        # 同步业务表 messages（冗余副本）：diff 本轮新增后写入
                        await persist_new_messages(
                            database,
                            session_id=session_id,
                            user_id=user_id,
                            before_messages=before_messages,
                            after_messages=turn_messages,
                        )
                    finally:
                        guard.task = None
            finally:
                guard.restore()
                await _flush_extractions(extract_tasks)
                await permission_mw.wait_pending_audits()
                console.print(
                    f"[dim]已退出，历史保存在 MySQL（{settings.mysql_host}:{settings.mysql_port}"
                    f"/{settings.mysql_database}）[/]"
                )
    finally:
        await database.aclose()


async def _handle_slash(
    text: str,
    *,
    checkpointer: Any,
    settings: Settings,
    session_id: str,
    skill_catalog: SkillCatalog | None = None,
) -> dict[str, Any]:
    parts = text[1:].split(maxsplit=1)
    command = parts[0].lower() if parts else ""
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command in {"exit", "quit", "q"}:
        return {"exit": True}
    if command in {"new", "n"}:
        return {"session_id": uuid.uuid4().hex}
    if command in {"help", "h", "?"}:
        console.print(SLASH_HELP)
        return {}
    if command in {"sessions", "ls"}:
        _render_sessions(await _collect_sessions(checkpointer))
        return {}
    if command in {"use", "resume"}:
        sessions = await _collect_sessions(checkpointer)
        if not argument:
            _render_sessions(sessions)
            return {}
        matched = [row for row in sessions if row["thread_id"].startswith(argument)]
        if len(matched) == 1:
            return {"session_id": matched[0]["thread_id"]}
        if not matched:
            index = int(argument) if argument.isdigit() else 0
            if 0 < index <= len(sessions):
                return {"session_id": sessions[index - 1]["thread_id"]}
            console.print(f"[red]找不到会话：{argument}[/]")
            return {}
        console.print(f"[red]前缀 {argument} 匹配到 {len(matched)} 个会话，请给更长的前缀[/]")
        return {}
    if command == "model":
        console.print(
            f"主模型 [cyan]{settings.llm_model}[/] @ {settings.llm_base_url}\n"
            f"快模型 [cyan]{settings.profile_for('fast').model}[/]\n"
            f"备用   [cyan]{settings.profile_for('fallback').model or '(未配置)'}[/]"
        )
        return {}
    if command == "skills":
        _render_skills(skill_catalog)
        return {}
    console.print(f"[red]未知命令：/{command}[/]\n[dim]{SLASH_HELP}[/]")
    return {}


def _render_skills(catalog: SkillCatalog | None) -> None:
    """渲染技能清单：名称 / 描述 / 脚本 / 禁用原因（与 /sessions 风格一致）。"""
    if catalog is None:
        console.print("[dim]技能功能未启用（config/agent.yaml 的 skills 段）[/]")
        return
    if not catalog.records and not catalog.disabled:
        console.print("[dim]没有已加载的技能（skills/ 目录为空，加目录即生效）[/]")
        return
    if catalog.records:
        table = Table(title="已加载技能", show_lines=False, expand=True)
        table.add_column("技能", style="cyan", no_wrap=True)
        table.add_column("描述")
        table.add_column("脚本", style="dim")
        for record in catalog.records:
            scripts = "、".join(record.scripts) if record.scripts else "-"
            table.add_row(record.name, record.description, scripts)
        console.print(table)
    for name, reason in sorted(catalog.disabled.items()):
        console.print(f"[yellow]未加载[/] {name}：{reason}")


def _report_error(error: BaseException) -> None:
    if isinstance(error, AppError):
        console.print(f"[red]✗ {error.message}[/]")
        return
    console.print(f"[red]✗ 出错了：{type(error).__name__}: {error}[/]")
    # DEBUG 级别才吐完整堆栈，避免日常使用被长 traceback 刷屏
    debug = logging.getLogger().isEnabledFor(logging.DEBUG)
    if not debug:
        console.print("[dim]  加 --log-level DEBUG 重跑可打印完整堆栈[/]")
    logger.error(
        "turn_failed",
        error=str(error),
        error_type=type(error).__name__,
        exc_info=error if debug else None,
    )


def _bootstrap(log_level: str | None, log_json: bool) -> Settings:
    settings = get_settings()
    setup_logging(
        log_level or settings.log_level,
        json_output=log_json or settings.log_json,
        log_dir=settings.log_dir_path,
        retention_days=settings.log_retention_days,
    )
    return settings


@cli_app.command()
def chat(
    session: str = typer.Option("", "--session", "-s", help="恢复指定会话 ID，留空则新建"),
    check: bool = typer.Option(
        True, "--check/--no-check", help="启动前用 1 token 请求校验模型配置"
    ),
    log_level: str = typer.Option("", "--log-level", help="覆盖 LOG_LEVEL"),
    log_json: bool = typer.Option(False, "--log-json", help="以 JSON 格式输出日志"),
) -> None:
    """进入终端流式对话。"""
    settings = _bootstrap(log_level, log_json)
    session_id = session.strip() or uuid.uuid4().hex
    try:
        asyncio.run(_chat_loop(settings=settings, session_id=session_id, check=check))
    except KeyboardInterrupt:
        console.print("[dim]已退出。[/]")
    except AppError as error:
        console.print(f"[red]✗ {error.message}[/]")
        raise typer.Exit(code=1) from None


@cli_app.command()
def sessions(
    limit: int = typer.Option(20, "--limit", "-n", help="最多显示多少个会话"),
    log_level: str = typer.Option("", "--log-level", help="覆盖 LOG_LEVEL"),
    log_json: bool = typer.Option(False, "--log-json", help="以 JSON 格式输出日志"),
) -> None:
    """列出历史会话。"""
    settings = _bootstrap(log_level, log_json)
    settings.ensure_dirs()

    async def _run() -> list[dict[str, Any]]:
        async with mysql_checkpointer(settings) as checkpointer:
            return await _collect_sessions(checkpointer)

    try:
        rows = asyncio.run(_run())
    except AppError as error:
        console.print(f"[red]✗ {error.message}[/]")
        raise typer.Exit(code=1) from None
    _render_sessions(rows[:limit])


@cli_app.command()
def config(
    log_level: str = typer.Option("", "--log-level", help="覆盖 LOG_LEVEL"),
    log_json: bool = typer.Option(False, "--log-json", help="以 JSON 格式输出日志"),
) -> None:
    """显示当前生效的配置（密钥已脱敏）。"""
    settings = _bootstrap(log_level, log_json)
    agent_config = get_agent_config()

    table = Table(title="运行配置", expand=False, show_header=True)
    table.add_column("项", style="cyan", no_wrap=True)
    table.add_column("值")
    for key, value in settings.redacted_dump().items():
        table.add_row(key, str(value) if value not in (None, "") else "[dim](空)[/]")
    console.print(table)
    console.print(
        Panel(
            f"名字：{agent_config.persona.name}\n"
            f"角色：{agent_config.persona.role}\n"
            f"语言：{agent_config.persona.language} · 时区：{agent_config.persona.timezone}\n"
            f"单轮工具上限：{agent_config.limits.max_tool_calls_per_turn} · "
            f"单轮模型上限：{agent_config.limits.max_model_calls_per_turn}\n"
            f"摘要触发 tokens：{agent_config.history.summarize_trigger_tokens} · "
            f"保留消息数：{agent_config.history.keep_messages}",
            title="人设（config/agent.yaml）",
            border_style="blue",
            expand=False,
        )
    )
    console.print(f"[dim]数据目录：{settings.data_dir}[/]")
    console.print(f"[dim]项目根目录：{Path(settings.data_dir).parent}[/]")


@cli_app.command()
def ping(
    tier: str = typer.Option("main", "--tier", "-t", help="main / fast / fallback"),
    log_level: str = typer.Option("", "--log-level", help="覆盖 LOG_LEVEL"),
    log_json: bool = typer.Option(False, "--log-json", help="以 JSON 格式输出日志"),
) -> None:
    """发一次 1 token 请求，验证模型配置是否可用。"""
    settings = _bootstrap(log_level, log_json)
    try:
        reply = asyncio.run(ping_model(tier, settings=settings))
    except AppError as error:
        console.print(f"[red]✗ {error.message}[/]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]✓[/] {tier} 模型可用：{settings.profile_for(tier).model}")
    if reply:
        console.print(f"[dim]回复预览：{reply[:60]}[/]")


@cli_app.command()
def version() -> None:
    """显示版本。"""
    console.print(f"daily-chat-agent v{__version__}")


device_app = typer.Typer(help="本地执行器设备管理（签发 / 列出 / 吊销令牌）")
cli_app.add_typer(device_app, name="device")


@device_app.command("create")
def device_create(
    name: str = typer.Option("", "--name", "-n", help="设备备注名，如 my-laptop"),
    user: str = typer.Option("", "--user", "-u", help="归属用户 ID，留空用默认用户"),
    log_level: str = typer.Option("", "--log-level", help="覆盖 LOG_LEVEL"),
    log_json: bool = typer.Option(False, "--log-json", help="以 JSON 格式输出日志"),
) -> None:
    """为本地执行器签发设备令牌（令牌只显示这一次）。"""
    settings = _bootstrap(log_level, log_json)
    user_id = user.strip() or settings.default_user_id

    async def _run() -> IssuedToken:
        async with database_scope(settings) as database, database.session() as session:
            return await DeviceRepo(session).create(user_id=user_id, name=name)

    try:
        issued = asyncio.run(_run())
    except AppError as error:
        console.print(f"[red]✗ {error.message}[/]")
        raise typer.Exit(code=1) from None
    console.print(
        Panel(
            f"[bold]{issued.token}[/]\n\n"
            f"设备 ID：{issued.device.id}\n"
            f"归属用户：{issued.device.user_id}\n"
            f"备注名：{issued.device.name or '(无)'}",
            title="新设备令牌（只显示这一次，请立即保存）",
            border_style="yellow",
            expand=False,
        )
    )
    console.print("[dim]写入执行器：python -m runner set-token <令牌>[/]")


@device_app.command("list")
def device_list(
    log_level: str = typer.Option("", "--log-level", help="覆盖 LOG_LEVEL"),
    log_json: bool = typer.Option(False, "--log-json", help="以 JSON 格式输出日志"),
) -> None:
    """列出全部已注册设备。"""
    settings = _bootstrap(log_level, log_json)

    async def _run() -> list[Device]:
        async with database_scope(settings) as database, database.session() as session:
            return await DeviceRepo(session).list_devices()

    try:
        rows = asyncio.run(_run())
    except AppError as error:
        console.print(f"[red]✗ {error.message}[/]")
        raise typer.Exit(code=1) from None
    table = Table(title="本地执行器设备", show_header=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("备注名")
    table.add_column("用户")
    table.add_column("创建时间")
    table.add_column("最近活跃")
    table.add_column("状态")
    for row in rows:
        table.add_row(
            row.id[:8],
            row.name or "[dim]-[/]",
            row.user_id,
            row.created_at.strftime("%Y-%m-%d %H:%M"),
            row.last_seen_at.strftime("%Y-%m-%d %H:%M") if row.last_seen_at else "[dim]从未[/]",
            "[red]已吊销[/]" if row.revoked else "[green]有效[/]",
        )
    console.print(table)


@device_app.command("revoke")
def device_revoke(
    device_id: str = typer.Argument(help="设备 ID（可用前缀匹配，取第一个匹配项）"),
    log_level: str = typer.Option("", "--log-level", help="覆盖 LOG_LEVEL"),
    log_json: bool = typer.Option(False, "--log-json", help="以 JSON 格式输出日志"),
) -> None:
    """吊销设备令牌（立即失效）。"""
    settings = _bootstrap(log_level, log_json)

    async def _run() -> Device | None:
        async with database_scope(settings) as database, database.session() as session:
            repo = DeviceRepo(session)
            if len(device_id) < 8:
                raise AppError("请至少提供 8 位 ID 前缀")
            rows = [d for d in await repo.list_devices() if d.id.startswith(device_id)]
            if not rows:
                return None
            return await repo.revoke(rows[0].id)

    try:
        record = asyncio.run(_run())
    except AppError as error:
        console.print(f"[red]✗ {error.message}[/]")
        raise typer.Exit(code=1) from None
    if record is None:
        console.print(f"[red]✗ 未找到匹配设备：{device_id}[/]")
        raise typer.Exit(code=1)
    console.print(f"[green]✓[/] 已吊销设备 {record.id[:8]}（{record.name or '无备注'}）")


def main() -> None:
    cli_app()


if __name__ == "__main__":
    main()
