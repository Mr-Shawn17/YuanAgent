"use client";

// 工作目录选择器：下拉常用目录（执行器上报）+ 手填绝对路径。
// 选中/手填后绑定到当前会话，动态热切换并持久化。
import { useEffect, useRef, useState } from "react";

import { bindSessionWorkspace, listWorkspaces } from "@/lib/api";
import type { WorkspaceOption } from "@/lib/types";

interface Props {
  sessionId: string;
  currentWorkspace: string;
  busy: boolean;
  onBound: (workspace: string) => void;
}

export default function WorkspacePicker({
  sessionId,
  currentWorkspace,
  busy,
  onBound,
}: Props) {
  const [options, setOptions] = useState<WorkspaceOption[]>([]);
  const [open, setOpen] = useState(false);
  const [custom, setCustom] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);

  // 拉取可用目录列表（执行器在线时返回；离线为空，仅能手填）
  useEffect(() => {
    let cancelled = false;
    listWorkspaces()
      .then((ws) => {
        if (!cancelled) setOptions(ws);
      })
      .catch(() => {
        if (!cancelled) setOptions([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // 点击外部关闭下拉
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  async function bind(workspace: string) {
    const path = workspace.trim();
    if (!path || saving) return;
    setSaving(true);
    setError("");
    try {
      await bindSessionWorkspace(sessionId, path);
      onBound(path);
      setCustom("");
      setOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  const label = currentWorkspace || "选择工作目录";
  const basename = currentWorkspace
    ? currentWorkspace.split(/[\\/]/).filter(Boolean).pop() ?? currentWorkspace
    : "";

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={busy}
        className="flex items-center gap-1.5 rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-700 transition hover:bg-slate-50 disabled:opacity-50"
        title={currentWorkspace || "未绑定工作目录"}
      >
        <span className="text-slate-400">📁</span>
        <span className="max-w-[180px] truncate font-medium">
          {currentWorkspace ? basename : label}
        </span>
        <span className="text-slate-400">▾</span>
      </button>

      {open ? (
        <div className="absolute right-0 z-20 mt-1 w-72 rounded-lg border border-slate-200 bg-white p-2 shadow-lg">
          <p className="px-1 pb-1 text-[11px] text-slate-400">
            {currentWorkspace ? `当前：${currentWorkspace}` : "选择本机工作目录"}
          </p>

          {/* 手填绝对路径 */}
          <div className="flex gap-1">
            <input
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void bind(custom);
              }}
              placeholder="输入绝对路径，如 D:\\项目"
              className="min-w-0 flex-1 rounded-md border border-slate-200 px-2 py-1.5 text-xs outline-none focus:border-slate-400"
            />
            <button
              type="button"
              onClick={() => void bind(custom)}
              disabled={saving || !custom.trim()}
              className="rounded-md bg-slate-800 px-2.5 py-1.5 text-xs text-white transition hover:bg-slate-700 disabled:opacity-40"
            >
              {saving ? "绑定中" : "绑定"}
            </button>
          </div>

          {/* 常用目录下拉 */}
          {options.length > 0 ? (
            <ul className="mt-2 max-h-56 space-y-0.5 overflow-y-auto">
              {options.map((w) => (
                <li key={w.path}>
                  <button
                    type="button"
                    onClick={() => void bind(w.path)}
                    disabled={saving}
                    className="block w-full rounded-md px-2 py-1.5 text-left text-xs text-slate-700 transition hover:bg-slate-100 disabled:opacity-50"
                  >
                    <span className="block truncate font-medium">
                      {w.name || w.path.split(/[\\/]/).filter(Boolean).pop()}
                    </span>
                    <span className="block truncate text-[11px] text-slate-400">{w.path}</span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-2 px-1 text-[11px] text-slate-400">
              {busy ? "" : "执行器离线，仅支持手填路径"}
            </p>
          )}

          {error ? <p className="mt-1 px-1 text-[11px] text-rose-500">{error}</p> : null}
        </div>
      ) : null}
    </div>
  );
}
