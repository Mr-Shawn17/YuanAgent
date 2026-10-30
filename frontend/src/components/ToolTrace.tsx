"use client";

// 工具轨迹折叠面板：每次调用一行，展开可见参数与结果预览
import type { ToolTraceItem } from "@/lib/types";

const STATUS_META: Record<ToolTraceItem["status"], { label: string; className: string }> = {
  running: { label: "执行中…", className: "bg-blue-100 text-blue-700" },
  awaiting_approval: { label: "待审批", className: "bg-amber-100 text-amber-700" },
  ok: { label: "完成", className: "bg-emerald-100 text-emerald-700" },
  failed: { label: "失败/拒绝", className: "bg-rose-100 text-rose-700" },
};

function TierBadge({ tier }: { tier: string }) {
  const color =
    tier === "danger"
      ? "bg-rose-50 text-rose-600 border-rose-200"
      : tier === "write"
        ? "bg-amber-50 text-amber-600 border-amber-200"
        : "bg-slate-50 text-slate-500 border-slate-200";
  return (
    <span className={`rounded border px-1 text-[10px] leading-4 ${color}`}>{tier || "read"}</span>
  );
}

export default function ToolTrace({ tools }: { tools: ToolTraceItem[] }) {
  if (tools.length === 0) return null;
  return (
    <details className="mt-2 rounded-lg border border-slate-200 bg-slate-50/60 text-xs">
      <summary className="cursor-pointer select-none px-3 py-1.5 font-medium text-slate-500">
        工具轨迹（{tools.length}）
      </summary>
      <ul className="space-y-2 px-3 pb-3">
        {tools.map((t) => {
          const meta = STATUS_META[t.status];
          return (
            <li key={t.callId} className="rounded-md border border-slate-200 bg-white p-2">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-mono font-semibold text-slate-700">{t.tool}</span>
                <TierBadge tier={t.tier} />
                <span className="text-slate-400">{t.origin}</span>
                <span className={`ml-auto rounded px-1.5 py-0.5 text-[10px] ${meta.className}`}>
                  {meta.label}
                  {typeof t.ms === "number" && t.ms > 0 ? ` · ${t.ms}ms` : ""}
                </span>
              </div>
              <pre className="mt-1.5 overflow-x-auto rounded bg-slate-50 p-1.5 font-mono text-[11px] text-slate-500">
                {JSON.stringify(t.args, null, 2)}
              </pre>
              {t.preview ? (
                <pre className="mt-1.5 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-slate-50 p-1.5 font-mono text-[11px] text-slate-600">
                  {t.preview}
                </pre>
              ) : null}
            </li>
          );
        })}
      </ul>
    </details>
  );
}
