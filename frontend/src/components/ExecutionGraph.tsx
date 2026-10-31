"use client";

// 执行图谱：主 Agent → 子 Agent(→工具调用) → 汇总 的有向图。
// 纯 SVG 绘制节点与连线（零依赖），节点内容用 foreignObject 嵌入 HTML 排版。
// 状态色：绿=完成 / 黄=执行中 / 红=失败 / 灰=等待；执行中节点旁实时计时。
import { useMemo } from "react";

import { useNow } from "@/hooks/useNow";
import type { ExecutionGraphModel, GraphNodeStatus, SubAgentNode } from "@/lib/types";

// 状态 → 颜色/符号（与现有 ToolTrace 语义一致）
const STATUS_META: Record<
  GraphNodeStatus,
  { fill: string; stroke: string; text: string; label: string; symbol: string }
> = {
  pending: { fill: "#f1f5f9", stroke: "#cbd5e1", text: "#64748b", label: "等待", symbol: "◔" },
  running: { fill: "#fef9c3", stroke: "#eab308", text: "#854d0e", label: "执行中", symbol: "◐" },
  ok: { fill: "#dcfce7", stroke: "#22c55e", text: "#15803d", label: "完成", symbol: "●" },
  failed: { fill: "#fee2e2", stroke: "#ef4444", text: "#b91c1c", label: "失败", symbol: "✕" },
};

// 布局常量（单位 px）
const NODE_W = 168;
const NODE_H = 58;
const TOOL_H = 30;
const GAP_X = 16;
const GAP_Y = 46;
const MAIN_H = 52;
const SUMMARY_H = 52;

