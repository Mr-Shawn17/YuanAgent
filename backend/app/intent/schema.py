"""意图快判 schema（docs/intent-recognition.md §5，D3/D4/D8）。

5 类意图沿用现有路由标签（config/agent.yaml 的 routing 段：闲聊 / research /
coding / env / 多对象对比，D3 不重定义）；快判输出三字段 {intent, top_k,
needs_context}（D8）：

- intent        主意图（高置信直走时直接路由的目标）；
- top_k         前 2~3 名候选及概率（降序），margin = p1 − p2 是置信度指标（D4，
                不用单点 confidence）；
- needs_context 当前句是否含指代/省略、需回指历史（支撑「多轮」升级信号，D6）。

IntentVerdict 是 predict_intent 的返回包装：解析失败（非 JSON / schema 不过 /
调用异常）时 prediction 为空并携带 error——失败本身就是升级规则集的 P0 确定性
信号（D6 第一层「模糊」场景），由 escalation 层消费，快判层绝不抛异常打断主链路。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, model_validator


class Intent(StrEnum):
    """YuanAgent 的 5 类意图（D3：沿用现有路由标签）。"""

    CHAT = "chat"
    RESEARCH = "research"
    CODING = "coding"
    ENV = "env"
    MULTI_COMPARE = "multi_compare"


INTENT_LABELS: dict[Intent, str] = {
    Intent.CHAT: "闲聊",
    Intent.RESEARCH: "调研",
    Intent.CODING: "编码",
    Intent.ENV: "环境准备",
    Intent.MULTI_COMPARE: "多对象对比",
}


class IntentCandidate(BaseModel):
    """top_k 里的单个候选：意图 + 概率（0~1）。"""

    intent: Intent
    prob: float = Field(ge=0.0, le=1.0, description="候选意图的概率估计（0~1）")


class IntentPrediction(BaseModel):
    """快判输出的三字段 schema（D8：{intent, top_k, needs_context}）。

    一致性由 schema 保证：top_k 按概率降序、无重复意图、第一名与 intent 一致，
    至少 2 项（保证 margin = p1 − p2 恒可计算，D4）。违反一致性的输出按
    「模糊」处理（escalation 的 P0 信号），而不是默认修复。
    """

    intent: Intent
    top_k: list[IntentCandidate] = Field(min_length=2, max_length=3)
    needs_context: bool = False

    @property
    def margin(self) -> float:
        """p1 − p2（D4 置信度指标）；top_k 已保证至少 2 项且降序。"""
        return self.top_k[0].prob - self.top_k[1].prob

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.top_k[0].intent != self.intent:
            msg = (
                f"top_k 第一名（{self.top_k[0].intent}）必须与主意图 intent"
                f"（{self.intent}）一致"
            )
            raise ValueError(msg)
        for index in range(len(self.top_k) - 1):
            left, right = self.top_k[index], self.top_k[index + 1]
            if left.prob < right.prob:
                msg = "top_k 必须按概率降序排列"
                raise ValueError(msg)
            if left.intent == right.intent:
                msg = f"top_k 里出现重复意图：{left.intent}"
                raise ValueError(msg)
        return self


class IntentVerdict(BaseModel):
    """predict_intent 的返回包装：成功时 prediction 非空，失败时 error 说明原因。"""

    prediction: IntentPrediction | None = None
    error: str = ""
    raw_text: str = ""

    @property
    def ok(self) -> bool:
        return self.prediction is not None

    @property
    def margin(self) -> float | None:
        return self.prediction.margin if self.prediction is not None else None


__all__ = [
    "INTENT_LABELS",
    "Intent",
    "IntentCandidate",
    "IntentPrediction",
    "IntentVerdict",
]
