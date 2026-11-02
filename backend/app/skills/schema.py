"""Skill 契约定义：frontmatter 结构、加载结果、配置段。

Skill = 一包可插拔的「领域操作指南」（渐进式披露：system prompt 只注入
清单，模型调用 load_skill 工具后才拿到全文；scripts/ 子目录可选携带
Python 脚本，经 execute_skill_script 工具在 danger 档审批下执行）。

目录约定（一个 skill 一个目录）：
    skills/<name>/SKILL.md          # 必需：YAML frontmatter + markdown 指南正文
    skills/<name>/template.md.j2    # 可选静态资源（正文内相对引用）
    skills/<name>/scripts/*.py      # 可选脚本（受白名单约束）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

# 技能名 = 目录名：只允许小写字母、数字、连字符、下划线，且以字母开头
SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
# description 上限：防清单注入撑爆 system prompt
MAX_DESCRIPTION_LENGTH = 500
DEFAULT_SKILLS_DIR = "skills"


class SkillParseError(Exception):
    """SKILL.md 解析/校验失败（加载时记录为禁用原因，不炸启动）。"""


class SkillScriptConfig(BaseModel):
    """config/agent.yaml 的 skills.script 段：脚本执行的全局约束。"""

    enabled: bool = True
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    max_output_chars: int = Field(default=20_000, ge=1_000, le=200_000)


class SkillsFileConfig(BaseModel):
    """config/agent.yaml 的 skills 段顶层结构。"""

    enabled: bool = True
    dir: str = DEFAULT_SKILLS_DIR
    # 空 = 加载全部；非空 = 只加载列表内的技能，其余记为禁用
    allow_list: list[str] = Field(default_factory=list)
    script: SkillScriptConfig = Field(default_factory=SkillScriptConfig)

    @field_validator("dir")
    @classmethod
    def _check_dir(cls, value: str) -> str:
        text = value.strip()
        if not text:
            msg = "skills.dir 不能为空"
            raise ValueError(msg)
        return text


class SkillManifest(BaseModel):
    """SKILL.md 的 YAML frontmatter 契约。"""

    name: str
    description: str
    # 空 = scripts/ 下全部 .py 可执行；非空 = 只允许列表内的脚本名
    allowed_scripts: list[str] = Field(default_factory=list)
    # 覆盖全局脚本超时（秒）；None = 用 skills.script.timeout_seconds
    script_timeout: int | None = Field(default=None, ge=1, le=600)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        name = value.strip()
        if not SKILL_NAME_PATTERN.match(name):
            msg = f"技能名非法：{value}（只允许小写字母/数字/连字符/下划线，以字母开头）"
            raise ValueError(msg)
        return name

    @field_validator("description")
    @classmethod
    def _check_description(cls, value: str) -> str:
        # 折叠换行与连续空白为单行，保证清单渲染紧凑
        text = " ".join(value.split())
        if not text:
            msg = "description 不能为空（这是模型判断是否使用该技能的唯一依据）"
            raise ValueError(msg)
        if len(text) > MAX_DESCRIPTION_LENGTH:
            msg = f"description 过长（{len(text)} > {MAX_DESCRIPTION_LENGTH} 字符）"
            raise ValueError(msg)
        return text

    @field_validator("allowed_scripts")
    @classmethod
    def _check_scripts(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            name = item.strip()
            if not name:
                continue
            if "/" in name or "\\" in name or ".." in name or not name.endswith(".py"):
                msg = f"allowed_scripts 条目非法：{item}（只允许 scripts/ 内的 .py 文件名）"
                raise ValueError(msg)
            if name not in cleaned:
                cleaned.append(name)
        return cleaned


@dataclass(slots=True)
class SkillRecord:
    """一个已加载的技能：manifest + 指南正文 + 脚本清单。"""

    name: str
    description: str
    directory: Path
    markdown: str
    scripts: list[str] = field(default_factory=list)
    allowed_scripts: list[str] = field(default_factory=list)
    script_timeout: int | None = None

    @property
    def scripts_dir(self) -> Path:
        return self.directory / "scripts"

    @property
    def effective_timeout(self) -> int | None:
        return self.script_timeout

    def script_allowed(self, script_name: str) -> bool:
        """脚本是否在白名单内（空白名单 = scripts/ 下全部 .py）。"""
        if self.allowed_scripts:
            return script_name in self.allowed_scripts
        return script_name.endswith(".py")


@dataclass(slots=True)
class SkillCatalog:
    """启动时一次性加载的技能目录：装配工具/中间件的数据源。"""

    records: list[SkillRecord] = field(default_factory=list)
    disabled: dict[str, str] = field(default_factory=dict)
    script_config: SkillScriptConfig = field(default_factory=SkillScriptConfig)
    root: Path | None = None

    def find(self, name: str) -> SkillRecord | None:
        key = name.strip()
        for record in self.records:
            if record.name == key:
                return record
        return None

    @property
    def names(self) -> list[str]:
        return [record.name for record in self.records]


__all__ = [
    "DEFAULT_SKILLS_DIR",
    "MAX_DESCRIPTION_LENGTH",
    "SKILL_NAME_PATTERN",
    "SkillCatalog",
    "SkillManifest",
    "SkillParseError",
    "SkillRecord",
    "SkillScriptConfig",
    "SkillsFileConfig",
]
