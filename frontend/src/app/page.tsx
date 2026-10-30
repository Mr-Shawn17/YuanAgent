"use client";

// 主页面：左侧会话列表 + 右侧聊天流（气泡/工具轨迹/引用/审批弹窗）
import { useEffect, useRef } from "react";

import ApprovalModal from "@/components/ApprovalModal";
import Composer from "@/components/Composer";
import MessageBubble from "@/components/MessageBubble";
import SessionSidebar from "@/components/SessionSidebar";
import { useChatStream } from "@/hooks/useChatStream";

export default function Home() {
  const {
    sessions,
    sessionId,
    messages,
    pendingApproval,
    busy,
    fatalError,
    send,
    approve,
    selectSession,
    newSession,
  } = useChatStream();
  const bottomRef = useRef<HTMLDivElement>(null);

  // 新 token / 新消息到达时自动滚到底部
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, pendingApproval]);

  return (
    <main className="flex h-screen overflow-hidden bg-slate-100 text-slate-900">
      <SessionSidebar
        sessions={sessions}
        activeId={sessionId}
        busy={busy}
        onSelect={(id) => void selectSession(id)}
        onNew={newSession}
      />
      <section className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-2 border-b border-slate-200 bg-white px-4 py-2.5">
          <h1 className="text-sm font-semibold text-slate-700">阿元 · 日常聊天 Agent</h1>
          <span className="ml-auto truncate font-mono text-[11px] text-slate-400">
            {sessionId ? `会话 ${sessionId.slice(0, 8)}` : "新会话（发送后自动创建）"}
          </span>
        </header>
        <div className="flex-1 overflow-y-auto px-4 py-4">
          <div className="mx-auto max-w-3xl space-y-4">
            {fatalError ? (
              <p className="rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-600">{fatalError}</p>
            ) : null}
            {messages.length === 0 && !fatalError ? (
              <div className="py-20 text-center text-sm text-slate-400">
                <p className="text-3xl">👋</p>
                <p className="mt-3">你好，我是阿元。可以聊天、查知识库、执行工具。</p>
                <p className="mt-1 text-xs">写操作会先弹出审批窗，经你确认后才执行。</p>
              </div>
            ) : null}
            {messages.map((m) => (
              <MessageBubble key={m.id} message={m} />
            ))}
            <div ref={bottomRef} />
          </div>
        </div>
        <Composer busy={busy || pendingApproval !== null} onSend={(text) => void send(text)} />
      </section>
      {pendingApproval ? (
        <ApprovalModal approval={pendingApproval} busy={busy} onDecide={(d) => void approve(d)} />
      ) : null}
    </main>
  );
}
