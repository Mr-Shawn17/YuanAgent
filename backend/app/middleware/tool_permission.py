from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from time import perf_counter
from typing import Any, Protocol

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest as GraphToolCallRequest
from langgraph.types import Command

from app.core.events import (
    AgentEvent,
    ApprovalDecidedEvent,
    ApprovalRequiredEvent,
    CitationsEvent,
    EventBus,
    ToolEndEvent,
    ToolStartEvent,
    current_task_id,
)
from app.core.logging import get_logger
from app.db.models import AUDIT_STATUS_DENIED, AUDIT_STATUS_ERROR, AUDIT_STATUS_OK
from app.tools.permissions import (
    TIER_WRITE,
    ApprovalCallback,
    PermissionTier,
    ToolCallRequest,
    requires_confirmation,
)

logger = get_logger(__name__)

# 审计参数脱敏：敏感键值一律替换，普通值转文本并截断后入库
SENSITIVE_ARG_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "authorization",
        "passcode",
        "passwd",
        "password",
        "secret",
        "token",
    }
)
ARG_VALUE_LIMIT = 200
ERROR_MESSAGE_LIMIT = 500
# 工具结果在事件总线上的预览长度（终端轨迹行用，审计仍存完整错误摘要）
PREVIEW_LIMIT = 120
# 退出时等待在途审计任务的最长秒数
AUDIT_FLUSH_TIMEOUT = 5.0

ToolResult = ToolMessage | Command[Any]

# 工具错误边界的固定前缀：命中即视为执行失败（见 tools/base.py 的 tool_error_boundary）
ERROR_CONTENT_PREFIXES = ("[工具执行失败]", "[permission_denied]")

# 知识库检索工具名后缀：内置 search_knowledge_base 与 MCP 前缀形态
# （如 rag__search_knowledge_base）都按此匹配，命中且执行成功时解析结果发布引用事件
KB_TOOL_SUFFIX = "search_knowledge_base"


class ToolAuditWriter(Protocol):
    """审计落库回调协议，签名与 SessionRepo.record_tool_audit 保持一致。

    中间件不直接依赖数据库，方便单测注入内存收集器。
    """

    async def __call__(
        self,
        *,
        session_id: str,
        tool_name: str,
        tier: str,
        status: str,
        args_redacted: dict[str, Any],
        approved_by_user: bool,
        latency_ms: int,
        error_message: str | None,
    ) -> None: ...


def redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """把工具参数处理成可安全入库的 JSON dict：敏感键打码、长值截断。"""
    redacted: dict[str, Any] = {}
    for key, value in args.items():
        if key.lower() in SENSITIVE_ARG_KEYS:
            redacted[key] = "***"
            continue
        text = value if isinstance(value, str) else repr(value)
        if len(text) > ARG_VALUE_LIMIT:
            text = text[: ARG_VALUE_LIMIT - 3] + "..."
        redacted[key] = text
    return redacted


def _state_field(state: Any, key: str) -> str:
    value = state.get(key, "") if isinstance(state, dict) else getattr(state, key, "")
    return str(value) if value else ""


