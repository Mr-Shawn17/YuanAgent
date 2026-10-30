// 与后端 SSE 协议（v1 计划 §6.3）和 api/schemas.py 一一对应的类型定义

/** RAG 知识库统一契约（计划 §7.2 KnowledgeChunk） */
export interface ChunkSource {
  doc_id?: string;
  title?: string;
  section?: string;
  page?: number;
  uri?: string;
  extra?: Record<string, unknown>;
}

export interface KnowledgeChunk {
  point_id: string;
  text: string;
  source: ChunkSource;
  rrf_score?: number;
  rerank_score?: number;
  rerank_rank?: number;
}

/** SSE 帧：type 判别联合 */
export type SseFrame =
  | { type: "session"; session_id: string; title?: string }
  | { type: "token"; delta: string }
  | {
      type: "tool_start";
      call_id: string;
      tool: string;
      tier: string;
      origin: string;
      args: Record<string, unknown>;
      task_id?: string;
    }
  | {
      type: "tool_end";
      call_id: string;
      ok: boolean;
      preview: string;
      ms: number;
      task_id?: string;
    }
  | {
      type: "fanout_start";
      fanout_id: string;
      tasks: { task_id: string; agent: string; task: string }[];
    }
  | { type: "fanout_task_start"; fanout_id: string; task_id: string; agent: string }
  | {
      type: "fanout_task_end";
      fanout_id: string;
      task_id: string;
      agent: string;
      ok: boolean;
      ms: number;
      error: string;
    }
  | {
      type: "fanout_end";
      fanout_id: string;
      ok_count: number;
      fail_count: number;
      ms: number;
    }
  | {
      type: "approval_required";
      run_id: string;
      call_id: string;
      tool: string;
      tier: string;
      reason: string;
    }
  | { type: "citation"; items: KnowledgeChunk[] }
  | { type: "memory_recalled"; count: number }
  | { type: "usage"; tokens_in: number; tokens_out: number }
  | { type: "done"; session_id: string }
  | { type: "error"; code: string; message: string; status?: number };

/** GET /v1/sessions 单条会话 */
export interface SessionInfo {
  thread_id: string;
  updated_at: string;
  message_count: number;
  title: string;
}

/** GET /v1/sessions/{id}/messages 回放消息 */
export interface SessionMessage {
  role: "user" | "assistant" | string;
  content: string;
}

/** 前端展示用：一次工具调用的完整轨迹 */
export interface ToolTraceItem {
  callId: string;
  tool: string;
  tier: string;
  origin: string;
  args: Record<string, unknown>;
  status: "running" | "awaiting_approval" | "ok" | "failed";
  preview?: string;
  ms?: number;
  /** fan_out 子任务归属：非空时该工具调用发生在对应子任务泳道内 */
  taskId?: string;
}

/** 执行链路图：fan_out 并行批次的子任务泳道 */
export interface FanoutTaskNode {
  taskId: string;
  agent: string;
  task: string;
  status: "pending" | "running" | "ok" | "failed";
  ms?: number;
  error?: string;
  /** 该子任务内部发生的工具调用（B+ 粒度归属） */
  tools: ToolTraceItem[];
}

/** 执行链路图：一次 fan_out 批次 */
export interface FanoutGraph {
  fanoutId: string;
  status: "running" | "done";
  okCount: number;
  failCount: number;
  totalMs?: number;
  tasks: FanoutTaskNode[];
}

/** 前端展示用：一条聊天气泡 */
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  tools: ToolTraceItem[];
  /** 主图流程的工具调用（不含 fan_out 子任务内部调用，避免重复展示） */
  citations: KnowledgeChunk[];
  memoryCount: number;
  usage?: { tokensIn: number; tokensOut: number };
  error?: string;
  streaming?: boolean;
  /** 执行链路图（本轮有 fan_out 批次时非空） */
  fanout?: FanoutGraph;
}

/** 待审批请求（approval_required 帧） */
export interface PendingApproval {
  runId: string;
  callId: string;
  tool: string;
  tier: string;
  reason: string;
}
