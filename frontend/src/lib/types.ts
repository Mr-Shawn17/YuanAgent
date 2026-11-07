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
      type: "delegate_start";
      delegate_id: string;
      agent: string;
      task: string;
    }
  | {
      type: "delegate_end";
      delegate_id: string;
      agent: string;
      ok: boolean;
      ms: number;
      error: string;
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
  workspace?: string;
}

/** GET /v1/workspaces 单条可用工作目录 */
export interface WorkspaceOption {
  path: string;
  name: string;
  kind: string;
}

/** GET /v1/sessions/{id}/messages 回放消息 */
export interface SessionMessage {
  role: "user" | "assistant" | string;
  content: string;
  /** 执行链路图谱（assistant 消息，后端落库的完成态快照；无委派时为 null） */
  graph?: ExecutionGraphModel | null;
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

/** 图谱节点统一状态：等待 / 执行中 / 完成 / 失败 */
export type GraphNodeStatus = "pending" | "running" | "ok" | "failed";

/** 执行图谱：统一的「主 Agent → 子 Agent(→工具调用)」有向图节点模型。
 *
 * 一次对话轮次内可能有多段委派（fan_out 并行批次 与 串行 delegate 并存），
 * 每段是一个 SubAgentNode；主 Agent 作为唯一根节点，汇总作为唯一叶子节点。
 */
export interface GraphToolNode {
  callId: string;
  tool: string;
  status: GraphNodeStatus;
  ms?: number;
  preview?: string;
  /** 进入 running 的客户端时间戳，驱动实时计时 */
  startedAt?: number;
}

export interface SubAgentNode {
  /** 委派 id（fanout_id 或 delegate_id） */
  id: string;
  agent: string;
  task: string;
  status: GraphNodeStatus;
  ms?: number;
  error?: string;
  /** 并行批次内的子任务归属 id（fan_out 用），串行 delegate 为空 */
  taskId?: string;
  tools: GraphToolNode[];
  /** 进入 running 的客户端时间戳，驱动实时计时 */
  startedAt?: number;
}

/** 执行图谱整体：一轮对话的委派拓扑 */
export interface ExecutionGraphModel {
  status: "running" | "done";
  subAgents: SubAgentNode[];
  okCount: number;
  failCount: number;
  totalMs?: number;
  /** 主 Agent 节点进入 running 的客户端时间戳，驱动全程实时计时（send 即刻创建） */
  mainStartedAt?: number;
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
  /** 执行链路图（本轮有子 Agent 委派时非空，含 fan_out 并行与串行 delegate） */
  graph?: ExecutionGraphModel;
}

/** 待审批请求（approval_required 帧） */
export interface PendingApproval {
  runId: string;
  callId: string;
  tool: string;
  tier: string;
  reason: string;
}
