"use client";

// 引用卡片：RAG 检索来源，可点开看片段全文；有 uri 时可跳转原文
import type { KnowledgeChunk } from "@/lib/types";

export default function CitationCards({ items }: { items: KnowledgeChunk[] }) {
  if (items.length === 0) return null;
  return (
    <div className="mt-2 space-y-1.5">
      {items.map((c, i) => {
        const src = c.source ?? {};
        const title = src.title || src.doc_id || c.point_id || "未命名来源";
        const locator = [src.section, src.page ? `第 ${src.page} 页` : ""]
          .filter(Boolean)
          .join(" · ");
        const body = (
          <>
            <div className="flex items-center gap-1.5 text-xs">
              <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-indigo-100 text-[10px] font-bold text-indigo-600">
                {i + 1}
              </span>
              <span className="truncate font-medium text-slate-700">{title}</span>
              {locator ? <span className="shrink-0 text-[10px] text-slate-400">{locator}</span> : null}
            </div>
            <p className="mt-1 line-clamp-2 text-[11px] leading-4 text-slate-500">{c.text}</p>
          </>
        );
        return src.uri ? (
          <a
            key={c.point_id || i}
            href={src.uri}
            target="_blank"
            rel="noreferrer"
            className="block rounded-lg border border-indigo-100 bg-indigo-50/40 p-2 transition hover:border-indigo-300 hover:bg-indigo-50"
          >
            {body}
          </a>
        ) : (
          <details
            key={c.point_id || i}
            className="rounded-lg border border-indigo-100 bg-indigo-50/40 p-2 transition open:bg-indigo-50"
          >
            <summary className="cursor-pointer list-none [&::-webkit-details-marker]:hidden">
              {body}
            </summary>
            <p className="mt-1.5 whitespace-pre-wrap border-t border-indigo-100 pt-1.5 text-[11px] leading-4 text-slate-600">
              {c.text}
            </p>
          </details>
        );
      })}
    </div>
  );
}
