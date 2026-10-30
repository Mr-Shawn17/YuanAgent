"use client";

// 审批弹窗：HITL 阻断点，未做决定前遮罩不可关闭
import type { PendingApproval } from "@/lib/types";

interface Props {
  approval: PendingApproval;
  busy: boolean;
  onDecide: (decision: "allow" | "deny") => void;
}

export default function ApprovalModal({ approval, busy, onDecide }: Props) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
      <div className="w-full max-w-md rounded-2xl bg-white p-5 shadow-xl">
        <div className="flex items-center gap-2">
          <span className="flex h-8 w-8 items-center justify-center rounded-full bg-amber-100 text-amber-600">
            ⚠
          </span>
          <h2 className="text-base font-semibold text-slate-800">需要你的确认</h2>
        </div>
        <p className="mt-3 text-sm leading-6 text-slate-600">
          智能体想执行 <span className="font-mono font-semibold text-slate-800">{approval.tool}</span>
          {approval.tier ? (
            <span className="ml-1.5 rounded border border-amber-200 bg-amber-50 px-1 text-[11px] text-amber-600">
              {approval.tier} 档
            </span>
          ) : null}
        </p>
        {approval.reason ? (
          <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-50 p-3 font-mono text-xs leading-5 text-slate-600">
            {approval.reason}
          </pre>
        ) : null}
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => onDecide("deny")}
            className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-600 transition hover:bg-slate-50 disabled:opacity-50"
          >
            拒绝
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onDecide("allow")}
            className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-emerald-500 disabled:opacity-50"
          >
            允许执行
          </button>
        </div>
      </div>
    </div>
  );
}
