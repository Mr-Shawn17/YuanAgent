"use client";

// 执行链路图：fan_out 并行批次的主 Agent 节点 + 子任务泳道可视化
// 状态符号：◐ 运行中（脉动）● 完成 ✕ 失败 ◔ 等待
import type { FanoutGraph, FanoutTaskNode } from "@/lib/types";

const TASK_STATUS: Record<
  FanoutTaskNode["status"],
  { symbol: string; label: string; symbolClass: string; badgeClass: string; rowClass: string }
> = {
  pending: {
    symbol: "◔",
    label: "等待中",
    symbolClass: "text-slate-400",
    badgeClass: "bg-slate-100 text-slate-500",
    rowClass: "border-slate-200 bg-slate-50/60",
  },
  running: {
    symbol: "◐",
    label: "执行中…",
    symbolClass: "animate-pulse text-blue-600",
    badgeClass: "bg-blue-100 text-blue-700",
    rowClass: "border-blue-200 bg-blue-50/50",
  },
  ok: {
    symbol: "●",
    label: "完成",
    symbolClass: "text-emerald-600",
    badgeClass: "bg-emerald-100 text-emerald-700",
    rowClass: "border-emerald-200 bg-white",
  },
  failed: {
    symbol: "✕",
    label: "失败",
    symbolClass: "text-rose-600",
    badgeClass: "bg-rose-100 text-rose-700",
    rowClass: "border-rose-200 bg-rose-50/50",
  },
};

function fmtMs(ms?: number): string {
  if (typeof ms !== "number" || ms <= 0) return "";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

function TaskRow({ task }: { task: FanoutTaskNode }) {
  const meta = TASK_STATUS[task.status];
  return (
    <li className={`rounded-lg border p-2.5 ${meta.rowClass}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className={`text-sm font-bold ${meta.symbolClass}`}>{meta.symbol}</span>
        <span className="rounded bg-violet-50 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-violet-600">
          {task.agent}
        </span>
        <span className="min-w-0 flex-1 truncate text-xs text-slate-600" title={task.task}>
          {task.task}
        </span>
        <span className={`rounded px-1.5 py-0.5 text-[10px] ${meta.badgeClass}`}>
          {meta.label}
          {task.ms ? ` · ${fmtMs(task.ms)}` : ""}
        </span>
      </div>
      {task.error ? (
        <p className="mt-1.5 rounded bg-rose-50 px-2 py-1 font-mono text-[11px] text-rose-600">
          {task.error}
        </p>
      ) : null}
      {task.tools.length > 0 ? (
        <details className="mt-1.5">
          <summary className="cursor-pointer select-none text-[11px] text-slate-400">
            内部工具调用（{task.tools.length}）
          </summary>
          <ul className="mt-1 space-y-1 border-l-2 border-slate-200 pl-2">
            {task.tools.map((t) => (
              <li key={t.callId} className="flex flex-wrap items-center gap-1.5 text-[11px]">
                <span
                  className={
                    t.status === "ok"
                      ? "text-emerald-600"
                      : t.status === "failed"
                        ? "text-rose-600"
                        : "animate-pulse text-blue-600"
                  }
                >
                  {t.status === "ok" ? "●" : t.status === "failed" ? "✕" : "◐"}
                </span>
                <span className="font-mono font-semibold text-slate-600">{t.tool}</span>
                {t.ms ? <span className="text-slate-400">{fmtMs(t.ms)}</span> : null}
                {t.preview ? (
                  <span className="min-w-0 flex-1 truncate text-slate-400" title={t.preview}>
                    {t.preview}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </li>
  );
}

export default function ExecutionGraph({ graph }: { graph: FanoutGraph }) {
  const running = graph.status === "running";
  const doneParts: string[] = [];
  if (graph.status === "done") {
    doneParts.push(`完成 ${graph.okCount}`);
    if (graph.failCount > 0) doneParts.push(`失败 ${graph.failCount}`);
    if (graph.totalMs) doneParts.push(fmtMs(graph.totalMs));
  }
  return (
    <details
      open={running}
      className="mt-2 rounded-xl border border-slate-200 bg-gradient-to-b from-slate-50 to-white text-xs shadow-sm"
    >
      <summary className="cursor-pointer select-none px-3 py-2 font-medium text-slate-600">
        <span className="mr-1.5 inline-block rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] font-semibold text-indigo-600">
          执行链路图
        </span>
        并行子任务 ×{graph.tasks.length}
        {running ? (
          <span className="ml-2 animate-pulse text-blue-600">执行中…</span>
        ) : (
          <span className="ml-2 text-slate-400">{doneParts.join(" · ")}</span>
        )}
      </summary>
      <div className="px-3 pb-3">
        {/* 主 Agent 节点 */}
        <div className="flex items-center gap-2 rounded-lg border border-indigo-200 bg-indigo-50/60 px-2.5 py-2">
          <span className="rounded-md bg-indigo-600 px-2 py-1 text-[10px] font-bold text-white">
            主 Agent
          </span>
          <span className="text-[11px] text-slate-500">
            判断需并行拆分 → 调用 fan_out 委派 {graph.tasks.length} 个子任务
            {running ? "，等待结果…" : "，已汇总全部结果输出对比结论"}
          </span>
        </div>
        {/* 连接线 */}
        <div className="ml-5 h-3 w-px bg-slate-300" aria-hidden />
        {/* 子任务泳道 */}
        <ul className="ml-3 space-y-2 border-l-2 border-slate-200 pl-3">
          {graph.tasks.map((t) => (
            <TaskRow key={t.taskId} task={t} />
          ))}
        </ul>
        {/* 汇总回流 */}
        <div className="ml-5 h-3 w-px bg-slate-300" aria-hidden />
        <div className="flex items-center gap-2 rounded-lg border border-indigo-200 bg-indigo-50/60 px-2.5 py-2">
          <span className="rounded-md bg-indigo-600 px-2 py-1 text-[10px] font-bold text-white">
            主 Agent
          </span>
          <span className="text-[11px] text-slate-500">
            {running ? "（等待中）结果汇总与对比" : "基于全部子任务结果完成汇总"}
          </span>
        </div>
      </div>
    </details>
  );
}
