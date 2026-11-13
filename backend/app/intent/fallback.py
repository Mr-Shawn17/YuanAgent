"""追问澄清 / 转人工兜底（docs/intent-recognition.md §7，D7「先追后转」）。

margin < low 阈值时的兜底状态机：先追问澄清**一次**，用户澄清后仍低置信才
转人工（返回人工提示文本，接入现有会话流；不阻断用户换问法重试）。

状态语义（per-session，进程内单例）：
- 无挂起 + 低置信   → ask：生成基于 top-k 候选的追问，登记挂起；
- 挂起中 + 再低置信 → handoff：清除挂起，返回转人工提示文本；
- 挂起中 + 意图明确 → 接入方调 resolve() 自动解除挂起（用户已澄清成功）。

持久化说明：状态存进程内存（本项目单副本部署约束不变），重启丢失挂起态的
降级方向是「再追问一次」（安全方向，不是误转人工）。挂起带 TTL，过期视为
首次低置信重新追问。
"""

from __future__ import annotations

from time import monotonic
from typing import Literal

from pydantic import BaseModel, Field

from app.intent.schema import INTENT_LABELS, Intent, IntentVerdict


def _label_text(intent: Intent) -> str:
    return INTENT_LABELS.get(intent, intent.value)

# 转人工提示文本（人工标记形态：直接返回提示文本接入会话流）
HANDOFF_REPLY = (
    "这个问题我反复没能确定你的意图，为避免答非所问，本轮先转人工处理："
    "你可以换个更明确的说法再试（例如直接说「帮我调研…」「帮我写…」「帮我部署…」），"
    "或把需求拆成一句一句来描述。"
)

# 挂起追问的过期时间（秒）：超过后视为首次低置信重新追问
DEFAULT_CLARIFY_TTL_SECONDS = 600.0

ClarifyAction = Literal["ask", "handoff"]


class ClarifyOutcome(BaseModel):
    """一次兜底动作：ask=向用户追问（短路本轮，不进主 Agent）；handoff=转人工提示。"""

    action: ClarifyAction
    reply: str


class PendingClarify(BaseModel):
    """一条挂起的追问记录（session_id → 记录）。"""

    question: str
    asked_at: float = Field(default_factory=monotonic)


def build_clarify_question(verdict: IntentVerdict) -> str:
    """基于快判 top-k 候选生成追问文本；解析失败时退化为通用追问。"""
    prediction = verdict.prediction
    if prediction is not None and len(prediction.top_k) >= 2:
        first = _label_text(prediction.top_k[0].intent)
        second = _label_text(prediction.top_k[1].intent)
        return (
            f"我有点拿不准你的意图：你是想让我「{first}」还是「{second}」呢？"
            "可以再补充说明一下需求。"
        )
    return "我没太确定你的意图，可以再补充说明一下你的需求吗？"


class ClarifyTracker:
    """追问/转人工状态机（per-session，进程内；单副本部署约束不变）。"""

    def __init__(self, ttl_seconds: float = DEFAULT_CLARIFY_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._pending: dict[str, PendingClarify] = {}

    def clarify(self, session_id: str, verdict: IntentVerdict) -> ClarifyOutcome:
        """低置信兜底入口：首次追问、再次转人工（状态机主逻辑）。"""
        pending = self._pending.get(session_id)
        if pending is not None and not self._expired(pending):
            # 已追问过一次仍低置信 → 转人工（清除挂起，允许用户之后换问法重来）
            self._pending.pop(session_id, None)
            return ClarifyOutcome(action="handoff", reply=HANDOFF_REPLY)
        # 无挂起或挂起已过期 → 追问一次
        question = build_clarify_question(verdict)
        self._pending[session_id] = PendingClarify(question=question)
        return ClarifyOutcome(action="ask", reply=question)

    def resolve(self, session_id: str) -> None:
        """意图已明确（本轮判定 DIRECT/ESCALATE）时解除挂起。"""
        self._pending.pop(session_id, None)

    def has_pending(self, session_id: str) -> bool:
        pending = self._pending.get(session_id)
        return pending is not None and not self._expired(pending)

    def _expired(self, pending: PendingClarify) -> bool:
        return monotonic() - pending.asked_at > self._ttl


# 进程内默认单例（接入方经 get_clarify_tracker() 获取，测试可自建实例注入）
_DEFAULT_TRACKER = ClarifyTracker()


def get_clarify_tracker() -> ClarifyTracker:
    return _DEFAULT_TRACKER


def reset_clarify_tracker() -> None:
    """清空默认追踪器（测试与运维用）。"""
    _DEFAULT_TRACKER._pending.clear()


__all__ = [
    "DEFAULT_CLARIFY_TTL_SECONDS",
    "HANDOFF_REPLY",
    "ClarifyAction",
    "ClarifyOutcome",
    "ClarifyTracker",
    "PendingClarify",
    "build_clarify_question",
    "get_clarify_tracker",
    "reset_clarify_tracker",
]
