"""升级触发规则集（docs/intent-recognition.md §3~§4，D6）。

两层多信号规则集，OR 命中即升级；信号**全部收集、不短路**（审计/日志友好，
回答「为什么升级」）。LLM 自报置信度校准极差（research 文档 §1.1），v1 不采
（P2 留扩展），置信度一律用 margin（D4）。

第一层确定性规则（零/低成本、低误判）：
- P0 schema 解析失败 / 意图不在枚举（「模糊」场景）
- P0 高风险硬规则：intent ∈ risk_intents（默认 coding/env，对应 highest_tier ≥
  write 的意图级简化）——与置信度无关强制升级
- P1 多轮指代/省略：needs_context（模型自报）+ 代词/指示词词典规则
- P1 轮次超阈值（会话内轮次计数）

第二层模型信号：
- P0 top-k margin 小（「歧义」场景）：margin < 该意图的 high 阈值 → 不可直走。
  仅该信号命中时由 threshold.decide 按 low 细分为 升级（低 ≤ margin < high）或
  追问澄清（margin < low）——见 threshold 模块，规则层不重复处理细分。
- P1 跨意图（单条多意图）：top_k 第二名概率 > cross_intent_top2（默认 0.3），
  单个 delegate 无法覆盖，升级让 main 拆分任务。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.intent.config import IntentConfig
from app.intent.schema import IntentVerdict

# 升级原因常量（固定评估顺序写入 reasons，便于审计断言）
REASON_PARSE_FAILURE = "schema_parse_failure"      # P0 模糊：快判输出不可解析
REASON_HIGH_RISK = "high_risk_intent"              # P0 高风险硬规则（coding/env）
REASON_NEEDS_CONTEXT = "needs_context"             # P1 多轮：模型自报需回指历史
REASON_CONTEXT_REFERENCE = "context_reference"     # P1 多轮：代词/指示词词典命中
REASON_TURN_LIMIT = "turn_limit"                   # P1 多轮：轮次超阈值
REASON_MARGIN_AMBIGUITY = "margin_ambiguity"       # P0 歧义：margin < high 阈值
REASON_CROSS_INTENT = "cross_intent"               # P1 跨意图：top2 概率高

# 指代/省略关键词（保守清单：误报代价仅为一次 main 裁决，宁升级不漏判；
# research 文档 §1.3——中文对话超 70% 含指代/省略）
_CONTEXT_REFERENCE_PATTERNS: tuple[str, ...] = (
    "它", "他", "她", "它们", "他们", "她们",
    "这个", "那个", "这些", "那些", "前者", "后者",
    "刚才", "刚刚", "上面", "前面", "之前", "上次", "上一条",
    "继续", "接着",
)
# 会把代词误吞进去的常见复合词（如「其他」含「他」），匹配前先剔除
_CONTEXT_FALSE_POSITIVE_WORDS: tuple[str, ...] = ("其他", "其它")


def _has_context_reference(text: str) -> bool:
    """代词/指示词词典规则：当前句是否疑似含指代或省略（需要回指历史）。"""
    if not text.strip():
        return False
    cleaned = text
    for word in _CONTEXT_FALSE_POSITIVE_WORDS:
        cleaned = cleaned.replace(word, "")
    return any(pattern in cleaned for pattern in _CONTEXT_REFERENCE_PATTERNS)


class EscalationContext(BaseModel):
    """升级规则集的判定上下文（由接入方在每轮调用前组装）。"""

    # 当前用户消息原文（代词规则用；快判输出 raw_text 是模型回复，不是用户输入）
    user_text: str = ""
    # 会话内轮次计数（含当前轮；超过 config.turn_limit 升级）
    turn_count: int = Field(default=1, ge=0)
    intent_config: IntentConfig = Field(default_factory=IntentConfig)


class EscalationDecision(BaseModel):
    """规则集判定结果：escalate=True 表示命中至少一个升级信号。

    reasons 按固定评估顺序收集全部命中信号（OR、不互斥、不短路）。
    """

    escalate: bool = False
    reasons: list[str] = Field(default_factory=list)


def should_escalate(verdict: IntentVerdict, context: EscalationContext) -> EscalationDecision:
    """评估升级规则集（纯函数：verdict + 上下文 → 是否升级及原因清单）。"""
    reasons: list[str] = []
    prediction = verdict.prediction

    # ---- 第一层：确定性规则 ----
    if not verdict.ok:
        # P0 模糊：快判输出不是合法 JSON / 意图不在 5 类枚举 / schema 校验不过
        reasons.append(REASON_PARSE_FAILURE)
    elif prediction is not None and prediction.intent in context.intent_config.risk_intents:
        # P0 高风险硬规则：coding/env 无论置信度多高都升级（误判代价不对称）
        reasons.append(REASON_HIGH_RISK)
    if prediction is not None and prediction.needs_context:
        # P1 多轮：快判模型自报当前句需要回指历史
        reasons.append(REASON_NEEDS_CONTEXT)
    if _has_context_reference(context.user_text):
        # P1 多轮：代词/指示词词典规则（模型漏报时的兜底）
        reasons.append(REASON_CONTEXT_REFERENCE)
    if context.turn_count >= context.intent_config.turn_limit:
        # P1 多轮：轮次超阈值（零成本兜底）
        reasons.append(REASON_TURN_LIMIT)

    # ---- 第二层：模型信号（仅在快判解析成功时可用）----
    if prediction is not None:
        thresholds = context.intent_config.thresholds_for(prediction.intent)
        margin = verdict.margin
        if margin is not None and margin < thresholds.high:
            # P0 歧义：margin 低于该意图的 high 阈值 → 不可直走（细分见 threshold.decide）
            reasons.append(REASON_MARGIN_AMBIGUITY)
        top2_prob = prediction.top_k[1].prob if len(prediction.top_k) >= 2 else 0.0
        if top2_prob > context.intent_config.cross_intent_top2:
            # P1 跨意图：top2 概率都高 → 单条消息可能含多个意图
            reasons.append(REASON_CROSS_INTENT)

    return EscalationDecision(escalate=bool(reasons), reasons=reasons)


__all__ = [
    "REASON_CONTEXT_REFERENCE",
    "REASON_CROSS_INTENT",
    "REASON_HIGH_RISK",
    "REASON_MARGIN_AMBIGUITY",
    "REASON_NEEDS_CONTEXT",
    "REASON_PARSE_FAILURE",
    "REASON_TURN_LIMIT",
    "EscalationContext",
    "EscalationDecision",
    "should_escalate",
]
