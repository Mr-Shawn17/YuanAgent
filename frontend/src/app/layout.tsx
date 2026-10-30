import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "阿元 · 日常聊天 Agent",
  description: "集成工具、RAG 与 MCP 的日常聊天智能体",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body className="antialiased">{children}</body>
    </html>
  );
}
