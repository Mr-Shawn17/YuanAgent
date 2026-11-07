"""端到端验证多工作目录：绑定会话 → 触发写文件 → 验证文件落在指定目录。

用法：python scripts/verify_workspace.py

流程：
1. 绑定会话到 %USERPROFILE%\\Desktop（桌面）
2. 通过 /v1/chat 触发写文件（会话 session_id 复用绑定过的）
3. 捕获 approval_required，POST approve 允许
4. 验证文件确实写到桌面目录（而非默认 ~/YuanAgent-workspace）
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"

DESKTOP = str((Path.home() / "Desktop").resolve())
SESSION_ID = "ws-e2e-demo"
TARGET_FILE = "ws_verify_hello.txt"


def put(url: str, body: dict) -> str:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    return urllib.request.urlopen(req).read().decode("utf-8")


def post_sse(url: str, body: dict) -> list[dict]:
    """POST 并解析 SSE 帧，返回帧列表。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    frames: list[dict] = []
    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            try:
                frames.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return frames


def main() -> int:
    # 1. 绑定会话到桌面
    put(f"{BASE}/v1/sessions/{SESSION_ID}/workspace", {"workspace": DESKTOP})
    print(f"1. 已绑定会话 {SESSION_ID} 到 {DESKTOP}")

    # 2. 触发写文件（会话复用已绑定的 session_id）
    message = f"请在工作目录写一个文件 {TARGET_FILE}，内容：多工作目录验证成功"
    frames = post_sse(
        f"{BASE}/v1/chat",
        {"message": message, "session_id": SESSION_ID},
    )

    approval = next(
        (f for f in frames if f.get("type") == "approval_required"), None
    )
    if approval is None:
        print("2. 未出现审批帧，帧列表：")
        for f in frames:
            print("   ", f.get("type"), str(f)[:120])
        return 1
    run_id = approval["run_id"]
    call_id = approval["call_id"]
    print(f"2. 触发写文件，捕获审批 run_id={run_id} call_id={call_id}")

    # 3. 批准
    post_sse(
        f"{BASE}/v1/chat/{run_id}/approve",
        {"call_id": call_id, "decision": "allow"},
    )
    print("3. 已批准写文件")

    # 4. 验证文件落在桌面（而非默认目录）
    target = Path(DESKTOP) / TARGET_FILE
    if target.exists():
        content = target.read_text(encoding="utf-8")
        print(f"4. ✅ 文件已写入桌面：{target}")
        print(f"   内容：{content}")
        target.unlink()  # 清理
        print("   （已清理验证文件）")
        return 0
    print(f"4. ❌ 桌面未发现文件：{target}")
    print("   请检查是否写到默认目录：")
    default = Path.home() / "YuanAgent-workspace" / TARGET_FILE
    print(f"   默认目录存在：{default.exists()}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