def content_to_text(content: Any) -> str:
    """把 ToolMessage.content 拍平成纯文本。

    内置工具的 content 是 str；MCP 工具（langchain.mcp.as_langchain_tool）返回的是
    content blocks 列表（如 [{"type": "text", "text": ...}, ...]），只提取 text 块拼接，
    避免审计摘要/终端预览里出现 str(list) 的噪音形态。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


def _classify_result(result: ToolResult) -> tuple[str, str | None]:
    """根据工具执行结果判定审计状态（ok / error）与错误摘要。"""
    if isinstance(result, ToolMessage):
        content = content_to_text(result.content)
        if result.status == "error" or content.startswith(ERROR_CONTENT_PREFIXES):
            return AUDIT_STATUS_ERROR, content[:ERROR_MESSAGE_LIMIT]
    return AUDIT_STATUS_OK, None


def _result_preview(result: ToolResult) -> str:
    """把工具结果压成单行短预览，供 tool_end 事件在终端渲染。"""
    if isinstance(result, ToolMessage):
        return " ".join(content_to_text(result.content).split())[:PREVIEW_LIMIT]
    return ""


def _strip_guardrail_wrapper(text: str) -> str:
    """剥掉 guardrail 中间件包上的 <external_content> 隔离壳，取出工具原始输出。"""
    stripped = text.strip()
    if not stripped.startswith("<external_content"):
        return stripped
    start = stripped.find("\n")
    end = stripped.find("</external_content>")
    if start == -1 or end <= start:
        return stripped
    return stripped[start + 1 : end].strip()


def extract_citation_chunks(result: ToolResult) -> list[dict[str, Any]]:
    """从知识库检索工具的结果里解析引用片段列表（§7.2 KnowledgeChunk 契约）。

    兼容两种载荷形态：内置 stub 工具的 {"count":..,"chunks":[..]} 与
    MCP RAG 工具直接返回的片段数组；解析失败时返回空列表（宁缺毋滥，不阻塞对话）。
    """
    if not isinstance(result, ToolMessage):
        return []
    text = _strip_guardrail_wrapper(content_to_text(result.content))
    if not text:
        return []
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    chunks = payload.get("chunks") if isinstance(payload, dict) else payload
    if not isinstance(chunks, list):
        return []
    return [c for c in chunks if isinstance(c, dict) and c.get("point_id")]


class ToolPermissionMiddleware(AgentMiddleware):
    """工具权限中间件：调用前判档 → write/danger 触发确认 → 全量写 tool_audit。

    对应计划 §6 安全矩阵：
    - read：直接放行，仅记录审计；
    - write/danger：执行前调用审批回调，被拒时返回错误 ToolMessage（不抛异常，
      让模型能看到拒绝原因并向用户解释）；
    - 未登记档位的工具（如 MCP 动态工具）保守按 write 处理；
    - 审计写入为 fire-and-forget，失败只记日志，绝不阻塞对话。
    """

    def __init__(
        self,
        *,
        tiers: dict[str, PermissionTier] | None = None,
        approval: ApprovalCallback | None = None,
        audit_writer: ToolAuditWriter | None = None,
        event_bus: EventBus | None = None,
        origin: str = "builtin",
    ) -> None:
        super().__init__()
        self._tiers: dict[str, PermissionTier] = dict(tiers or {})
        self._approval = approval
        self._audit_writer = audit_writer
        self._event_bus = event_bus
        # 事件来源标记：主图为 builtin，子 Agent 为 subagent:<name>（P6'）
        self._origin = origin
        self._audit_tasks: set[asyncio.Task[None]] = set()

    def tier_of(self, tool_name: str) -> PermissionTier:
        return self._tiers.get(tool_name, TIER_WRITE)

    async def _emit(self, event: AgentEvent) -> None:
        if self._event_bus is not None:
            await self._event_bus.publish(event)

    async def awrap_tool_call(
        self,
        request: GraphToolCallRequest,
        handler: Callable[[GraphToolCallRequest], Awaitable[ToolResult]],
    ) -> ToolResult:
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name", ""))
        tool_args = dict(tool_call.get("args") or {})
        call_id = str(tool_call.get("id", ""))
        tier = self.tier_of(tool_name)
        session_id = _state_field(request.state, "session_id")
        redacted = redact_args(tool_args)

        await self._emit(
            ToolStartEvent(
                call_id=call_id,
                tool_name=tool_name,
                tier=tier,
                args=redacted,
                origin=self._origin,
                task_id=current_task_id(),
            )
        )

        approved_by_user = False
        if requires_confirmation(tier):
            if self._approval is None:
                reason = "当前未配置审批人（无人值守模式），已自动拒绝。"
                await self._emit(
                    ApprovalDecidedEvent(call_id=call_id, tool_name=tool_name, approved=False)
                )
                self._schedule_audit(
                    session_id=session_id,
                    tool_name=tool_name,
                    tier=tier,
                    status=AUDIT_STATUS_DENIED,
                    args_redacted=redacted,
                    approved_by_user=False,
                    latency_ms=0,
                    error_message=reason,
                )
                denied = self._denied_message(call_id, tool_name, tier, reason)
                await self._emit(
                    ToolEndEvent(
                        call_id=call_id,
                        tool_name=tool_name,
                        ok=False,
                        latency_ms=0,
                        preview=reason,
                        task_id=current_task_id(),
                    )
                )
                return denied
            decision = ToolCallRequest(tool_name=tool_name, tier=tier, tool_args=tool_args)
            await self._emit(
                ApprovalRequiredEvent(
                    call_id=call_id,
                    tool_name=tool_name,
                    tier=tier,
                    summary=decision.summary(),
                )
            )
            approved_by_user = await self._approval(decision)
            await self._emit(
                ApprovalDecidedEvent(
                    call_id=call_id, tool_name=tool_name, approved=approved_by_user
                )
            )
            if not approved_by_user:
                reason = "用户拒绝了本次调用。"
                self._schedule_audit(
                    session_id=session_id,
                    tool_name=tool_name,
                    tier=tier,
                    status=AUDIT_STATUS_DENIED,
                    args_redacted=redacted,
                    approved_by_user=False,
                    latency_ms=0,
                    error_message=reason,
                )
                denied = self._denied_message(call_id, tool_name, tier, reason)
                await self._emit(
                    ToolEndEvent(
                        call_id=call_id,
                        tool_name=tool_name,
                        ok=False,
                        latency_ms=0,
                        preview=reason,
                        task_id=current_task_id(),
                    )
                )
                return denied

        started = perf_counter()
        try:
            result = await handler(request)
        except Exception as exc:
            latency_ms = int((perf_counter() - started) * 1000)
            self._schedule_audit(
                session_id=session_id,
                tool_name=tool_name,
                tier=tier,
                status=AUDIT_STATUS_ERROR,
                args_redacted=redacted,
                approved_by_user=approved_by_user,
                latency_ms=latency_ms,
                error_message=f"{type(exc).__name__}: {exc}"[:ERROR_MESSAGE_LIMIT],
            )
            await self._emit(
                ToolEndEvent(
                    call_id=call_id,
                    tool_name=tool_name,
                    ok=False,
                    latency_ms=latency_ms,
                    preview=f"{type(exc).__name__}: {exc}"[:PREVIEW_LIMIT],
                    task_id=current_task_id(),
                )
            )
            raise
        latency_ms = int((perf_counter() - started) * 1000)
        status, error_message = _classify_result(result)
        self._schedule_audit(
            session_id=session_id,
            tool_name=tool_name,
            tier=tier,
            status=status,
            args_redacted=redacted,
            approved_by_user=approved_by_user,
            latency_ms=latency_ms,
            error_message=error_message,
        )
        await self._emit(
            ToolEndEvent(
                call_id=call_id,
                tool_name=tool_name,
                ok=status == AUDIT_STATUS_OK,
                latency_ms=latency_ms,
                preview=_result_preview(result),
                task_id=current_task_id(),
            )
        )
        if status == AUDIT_STATUS_OK and tool_name.endswith(KB_TOOL_SUFFIX):
            # 知识库检索成功：把命中片段作为引用发布（stub 与 MCP 工具统一在此拦截）
            chunks = extract_citation_chunks(result)
            if chunks:
                await self._emit(CitationsEvent(items=chunks))
        return result

    def _denied_message(
        self, call_id: str, tool_name: str, tier: PermissionTier, reason: str
    ) -> ToolMessage:
        content = f"[permission_denied] 工具 {tool_name} 属于 {tier} 档操作，需要用户确认：{reason}"
        return ToolMessage(
            content=content,
            tool_call_id=call_id,
            name=tool_name,
            status="error",
        )

    def _schedule_audit(
        self,
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
        writer = self._audit_writer
        if writer is None:
            return

        async def _write() -> None:
            try:
                await writer(
                    session_id=session_id,
                    tool_name=tool_name,
                    tier=tier,
                    status=status,
                    args_redacted=args_redacted,
                    approved_by_user=approved_by_user,
                    latency_ms=latency_ms,
                    error_message=error_message,
                )
            except Exception:
                logger.warning(
                    "tool_audit_write_failed",
                    tool=tool_name,
                    status=status,
                    exc_info=True,
                )

        task = asyncio.create_task(_write())
        self._audit_tasks.add(task)
        task.add_done_callback(self._audit_tasks.discard)

    async def wait_pending_audits(self) -> None:
        """退出前等待在途审计任务完成（上限 AUDIT_FLUSH_TIMEOUT 秒），避免丢审计记录。"""
        if not self._audit_tasks:
            return
        await asyncio.wait(set(self._audit_tasks), timeout=AUDIT_FLUSH_TIMEOUT)


__all__ = [
    "ARG_VALUE_LIMIT",
    "AUDIT_FLUSH_TIMEOUT",
    "ERROR_CONTENT_PREFIXES",
    "ERROR_MESSAGE_LIMIT",
    "KB_TOOL_SUFFIX",
    "PREVIEW_LIMIT",
    "SENSITIVE_ARG_KEYS",
    "ToolAuditWriter",
    "ToolPermissionMiddleware",
    "ToolResult",
    "content_to_text",
    "extract_citation_chunks",
    "redact_args",
]
