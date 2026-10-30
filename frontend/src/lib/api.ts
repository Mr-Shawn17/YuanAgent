// API 访问层：手写 fetch + ReadableStream 的 SSE 解析（EventSource 不支持 POST）
import type { SessionInfo, SessionMessage, SseFrame } from "./types";

// 后端轻量鉴权：X-API-Key 头。密钥从 NEXT_PUBLIC_API_KEY 注入（浏览器可见，
// 对个人工具定位够用——真正挡住的是绕过前端直接打后端 8000 端口的外部请求）。
// 未配置时后端鉴权整体关闭，此头为无害冗余。
const API_KEY = process.env.NEXT_PUBLIC_API_KEY ?? "";

function apiHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...extra };
  if (API_KEY) headers["X-API-Key"] = API_KEY;
  return headers;
}

/** 后端统一错误体：{"error": {"code", "message"}} */
async function errorMessage(res: Response): Promise<string> {
  try {
    const data = (await res.json()) as { error?: { message?: string } };
    return data?.error?.message ?? `HTTP ${res.status}`;
  } catch {
    return `HTTP ${res.status}`;
  }
}

/**
 * POST 一个 JSON 请求并逐帧消费 SSE 流。
 * 帧格式（后端 api/sse.py）：`data: {json}\n\n`，按行切分解析。
 */
export async function streamSse(
  url: string,
  body: unknown,
  onFrame: (frame: SseFrame) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(url, {
    method: "POST",
    headers: apiHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(await errorMessage(res));
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx = buffer.indexOf("\n");
    while (idx >= 0) {
      const line = buffer.slice(0, idx).trimEnd();
      buffer = buffer.slice(idx + 1);
      idx = buffer.indexOf("\n");
      if (!line.startsWith("data: ")) continue;
      const payload = line.slice(6);
      if (!payload) continue;
      try {
        onFrame(JSON.parse(payload) as SseFrame);
      } catch {
        // 忽略无法解析的帧，保持流继续
      }
    }
  }
}

export async function listSessions(limit = 50): Promise<SessionInfo[]> {
  const res = await fetch(`/v1/sessions?limit=${limit}`, {
    cache: "no-store",
    headers: apiHeaders(),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = (await res.json()) as { sessions: SessionInfo[] };
  return data.sessions ?? [];
}

export async function fetchSessionMessages(sessionId: string): Promise<SessionMessage[]> {
  const res = await fetch(`/v1/sessions/${encodeURIComponent(sessionId)}/messages`, {
    cache: "no-store",
    headers: apiHeaders(),
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = (await res.json()) as { messages: SessionMessage[] };
  return data.messages ?? [];
}
