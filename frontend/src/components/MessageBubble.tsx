"use client";

// 消息气泡：assistant 气泡内嵌执行链路图、工具轨迹、引用卡片、记忆提示与错误
import CitationCards from "./CitationCards";
import ExecutionGraph from "./ExecutionGraph";
import ToolTrace from "./ToolTrace";
import type { ChatMessage } from "@/lib/types";

export default function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`max-w-[85%] ${isUser ? "" : "flex-1"} md:max-w-[75%]`}>
        <div
          className={`rounded-2xl px-4 py-2.5 text-sm leading-6 shadow-sm ${
            isUser
              ? "rounded-br-md bg-blue-600 text-white"
              : "rounded-bl-md border border-slate-200 bg-white text-slate-800"
          }`}
        >
          {message.content ? (
            <p className="whitespace-pre-wrap break-words">{message.content}</p>
          ) : null}
          {!isUser && message.streaming ? (
            <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse rounded-sm bg-slate-400 align-text-bottom" />
          ) : null}
          {!isUser && !message.content && !message.streaming && !message.error ? (
            <p className="text-slate-400">（本轮无文本输出）</p>
          ) : null}
          {message.error ? (
            <p className="mt-1.5 rounded-lg bg-rose-50 px-2.5 py-1.5 text-xs text-rose-600">
              出错了：{message.error}
            </p>
          ) : null}
        </div>
        {!isUser ? (
          <div className="mt-0.5">
            {message.memoryCount > 0 ? (
              <p className="mt-1 text-[11px] text-violet-500">
                💭 已召回 {message.memoryCount} 条长期记忆
              </p>
            ) : null}
            {message.graph ? <ExecutionGraph graph={message.graph} /> : null}
            <ToolTrace tools={message.tools} />
            <CitationCards items={message.citations} />
            {message.usage ? (
              <p className="mt-1 text-right text-[10px] text-slate-300">
                tokens ↑{message.usage.tokensIn} ↓{message.usage.tokensOut}
              </p>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
