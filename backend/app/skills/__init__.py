"""Skill 子系统：可插拔的领域操作指南 + 受控脚本执行。

组成：
- schema.py   契约定义（frontmatter / 配置段 / 加载结果）
- loader.py   启动时扫描 skills/ 目录
- runner.py   脚本执行沙箱（独立进程 + 超时 + 输出封顶）
- middleware/skill.py 注入技能清单（渐进式披露的前半段）
- tools/builtin/skill.py load_skill / execute_skill_script 工具（后半段）
"""

from __future__ import annotations

from app.skills.loader import load_skills, load_skills_config, parse_skill_markdown
from app.skills.schema import (
    SkillCatalog,
    SkillManifest,
    SkillParseError,
    SkillRecord,
    SkillScriptConfig,
    SkillsFileConfig,
)

__all__ = [
    "SkillCatalog",
    "SkillManifest",
    "SkillParseError",
    "SkillRecord",
    "SkillScriptConfig",
    "SkillsFileConfig",
    "load_skills",
    "load_skills_config",
    "parse_skill_markdown",
]
