from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from langchain_core.tools import BaseTool
from langgraph.store.base import BaseStore
from pydantic import BaseModel, field_validator

from app.core.config import Settings
from app.core.errors import AppError, PermissionDeniedError
from app.core.events import EventBus
from app.skills.schema import SkillCatalog
from app.tools.permissions import PERMISSION_TIERS, PermissionTier

# 单个工具读取/写入的默认上限，防止超长内容撑爆上下文窗口
MAX_LIST_ENTRIES = 200
DEFAULT_READ_LIMIT = 20_000
MAX_READ_LIMIT = 200_000


class ToolEntryConfig(BaseModel):
    """configs/tools.yaml 中单个工具的配置项。"""

    enabled: bool = True
    tier: str = ""  # 留空时使用内置默认档位
    mode: str = ""  # 预留给 search_knowledge_base 的 stub|mcp 切换
    requires_key: str = ""  # 依赖的 Settings 密钥字段名，缺失时自动禁用
    sandbox_root: str = ""  # 预留：单工具覆盖沙箱根目录

    @field_validator("tier")
    @classmethod
    def _check_tier(cls, value: str) -> str:
        tier = value.strip().lower()
        if tier and tier not in PERMISSION_TIERS:
            msg = f"工具档位非法：{value}，可选 read/write/danger"
            raise ValueError(msg)
        return tier


@dataclass(slots=True)
class ToolContext:
    """工具工厂的运行时上下文，由装配方（CLI/API）构造并传入 registry。"""

    settings: Settings
    user_id: str = ""
    session_id: str = ""
    store: BaseStore | None = None
    sandbox_root: Path | None = None
    # 工具内部主动发布事件（如知识库引用），None 表示不发布
    event_bus: EventBus | None = None
    # 技能目录（skills 功能开启时由装配方注入，skill 工具据此装配/禁用）
    skill_catalog: SkillCatalog | None = None
    # 本地执行器路由（API 场景注入）：文件工具 mode=local 时经它转发到用户电脑。
    # None（CLI 场景）时 local 模式退化为直接读本机沙箱——CLI 进程本就在用户机器上。
    local_router: LocalRouter | None = None
    # 当前会话绑定的工作目录（绝对路径）；local 模式转发时随任务下发，
    # 空串表示未绑定，执行器回退到连接默认目录。
    workspace: str = ""

    def sandbox(self) -> Path:
        """文件类工具的沙箱根目录（绝对路径）。"""
        root = self.sandbox_root or self.settings.workspace_dir
        return root.resolve()


# 工具工厂协议：返回 None 表示依赖缺失、由 registry 记录禁用原因
ToolFactory = Callable[[ToolContext, ToolEntryConfig], BaseTool | None]


@runtime_checkable
class LocalRouter(Protocol):
    """文件工具 local 模式的转发协议（实现方：api.local_hub.LocalTaskHub）。

    返回值是给模型看的文本：成功为工具输出，失败为带前缀的错误说明，
    实现方保证不抛异常（工具错误边界兜底后返回给模型）。
    """

    async def dispatch(
        self,
        *,
        user_id: str,
        tool: str,
        args: dict[str, Any],
        workspace: str = "",
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class BuiltinToolSpec:
    """内置工具的注册元信息：工厂 + 默认档位 + 默认依赖密钥。"""

    factory: ToolFactory
    default_tier: PermissionTier
    default_requires_key: str = ""


def describe_exception(exc: Exception) -> str:
    text = str(exc).strip()
    if not text:
        return exc.__class__.__name__
    return f"{exc.__class__.__name__}: {text}"


def tool_error_boundary[F: Callable[..., Any]](func: F) -> F:
    """把工具内部的异常转成可读的错误文本返回给模型。

    工具失败不应该炸掉整个 Agent 图：模型拿到错误描述后，
    可以选择重试、换参数或直接向用户解释。
    """
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except AppError as exc:
                return f"[{exc.code}] {exc.message}"
            except Exception as exc:  # 边界层必须兜住一切异常
                return f"[工具执行失败] {describe_exception(exc)}"

        return cast("F", async_wrapper)

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except AppError as exc:
            return f"[{exc.code}] {exc.message}"
        except Exception as exc:  # 边界层必须兜住一切异常
            return f"[工具执行失败] {describe_exception(exc)}"

    return cast("F", sync_wrapper)


def resolve_in_sandbox(root: Path, path: str) -> Path:
    """把用户/模型给出的路径解析到沙箱内，拒绝任何形式的越界（../、绝对路径等）。"""
    raw = path.strip()
    if not raw:
        msg = "文件路径不能为空"
        raise PermissionDeniedError(msg)
    resolved_root = root.resolve()
    # Path("/root") / "D:/x" 会直接得到 "D:/x"，所以绝对路径也会被下面的包含检查拦住
    candidate = (resolved_root / raw).resolve()
    if not candidate.is_relative_to(resolved_root):
        msg = f"禁止访问沙箱之外的路径：{path}（沙箱根目录：{resolved_root}）"
        raise PermissionDeniedError(msg)
    return candidate


__all__ = [
    "DEFAULT_READ_LIMIT",
    "MAX_LIST_ENTRIES",
    "MAX_READ_LIMIT",
    "AppError",
    "BuiltinToolSpec",
    "LocalRouter",
    "ToolContext",
    "ToolEntryConfig",
    "ToolFactory",
    "describe_exception",
    "resolve_in_sandbox",
    "tool_error_boundary",
]