function fmtMs(ms?: number): string {
  if (typeof ms !== "number" || ms <= 0) return "";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

/** 单个子 Agent 节点卡片（foreignObject 内 HTML） */
function SubAgentCard({ node, liveMs }: { node: SubAgentNode; liveMs?: number }) {
  const meta = STATUS_META[node.status];
  const isRunning = node.status === "running";
  const elapsed = isRunning && liveMs !== undefined ? liveMs : node.ms;
  return (
    <div
      className="flex h-full w-full flex-col justify-between rounded-lg border bg-white px-2 py-1.5"
      style={{ borderColor: meta.stroke, background: meta.fill }}
    >
      <div className="flex items-center gap-1">
        <span
          className={`text-[11px] font-bold ${isRunning ? "animate-pulse" : ""}`}
          style={{ color: meta.stroke }}
        >
          {meta.symbol}
        </span>
        <span className="truncate rounded bg-violet-100 px-1 font-mono text-[9px] font-semibold text-violet-700">
          {node.agent}
        </span>
      </div>
      <div className="truncate text-[10px] leading-tight text-slate-600" title={node.task}>
        {node.task}
      </div>
      <div className="flex items-center justify-between">
        <span className="text-[9px]" style={{ color: meta.text }}>
          {meta.label}
        </span>
        <span className="font-mono text-[10px] tabular-nums" style={{ color: meta.text }}>
          {elapsed !== undefined && elapsed > 0 ? fmtMs(elapsed) : isRunning ? "0ms" : ""}
        </span>
      </div>
      {node.error ? (
        <div className="truncate text-[9px] text-rose-600" title={node.error}>
          {node.error}
        </div>
      ) : null}
    </div>
  );
}

/** 工具调用小节点 */
function ToolCard({
  tool,
  status,
  ms,
  liveMs,
}: {
  tool: string;
  status: GraphNodeStatus;
  ms?: number;
  liveMs?: number;
}) {
  const meta = STATUS_META[status];
  const isRunning = status === "running";
  const elapsed = isRunning && liveMs !== undefined ? liveMs : ms;
  return (
    <div
      className="flex h-full w-full items-center justify-between rounded-md border px-2"
      style={{ borderColor: meta.stroke, background: meta.fill }}
    >
      <span className="flex min-w-0 items-center gap-1">
        <span className={`text-[10px] ${isRunning ? "animate-pulse" : ""}`} style={{ color: meta.stroke }}>
          {meta.symbol}
        </span>
        <span className="truncate font-mono text-[10px] text-slate-600" title={tool}>
          {tool}
        </span>
      </span>
      <span className="ml-1 shrink-0 font-mono text-[9px] tabular-nums" style={{ color: meta.text }}>
        {elapsed !== undefined && elapsed > 0 ? fmtMs(elapsed) : isRunning ? "0ms" : ""}
      </span>
    </div>
  );
}

export default function ExecutionGraph({ graph }: { graph: ExecutionGraphModel }) {
  const hasRunning = useMemo(
    () =>
      graph.status === "running" ||
      graph.subAgents.some((n) => n.status === "running" || n.tools.some((t) => t.status === "running")),
    [graph],
  );
  const now = useNow(hasRunning);

  const subAgents = graph.subAgents;
  const count = subAgents.length;

  // 计算布局：每个子 Agent 一列，列宽 = 节点宽；工具竖排在节点下方
  // 无子 Agent 时（纯主 Agent 直接回答），仍保留「主 Agent → 汇总」单链路
  const maxTools = Math.max(1, ...subAgents.map((n) => n.tools.length));
  const colW = NODE_W + GAP_X;
  const totalW = Math.max(400, count * colW - GAP_X + 24);
  const subTop = MAIN_H + GAP_Y;
  const toolsTop = subTop + NODE_H + 10;
  // 无子 Agent 时汇总节点直接紧贴主 Agent（跳过空工具区）
  const summaryTop = count === 0 ? MAIN_H + GAP_Y : toolsTop + maxTools * (TOOL_H + 6) + 28;
  const totalH = summaryTop + SUMMARY_H + 16;

  // 各子 Agent 列的中心 X
  const centers = subAgents.map((_, i) => 12 + i * colW + NODE_W / 2);
  const mainCenterX = totalW / 2;

  // 主 Agent 状态：整体 done → 完成；否则执行中（全程实时计时）
  const mainStatus: GraphNodeStatus = graph.status === "done" ? "ok" : "running";
  const summaryStatus: GraphNodeStatus =
    graph.status === "done" ? (graph.failCount > 0 ? "failed" : "ok") : "pending";
  const mainMeta = STATUS_META[mainStatus];
  const summaryMeta = STATUS_META[summaryStatus];
  const mainLiveMs = graph.mainStartedAt ? now - graph.mainStartedAt : undefined;

  return (
    <details
      open={graph.status === "running"}
      className="mt-2 overflow-hidden rounded-xl border border-slate-200 bg-gradient-to-b from-slate-50 to-white text-xs shadow-sm"
    >
      <summary className="cursor-pointer select-none px-3 py-2 font-medium text-slate-600">
        <span className="mr-1.5 inline-block rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] font-semibold text-indigo-600">
          执行图谱
        </span>
        子 Agent ×{count}
        {graph.status === "running" ? (
          <span className="ml-2 animate-pulse text-blue-600">执行中…</span>
        ) : (
          <span className="ml-2 text-slate-400">
            {graph.okCount > 0 ? `完成 ${graph.okCount}` : ""}
            {graph.failCount > 0 ? ` · 失败 ${graph.failCount}` : ""}
            {graph.totalMs ? ` · ${fmtMs(graph.totalMs)}` : ""}
          </span>
        )}
      </summary>
      <div className="px-2 pb-2">
        <svg width="100%" viewBox={`0 0 ${totalW} ${totalH}`} className="block">
          {/* 主 Agent 节点 */}
          <rect
            x={mainCenterX - NODE_W / 2}
            y={0}
            width={NODE_W}
            height={MAIN_H}
            rx={10}
            fill={mainMeta.fill}
            stroke={mainMeta.stroke}
            strokeWidth={1.5}
          />
          <foreignObject x={mainCenterX - NODE_W / 2} y={0} width={NODE_W} height={MAIN_H}>
            <div className="flex h-full flex-col items-center justify-center gap-0.5">
              <span className="rounded bg-indigo-600 px-1.5 py-0.5 text-[9px] font-bold text-white">主 Agent</span>
              <span className="px-1 text-center text-[9px] leading-tight text-slate-500">
                {graph.status === "done"
                  ? count > 0
                    ? "已完成全部委派与汇总"
                    : "已直接完成回答"
                  : "分析问题并拆分/委派子任务…"}
              </span>
              {mainLiveMs !== undefined && mainLiveMs > 0 ? (
                <span
                  className={`font-mono text-[9px] tabular-nums ${mainStatus === "running" ? "animate-pulse" : ""}`}
                  style={{ color: mainMeta.text }}
                >
                  {fmtMs(mainLiveMs)}
                </span>
              ) : null}
            </div>
          </foreignObject>

          {/* 主 Agent → 各子 Agent 连线 */}
          {centers.map((cx, i) => (
            <line
              key={`m2s-${i}`}
              x1={mainCenterX}
              y1={MAIN_H}
              x2={cx}
              y2={subTop}
              stroke={STATUS_META[subAgents[i].status].stroke}
              strokeWidth={1.5}
              strokeDasharray={subAgents[i].status === "running" ? "4 3" : undefined}
            />
          ))}

          {/* 无子 Agent 时：主 Agent → 汇总 直连（单链路） */}
          {count === 0 ? (
            <line
              x1={mainCenterX}
              y1={MAIN_H}
              x2={mainCenterX}
              y2={summaryTop}
              stroke={mainMeta.stroke}
              strokeWidth={1.5}
              strokeDasharray={graph.status === "running" ? "4 3" : undefined}
            />
          ) : null}

          {/* 子 Agent 节点 + 工具 + 连线 */}
          {subAgents.map((node, i) => {
            const cx = centers[i];
            const meta = STATUS_META[node.status];
            const tools = node.tools;
            return (
              <g key={node.id}>
                {/* 子 Agent 节点 */}
                <rect
                  x={cx - NODE_W / 2}
                  y={subTop}
                  width={NODE_W}
                  height={NODE_H}
                  rx={8}
                  fill={meta.fill}
                  stroke={meta.stroke}
                  strokeWidth={1.5}
                />
                <foreignObject x={cx - NODE_W / 2} y={subTop} width={NODE_W} height={NODE_H}>
                  <SubAgentCard
                    node={node}
                    liveMs={node.startedAt ? now - node.startedAt : undefined}
                  />
                </foreignObject>

                {/* 子 Agent → 工具连线 */}
                {tools.map((t, ti) => {
                  const ty = toolsTop + ti * (TOOL_H + 6);
                  return (
                    <g key={t.callId}>
                      <line
                        x1={cx}
                        y1={subTop + NODE_H}
                        x2={cx}
                        y2={ty}
                        stroke={STATUS_META[t.status].stroke}
                        strokeWidth={1.2}
                        strokeDasharray={t.status === "running" ? "3 2" : undefined}
                      />
                      <rect
                        x={cx - NODE_W / 2}
                        y={ty}
                        width={NODE_W}
                        height={TOOL_H}
                        rx={6}
                        fill={STATUS_META[t.status].fill}
                        stroke={STATUS_META[t.status].stroke}
                        strokeWidth={1.2}
                      />
                      <foreignObject x={cx - NODE_W / 2} y={ty} width={NODE_W} height={TOOL_H}>
                        <ToolCard
                          tool={t.tool}
                          status={t.status}
                          ms={t.ms}
                          liveMs={t.startedAt ? now - t.startedAt : undefined}
                        />
                      </foreignObject>
                    </g>
                  );
                })}

                {/* 子 Agent → 汇总连线 */}
                <line
                  x1={cx}
                  y1={toolsTop + tools.length * (TOOL_H + 6) - 6}
                  x2={mainCenterX}
                  y2={summaryTop}
                  stroke={meta.stroke}
                  strokeWidth={1.2}
                  strokeDasharray={node.status === "running" ? "3 2" : undefined}
                />
              </g>
            );
          })}

          {/* 汇总节点 */}
          <rect
            x={mainCenterX - NODE_W / 2}
            y={summaryTop}
            width={NODE_W}
            height={SUMMARY_H}
            rx={10}
            fill={summaryMeta.fill}
            stroke={summaryMeta.stroke}
            strokeWidth={1.5}
          />
          <foreignObject x={mainCenterX - NODE_W / 2} y={summaryTop} width={NODE_W} height={SUMMARY_H}>
            <div className="flex h-full flex-col items-center justify-center gap-0.5">
              <span className="rounded bg-indigo-600 px-1.5 py-0.5 text-[9px] font-bold text-white">汇总</span>
              <span className="px-1 text-center text-[9px] leading-tight" style={{ color: summaryMeta.text }}>
                {summaryStatus === "pending"
                  ? count === 0
                    ? "主 Agent 分析中…"
                    : "等待子任务结果…"
                  : summaryStatus === "failed"
                    ? `完成 ${graph.okCount} · 失败 ${graph.failCount}`
                    : count === 0
                      ? "已完成"
                      : `完成 ${graph.okCount} 个子任务`}
              </span>
            </div>
          </foreignObject>
        </svg>
      </div>
    </details>
  );
}
