from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator

PROJECT_ROOT: Path = Path(__file__).resolve().parents[3]
CONFIG_DIR: Path = PROJECT_ROOT / "config"

ModelTier = str
_ALLOWED_TIERS: frozenset[str] = frozenset({"main", "fast", "fallback"})
_ALLOWED_KB_MODES: frozenset[str] = frozenset({"stub", "mcp"})
# 运行时配置所在的 YAML 文件名与顶层键（原 .env 的内容统一迁入 config/agent.yaml 的 runtime 段）
RUNTIME_CONFIG_NAME = "agent"
RUNTIME_KEY = "runtime"
# *_api_key_env 留空时，按顺序回退到这些系统环境变量读取密钥
_API_KEY_ENV_FALLBACKS: tuple[str, ...] = ("DASHSCOPE_API_KEY", "OPENAI_API_KEY", "GLM_API_KEY")


def api_key_from_env() -> str:
    for name in _API_KEY_ENV_FALLBACKS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


class PersonaConfig(BaseModel):
    name: str = "阿元"
    role: str = "用户的私人 AI 助理与日常聊天伙伴"
    language: str = "简体中文"
    timezone: str = "Asia/Shanghai"
    style: str = ""
    boundaries: str = ""


class LimitsConfig(BaseModel):
    max_tool_calls_per_turn: int = Field(default=12, ge=1, le=64)
    max_model_calls_per_turn: int = Field(default=15, ge=1, le=64)


class HistoryConfig(BaseModel):
    summarize_trigger_tokens: int = Field(default=12000, ge=1000)
    keep_messages: int = Field(default=20, ge=2)


class ServerConfig(BaseModel):
    """启动脚本运行环境（scripts/start_backend.py / start_frontend.py 读取）。

    python_env    Python 环境根目录，脚本按平台拼接解释器（Windows 用 Scripts/python.exe，
                  Linux/macOS 用 bin/python）；换机器时替换此路径即可。
    backend_port  后端 API 监听端口（uvicorn --port）。
    frontend_port 前端 Next.js dev 监听端口（next dev -p）。
    """

    python_env: str = ""
    backend_port: int = Field(default=8000, ge=1, le=65535)
    frontend_port: int = Field(default=3000, ge=1, le=65535)


class AgentFileConfig(BaseModel):
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)


class ModelProfile(BaseModel):
    tier: ModelTier
    base_url: str
    api_key: SecretStr
    model: str
    temperature: float = 0.7
    timeout: float = 60.0
    max_retries: int = 2

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key.get_secret_value().strip()) and bool(self.model.strip())


