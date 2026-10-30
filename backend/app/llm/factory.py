from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from app.core.config import ModelProfile, Settings, get_settings
from app.core.errors import ConfigError, LLMError

_MODEL_CACHE: dict[str, BaseChatModel] = {}


def _require_profile(settings: Settings, tier: str) -> ModelProfile:
    profile = settings.profile_for(tier)
    if not profile.is_configured:
        hint = (
            "LLM_API_KEY / LLM_MODEL（LLM_API_KEY 留空时会自动回退系统环境变量 DASHSCOPE_API_KEY）"
            if tier == "main"
            else f"{tier.upper()}_LLM_* 或回退用的 LLM_*"
        )
        msg = (
            f"模型未配置（档位 {tier}）。请在项目根目录复制 .env.example 为 .env，并填写 {hint}。"
            f"当前 base_url={profile.base_url}，model={profile.model or '(空)'}"
        )
        raise ConfigError(msg)
    return profile


def get_chat_model(
    tier: str = "main",
    *,
    settings: Settings | None = None,
    streaming: bool = True,
    cache: bool = True,
) -> BaseChatModel:
    resolved = settings or get_settings()
    profile = _require_profile(resolved, tier)
    cache_key = f"chat:{tier}:{streaming}:{profile.base_url}:{profile.model}"
    cached = _MODEL_CACHE.get(cache_key)
    if cache and cached is not None:
        return cached

    model: BaseChatModel = ChatOpenAI(
        base_url=profile.base_url,
        api_key=profile.api_key.get_secret_value(),
        model=profile.model,
        temperature=profile.temperature,
        timeout=profile.timeout,
        max_retries=profile.max_retries,
        streaming=streaming,
    )
    if cache:
        _MODEL_CACHE[cache_key] = model
    return model


async def ping_model(tier: str = "main", *, settings: Settings | None = None) -> str:
    resolved = settings or get_settings()
    profile = _require_profile(resolved, tier)
    model = get_chat_model(tier, settings=resolved, streaming=False, cache=False)
    try:
        result = await model.ainvoke([HumanMessage(content="ping")], max_tokens=1)
    except Exception as exc:
        msg = (
            f"连接模型失败（{profile.base_url} / {profile.model}）：{exc}\n"
            "请检查 .env 中的 LLM_BASE_URL、LLM_API_KEY、LLM_MODEL 是否正确，以及网络是否可达。"
        )
        raise LLMError(msg, details={"tier": tier, "base_url": profile.base_url}) from exc
    content = getattr(result, "content", "")
    return content if isinstance(content, str) else str(content)


def clear_model_cache() -> None:
    _MODEL_CACHE.clear()


__all__ = ["clear_model_cache", "get_chat_model", "ping_model"]
