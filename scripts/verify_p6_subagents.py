"""P6' 子 Agent 分流手工验收脚本（真实 LLM + 真实 MySQL，离线测试之外的一次性验证）。

验收口径（P6' 计划：主 Agent 只做路由与汇总）：
1. 普通闲聊不委派：主 Agent 直接回答，全程无 delegate_to_* 工具调用；
2. 调研类任务委派 research：delegate_to_research（read 档）直接执行，
   子 Agent 的工具事件以 origin=subagent:research 混入主轨迹；
3. 写文件任务委派 coding：delegate_to_coding（write 档）触发审批一次，
   批准后子 Agent 沙箱写文件成功（整体授权，内部不再逐条打断）；
4. 审计落库：tool_audit 表里能查到委派与子 Agent 工具的记录。

跑法：.venv312\\Scripts\\python.exe scripts/verify_p6_subagents.py
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import asyncmy
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.builder import (
    build_agent,
    mysql_checkpointer,
    mysql_store,
    thread_config,
)
from app.agents.state import initial_state_fields
from app.agents.subagents import DELEGATE_PREFIX, apply_routing, load_routing_config, routing_note
from app.core.config import get_agent_config, get_settings
from app.core.events import EventBus, ToolStartEvent
from app.db.engine import build_database
from app.db.session_repo import SessionRepo
from app.llm.factory import get_chat_model
from app.middleware.tool_permission import ToolAuditWriter, ToolPermissionMiddleware
from app.tools.base import ToolContext
from app.tools.permissions import ToolCallRequest
from app.tools.registry import build_tools

SESSION = f"verify-p6-{uuid.uuid4().hex[:8]}"
SANDBOX_FILE = "hello_p6.txt"
SANDBOX_CONTENT = "P6 分流验收 OK"


def _last_ai_text(result: dict[str, Any]) -> str:
    for message in reversed(result.get("messages") or []):
        if isinstance(message, AIMessage) and isinstance(message.content, str):
            return message.content
    return ""


def _make_audit_writer(database: Any) -> ToolAuditWriter:
    """审计落库回调（与 cli._make_audit_writer 同款策略）。"""

    async def _write(**kwargs: Any) -> None:
        async with database.session() as session:
            await SessionRepo(session).record_tool_audit(
                session_id=kwargs["session_id"] or "unknown",
                tool_name=kwargs["tool_name"],
                tier=kwargs["tier"],
                status=kwargs["status"],
                args_redacted=kwargs["args_redacted"],
                approved_by_user=kwargs["approved_by_user"],
                latency_ms=kwargs["latency_ms"],
                error_message=kwargs["error_message"],
            )

    return _write


async def main() -> int:
    settings = get_settings()
    agent_config = get_agent_config()
    user_id = settings.default_user_id
    failures: list[str] = []
    approvals: list[str] = []
    starts: list[ToolStartEvent] = []

    bus = EventBus()
    bus.subscribe(lambda event: starts.append(event) if isinstance(event, ToolStartEvent) else None)

    async def approve(request: ToolCallRequest) -> bool:
        approvals.append(request.tool_name)
        return True

    async with mysql_checkpointer(settings) as checkpointer, mysql_store(settings) as store:
        database = build_database(settings)
        audit_writer = _make_audit_writer(database)
        model = get_chat_model("main", settings=settings)
        tool_context = ToolContext(
            settings=settings,
            user_id=user_id,
            session_id=SESSION,
            store=store,
            event_bus=bus,
        )
        registry = build_tools(tool_context)
        routing_config = load_routing_config()
        routing = apply_routing(
            registry=registry,
            config=routing_config,
            model_for=lambda tier: get_chat_model(tier, settings=settings),
            event_bus=bus,
            audit_writer=audit_writer,
        )
        if routing is None:
            print("[FAIL] 路由未装配（agents.yaml enabled 或子 Agent 工具匹配为空）")
            return 1
        registry.tools = routing.main_tools
        registry.tiers.update(routing.delegate_tiers)
        print("主 Agent 工具：", ", ".join(routing.names))
        for name, tools in routing.sub_agent_tools.items():
            print(f"子 Agent {name}（{routing.delegate_tiers[f'{DELEGATE_PREFIX}{name}']} 档）：",
                  ", ".join(tools))

        permission_mw = ToolPermissionMiddleware(
            tiers=registry.tiers, approval=approve, audit_writer=audit_writer, event_bus=bus
        )
        agent = build_agent(
            model=model,
            tools=registry.tools,
            middleware=[permission_mw],
            settings=settings,
            agent_config=agent_config,
            checkpointer=checkpointer,
            store=store,
            user_id=user_id,
            notes=routing_note(routing, routing_config),
        )
        budget = agent_config.limits.max_tool_calls_per_turn

        async def ask(text: str) -> str:
            payload = {
                "messages": [HumanMessage(content=text)],
                **initial_state_fields(session_id=SESSION, user_id=user_id, tool_budget=budget),
            }
            result = await agent.ainvoke(payload, config=thread_config(SESSION))
            return _last_ai_text(result)

        print("\n== 轮 1：普通闲聊（不应委派） ==")
        starts.clear()
        reply = await ask("用一句话介绍一下你自己。")
        print(f"Agent: {reply[:160]}")
        delegated = [e.tool_name for e in starts if e.tool_name.startswith(DELEGATE_PREFIX)]
        if not delegated:
            print("[PASS] 闲聊未触发委派")
        else:
            failures.append(f"闲聊触发了委派：{delegated}")
            print(f"[FAIL] 闲聊触发了委派：{delegated}")

        print("\n== 轮 2：调研类任务（应委派 research，read 档免审批） ==")
        starts.clear()
        approvals.clear()
        reply = await ask(
            "请调研：沙箱目录里现在有哪些文件？各自大概是什么内容？给我一份简短清单。"
        )
        print(f"Agent: {reply[:200]}")
        research_calls = [e for e in starts if e.tool_name == f"{DELEGATE_PREFIX}research"]
        sub_events = [e for e in starts if e.origin == "subagent:research"]
        if research_calls:
            print(f"[PASS] 触发 {DELEGATE_PREFIX}research 委派（read 档，未弹审批："
                  f"{f'{DELEGATE_PREFIX}research' not in approvals}）")
        else:
            failures.append("调研任务未委派给 research")
            print("[FAIL] 调研任务未委派给 research")
        if sub_events:
            print(f"[PASS] 子 Agent 工具事件混入主轨迹：{[e.tool_name for e in sub_events]}")
        else:
            failures.append("未见 origin=subagent:research 的工具事件")
            print("[FAIL] 未见 origin=subagent:research 的工具事件")

        print("\n== 轮 3：写文件任务（应委派 coding，write 档审批一次） ==")
        starts.clear()
        approvals.clear()
        reply = await ask(
            f"请立即在沙箱里创建文件 {SANDBOX_FILE}，内容写：{SANDBOX_CONTENT}。"
            "直接执行，不要向我口头确认。"
        )
        print(f"Agent: {reply[:200]}")
        coding_calls = [e for e in starts if e.tool_name == f"{DELEGATE_PREFIX}coding"]
        sub_writes = [e for e in starts if e.origin == "subagent:coding"]
        if coding_calls:
            print(f"[PASS] 触发 {DELEGATE_PREFIX}coding 委派")
        else:
            failures.append("写文件任务未委派给 coding")
            print("[FAIL] 写文件任务未委派给 coding")
        if f"{DELEGATE_PREFIX}coding" in approvals:
            print(f"[PASS] write 档委派触发审批一次（审批队列：{approvals}）")
        else:
            failures.append(f"write 档委派未触发审批（审批队列：{approvals}）")
            print(f"[FAIL] write 档委派未触发审批（审批队列：{approvals}）")
        if sub_writes:
            print(f"[PASS] coding 子 Agent 内部工具（整体授权，未再审批）："
                  f"{[e.tool_name for e in sub_writes]}")
        else:
            failures.append("未见 origin=subagent:coding 的工具事件")
            print("[FAIL] 未见 origin=subagent:coding 的工具事件")

        target = settings.workspace_dir / SANDBOX_FILE
        if target.exists() and SANDBOX_CONTENT in target.read_text(encoding="utf-8"):
            print(f"[PASS] 沙箱文件已写入：{target}")
        else:
            failures.append(f"沙箱文件未写入或内容不符：{target}")
            print(f"[FAIL] 沙箱文件未写入或内容不符：{target}")

        await permission_mw.wait_pending_audits()
        await database.aclose()

    print(f"\n== MySQL tool_audit 表检查：session_id = {SESSION} ==")
    conn = await asyncmy.connect(db=settings.mysql_database, **settings.mysql_connect_kwargs)
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT tool_name, tier, status FROM tool_audit "
                "WHERE session_id = %s ORDER BY id",
                (SESSION,),
            )
            rows = await cursor.fetchall()
        for tool_name, tier, status in rows:
            print(f"  {tool_name} | {tier} | {status}")
        audited = {row[0] for row in rows}
        needed = {
            f"{DELEGATE_PREFIX}research",
            f"{DELEGATE_PREFIX}coding",
            "write_file",
        }
        missing = needed - audited
        if not missing:
            print("[PASS] 委派与子 Agent 写操作均已落审计")
        else:
            failures.append(f"审计缺失：{missing}")
            print(f"[FAIL] 审计缺失：{missing}")
    finally:
        conn.close()

    print("\n验收结果：", "全部通过" if not failures else f"失败 {len(failures)} 项：{failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
