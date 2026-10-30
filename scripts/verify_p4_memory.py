"""P4' 记忆系统手工验收脚本（真实 LLM + 真实 MySQL，离线测试之外的一次性验证）。

验收口径（计划 §14.2）：
1. 会话 A 告诉 Agent 一个新偏好 → remember 工具落库 MySQL store；
2. 全新会话 B 里 Agent 依然记得该偏好（记忆召回注入生效）；
3. SELECT * FROM store WHERE prefix LIKE 'memories.%' 有记录。

跑法：.venv312\\Scripts\\python.exe scripts/verify_p4_memory.py
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
from app.core.config import get_agent_config, get_settings
from app.core.events import EventBus
from app.llm.factory import get_chat_model
from app.middleware.memory_recall import MemoryRecallMiddleware
from app.middleware.tool_permission import ToolPermissionMiddleware
from app.tools.base import ToolContext
from app.tools.registry import build_tools

PREFERENCE = "冰美式咖啡"
SESSION_A = f"verify-p4-a-{uuid.uuid4().hex[:8]}"
SESSION_B = f"verify-p4-b-{uuid.uuid4().hex[:8]}"


def _last_ai_text(result: dict[str, Any]) -> str:
    for message in reversed(result.get("messages") or []):
        if isinstance(message, AIMessage) and isinstance(message.content, str):
            return message.content
    return ""


async def _ask(agent: Any, session_id: str, text: str, *, user_id: str, budget: int) -> str:
    payload = {
        "messages": [HumanMessage(content=text)],
        **initial_state_fields(session_id=session_id, user_id=user_id, tool_budget=budget),
    }
    result = await agent.ainvoke(payload, config=thread_config(session_id))
    return _last_ai_text(result)


async def main() -> int:
    settings = get_settings()
    agent_config = get_agent_config()
    user_id = settings.default_user_id
    failures: list[str] = []

    async def approve(_request: Any) -> bool:
        return True

    async with mysql_checkpointer(settings) as checkpointer, mysql_store(settings) as store:
        model = get_chat_model("main", settings=settings)
        bus = EventBus()
        tool_context = ToolContext(
            settings=settings,
            user_id=user_id,
            session_id=SESSION_A,
            store=store,
            event_bus=bus,
        )
        registry = build_tools(tool_context)
        permission_mw = ToolPermissionMiddleware(
            tiers=registry.tiers, approval=approve, event_bus=bus
        )
        recall_mw = MemoryRecallMiddleware(store=store, user_id=user_id, event_bus=bus)
        agent = build_agent(
            model=model,
            tools=registry.tools,
            middleware=[permission_mw, recall_mw],
            settings=settings,
            agent_config=agent_config,
            checkpointer=checkpointer,
            store=store,
            user_id=user_id,
        )
        budget = agent_config.limits.max_tool_calls_per_turn

        print(f"== 会话 A（{SESSION_A}）：告知新偏好 ==")
        reply_a = await _ask(
            agent,
            SESSION_A,
            f"请记住：我最喜欢的饮品是{PREFERENCE}。",
            user_id=user_id,
            budget=budget,
        )
        print(f"Agent: {reply_a[:200]}")

        print(f"\n== 会话 B（{SESSION_B}）：全新会话验证跨会话记忆 ==")
        reply_b = await _ask(
            agent,
            SESSION_B,
            "我最喜欢的饮品是什么？直接说名字。",
            user_id=user_id,
            budget=budget,
        )
        print(f"Agent: {reply_b[:200]}")
        if PREFERENCE in reply_b.replace(" ", ""):
            print("[PASS] 新会话里 Agent 记得该偏好")
        else:
            failures.append("会话 B 回复未命中偏好关键词")
            print("[FAIL] 会话 B 回复未命中偏好关键词")

        await permission_mw.wait_pending_audits()

    print("\n== MySQL store 表检查：prefix LIKE 'memories.%' ==")
    conn = await asyncmy.connect(db=settings.mysql_database, **settings.mysql_connect_kwargs)
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT prefix, `key`, JSON_UNQUOTE(JSON_EXTRACT(value, '$.content')) "
                "FROM store WHERE prefix LIKE 'memories.%' ORDER BY updated_at DESC LIMIT 5"
            )
            rows = await cursor.fetchall()
        for prefix, key, content in rows:
            print(f"  {prefix} | {key} | {str(content)[:60]}")
        if rows:
            print(f"[PASS] store 表 memories.% 共取到 {len(rows)} 条（最近 5 条如上）")
        else:
            failures.append("store 表没有 memories.% 记录")
            print("[FAIL] store 表没有 memories.% 记录")
    finally:
        conn.close()

    print("\n验收结果：", "全部通过" if not failures else f"失败 {len(failures)} 项：{failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
