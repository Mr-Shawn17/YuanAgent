"use client";

// 轻量实时计时：每 500ms 触发一次重渲染，用于「执行中」节点的秒表。
// 只在存在 running 节点时开启 tick，避免无谓的全局重渲染。
import { useEffect, useState } from "react";

export function useNow(active: boolean, intervalMs = 500): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [active, intervalMs]);
  return now;
}
