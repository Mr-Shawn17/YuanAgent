"""意图快判配置（config/agent.yaml 的 intent 段，docs/intent-recognition.md §8）。

加载模式与 routing 段保持一致（subagents.load_routing_config 的惯例）：专段
Pydantic 模型 + load_yaml_raw 提取，模型定义放在消费方（intent 包）而非
core/config.py。文件缺失 / intent 段缺失 / 段非法时返回默认（enabled=False，
即不启用预判层，保持现有主 Agent 路由行为）。

thresholds 是 margin = p1 − p2 的三段两阈值（D5），按意图差异化（键为意图名
或 default，写档类 coding/env 更严）：margin ≥ high 直走、low ≤ margin < high
升级裁决、margin < low 追问转人工。配置值只是初值，上线前须在自有标注样本上
校准（Selective Guaranteed Risk），非免检值。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.config import load_yaml_raw
from app.intent.schema import Intent

INTENT_CONFIG_NAME = "agent"
INTENT_KEY = "intent"

# thresholds 段可用的键：default + 5 类意图名
_ALLOWED_THRESHOLD_KEYS: frozenset[str] = frozenset(
    {"default"} | {item.value for item in Intent}
)

# 阈值兜底初值（config 未配置 default 时；设计文档 §6 的示意值）
DEFAULT_HIGH_MARGIN = 0.15
DEFAULT_LOW_MARGIN = 0.05


class IntentThresholds(BaseModel):
    """单类意图的 margin 三段两阈值（high ≥ low，三段：直走 / 升级 / 追问转人工）。"""

    high: float = Field(default=DEFAULT_HIGH_MARGIN, ge=0.0, le=1.0)
    low: float = Field(default=DEFAULT_LOW_MARGIN, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_low_le_high(self) -> Self:
        if self.low > self.high:
            msg = f"thresholds 非法：low（{self.low}）不能大于 high（{self.high}）"
            raise ValueError(msg)
        return self


def _default_thresholds() -> dict[str, IntentThresholds]:
    """阈值兜底初值（设计文档 §6/§8 示意值：写档类 coding/env 更严）。"""
    return {
        "default": IntentThresholds(),
        "coding": IntentThresholds(high=0.20, low=0.08),
        "env": IntentThresholds(high=0.20, low=0.08),
    }


class IntentConfig(BaseModel):
    """config/agent.yaml 的 intent 段顶层结构。"""

    enabled: bool = False
    fast_model_tier: Literal["main", "fast", "fallback"] = "fast"
    upgrade_model_tier: Literal["main", "fast", "fallback"] = "main"
    thresholds: dict[str, IntentThresholds] = Field(default_factory=_default_thresholds)
    turn_limit: int = Field(default=6, ge=1, le=100)
    # 高风险硬规则：这些意图无论置信度多高都升级 main 裁决（D6 第一层 P0，
    # 对应 highest_tier ≥ write 的意图级简化——当前 coding/env 子 Agent 均含写/危险档工具）
    risk_intents: list[Intent] = Field(default_factory=lambda: [Intent.CODING, Intent.ENV])
    # 跨意图信号：top_k 第二名概率超过该值视为单条多意图（升级让 main 拆分任务）
    cross_intent_top2: float = Field(default=0.3, ge=0.0, le=1.0)

    @field_validator("thresholds")
    @classmethod
    def _check_threshold_keys(
        cls, value: dict[str, IntentThresholds]
    ) -> dict[str, IntentThresholds]:
        """拒绝拼错的意图键（否则静默回退 default，阈值差异形同虚设）。"""
        unknown = set(value) - _ALLOWED_THRESHOLD_KEYS
        if unknown:
            intent_names = "/".join(sorted(_ALLOWED_THRESHOLD_KEYS - {"default"}))
            msg = f"thresholds 键非法：{sorted(unknown)}（可用：default 或意图名 {intent_names}）"
            raise ValueError(msg)
        return value

    def thresholds_for(self, intent: Intent | str) -> IntentThresholds:
        """取某类意图的阈值；未单独配置时回退 default，再缺省用内置初值。"""
        key = intent.value if isinstance(intent, Intent) else intent
        entry = self.thresholds.get(key)
        if entry is None:
            entry = self.thresholds.get("default")
        return entry if entry is not None else IntentThresholds()


def load_intent_config(
    name: str = INTENT_CONFIG_NAME, *, directory: Path | None = None
) -> IntentConfig:
    """读取意图快判配置：从主配置文件（agent.yaml）的 intent 段提取。

    文件不存在 / 缺少 intent 段 / 段不是映射时返回默认（enabled=False）。
    """
    raw = load_yaml_raw(name, directory=directory)
    section = raw.get(INTENT_KEY, {})
    if not isinstance(section, dict):
        return IntentConfig()
    return IntentConfig.model_validate(section)


__all__ = [
    "DEFAULT_HIGH_MARGIN",
    "DEFAULT_LOW_MARGIN",
    "INTENT_CONFIG_NAME",
    "INTENT_KEY",
    "IntentConfig",
    "IntentThresholds",
    "load_intent_config",
]