class Settings(BaseModel):
    """运行时配置（原 .env 内容），统一从 config/agent.yaml 的 runtime 段加载。

    字段名与旧环境变量一一对应（如 llm_model ↔ LLM_MODEL），但数据来源改为 YAML；
    敏感密钥通过 *_api_key_env 字段指定「环境变量名」（如 GLM_API_KEY），
    密钥本身绝不写进 YAML（见 _resolve_key / api_key_from_env）。
    """

    app_env: str = "dev"
    log_level: str = "INFO"
    log_json: bool = False
    # 文件日志：目录（相对路径按项目根解析）与保留天数（启动时清理过期文件；0=永久保留）
    log_dir: str = "log"
    log_retention_days: int = Field(default=7, ge=0)
    data_dir: Path = PROJECT_ROOT / "data"
    default_user_id: str = "me"
    cors_origins: str = "http://localhost:3000"
    # Web 端轻量鉴权：留空表示关闭鉴权（本地开发/个人工具场景）
    api_key: SecretStr = SecretStr("")

    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # 密钥来源：存环境变量名（如 GLM_API_KEY），密钥本身绝不写进 YAML；留空回退 api_key_from_env()
    llm_api_key_env: str = ""
    llm_model: str = "qwen-plus"
    llm_temperature: float = 0.7
    llm_timeout: float = 60.0
    llm_max_retries: int = 2

    fallback_llm_base_url: str = ""
    fallback_llm_api_key_env: str = ""
    fallback_llm_model: str = ""

    fast_llm_base_url: str = ""
    fast_llm_api_key_env: str = ""
    fast_llm_model: str = "qwen-turbo"
    fast_llm_temperature: float = 0.3

    mysql_host: str = "127.0.0.1"
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_user: str = "root"
    mysql_password: SecretStr = SecretStr("")
    mysql_database: str = "agent"
    mysql_pool_size: int = Field(default=5, ge=1, le=128)
    mysql_max_overflow: int = Field(default=10, ge=0, le=128)
    mysql_echo: bool = False

    # ===== Langfuse 可观测（自托管；留空/关闭则优雅降级为 no-op，不影响对话）=====
    langfuse_enabled: bool = False
    langfuse_host: str = "http://localhost:3030"
    langfuse_public_key: SecretStr = SecretStr("")
    langfuse_secret_key: SecretStr = SecretStr("")

    kb_mode: str = "stub"
    kb_collection: str = "agent_knowledge"
    kb_stub_data: Path = PROJECT_ROOT / "data" / "kb_stub" / "chunks.json"
    rag_mcp_url: str = "http://127.0.0.1:8100/mcp"

    tavily_api_key: SecretStr = SecretStr("")

    @field_validator("log_level")
    @classmethod
    def _normalize_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            msg = f"LOG_LEVEL 非法：{value}，可选 DEBUG/INFO/WARNING/ERROR/CRITICAL"
            raise ValueError(msg)
        return level

    @field_validator("log_dir")
    @classmethod
    def _resolve_log_dir(cls, value: str) -> str:
        return value if Path(value).is_absolute() else str((PROJECT_ROOT / value).resolve())

    @property
    def log_dir_path(self) -> Path:
        return Path(self.log_dir)

    @field_validator("data_dir")
    @classmethod
    def _resolve_data_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @field_validator("kb_stub_data")
    @classmethod
    def _resolve_kb_stub_data(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @field_validator("kb_mode")
    @classmethod
    def _normalize_kb_mode(cls, value: str) -> str:
        mode = value.strip().lower()
        if mode not in _ALLOWED_KB_MODES:
            msg = f"KB_MODE 非法：{value}，可选 stub/mcp"
            raise ValueError(msg)
        return mode

    @field_validator("llm_temperature", "fast_llm_temperature")
    @classmethod
    def _clamp_temperature(cls, value: float) -> float:
        if not 0.0 <= value <= 2.0:
            msg = f"温度必须在 0.0~2.0 之间，收到 {value}"
            raise ValueError(msg)
        return value

    @field_validator("tavily_api_key")
    @classmethod
    def _sanitize_api_key(cls, value: SecretStr) -> SecretStr:
        """清洗 API Key：剥掉行内注释残留，并拒绝含空白/非 ASCII 的值。

        密码类字段（mysql_password）不做此处理，因为 # 与空格对密码是合法字符。
        """
        raw = value.get_secret_value().strip()
        if not raw:
            return SecretStr("")
        key = "" if raw.startswith("#") else raw.split("#", 1)[0].strip()
        if not key:
            return SecretStr("")
        if not key.isascii() or any(char.isspace() for char in key):
            msg = (
                f"API Key 非法（应为不含空白的 ASCII 串）：{key[:4]}…；"
                "注意密钥值不应含空白或非 ASCII 字符"
            )
            raise ValueError(msg)
        return SecretStr(key)

    def _resolve_key(self, env_field: str) -> str:
        """按 *_api_key_env 字段解析密钥。

        env_field 是「环境变量名」（如 GLM_API_KEY）：
        - 非空时，直接从该环境变量读取密钥；
        - 为空时返回空字符串，由调用方决定是否回退主模型密钥。
        """
        name = (env_field or "").strip()
        if name:
            return os.environ.get(name, "").strip()
        return ""

    @property
    def resolved_llm_api_key(self) -> SecretStr:
        """主模型密钥：由 llm_api_key_env 指定的环境变量读取，留空回退通用回退链。"""
        return SecretStr(self._resolve_key(self.llm_api_key_env) or api_key_from_env())

    @property
    def _mysql_credentials(self) -> tuple[str, str]:
        password = self.mysql_password.get_secret_value().strip()
        if not password:
            password = os.environ.get("MYSQL_PASSWORD", "").strip()
        return quote_plus(self.mysql_user), quote_plus(password)

    @property
    def _mysql_netloc(self) -> str:
        user, password = self._mysql_credentials
        return f"{user}:{password}@{self.mysql_host}:{self.mysql_port}"

    @property
    def sqlalchemy_dsn(self) -> str:
        """业务表（SQLAlchemy 2.0 async）使用的 DSN。"""
        return f"mysql+asyncmy://{self._mysql_netloc}/{self.mysql_database}?charset=utf8mb4"

    @property
    def langgraph_mysql_uri(self) -> str:
        """标准 MySQL 连接串（密码已百分号转义），仅用于日志/展示与外部工具。

        注意：langgraph-checkpoint-mysql 的 parse_conn_string 用 urlparse().password，
        不做百分号解码，密码含 @ 等字符会被原样误传，所以内部连接一律走
        mysql_connect_kwargs 显式建连。
        """
        return f"mysql://{self._mysql_netloc}/{self.mysql_database}"

    @property
    def mysql_connect_kwargs(self) -> dict[str, Any]:
        """asyncmy.connect 参数（不含 db，由调用方按需传入）。"""
        return {
            "host": self.mysql_host,
            "port": self.mysql_port,
            "user": self.mysql_user,
            "password": self.mysql_password.get_secret_value(),
            "charset": "utf8mb4",
            "autocommit": True,
        }

    @property
    def mysql_server_dsn(self) -> str:
        """不指定库名的 DSN，建库脚本（scripts/init_mysql.py）用。"""
        return f"mysql+asyncmy://{self._mysql_netloc}/?charset=utf8mb4"

    @property
    def workspace_dir(self) -> Path:
        """文件类工具的沙箱根目录。"""
        return self.data_dir / "workspace"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.workspace_dir,
            self.kb_stub_data.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def profile_for(self, tier: ModelTier = "main") -> ModelProfile:
        if tier not in _ALLOWED_TIERS:
            msg = f"未知的模型档位：{tier}，可选 main/fast/fallback"
            raise ValueError(msg)

        main_key = self.resolved_llm_api_key.get_secret_value()

        if tier == "fast":
            return ModelProfile(
                tier="fast",
                base_url=self.fast_llm_base_url or self.llm_base_url,
                api_key=SecretStr(self._resolve_key(self.fast_llm_api_key_env) or main_key),
                model=self.fast_llm_model or self.llm_model,
                temperature=self.fast_llm_temperature,
                timeout=self.llm_timeout,
                max_retries=self.llm_max_retries,
            )

        if tier == "fallback":
            return ModelProfile(
                tier="fallback",
                base_url=self.fallback_llm_base_url or self.llm_base_url,
                api_key=SecretStr(self._resolve_key(self.fallback_llm_api_key_env) or main_key),
                model=self.fallback_llm_model,
                temperature=self.llm_temperature,
                timeout=self.llm_timeout,
                max_retries=1,
            )

        return ModelProfile(
            tier="main",
            base_url=self.llm_base_url,
            api_key=SecretStr(main_key),
            model=self.llm_model,
            temperature=self.llm_temperature,
            timeout=self.llm_timeout,
            max_retries=self.llm_max_retries,
        )

    def has_secret(self, env_name: str) -> bool:
        value = getattr(self, env_name.lower(), None)
        if isinstance(value, SecretStr):
            return bool(value.get_secret_value().strip())
        return bool(value)

    def redacted_dump(self) -> dict[str, Any]:
        data = self.model_dump()
        for key, value in data.items():
            if isinstance(value, SecretStr):
                data[key] = "***" if value.get_secret_value() else ""
            elif isinstance(value, Path):
                data[key] = str(value)
        return data


def load_settings(**overrides: Any) -> Settings:
    """从 config/agent.yaml 的 runtime 段加载 Settings。

    配置来源只有两处：
    1. YAML runtime 段（基础配置）
    2. overrides 参数（测试/程序化注入）

    敏感密钥仍由 api_key_from_env() 统一从系统环境变量回退（DASHSCOPE/OPENAI/GLM）。
    """
    raw = load_yaml_raw(RUNTIME_CONFIG_NAME)
    runtime = raw.get(RUNTIME_KEY, {})
    if not isinstance(runtime, dict):
        runtime = {}

    merged: dict[str, Any] = dict(runtime)
    merged.update(overrides)
    return Settings.model_validate(merged)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
    get_agent_config.cache_clear()


def load_yaml_config[T: BaseModel](
    name: str, schema: type[T], *, directory: Path | None = None
) -> T:
    path = Path(name) if Path(name).is_absolute() else (directory or CONFIG_DIR) / f"{name}.yaml"
    if not path.exists():
        return schema()
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        msg = f"配置文件 {path} 的顶层结构必须是映射（key: value）"
        raise ValueError(msg)
    return schema.model_validate(raw)


def load_yaml_raw(name: str, *, directory: Path | None = None) -> dict[str, Any]:
    """读取 YAML 文件的原始顶层 dict（不套 schema），供提取子段（如 agent.yaml 的 routing）。"""
    path = Path(name) if Path(name).is_absolute() else (directory or CONFIG_DIR) / f"{name}.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        msg = f"配置文件 {path} 的顶层结构必须是映射（key: value）"
        raise ValueError(msg)
    return raw


def load_agent_config(path: Path | None = None) -> AgentFileConfig:
    if path is not None:
        with path.open(encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        return AgentFileConfig.model_validate(raw)
    return load_yaml_config("agent", AgentFileConfig)


@lru_cache(maxsize=1)
def get_agent_config() -> AgentFileConfig:
    return load_agent_config()
