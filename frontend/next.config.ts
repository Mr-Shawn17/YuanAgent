import type { NextConfig } from "next";

// 后端 API 统一走 Next rewrites 代理：浏览器同源访问 /v1/*，免 CORS 且 SSE 可流式透传
const BACKEND = process.env.BACKEND_ORIGIN ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      { source: "/v1/:path*", destination: `${BACKEND}/v1/:path*` },
      { source: "/health", destination: `${BACKEND}/health` },
    ];
  },
};

export default nextConfig;
