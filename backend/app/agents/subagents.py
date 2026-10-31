"""config/agent.yaml 的 routing 段 schema 与子 Agent 装配（P6' 多智能体分流）。

主 Agent 只做路由与汇总：检索调研类任务委派给 research 子 Agent，沙箱文件
编写类任务委派给 coding 子 Agent。每个子 Agent 对应一个 delegate_to_<name>
工具，档位取该子 Agent 允许工具集的最高档位（danger > write > read）：
纯只读 → read 免审批；含写操作 → write，用户批准整次委派即视为授权子 Agent
内部的全部操作（整体授权），但每一步仍写审计并发布工具事件。
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Annotated, Any, Literal, Self, cast

from langchain.agents import create_agent
from langchain.tools import InjectedState
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field, field_validator, model_validator

from app.agents.state import AgentState
from app.core.config import load_yaml_raw
from app.core.events import (
    DelegateEndEvent,
    DelegateStartEvent,
    EventBus,
    FanoutEndEvent,
    FanoutStartEvent,
    FanoutTaskEndEvent,
    FanoutTaskStartEvent,
    reset_current_task_id,
    set_current_task_id,
)
from app.core.logging import get_logger
from app.middleware.guardrail import GuardrailMiddleware
from app.middleware.tool_permission import ToolAuditWriter, ToolPermissionMiddleware
from app.tools.base import tool_error_boundary
from app.tools.permissions import (
    TIER_DANGER,
    TIER_READ,
    TIER_WRITE,
    PermissionTier,
    approve_everything,
)
from app.tools.registry import ToolRegistry

logger = get_logger(__name__)

AGENTS_CONFIG_NAME = "agent"
ROUTING_KEY = "routing"
DELEGATE_PREFIX = "delegate_to_"
DEFAULT_TIMEOUT_SECONDS = 300.0

# 子 Agent 名会拼进工具名 delegate_to_<name>，只允许小写标识符字符
_SUB_AGENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

SubModelTier = Literal["main", "fast"]

# 模型工厂：档位（main/fast）→ BaseChatModel，由装配方注入，便于测试替换
ModelFactory = Callable[[str], BaseChatModel]


class SubAgentConfig(BaseModel):
    """单个子 Agent 的定义。"""

    description: str
    system_prompt: str
    model_tier: SubModelTier = "main"
    allowed_tools: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0, le=1800)

    @field_validator("description", "system_prompt")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            msg = "子 Agent 的 description / system_prompt 不能为空"
            raise ValueError(msg)
        return text

    @field_validator("allowed_tools")
    @classmethod
    def _reject_delegate_patterns(cls, value: list[str]) -> list[str]:
        for pattern in value:
            if pattern.strip().startswith(DELEGATE_PREFIX):
                msg = f"子 Agent 不允许使用委派工具（防止递归委派）：{pattern}"
                raise ValueError(msg)
        return value


class MainAgentRoutingConfig(BaseModel):
    """路由模式下主 Agent 保留的工具白名单（支持 fnmatch 通配）。"""

    keep_tools: list[str] = Field(default_factory=lambda: ["get_current_time", "remember"])


class RoutingConfig(BaseModel):
    """config/agent.yaml 的 routing 段顶层结构。"""

    enabled: bool = False
    main_agent: MainAgentRoutingConfig = Field(default_factory=MainAgentRoutingConfig)
    sub_agents: dict[str, SubAgentConfig] = Field(default_factory=dict)
    # fan_out 并行委派工具的开关与上限（None 表示未配置，按默认开关处理）
    fan_out: FanOutConfig | None = None

    @model_validator(mode="after")
    def _check_sub_agent_names(self) -> Self:
        for name in self.sub_agents:
            if not _SUB_AGENT_NAME_PATTERN.match(name):
                msg = f"子 Agent 名非法：{name}（只允许小写字母、数字、下划线，且以字母开头）"
                raise ValueError(msg)
        return self


class FanOutConfig(BaseModel):
    """fan_out 并行委派工具配置（config/agent.yaml 的 routing.fan_out 段）。

    - enabled      总开关；false 时主 Agent 不获得 fan_out 工具（单任务委派不受影响）
    - max_parallel 单次 fan_out 允许的子任务数上限，超出整批拒绝（提示模型拆批）
    - read_only    true 时仅允许纯只读（read 档）子 Agent 进批，混入写档直接拒绝；
                   这样整批免审批；false 时批内最高档位触发一次整体审批
    """

    enabled: bool = True
    max_parallel: int = Field(default=4, ge=2, le=10)
    read_only: bool = True


def load_routing_config(name: str = AGENTS_CONFIG_NAME) -> RoutingConfig:
    """读取路由配置：从主配置文件（agent.yaml）的 routing 段提取。

    文件不存在或缺少 routing 段时返回默认（enabled=False，保持 P5' 行为）。
    """
    raw = load_yaml_raw(name)
    routing_raw = raw.get(ROUTING_KEY, {})
    if not isinstance(routing_raw, dict):
        return RoutingConfig()
    return RoutingConfig.model_validate(routing_raw)


def match_tool_names(patterns: Sequence[str], names: Sequence[str]) -> list[str]:
    """按 fnmatch 模式筛选工具名，保持 names 原始顺序并去重。"""
    cleaned = [pattern.strip() for pattern in patterns if pattern.strip()]
    matched: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in cleaned):
            matched.append(name)
            seen.add(name)
    return matched


def highest_tier(tiers: Iterable[PermissionTier]) -> PermissionTier:
    """一组档位里的最高档（danger > write > read），决定 delegate 工具的档位。"""
    values = set(tiers)
    if TIER_DANGER in values:
        return TIER_DANGER
    if TIER_WRITE in values:
        return TIER_WRITE
    return TIER_READ


def _tier_rank(tier: PermissionTier) -> int:
    """档位排序权重：danger(2) > write(1) > read(0)，用于 max() 取最高档。"""
    if tier == TIER_DANGER:
        return 2
    if tier == TIER_WRITE:
        return 1
    return 0


@dataclass(slots=True)
class RoutingResult:
    """路由装配结果：主 Agent 工具集 + 委派工具档位 + 各子 Agent 认领的工具。"""

    main_tools: list[BaseTool]
    delegate_tiers: dict[str, PermissionTier]
    sub_agent_tools: dict[str, list[str]]
    dropped_tools: list[str]

    @property
    def names(self) -> list[str]:
        return [item.name for item in self.main_tools]

    @property
    def sub_agent_names(self) -> list[str]:
        return list(self.sub_agent_tools)


def _state_value(state: Any, key: str) -> str:
    """从注入的图 state（dict 或对象）里取字符串字段，缺省为空串。"""
    value = state.get(key, "") if isinstance(state, dict) else getattr(state, key, "")
    return str(value) if value else ""


def _final_text(result: Any) -> str:
    """从子 Agent 的执行结果里取最后一条带文本的 AI 消息。"""
    if isinstance(result, dict):
        messages = list(result.get("messages") or [])
    else:
        messages = list(getattr(result, "messages", None) or [])
    for message in reversed(messages):
        if isinstance(message, AIMessage) and str(message.text).strip():
            return str(message.text).strip()
    return "（子 Agent 未返回文本结果）"


def build_sub_agent(
    name: str,
    config: SubAgentConfig,
    *,
    tools: Sequence[BaseTool],
    tiers: dict[str, PermissionTier],
    model: BaseChatModel,
    event_bus: EventBus,
    audit_writer: ToolAuditWriter | None,
) -> Any:
    """构建一个子 Agent 图：独立 system prompt + 权限（整体授权）+ guardrail。

    子 Agent 不挂 checkpointer（一次性执行，结果经 ToolMessage 回到主图）、
    不挂记忆/人设/摘要中间件；工具事件与审计复用主 Agent 的总线与写入器，
    origin 标记为 subagent:<name> 便于在轨迹与审计里区分来源。
    """
    permission_mw = ToolPermissionMiddleware(
        tiers=tiers,
        # 整体授权：delegate 工具已按最高档位在外层审批过，内部不再逐条打断用户
        approval=approve_everything,
        audit_writer=audit_writer,
        event_bus=event_bus,
        origin=f"subagent:{name}",
    )
    create = cast(Any, create_agent)
    return cast(
        Any,
        create(
            model=model,
            tools=list(tools),
            system_prompt=config.system_prompt,
            state_schema=AgentState,
            middleware=[permission_mw, GuardrailMiddleware()],
        ),
    )


def make_delegate_tool(
    name: str,
    config: SubAgentConfig,
    graph: Any,
    event_bus: EventBus | None = None,
) -> BaseTool:
    """生成 delegate_to_<name> 工具：把自包含任务委派给子 Agent 并取回最终答复。

    session_id / user_id 经 InjectedState 从主图 state 动态读取（CLI 切换会话后
    不重建 agent，闭包值会过期），子 Agent 的审计与事件据此关联到当前会话。

    委派前后发布 DelegateStartEvent / DelegateEndEvent，供前端画成
    「主 Agent → 子 Agent」节点（与 fan_out 并行图并列）。
    """
    tool_name = f"{DELEGATE_PREFIX}{name}"
    description = (
        f"把任务委派给「{name}」子 Agent：{config.description}。"
        "task 必须是自包含的完整任务描述（写清目标与必要背景），"
        "子 Agent 的最终答复作为本工具结果返回。"
    )

    @tool(tool_name, description=description)
    @tool_error_boundary
    async def _delegate(
        task: str,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> str:
        delegate_id = f"del_{int(perf_counter() * 1000)}"
        started = perf_counter()
        if event_bus is not None:
            await event_bus.publish(
                DelegateStartEvent(delegate_id=delegate_id, agent=name, task=task)
            )
        payload: dict[str, Any] = {
            "messages": [HumanMessage(content=task)],
            "session_id": _state_value(state, "session_id"),
            "user_id": _state_value(state, "user_id"),
        }
        try:
            result = await asyncio.wait_for(graph.ainvoke(payload), timeout=config.timeout_seconds)
            if event_bus is not None:
                await event_bus.publish(
                    DelegateEndEvent(
                        delegate_id=delegate_id,
                        agent=name,
                        ok=True,
                        latency_ms=int((perf_counter() - started) * 1000),
                    )
                )
            return _final_text(result)
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"[:200]
            if event_bus is not None:
                await event_bus.publish(
                    DelegateEndEvent(
                        delegate_id=delegate_id,
                        agent=name,
                        ok=False,
                        latency_ms=int((perf_counter() - started) * 1000),
                        error=error_text,
                    )
                )
            raise

    return _delegate


def _run_one_sub_task(
    *,
    graph: Any,
    agent_name: str,
    task: str,
    fanout_id: str,
    task_id: str,
    timeout_seconds: float,
    state: dict[str, Any],
    event_bus: EventBus,
) -> asyncio.Task[str]:
    """单个子任务的协程封装：设 task_id 标记 → 发开始事件 → 执行 → 发结束事件。

    超时/异常不抛出（返回错误文本），保证 gather 整批完成、部分失败可汇报。
    """

    async def _run() -> str:
        token = set_current_task_id(task_id)
        started = perf_counter()
        await event_bus.publish(
            FanoutTaskStartEvent(fanout_id=fanout_id, task_id=task_id, agent=agent_name)
        )
        try:
            payload: dict[str, Any] = {
                "messages": [HumanMessage(content=task)],
                "session_id": _state_value(state, "session_id"),
                "user_id": _state_value(state, "user_id"),
            }
            result = await asyncio.wait_for(graph.ainvoke(payload), timeout=timeout_seconds)
            latency_ms = int((perf_counter() - started) * 1000)
            await event_bus.publish(
                FanoutTaskEndEvent(
                    fanout_id=fanout_id, task_id=task_id, agent=agent_name, ok=True,
                    latency_ms=latency_ms,
                )
            )
            return _final_text(result)
        except Exception as exc:
            latency_ms = int((perf_counter() - started) * 1000)
            error_text = f"{type(exc).__name__}: {exc}"[:200]
            await event_bus.publish(
                FanoutTaskEndEvent(
                    fanout_id=fanout_id, task_id=task_id, agent=agent_name, ok=False,
                    latency_ms=latency_ms, error=error_text,
                )
            )
            return f"（子任务失败：{error_text}）"
        finally:
            reset_current_task_id(token)

    return asyncio.create_task(_run())


def make_fanout_tool(
    *,
    config: FanOutConfig,
    sub_graphs: dict[str, Any],
    sub_configs: dict[str, SubAgentConfig],
    sub_tiers: dict[str, PermissionTier],
    event_bus: EventBus,
) -> BaseTool:
    """生成 fan_out 工具：一次调用并行委派多个子任务，结构化返回各任务结果。

    参数校验（不通过整批拒绝，返回错误文本让模型看到原因并拆批/改单）：
    - agent 名必须是已装配的子 Agent；
    - read_only=true 时目标子 Agent 必须为 read 档（保证整批免审批且无写风险）；
    - 子任务数 ≤ max_parallel。
    """

    description = (
        "并行委派多个子任务给子 Agent（fan-out），适合可拆分的多对象查询/调研，"
        "例如『对比 A、B 两家公司 2024 年营收』应拆成两个子任务并行执行。"
        "calls 是子任务数组，每项 {agent: 子Agent名, task: 自包含的任务描述}；"
        "各子任务并行执行、互不依赖（有依赖的任务请改用单个 delegate_to_<name> 工具）。"
    )

    @tool("fan_out", description=description)
    @tool_error_boundary
    async def _fan_out(
        calls: list[dict[str, Any]],
        state: Annotated[dict[str, Any], InjectedState],
    ) -> str:
        if not isinstance(calls, list) or not calls:
            return "[fan_out 参数错误] calls 必须是非空数组，每项 {agent, task}。"
        if len(calls) > config.max_parallel:
            return (
                f"[fan_out 拒绝] 子任务数 {len(calls)} 超过单批上限 {config.max_parallel}，"
                f"请拆成多批（每批 ≤ {config.max_parallel}）或改用单任务委派。"
            )
        normalized: list[tuple[str, str]] = []
        for idx, call in enumerate(calls, start=1):
            agent = str((call or {}).get("agent", "")).strip()
            task = str((call or {}).get("task", "")).strip()
            if agent not in sub_graphs:
                return (
                    f"[fan_out 参数错误] 第 {idx} 项的 agent「{agent}」不存在，"
                    f"可用子 Agent：{sorted(sub_graphs)}。"
                )
            if not task:
                return f"[fan_out 参数错误] 第 {idx} 项缺少 task（自包含任务描述）。"
            normalized.append((agent, task))
        if config.read_only:
            offenders = sorted(
                {agent for agent, _ in normalized if sub_tiers.get(agent) != TIER_READ}
            )
            if offenders:
                return (
                    f"[fan_out 拒绝] 当前仅允许只读子 Agent 进批，"
                    f"以下子 Agent 含写操作：{offenders}；请改用对应 delegate_to_<name>。"
                )

        fanout_id = f"fan_{int(perf_counter() * 1000)}"
        task_specs: list[dict[str, Any]] = [
            {"task_id": f"{fanout_id}_t{i}", "agent": agent, "task": task}
            for i, (agent, task) in enumerate(normalized, start=1)
        ]
        batch_started = perf_counter()
        await event_bus.publish(FanoutStartEvent(fanout_id=fanout_id, tasks=task_specs))
        results = await asyncio.gather(
            *(
                _run_one_sub_task(
                    graph=sub_graphs[spec["agent"]],
                    agent_name=str(spec["agent"]),
                    task=str(spec["task"]),
                    fanout_id=fanout_id,
                    task_id=str(spec["task_id"]),
                    timeout_seconds=sub_configs[str(spec["agent"])].timeout_seconds,
                    state=state,
                    event_bus=event_bus,
                )
                for spec in task_specs
            )
        )
        # 失败任务在 _run 内已转为「（子任务失败：…）」文本，按前缀统计真实失败数
        fail_count = sum(1 for r in results if r.startswith("（子任务失败"))
        ok_count = len(results) - fail_count
        total_ms = int((perf_counter() - batch_started) * 1000)
        await event_bus.publish(
            FanoutEndEvent(
                fanout_id=fanout_id, ok_count=ok_count, fail_count=fail_count, total_ms=total_ms
            )
        )
        sections = [
            f"【任务{i}·{spec['agent']}】{spec['task']}\n{result}"
            for i, (spec, result) in enumerate(zip(task_specs, results, strict=True), start=1)
        ]
        return "\n\n".join(sections)

    return _fan_out


def apply_routing(
    *,
    registry: ToolRegistry,
    config: RoutingConfig,
    model_for: ModelFactory,
    event_bus: EventBus,
    audit_writer: ToolAuditWriter | None,
) -> RoutingResult | None:
    """按 agents.yaml 装配子 Agent 并产出主 Agent 的收缩后工具集。

    返回 None 表示路由未启用或没有可用子 Agent，调用方保持原有全工具行为。
    """
    if not config.enabled or not config.sub_agents:
        return None

    all_names = registry.names
    delegated: dict[str, list[str]] = {}
    delegate_tiers: dict[str, PermissionTier] = {}
    delegate_tools: list[BaseTool] = []
    sub_graphs: dict[str, Any] = {}
    sub_tiers: dict[str, PermissionTier] = {}

    for name, sub_config in config.sub_agents.items():
        matched = match_tool_names(sub_config.allowed_tools, all_names)
        if not matched:
            logger.warning(
                "sub_agent_has_no_tools",
                sub_agent=name,
                allowed=sub_config.allowed_tools,
            )
            continue
        matched_set = set(matched)
        tiers = {tool_name: registry.tier_of(tool_name) for tool_name in matched}
        graph = build_sub_agent(
            name,
            sub_config,
            tools=[item for item in registry.tools if item.name in matched_set],
            tiers=tiers,
            model=model_for(sub_config.model_tier),
            event_bus=event_bus,
            audit_writer=audit_writer,
        )
        delegated[name] = matched
        sub_graphs[name] = graph
        sub_tiers[name] = highest_tier(tiers.values())
        delegate_tools.append(make_delegate_tool(name, sub_config, graph, event_bus=event_bus))
        delegate_tiers[f"{DELEGATE_PREFIX}{name}"] = highest_tier(tiers.values())

    if not delegated:
        logger.warning("routing_enabled_but_no_sub_agent", sub_agents=list(config.sub_agents))
        return None

    # fan_out 并行委派：按配置开关装配（默认开）；纯只读模式下档位为 read（免审批）
    fan_out_cfg = config.fan_out or FanOutConfig()
    if fan_out_cfg.enabled and sub_graphs:
        delegate_tools.append(
            make_fanout_tool(
                config=fan_out_cfg,
                sub_graphs=sub_graphs,
                sub_configs=dict(config.sub_agents),
                sub_tiers=sub_tiers,
                event_bus=event_bus,
            )
        )
        delegate_tiers["fan_out"] = (
            TIER_READ if fan_out_cfg.read_only else max(sub_tiers.values(), key=_tier_rank)
        )

    keep = set(match_tool_names(config.main_agent.keep_tools, all_names))
    main_tools = [item for item in registry.tools if item.name in keep] + delegate_tools
    claimed = {tool_name for names in delegated.values() for tool_name in names}
    dropped = [
        tool_name for tool_name in all_names if tool_name not in keep and tool_name not in claimed
    ]
    return RoutingResult(
        main_tools=main_tools,
        delegate_tiers=delegate_tiers,
        sub_agent_tools=delegated,
        dropped_tools=dropped,
    )


def routing_note(result: RoutingResult, config: RoutingConfig) -> str:
    """渲染进主 Agent system prompt 的路由说明（经 build_agent 的 notes 参数注入）。"""
    lines = ["可用子 Agent（遇到匹配任务时委派，自己负责路由与汇总）："]
    for name in result.sub_agent_names:
        sub_config = config.sub_agents[name]
        tier = result.delegate_tiers[f"{DELEGATE_PREFIX}{name}"]
        confirm = (
            "写档操作：直接发起委派，系统会自动弹出审批窗让用户确认，你不必先口头追问"
            if tier in {TIER_WRITE, TIER_DANGER}
            else "只读操作，可直接执行"
        )
        lines.append(f"- {DELEGATE_PREFIX}{name}：{sub_config.description}（{confirm}）")
    fan_out_cfg = config.fan_out or FanOutConfig()
    if fan_out_cfg.enabled and "fan_out" in result.delegate_tiers:
        scope = (
            "仅限只读子 Agent" if fan_out_cfg.read_only else "任意子 Agent（含写档，需整体审批）"
        )
        lines.append(
            "- fan_out：并行委派多个子任务（每批 ≤ "
            f"{fan_out_cfg.max_parallel} 个，{scope}）。"
            "用户问题涉及多个对象的对比/统计（如『A 与 B 两家公司 2024 年营收对比』）时，"
            "优先把每个对象的查询拆成独立子任务一次性并行执行，再基于结果汇总对比；"
            "单对象任务或任务间有依赖时改用对应的 delegate_to_<name>。"
        )
    lines.append("简单闲聊与日常问答直接自己回答，不必委派。")
    return "\n".join(lines)


__all__ = [
    "AGENTS_CONFIG_NAME",
    "DEFAULT_TIMEOUT_SECONDS",
    "DELEGATE_PREFIX",
    "FanOutConfig",
    "MainAgentRoutingConfig",
    "ModelFactory",
    "RoutingConfig",
    "RoutingResult",
    "SubAgentConfig",
    "apply_routing",
    "build_sub_agent",
    "highest_tier",
    "load_routing_config",
    "make_delegate_tool",
    "make_fanout_tool",
    "match_tool_names",
    "routing_note",
]
