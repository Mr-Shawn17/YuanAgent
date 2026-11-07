"""通信协议消息模型（执行器侧独立维护，与服务端 JSON 结构保持一致）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HelloMessage(BaseModel):
    type: str = "hello"
    device_id: str = ""
    client: str = "yuan-runner/0.1"


class PingMessage(BaseModel):
    type: str = "ping"


class WorkspaceInfo(BaseModel):
    """执行器上报的一个可用工作目录。"""

    path: str
    name: str = ""
    kind: str = "custom"  # home / desktop / documents / downloads / code / custom


class WorkspacesMessage(BaseModel):
    """执行器握手后上报本机可用工作目录列表。"""

    type: str = "workspaces"
    workspaces: list[WorkspaceInfo] = Field(default_factory=list)


class ToolRequest(BaseModel):
    """中心下发的一条工具任务。"""

    type: str = "tool_request"
    request_id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = 120.0
    # 指定在哪个工作目录下执行；为空时回退到连接默认 workspace
    workspace: str = ""


class CancelMessage(BaseModel):
    type: str = "cancel"
    request_id: str


class ToolResult(BaseModel):
    """执行器回传的一条结果。"""

    type: str = "tool_result"
    request_id: str
    ok: bool
    output: str = ""
    error: str = ""


__all__ = [
    "CancelMessage",
    "HelloMessage",
    "PingMessage",
    "ToolRequest",
    "ToolResult",
    "WorkspaceInfo",
    "WorkspacesMessage",
]
