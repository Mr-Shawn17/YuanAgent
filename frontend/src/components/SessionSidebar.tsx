"use client";

// 会话侧栏：历史会话列表 + 新对话入口
import type { SessionInfo } from "@/lib/types";

interface Props {
  sessions: SessionInfo[];
  activeId: string;
  busy: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
}

function formatTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 16);
  return `${(d.getMonth() + 1).toString().padStart(2, "0")}-${d.getDate().toString().padStart(2, "0")} ${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
}

export default function SessionSidebar({ sessions, activeId, busy, onSelect, onNew }: Props) {
  return (
    <aside className="flex h-full w-64 shrink-0 flex-col border-r border-slate-200 bg-slate-50">
      <div className="p-3">
        <button
          type="button"
          onClick={onNew}
          disabled={busy}
          className="w-full rounded-lg bg-slate-800 px-3 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:opacity-50"
        >
          ＋ 新对话
        </button>
      </div>
      <nav className="flex-1 space-y-1 overflow-y-auto px-2 pb-3">
        {sessions.length === 0 ? (
          <p className="px-2 py-4 text-xs text-slate-400">还没有历史会话</p>
        ) : null}
        {sessions.map((s) => (
          <button
            key={s.thread_id}
            type="button"
            disabled={busy}
            onClick={() => onSelect(s.thread_id)}
            className={`block w-full rounded-lg px-3 py-2 text-left transition disabled:cursor-not-allowed ${
              s.thread_id === activeId
                ? "bg-white shadow-sm ring-1 ring-slate-200"
                : "hover:bg-white/60"
            }`}
          >
            <span className="block truncate text-sm text-slate-700">{s.title || "(无标题)"}</span>
            <span className="mt-0.5 block text-[11px] text-slate-400">
              {formatTime(s.updated_at)} · {s.message_count} 条
            </span>
          </button>
        ))}
      </nav>
    </aside>
  );
}
