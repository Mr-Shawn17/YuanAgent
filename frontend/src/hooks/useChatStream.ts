"use client";

// 聊天流状态机：维护会话/消息/工具轨迹/待审批，驱动 SSE 收发
import { useCallback, useEffect, useRef, useState } from "react";

import { fetchSessionMessages, listSessions, streamSse } from "@/lib/api";
import type {
  ChatMessage,
  PendingApproval,
  SessionInfo,
  SseFrame,
  ToolTraceItem,
} from "@/lib/types";

let seq = 0;
function nextId(): string {
  seq += 1;
  return `m${Date.now().toString(36)}_${seq}`;
}

function emptyAssistant(id: string): ChatMessage {
  return {
    id,
    role: "assistant",
    content: "",
    tools: [],
    citations: [],
    memoryCount: 0,
    streaming: true,
    fanout: undefined,
  };
}

export function useChatStream() {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null);
  const [busy, setBusy] = useState(false);
  const [fatalError, setFatalError] = useState("");
  // 当前正在接收流式更新的 assistant 消息 id（approve 续流也复用同一条）
  const activeIdRef = useRef("");

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await listSessions());
    } catch {
      // 侧栏刷新失败不打断聊天
    }
  }, []);

  useEffect(() => {
    void refreshSessions();
  }, [refreshSessions]);

  /** 单帧 → 状态更新（纯函数式，避免在 updater 里做副作用） */
  const applyFrame = useCallback((frame: SseFrame) => {
    if (frame.type === "session") {
      setSessionId(frame.session_id);
      return;
    }
    if (frame.type === "approval_required") {
      setPendingApproval({
        runId: frame.run_id,
        callId: frame.call_id,
        tool: frame.tool,
        tier: frame.tier,
        reason: frame.reason,
      });
    }
    const id = activeIdRef.current;
    setMessages((prev) =>
      prev.map((m) => {
        if (m.id !== id) return m;
        switch (frame.type) {
          case "token":
            return { ...m, content: m.content + frame.delta };
          case "fanout_start":
            return {
              ...m,
              fanout: {
                fanoutId: frame.fanout_id,
                status: "running" as const,
                okCount: 0,
                failCount: 0,
                tasks: frame.tasks.map((t) => ({
                  taskId: t.task_id,
                  agent: t.agent,
                  task: t.task,
                  status: "pending" as const,
                  tools: [],
                })),
              },
            };
          case "fanout_task_start":
            return m.fanout
              ? {
                  ...m,
                  fanout: {
                    ...m.fanout,
                    tasks: m.fanout.tasks.map((t) =>
                      t.taskId === frame.task_id ? { ...t, status: "running" as const } : t,
                    ),
                  },
                }
              : m;
          case "fanout_task_end":
            return m.fanout
              ? {
                  ...m,
                  fanout: {
                    ...m.fanout,
                    tasks: m.fanout.tasks.map((t) =>
                      t.taskId === frame.task_id
                        ? {
                            ...t,
                            status: frame.ok ? ("ok" as const) : ("failed" as const),
                            ms: frame.ms,
                            error: frame.error || undefined,
                          }
                        : t,
                    ),
                  },
                }
              : m;
          case "fanout_end":
            return m.fanout
              ? {
                  ...m,
                  fanout: {
                    ...m.fanout,
                    status: "done" as const,
                    okCount: frame.ok_count,
                    failCount: frame.fail_count,
                    totalMs: frame.ms,
                  },
                }
              : m;
          case "tool_start": {
            // 有 task_id 的工具调用归属到 fan_out 子任务泳道，其余进主轨迹列表
            if (frame.task_id && m.fanout) {
              const item: ToolTraceItem = {
                callId: frame.call_id,
                tool: frame.tool,
                tier: frame.tier,
                origin: frame.origin,
                args: frame.args,
                status: "running",
                taskId: frame.task_id,
              };
              return {
                ...m,
                fanout: {
                  ...m.fanout,
                  tasks: m.fanout.tasks.map((t) =>
                    t.taskId === frame.task_id ? { ...t, tools: [...t.tools, item] } : t,
                  ),
                },
              };
            }
            return {
              ...m,
              tools: [
                ...m.tools,
                {
                  callId: frame.call_id,
                  tool: frame.tool,
                  tier: frame.tier,
                  origin: frame.origin,
                  args: frame.args,
                  status: "running" as const,
                },
              ],
            };
          }
          case "tool_end": {
            // fan_out 子任务内的工具结束：更新对应泳道内的条目
            if (frame.task_id && m.fanout) {
              return {
                ...m,
                fanout: {
                  ...m.fanout,
                  tasks: m.fanout.tasks.map((t) =>
                    t.taskId === frame.task_id
                      ? {
                          ...t,
                          tools: t.tools.map((it) =>
                            it.callId === frame.call_id
                              ? {
                                  ...it,
                                  status: frame.ok ? ("ok" as const) : ("failed" as const),
                                  preview: frame.preview,
                                  ms: frame.ms,
                                }
                              : it,
                          ),
                        }
                      : t,
                  ),
                },
              };
            }
            return {
              ...m,
              tools: m.tools.map((t) =>
                t.callId === frame.call_id
                  ? { ...t, status: frame.ok ? ("ok" as const) : ("failed" as const), preview: frame.preview, ms: frame.ms }
                  : t,
              ),
            };
          }
          case "approval_required":
            return {
              ...m,
              tools: m.tools.map((t) =>
                t.callId === frame.call_id ? { ...t, status: "awaiting_approval" as const } : t,
              ),
            };          case "citation":
            return { ...m, citations: frame.items };
          case "memory_recalled":
            return { ...m, memoryCount: frame.count };
          case "usage":
            return { ...m, usage: { tokensIn: frame.tokens_in, tokensOut: frame.tokens_out } };
          case "error":
            return { ...m, error: frame.message, streaming: false };
          case "done":
            return { ...m, streaming: false };
          default:
            return m;
        }
      }),
    );
  }, []);

  /** 流收尾：确保 streaming 标记复位并刷新会话列表 */
  const finishStream = useCallback(async (errorMessage?: string) => {
    const id = activeIdRef.current;
    setMessages((prev) =>
      prev.map((m) =>
        m.id === id && (m.streaming || errorMessage)
          ? { ...m, streaming: false, error: errorMessage ?? m.error }
          : m,
      ),
    );
    setBusy(false);
    await refreshSessions();
  }, [refreshSessions]);

  const send = useCallback(
    async (text: string) => {
      const content = text.trim();
      if (!content || busy) return;
      setFatalError("");
      const userMsg: ChatMessage = {
        id: nextId(),
        role: "user",
        content,
        tools: [],
        citations: [],
        memoryCount: 0,
      };
      const botMsg = emptyAssistant(nextId());
      activeIdRef.current = botMsg.id;
      setMessages((prev) => [...prev, userMsg, botMsg]);
      setBusy(true);
      try {
        await streamSse("/v1/chat", { message: content, session_id: sessionId }, applyFrame);
        await finishStream();
      } catch (e) {
        await finishStream(e instanceof Error ? e.message : String(e));
      }
    },
    [busy, sessionId, applyFrame, finishStream],
  );

  /** 审批决定：恢复被 interrupt 挂起的图，续流写回同一条 assistant 消息 */
  const approve = useCallback(
    async (decision: "allow" | "deny") => {
      if (!pendingApproval || busy) return;
      const { runId, callId } = pendingApproval;
      setPendingApproval(null);
      setBusy(true);
      const id = activeIdRef.current;
      setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, streaming: true } : m)));
      try {
        await streamSse(
          `/v1/chat/${encodeURIComponent(runId)}/approve`,
          { call_id: callId, decision },
          applyFrame,
        );
        await finishStream();
      } catch (e) {
        await finishStream(e instanceof Error ? e.message : String(e));
      }
    },
    [pendingApproval, busy, applyFrame, finishStream],
  );

  /** 切换会话：回放历史（工具轨迹不回放，与后端约定一致） */
  const selectSession = useCallback(
    async (id: string) => {
      if (busy) return;
      setSessionId(id);
      setPendingApproval(null);
      setFatalError("");
      try {
        const history = await fetchSessionMessages(id);
        setMessages(
          history.map((m) => ({
            id: nextId(),
            role: m.role === "user" ? ("user" as const) : ("assistant" as const),
            content: m.content,
            tools: [],
            citations: [],
            memoryCount: 0,
          })),
        );
      } catch (e) {
        setMessages([]);
        setFatalError(e instanceof Error ? e.message : String(e));
      }
    },
    [busy],
  );

  const newSession = useCallback(() => {
    if (busy) return;
    setSessionId("");
    setMessages([]);
    setPendingApproval(null);
    setFatalError("");
  }, [busy]);

  return {
    sessions,
    sessionId,
    messages,
    pendingApproval,
    busy,
    fatalError,
    send,
    approve,
    selectSession,
    newSession,
  };
}
