"""API 驱动的端到端验证：发消息 → SSE → 自动审批 danger 工具 → 流结束。

验证点（2026-11-08 死锁修复回归）：
- delegate_to_env 审批后，子 Agent 内的 git_clone 经 hub.dispatch_threadsafe
  下发 runner 并可靠返回（修复前：概率性永久挂起）。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections.abc import Iterator
from typing import Any

# Windows 控制台 GBK 打不住 ✅ 等字符，切 UTF-8 输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8000"
MESSAGE = (
    "把 https://github.com/Mr-Shawn17/YuanAgent.git 克隆到本地，"
    "拉 feature/codex-devtools 分支"
)


def post(path: str, payload: dict) -> urllib.request.Request:
    return urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )


def read_sse(resp: Any) -> Iterator[dict]:
    buffer = b""
    for chunk in iter(lambda: resp.read1(4096) or resp.read(4096), b""):
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("data: "):
                try:
                    yield json.loads(text[6:])
                except ValueError:
                    pass


def main() -> int:
    # ① 发起对话
    req = post("/v1/chat", {"message": MESSAGE, "session_id": ""})
    resp = urllib.request.urlopen(req, timeout=600)

    approvals: list[dict] = []
    tokens: list[str] = []
    tool_events: list[str] = []
    final_text = ""
    # ② 流内自动审批：最多 6 次（delegate_to_env → 内层 git_clone/install_deps…）
    for _round in range(6):
        pending: dict | None = None
        for frame in read_sse(resp):
            ftype = frame.get("type", "")
            if ftype == "approval_required":
                pending = frame
                approvals.append(frame)
                print(f"[审批请求] {frame.get('tool')} call={frame.get('call_id')}")
                break
            if ftype == "tool_start":
                tool_events.append(f"start:{frame.get('tool')}")
            elif ftype == "tool_end":
                tool_events.append(f"end:{frame.get('tool')}")
            elif ftype == "run_id":
                pass  # run_id 帧已由 approval_required.run_id 覆盖
            elif ftype == "token":
                tokens.append(str(frame.get("delta", "")))
            elif ftype == "done":
                final_text = str(frame.get("text", ""))
        if pending is None:
            break  # 流自然结束
        # ③ 批准并继续读同一条 SSE
        resp.close()
        req2 = post(
            f"/v1/chat/{pending['run_id']}/approve",
            {"decision": "allow", "call_id": pending["call_id"]},
        )
        resp = urllib.request.urlopen(req2, timeout=600)

    print("=== 工具事件 ===")
    for item in tool_events:
        print(" ", item)
    print("=== 审批次数 ===", len(approvals))
    print("=== 最终回复 ===")
    print(final_text or "".join(tokens)[:800])
    return 0 if final_text or tokens else 1


if __name__ == "__main__":
    sys.exit(main())
