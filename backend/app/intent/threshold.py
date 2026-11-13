"""三段阈值判定（docs/intent-recognition.md §6~§7，D4/D5/D9）。

置信度指标 = margin（p1 − p2，不用单点 confidence），阈值按意图差异化
（写档类 coding/env 更严），三段两阈值：

    margin ≥ high        → DIRECT    高置信直走（仅非高风险只读路径——高风险
                                      已被硬规则升级，本层天然不会对其返回 DIRECT）
    low ≤ margin < high  → ESCALATE  升级 main 裁决（「歧义」带）
    margin < low         → CLARIFY   追问澄清一次，仍低置信转人工

decide 是预判层的完整判定入口（规则集 + margin 三段的组合）：
- should_escalate 命中任一信号（含 margin < high 的歧义信号）→ 不直走；
- 其中「仅 margin 歧义命中」时按 low 细分为 ESCALATE / CLARIFY；
- 其余信号（schema 失败 / 高风险 / 多轮 / 轮次超限 / 跨意图）命中一律 ESCALATE
  ——这些场景问题明确，交 main 裁决而不是反问用户。
"""

from __future__ import annotations

from enum import StrEnum

from app.intent.escalation import (
    REASON_MARGIN_AMBIGUITY,
    EscalationContext,
    should_escalate,
)
from app.intent.schema import IntentVerdict


class DecisionTier(StrEnum):
    """预判层的三段决策（D9 衔接协议）。"""

    DIRECT = "direct"      # 高置信直走：主 Agent 据 intent 直接路由（跳过自主判断）
    ESCALATE = "escalate"  # 升级 main 档裁决（top-k 作为强提示带回）
    CLARIFY = "clarify"    # 追问澄清一次 → 仍低置信转人工


def decide(verdict: IntentVerdict, context: EscalationContext) -> DecisionTier:
    """完整三段判定：规则集命中 → 升级；仅 margin 歧义 → 按 low 细分；否则直走。"""
    decision = should_escalate(verdict, context)
    if not decision.escalate:
        # 无任何信号命中：margin ≥ high 且非高风险意图 → 高置信直走
        return DecisionTier.DIRECT

    # 仅 margin 歧义命中时按三段细分（其余信号命中一律升级，不追问）
    if decision.reasons == [REASON_MARGIN_AMBIGUITY] and verdict.prediction is not None:
        thresholds = context.intent_config.thresholds_for(verdict.prediction.intent)
        margin = verdict.margin
        if margin is not None and margin < thresholds.low:
            return DecisionTier.CLARIFY
    return DecisionTier.ESCALATE


__all__ = ["DecisionTier", "decide"]
