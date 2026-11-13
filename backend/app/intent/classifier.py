"""意图快判调用器（docs/intent-recognition-implementation.md Step 2）。

封装「调用 fast 档模型 → 解析三字段 schema」：
- parse_intent_output  纯解析（不调模型，可独立单测）：剥代码围栏 → 提取 JSON →
  归一化（百分数概率、乱序候选）→ Pydantic 校验；
- predict_intent       组装 prompt（意图定义 + 历史 + 当前消息）→ 调模型 → 解析。

失败处理约定（为 Step 3 的 P0「模糊」信号提供输入）：输出非 JSON、schema 校验
不过、模型调用异常，一律返回 ok=False 的 IntentVerdict 并携带 error，绝不在
快判层抛异常打断主链路。fast 档未配置（ConfigError）同理降级为失败 → 全量
升级 main 裁决，等价于未启用预判层时的现有行为。
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from app.core.logging import get_logger
from app.intent.schema import IntentPrediction, IntentVerdict

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import BaseMessage

    from app.core.config import Settings

logger = get_logger(__name__)

# 快判喂给模型的历史条数上限（只取最近一段，控制 fast 模型成本）
CLASSIFY_HISTORY_LIMIT = 6

CLASSIFY_SYSTEM_PROMPT = """你是 YuanAgent 的意图快判器。
判断用户当前消息属于哪类意图，只输出一个 JSON 对象。

意图类别（intent 只能取以下 5 个值之一）：
- chat: 闲聊——打招呼、寒暄、情绪陪伴等无任务的日常对话
- research: 调研——信息检索、知识问答、资料整理、联网查证
- coding: 编码——编写/修改代码或文件、跑测试等开发类任务
- env: 环境准备——git 克隆/更新、安装依赖、提交推送等确定性环境操作
- multi_compare: 多对象对比——同时对比两个及以上对象（如公司/产品/方案对比）

输出格式（严格遵守）：
{"intent": "<主意图>", "top_k": [{"intent": "<候选1>", "prob": <0~1 小数>},
 {"intent": "<候选2>", "prob": <0~1 小数>}], "needs_context": <true|false>}

要求：
1. 只输出 JSON，不要 markdown 代码块、不要任何解释文字；
2. top_k 给出前 2~3 个候选意图及概率，按概率从高到低排列，第一名必须与 intent 一致；
3. needs_context：当前消息含指代或省略、必须结合对话历史才能判断（如出现
   「它/他/这个/那个/刚才/上面」或省略了主语/宾语）为 true，否则为 false；
4. 对话历史仅用于判断指代与省略，不要把历史内容当作当前消息来分类。"""

# 剥掉 markdown 代码围栏（模型即使被要求不写也常带）
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
# 兜底提取首个 JSON 对象（模型输出「结果：{...}」之类的前缀文字时）
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_text(raw: str) -> str:
    text = raw.strip()
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        obj = _JSON_OBJECT_RE.search(text)
        if obj:
            text = obj.group(0)
    return text


def _normalize_prob(value: Any) -> Any:
    """容忍模型把概率写成 0~100 百分数：>1 的值除以 100；不可转换的交给 schema 报错。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if number > 1.0:
        number = number / 100.0
    return number


def _prob_sort_key(entry: dict[str, Any]) -> float:
    try:
        return float(entry.get("prob", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _normalize_payload(data: Any) -> Any:
    """宽容归一化：候选按概率降序（模型乱序时不误判）、截取前 3、概率百分数转小数。

    只做无损/方向安全的修复；结构性问题（缺字段、意图不在枚举）留给 schema 校验
    判「模糊」——宁可升级，不可静默猜。
    """
    if not isinstance(data, dict):
        return data
    payload = dict(data)
    top_k = payload.get("top_k")
    if isinstance(top_k, list):
        candidates: list[dict[str, Any]] = []
        for item in top_k:
            if not isinstance(item, dict):
                continue
            entry = dict(item)
            if "prob" in entry:
                entry["prob"] = _normalize_prob(entry["prob"])
            candidates.append(entry)
        candidates.sort(key=_prob_sort_key, reverse=True)
        payload["top_k"] = candidates[:3]
    return payload


def parse_intent_output(raw: str) -> IntentVerdict:
    """把 fast 模型的原始输出解析为 IntentVerdict（纯函数，不调模型）。"""
    text = _extract_json_text(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return IntentVerdict(error="快判输出不是合法 JSON", raw_text=raw)
    try:
        prediction = IntentPrediction.model_validate(_normalize_payload(data))
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first.get("loc", ()))
        msg = str(first.get("msg", "schema 校验失败"))
        detail = f"{loc}：{msg}" if loc else msg
        return IntentVerdict(error=f"快判输出不符合 schema：{detail}", raw_text=raw)
    return IntentVerdict(prediction=prediction, raw_text=raw)


def _render_history(history: Sequence[BaseMessage] | None) -> str:
    if not history:
        return ""
    lines: list[str] = []
    for message in list(history)[-CLASSIFY_HISTORY_LIMIT:]:
        content = str(message.content).strip()
        if not content:
            continue
        role = "用户" if isinstance(message, HumanMessage) else "阿元"
        lines.append(f"{role}：{content}")
    if not lines:
        return ""
    return "【对话历史（仅用于判断指代/省略）】\n" + "\n".join(lines) + "\n\n"


async def predict_intent(
    text: str,
    history: Sequence[BaseMessage] | None = None,
    *,
    model: BaseChatModel | None = None,
    tier: str = "fast",
    settings: Settings | None = None,
) -> IntentVerdict:
    """调 fast 档模型快判意图，返回 IntentVerdict（失败返回 ok=False，不抛异常）。

    model / settings 参数用于测试与装配方注入；默认经 get_chat_model(tier) 取
    fast 档（tier 由调用方按 config/agent.yaml 的 intent.fast_model_tier 传入）。
    """
    cleaned = text.strip()
    if not cleaned:
        return IntentVerdict(error="用户消息为空，无法快判")
    if model is None:
        from app.llm.factory import get_chat_model

        model = get_chat_model(tier, settings=settings, streaming=False)
    prompt = _render_history(history) + f"【当前用户消息】\n{cleaned}"
    messages: list[BaseMessage] = [
        SystemMessage(content=CLASSIFY_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]
    try:
        result = await model.ainvoke(messages)
    except Exception as exc:
        logger.warning("intent_classify_call_failed", tier=tier, error=str(exc))
        return IntentVerdict(error=f"快判模型调用失败：{exc}")
    content = getattr(result, "content", "")
    return parse_intent_output(content if isinstance(content, str) else str(content))


__all__ = [
    "CLASSIFY_HISTORY_LIMIT",
    "CLASSIFY_SYSTEM_PROMPT",
    "parse_intent_output",
    "predict_intent",
]
