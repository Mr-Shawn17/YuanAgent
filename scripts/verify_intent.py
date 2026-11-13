"""意图预判层端到端验收脚本（实施 Step 8；离线自检 + 可选 --online 真实模型）。

验收口径（docs/intent-recognition.md §7 衔接协议 + 实施方案 Step 8）：
1. 离线自检（默认，不联网不连库，替身快判模型回放）：
   a. 预判层未启用（intent.enabled=false）→ gate 直通 skipped，行为与现状一致；
   b. 高置信 research（margin 0.85）→ DIRECT，note 含 delegate_to_research 直走指令；
   c. 高风险 coding（margin 0.85）→ 恒 ESCALATE（硬规则与置信度无关，写档类不直走）；
   d. 低置信 margin≈0.03 → CLARIFY 追问一次；再次低置信 → 转人工提示文本；
   e. 快判输出乱码（解析失败）→ ESCALATE，强提示带回「不可解析」（D9 不丢弃）。
2. --online：真实 fast 档跑样例分类，验证三字段 schema、margin 三段与硬规则。

跑法：
    .venv312\\Scripts\\python.exe scripts/verify_intent.py           # 离线自检
    .venv312\\Scripts\\python.exe scripts/verify_intent.py --online  # 需 GLM_API_KEY
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from pydantic import Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.intent.config import IntentConfig, IntentThresholds
from app.intent.fallback import ClarifyTracker
from app.intent.gate import run_intent_gate
from app.intent.schema import IntentVerdict
from app.intent.threshold import DecisionTier


class ScriptedModel(BaseChatModel):
    """按次序回放预设回复的替身快判模型（离线自检用）。"""

    replies: list[str] = Field(default_factory=list)
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-intent-model"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: object,
    ) -> ChatResult:
        index = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        message = AIMessage(content=self.replies[index])
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: object,
    ) -> ChatResult:
        return self._generate(messages)


def _fast_json(intent: str, p1: float, p2: float, second: str = "coding") -> str:
    return (
        f'{{"intent": "{intent}", "top_k": [{{"intent": "{intent}", "prob": {p1}}}, '
        f'{{"intent": "{second}", "prob": {p2}}}], "needs_context": false}}'
    )


def _enabled() -> IntentConfig:
    return IntentConfig(
        enabled=True,
        thresholds={"default": IntentThresholds(high=0.15, low=0.05)},
    )


async def _offline_checks() -> list[tuple[str, bool, str]]:
    """离线自检：返回 (名称, 是否通过, 说明) 列表。"""
    results: list[tuple[str, bool, str]] = []

    # a. 未启用 → 直通
    gate = await run_intent_gate(
        text="你好", intent_config=IntentConfig(enabled=False), fast_model=ScriptedModel()
    )
    ok_a = gate.skipped and gate.note == "" and gate.tier is None
    results.append(("未启用直通 skipped", ok_a, ""))

    # b. 高置信 research → DIRECT 直走指令
    gate = await run_intent_gate(
        text="帮我调研一下混合检索",
        intent_config=_enabled(),
        fast_model=ScriptedModel(replies=[_fast_json("research", 0.9, 0.05)]),
    )
    ok = gate.tier is DecisionTier.DIRECT and "delegate_to_research" in gate.note
    results.append(("高置信 research 直走（note 含委派指令）", ok, gate.note))

    # c. 高风险 coding → 恒升级（硬规则）
    gate = await run_intent_gate(
        text="帮我在沙箱里写一个 hello.py",
        intent_config=_enabled(),
        fast_model=ScriptedModel(replies=[_fast_json("coding", 0.9, 0.05, second="research")]),
    )
    ok = gate.tier is DecisionTier.ESCALATE and "high_risk_intent" in gate.note
    results.append(("高风险 coding 恒升级（硬规则）", ok, gate.note))

    # d. 低置信 → 追问一次 → 转人工
    tracker = ClarifyTracker()
    gate = await run_intent_gate(
        text="帮我看看",
        intent_config=_enabled(),
        fast_model=ScriptedModel(replies=[_fast_json("research", 0.3, 0.27)]),
        tracker=tracker,
    )
    asked = gate.tier is DecisionTier.CLARIFY and gate.short_reply != ""
    gate = await run_intent_gate(
        text="帮我看看",
        intent_config=_enabled(),
        fast_model=ScriptedModel(replies=[_fast_json("research", 0.3, 0.27)]),
        tracker=tracker,
    )
    handed = gate.short_reply.startswith("这个问题我反复没能确定")
    results.append(("低置信追问一次 → 转人工", asked and handed, gate.short_reply))

    # e. 解析失败 → 升级 + 强提示带回
    gate = await run_intent_gate(
        text="随便说点什么",
        intent_config=_enabled(),
        fast_model=ScriptedModel(replies=["这完全不是 JSON 输出 $%^"]),
    )
    ok = gate.tier is DecisionTier.ESCALATE and "不可解析" in gate.note
    results.append(("快判输出乱码 → 升级 + 强提示", ok, gate.note))
    return results


async def _online_checks() -> list[tuple[str, bool, str]]:
    """真实 fast 档样例分类：验证三字段 schema 与硬规则（coding 恒升级）。"""
    from app.core.config import get_settings
    from app.intent.classifier import predict_intent
    from app.llm.factory import get_chat_model
    from app.llm.factory import ping_model as _ping  # noqa: F401 - 连通性提示用

    settings = get_settings()
    fast_model = get_chat_model("fast", settings=settings, streaming=False)
    config = _enabled()
    samples = (
        ("你好呀，今天天气不错", DecisionTier.DIRECT),
        ("帮我调研一下混合检索的定义", DecisionTier.DIRECT),
        ("帮我在沙箱里写一个 hello.py", DecisionTier.ESCALATE),  # 高风险硬规则
        ("对比一下 A 公司和 B 公司的营收", DecisionTier.DIRECT),
    )
    results: list[tuple[str, bool, str]] = []
    for text, _expected in samples:
        verdict = await predict_intent(text, model=fast_model)
        if not verdict.ok:
            results.append((f"真实快判：{text}", False, verdict.error))
            continue
        gate = await run_intent_gate(
            text=text, intent_config=config, fast_model=ScriptedModel(replies=[verdict.raw_text])
        )
        verdict_obj: IntentVerdict | None = gate.verdict
        margin = verdict_obj.margin if verdict_obj else None
        results.append(
            (
                f"真实快判：{text}",
                verdict_obj is not None,
                f"intent={verdict_obj.prediction.intent if verdict_obj else '?'} "
                f"margin={margin:.2f} tier={gate.tier.value if gate.tier else '?'}",
            )
        )
    return results


async def main() -> int:
    online = "--online" in sys.argv
    print(f"意图预判层验收（{'--online 真实模型' if online else '离线自检'}）")
    print("=" * 60)
    failures = 0
    for name, ok, detail in await (_online_checks() if online else _offline_checks()):
        mark = "✓" if ok else "✗"
        print(f"{mark} {name}")
        if detail:
            print(f"  · {detail[:120]}")
        if not ok:
            failures += 1
    print("=" * 60)
    print("全部通过" if failures == 0 else f"{failures} 项未通过")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
