"use client";

// 聊天流状态机：维护会话/消息/工具轨迹/待审批，驱动 SSE 收发
import { useCallback, useEffect, useRef, useState } from "react";

import { fetchSessionMessages, listSessions, streamSse } from "@/lib/api";
import type {
  ChatMessage,
  GraphToolNode,
  PendingApproval,
  SessionInfo,
  SseFrame,
} from "@/lib/types";

let seq = 0;
function nextId(): string {
  seq += 1;
  return `m${Date.now().toString(36)}_${seq}`;
}

function newSessionId(): string {
  // 客户端预生成会话 id，使新会话在发送前即可绑定工作目录
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
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
    // 发送即创建执行图谱：主 Agent 节点立刻展示，后续委派实时增补子 Agent 节点
    graph: {
      status: "running",
      subAgents: [],
      okCount: 0,
      failCount: 0,
      mainStartedAt: Date.now(),
    },
  };
}

export function useChatStream() {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null);
  const [busy, setBusy] = useState(false);
  const [fatalError, setFatalError] = useState("");
  // 当前正在接收流式更新的 assistant 消息 id（approve 续流也复用同一条）
  const activeIdRef = useRef("");
  // fanout_start 携带的完整子任务描述映射（task_id → task 文案），
  // fanout_task_start 事件不含 task 字段，靠此补全子 Agent 节点的任务说明
  const fanoutTasksRef = useRef<Record<string, string>>({});

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await listSessions());
    } catch {
      // 侧栏刷新失败不打断聊天
    }
  }, []);

  useEffect(() => {
    void refreshSessions();
    // 初始化即生成一个会话 id，保证「发送前绑定工作目录」始终有合法目标，
    // 否则 sessionId 为空时绑定向 /v1/sessions//workspace 会 404
    setSessionId((cur) => cur || newSessionId());
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
            // 并行批次开始：先不画子 Agent 节点，等逐个 FanoutTaskStart 事件到达再实时新增，
            // 这样「执行链路/耗时不同的子 Agent」会一个接一个冒出来，而不是一次性全出。
            // 这里仅记录 task_id → task 文案映射，供 fanout_task_start 补全节点说明。
            fanoutTasksRef.current = Object.fromEntries(
              frame.tasks.map((t) => [t.task_id, t.task]),
            );
            return {
              ...m,
              graph: {
                status: "running" as const,
                okCount: 0,
                failCount: 0,
                mainStartedAt: m.graph?.mainStartedAt,
                subAgents: m.graph?.subAgents ?? [],
              },
            };
          case "fanout_task_start":
            // 单个子任务真正开始执行：此刻才新增该子 Agent 节点（逐个实时涌现）
            return {
              ...m,
              graph: {
                ...(m.graph ?? {
                  status: "running" as const,
                  okCount: 0,
                  failCount: 0,
                }),
                subAgents: [
                  ...(m.graph?.subAgents ?? []),
                  {
                    id: frame.task_id,
                    taskId: frame.task_id,
                    agent: frame.agent,
                    task: fanoutTasksRef.current[frame.task_id] ?? "",
                    status: "running" as const,
                    startedAt: Date.now(),
                    tools: [],
                  },
                ],
              },
            };
          case "fanout_task_end":
            return m.graph
              ? {
                  ...m,
                  graph: {
                    ...m.graph,
                    subAgents: m.graph.subAgents.map((n) =>
                      n.taskId === frame.task_id
                        ? {
                            ...n,
                            status: frame.ok ? ("ok" as const) : ("failed" as const),
                            ms: frame.ms,
                            error: frame.error || undefined,
                          }
                        : n,
                    ),
                  },
                }
              : m;
          case "fanout_end":
            return m.graph
              ? {
                  ...m,
                  graph: {
                    ...m.graph,
                    status: "done" as const,
                    okCount: frame.ok_count,
                    failCount: frame.fail_count,
                    totalMs: frame.ms,
                  },
                }
              : m;
          case "delegate_start":
            return {
              ...m,
              graph: {
                status: "running" as const,
                okCount: m.graph?.okCount ?? 0,
                failCount: m.graph?.failCount ?? 0,
                totalMs: m.graph?.totalMs,
                mainStartedAt: m.graph?.mainStartedAt,
                subAgents: [
                  ...(m.graph?.subAgents ?? []),
                  {
                    id: frame.delegate_id,
                    agent: frame.agent,
                    task: frame.task,
                    status: "running" as const,
                    startedAt: Date.now(),
                    tools: [],
                  },
                ],
              },
            };
          case "delegate_end":
            return m.graph
              ? {
                  ...m,
                  graph: {
                    ...m.graph,
                    okCount: m.graph.okCount + (frame.ok ? 1 : 0),
                    failCount: m.graph.failCount + (frame.ok ? 0 : 1),
                    subAgents: m.graph.subAgents.map((n) =>
                      n.id === frame.delegate_id
                        ? {
                            ...n,
                            status: frame.ok ? ("ok" as const) : ("failed" as const),
                            ms: frame.ms,
                            error: frame.error || undefined,
                          }
                        : n,
                    ),
                  },
                }
              : m;
          case "tool_start": {
            // 委派调度工具（fan_out / delegate_to_*）已在执行图谱中完整表达，不进主工具轨迹
            const isOrchestrator = frame.tool === "fan_out" || frame.tool.startsWith("delegate_to_");
            // 有 task_id 的工具调用归属到 fan_out 子任务泳道，其余进主轨迹列表
            if (frame.task_id && m.graph) {
              const item: GraphToolNode = {
                callId: frame.call_id,
                tool: frame.tool,
                status: "running" as const,
                startedAt: Date.now(),
              };
              return {
                ...m,
                graph: {
                  ...m.graph,
                  subAgents: m.graph.subAgents.map((n) =>
                    n.taskId === frame.task_id ? { ...n, tools: [...n.tools, item] } : n,
                  ),
                },
              };
            }
            if (isOrchestrator) return m;
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
            if (frame.task_id && m.graph) {
              return {
                ...m,
                graph: {
                  ...m.graph,
                  subAgents: m.graph.subAgents.map((n) =>
                    n.taskId === frame.task_id
                      ? {
                          ...n,
                          tools: n.tools.map((it) =>
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
                      : n,
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
            return {
              ...m,
              streaming: false,
              // 收尾：主 Agent 计时定格，图谱整体结束
              graph: m.graph ? { ...m.graph, status: "done" as const } : m.graph,
            };
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
      // 回填该会话绑定的工作目录（从会话列表里取，若无则空）
      setWorkspace(sessions.find((s) => s.thread_id === id)?.workspace ?? "");
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
            // 回填执行链路图谱（完成态快照，供历史渲染）
            graph: m.graph ?? undefined,
          })),
        );
      } catch (e) {
        setMessages([]);
        setFatalError(e instanceof Error ? e.message : String(e));
      }
    },
    [busy, sessions],
  );

  const newSession = useCallback(() => {
    if (busy) return;
    setSessionId(newSessionId());
    setWorkspace("");
    setMessages([]);
    setPendingApproval(null);
    setFatalError("");
  }, [busy]);

  /** 绑定/切换工作目录后更新本地状态（持久化已在 api 层完成） */
  const bindWorkspace = useCallback((path: string) => {
    setWorkspace(path);
    // 同步更新会话列表里该会话的 workspace，保证下次切换回填正确
    setSessions((prev) =>
      prev.map((s) => (s.thread_id === sessionId ? { ...s, workspace: path } : s)),
    );
  }, [sessionId]);

  return {
    sessions,
    sessionId,
    workspace,
    messages,
    pendingApproval,
    busy,
    fatalError,
    send,
    approve,
    selectSession,
    newSession,
    bindWorkspace,
  };
}
