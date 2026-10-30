"use client";

// 输入区：Enter 发送、Shift+Enter 换行；流进行中禁用
import { useState } from "react";

interface Props {
  busy: boolean;
  onSend: (text: string) => void;
}

export default function Composer({ busy, onSend }: Props) {
  const [text, setText] = useState("");

  const submit = () => {
    const value = text.trim();
    if (!value || busy) return;
    setText("");
    onSend(value);
  };

  return (
    <div className="border-t border-slate-200 bg-white p-3">
      <div className="mx-auto flex max-w-3xl items-end gap-2">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
          rows={Math.min(6, Math.max(1, text.split("\n").length))}
          placeholder={busy ? "正在生成中…" : "说点什么吧（Enter 发送，Shift+Enter 换行）"}
          disabled={busy}
          className="flex-1 resize-none rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm leading-6 text-slate-800 placeholder:text-slate-400 focus:border-blue-400 focus:outline-none disabled:bg-slate-50"
        />
        <button
          type="button"
          onClick={submit}
          disabled={busy || !text.trim()}
          className="h-10 shrink-0 rounded-xl bg-blue-600 px-5 text-sm font-medium text-white transition hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-40"
        >
          发送
        </button>
      </div>
    </div>
  );
}
