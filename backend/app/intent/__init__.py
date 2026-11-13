"""意图识别前置预判层（docs/intent-recognition.md，D1~D9）。

加在现有「主 Agent 大模型自主路由」之前：fast 档先快判意图 → 多信号规则集
（确定性规则 + margin 等模型信号）决定 高置信直走 / 低置信回落（top-k 作强
提示带上）/ 追问澄清 → 仍低置信转人工。每轮入口调用 run_intent_gate。

模块分层：
- schema      意图枚举与快判三字段 schema（D3/D4/D8）
- classifier  fast 档调用与解析（Step 2）
- config      config/agent.yaml 的 intent 段（Step 6）
- escalation  升级触发规则集（Step 3，D6 两层多信号）
- threshold   margin 三段阈值判定（Step 4，D5）
- fallback    追问澄清/转人工兜底（Step 5，D7）
- gate        每轮门控：快判 → 三段判定 → 短路/注入产出（Step 7，D9）
"""

from app.intent.classifier import (
    CLASSIFY_HISTORY_LIMIT,
    CLASSIFY_SYSTEM_PROMPT,
    parse_intent_output,
    predict_intent,
)
from app.intent.config import (
    INTENT_CONFIG_NAME,
    INTENT_KEY,
    IntentConfig,
    IntentThresholds,
    load_intent_config,
)
from app.intent.escalation import (
    EscalationContext,
    EscalationDecision,
    should_escalate,
)
from app.intent.fallback import (
    HANDOFF_REPLY,
    ClarifyOutcome,
    ClarifyTracker,
    build_clarify_question,
    get_clarify_tracker,
    reset_clarify_tracker,
)
from app.intent.gate import IntentGateResult, run_intent_gate
from app.intent.schema import (
    INTENT_LABELS,
    Intent,
    IntentCandidate,
    IntentPrediction,
    IntentVerdict,
)
from app.intent.threshold import DecisionTier, decide

__all__ = [
    "CLASSIFY_HISTORY_LIMIT",
    "CLASSIFY_SYSTEM_PROMPT",
    "HANDOFF_REPLY",
    "INTENT_CONFIG_NAME",
    "INTENT_KEY",
    "INTENT_LABELS",
    "ClarifyOutcome",
    "ClarifyTracker",
    "DecisionTier",
    "EscalationContext",
    "EscalationDecision",
    "Intent",
    "IntentCandidate",
    "IntentConfig",
    "IntentGateResult",
    "IntentPrediction",
    "IntentThresholds",
    "IntentVerdict",
    "build_clarify_question",
    "decide",
    "get_clarify_tracker",
    "load_intent_config",
    "parse_intent_output",
    "predict_intent",
    "reset_clarify_tracker",
    "run_intent_gate",
    "should_escalate",
]
