"""执行链路图谱聚合器：把流式委派事件累积成一张最终完成态图谱快照。

与前端 ExecutionGraphModel 结构一一对应（dict 形式，便于 JSON 落库与回放）。
SSE 流式过程里事件按发生顺序到达，本聚合器在消费事件的同一处调用，
把 fan_out / delegate 的委派拓扑、各子 Agent 状态、内部工具调用、耗时累积起来；
轮次结束时导出 dict，写入业务表 messages.execution_graph 供历史回放渲染。

结构与前端 types.ts 对齐：
    {
      "status": "done",
      "subAgents": [
        {
          "id": str, "agent": str, "task": str,
          "status": "ok"|"failed", "ms": int, "error": str|None,
          "taskId": str|None,
          "tools": [{"callId": str, "tool": str, "status": "ok"|"failed",
                     "ms": int, "preview": str|None}]
        }
      ],
      "okCount": int, "failCount": int, "totalMs": int|None
    }
"""

from __future__ import annotations

from typing import Any


class ExecutionGraphAggregator:
    """累积委派事件 → 最终图谱快照（供落库回放）。"""

    def __init__(self) -> None:
        self._subagents: list[dict[str, Any]] = []
        # fan_out 批次里 task_id → task 文案映射（fanout_task_start 不含 task 字段）
        self._task_labels: dict[str, str] = {}
        self._ok_count = 0
        self._fail_count = 0
        self._total_ms: int | None = None

    def handle_event(self, event: dict[str, Any]) -> None:
        """按事件类型累积图谱。只处理委派相关事件，其余忽略。"""
        etype = event.get("type")
        if etype == "fanout_start":
            for t in event.get("tasks") or []:
                self._task_labels[str(t.get("task_id", ""))] = str(t.get("task", ""))
        elif etype == "fanout_task_start":
            task_id = str(event.get("task_id", ""))
            self._subagents.append(
                {
                    "id": task_id,
                    "agent": str(event.get("agent", "")),
                    "task": self._task_labels.get(task_id, ""),
                    "status": "running",
                    "ms": None,
                    "error": None,
                    "taskId": task_id,
                    "tools": [],
                }
            )
        elif etype == "fanout_task_end":
            self._settle_node(
                node_id=str(event.get("task_id", "")),
                ok=bool(event.get("ok")),
                ms=event.get("latency_ms", 0),
                error=event.get("error") or None,
            )
        elif etype == "delegate_start":
            self._subagents.append(
                {
                    "id": str(event.get("delegate_id", "")),
                    "agent": str(event.get("agent", "")),
                    "task": str(event.get("task", "")),
                    "status": "running",
                    "ms": None,
                    "error": None,
                    "taskId": None,
                    "tools": [],
                }
            )
        elif etype == "delegate_end":
            self._settle_node(
                node_id=str(event.get("delegate_id", "")),
                ok=bool(event.get("ok")),
                ms=event.get("latency_ms", 0),
                error=event.get("error") or None,
            )
        elif etype == "tool_start":
            task_id = str(event.get("task_id", ""))
            if task_id:
                self._add_tool(task_id, event)
        elif etype == "tool_end":
            task_id = str(event.get("task_id", ""))
            if task_id:
                self._settle_tool(task_id, event)
        elif etype == "fanout_end":
            # 单批统计：合并进累计（多批 fan_out 时累加）
            self._ok_count += int(event.get("ok_count", 0))
            self._fail_count += int(event.get("fail_count", 0))

    def _find(self, node_id: str) -> dict[str, Any] | None:
        for node in self._subagents:
            if node["id"] == node_id:
                return node
        return None

    def _settle_node(self, *, node_id: str, ok: bool, ms: int, error: str | None) -> None:
        node = self._find(node_id)
        if node is None:
            return
        node["status"] = "ok" if ok else "failed"
        node["ms"] = int(ms)
        node["error"] = error

    def _add_tool(self, task_id: str, event: dict[str, Any]) -> None:
        node = self._find(task_id)
        if node is None:
            return
        node["tools"].append(
            {
                "callId": str(event.get("call_id", "")),
                "tool": str(event.get("tool_name", "")),
                "status": "running",
                "ms": None,
                "preview": None,
            }
        )

    def _settle_tool(self, task_id: str, event: dict[str, Any]) -> None:
        node = self._find(task_id)
        if node is None:
            return
        call_id = str(event.get("call_id", ""))
        for tool in node["tools"]:
            if tool["callId"] == call_id:
                tool["status"] = "ok" if bool(event.get("ok")) else "failed"
                tool["ms"] = int(event.get("latency_ms", 0))
                tool["preview"] = event.get("preview") or None
                break

    def finalize(self, *, total_ms: int | None = None) -> dict[str, Any] | None:
        """导出最终图谱快照；本轮无任何委派时返回 None（前端不渲染图谱）。"""
        if not self._subagents:
            return None
        # 收尾仍为 running 的节点（理论上不该有，防御性置 failed）
        for node in self._subagents:
            if node["status"] == "running":
                node["status"] = "failed"
            for tool in node["tools"]:
                if tool["status"] == "running":
                    tool["status"] = "failed"
        # 若单批统计未覆盖（如纯串行 delegate 无 fanout_end），按节点状态兜底统计
        ok = sum(1 for n in self._subagents if n["status"] == "ok")
        fail = sum(1 for n in self._subagents if n["status"] == "failed")
        return {
            "status": "done",
            "subAgents": self._subagents,
            "okCount": self._ok_count or ok,
            "failCount": self._fail_count or fail,
            "totalMs": total_ms,
        }


__all__ = ["ExecutionGraphAggregator"]
