"""预判层每轮门控（docs/intent-recognition.md §7 衔接协议，实施 Step 7）。

run_intent_gate = 快判（predict_intent）→ 三段判定（decide）→ 兜底/注入产出：
- DIRECT   → note：主 Agent 据 intent 直接路由的指令（跳过自主判断，保留审计）；
- ESCALATE → note：把 top-k 候选与触发信号作为强提示带回（D9：回落不丢弃 fast 结果）；
- CLARIFY  → short_reply：追问/转人工文本，短路本轮（不进主 Agent，零 main 成本）。

调用方按三消费分支处理（cli.py / routes_chat.py）：
1. skipped=True（intent.enabled=false）→ 行为与现状完全一致；
2. short_reply 非空 → 直接把文本作为助手回复返回，不调主 Agent；
3. 否则 → 把 note 写进本轮 state.intent_note（经 IntentNoteMiddleware 注入
   system prompt），照常调主 Agent——工具调用仍走现有审计。

任何快判异常都在 predict_intent 内降级为失败 verdict → 规则集判「模糊」→
升级，等价于未启用预判层（现有自主路由），主链路不会因预判层挂掉。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.intent.config import IntentConfig
from app.intent.escalation import EscalationContext, should_escalate
from app.intent.fallback import ClarifyTracker, get_clarify_tracker
from app.intent.schema import INTENT_LABELS, Intent, IntentVerdict
from app.intent.threshold import DecisionTier, decide

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import BaseMessage

logger = get_logger(__name__)

# 门控读取的会话历史上限条数（predict_intent 内部还会再截到 CLASSIFY_HISTORY_LIMIT）
GATE_HISTORY_LIMIT = 8


class IntentGateResult(BaseModel):
    """一轮的预判门控结果（调用方按 skipped / short_reply / note 三分支消费）。"""

    skipped: bool = False
    verdict: IntentVerdict | None = None
    tier: DecisionTier | None = None
    reasons: list[str] = Field(default_factory=list)
    # DIRECT/ESCALATE 时注入 system prompt 的路由指令/强提示（每轮覆盖 state.intent_note）
    note: str = ""
    # 非空表示本轮短路：直接把该文本作为助手回复返回（追问/转人工），不调主 Agent
    short_reply: str = ""


def _direct_note(verdict: IntentVerdict) -> str:
    """直走指令：让主 Agent 跳过自主路由，按快判意图直接行动（保留兜底退路）。"""
    prediction = verdict.prediction
    if prediction is None:
        return ""
    margin = verdict.margin or 0.0
    intent = prediction.intent
    label = INTENT_LABELS.get(intent, intent.value)
    if intent is Intent.CHAT:
        return (
            f"【意图快判·直走】本轮消息已由快判层高置信判定为闲聊（{label}，"
            f"margin={margin:.2f}）：直接以阿元的口吻回答用户，无需委派子 Agent。"
        )
    if intent is Intent.MULTI_COMPARE:
        return (
            f"【意图快判·直走】本轮消息已由快判层高置信判定为多对象对比（margin={margin:.2f}）："
            "优先用 fan_out 并行委派各对象的子任务再汇总对比结论；"
            "没有 fan_out 工具时自行对比回答。"
        )
    return (
        f"【意图快判·直走】本轮消息已由快判层高置信判定为「{label}」"
        f"（intent={intent.value}，margin={margin:.2f}）：直接调用 "
        f"delegate_to_{intent.value} 处理本轮用户消息，跳过自主路由判断；"
        "仅当该工具不存在、或你明确判定快判结果与消息内容不符时，才退回自主路由。"
    )


def _escalate_note(verdict: IntentVerdict, reasons: list[str]) -> str:
    """升级提示：把 top-k 候选与触发信号作为强提示带回 main（D9：不丢弃）。"""
    prediction = verdict.prediction
    if prediction is not None:
        candidates = "、".join(
            f"{item.intent.value} {item.prob:.2f}" for item in prediction.top_k
        )
    else:
        candidates = "快判输出不可解析"
    signals = "、".join(reasons) if reasons else "低置信"
    return (
        f"【意图快判·升级裁决】快判层对本轮消息拿不准（触发：{signals}；候选：{candidates}）。"
        "请你按路由说明自行判断意图并选择委派方式；上述候选只是参考信号，不可盲从。"
    )


async def run_intent_gate(
    *,
    text: str,
    history: Sequence[BaseMessage] | None = None,
    session_id: str = "",
    turn_count: int = 1,
    intent_config: IntentConfig | None = None,
    fast_model: BaseChatModel | None = None,
    tracker: ClarifyTracker | None = None,
) -> IntentGateResult:
    """每轮预判门控：快判 → 三段判定 → 兜底短路或路由注入产出。

    intent_config 未启用时返回 skipped=True；fast_model 为 None 时由
    predict_intent 按 config.fast_model_tier 解析（测试注入 model 参数）。
    """
    config = intent_config or IntentConfig()
    if not config.enabled:
        return IntentGateResult(skipped=True)

    from app.intent.classifier import predict_intent  # 延迟导入：快判层失败不阻断门控装配

    verdict = await predict_intent(
        text,
        history,
        model=fast_model,
        tier=config.fast_model_tier,
    )
    resolved_tracker = tracker if tracker is not None else get_clarify_tracker()
    context = EscalationContext(
        user_text=text,
        turn_count=turn_count,
        intent_config=config,
    )
    decision = should_escalate(verdict, context)
    tier = decide(verdict, context)
    logger.info(
        "intent_gate",
        session_id=session_id,
        tier=tier.value,
        margin=verdict.margin,
        reasons=decision.reasons,
    )
    if tier is DecisionTier.CLARIFY:
        # 追问澄清一次 → 仍低置信转人工（状态机见 fallback 模块）
        outcome = resolved_tracker.clarify(session_id, verdict)
        return IntentGateResult(
            verdict=verdict,
            tier=tier,
            reasons=decision.reasons,
            short_reply=outcome.reply,
        )
    # 意图已明确（DIRECT/ESCALATE）：解除挂起的追问
    resolved_tracker.resolve(session_id)
    note = (
        _direct_note(verdict)
        if tier is DecisionTier.DIRECT
        else _escalate_note(verdict, decision.reasons)
    )
    return IntentGateResult(verdict=verdict, tier=tier, reasons=decision.reasons, note=note)


__all__ = ["GATE_HISTORY_LIMIT", "IntentGateResult", "run_intent_gate"]
