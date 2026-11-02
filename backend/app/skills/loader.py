"""Skill 加载器：启动时扫描 skills/ 目录，解析每个 SKILL.md 为 SkillRecord。

设计对齐 McpManager 的容错哲学：单个 skill 解析失败只记入 disabled
（不炸启动）；目录不存在视为空目录（功能静默关闭）。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.core.config import PROJECT_ROOT, RUNTIME_CONFIG_NAME, load_yaml_raw
from app.core.logging import get_logger
from app.skills.schema import (
    SkillCatalog,
    SkillManifest,
    SkillParseError,
    SkillRecord,
    SkillsFileConfig,
)

logger = get_logger(__name__)

SKILL_ENTRY_FILENAME = "SKILL.md"
SKILLS_CONFIG_KEY = "skills"


def load_skills_config() -> SkillsFileConfig:
    """读取主配置（config/agent.yaml）的 skills 段；缺省返回默认（enabled=True）。"""
    raw = load_yaml_raw(RUNTIME_CONFIG_NAME)
    section = raw.get(SKILLS_CONFIG_KEY, {})
    if not isinstance(section, dict):
        return SkillsFileConfig()
    return SkillsFileConfig.model_validate(section)


def resolve_skills_root(directory: str) -> Path:
    """skills 目录按项目根解析相对路径。"""
    path = Path(directory)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def parse_skill_markdown(text: str) -> tuple[SkillManifest, str]:
    """解析 SKILL.md：YAML frontmatter + markdown 正文。

    frontmatter 必须是首行 `---` 包围的 YAML 块；解析失败抛 SkillParseError。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        msg = "缺少 YAML frontmatter（首行必须是 ---）"
        raise SkillParseError(msg)

    end_index = -1
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_index = index
            break
    if end_index < 0:
        msg = "frontmatter 未闭合（缺少结束 ---）"
        raise SkillParseError(msg)

    frontmatter_text = "\n".join(lines[1:end_index])
    try:
        data = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        msg = f"frontmatter YAML 解析失败：{exc}"
        raise SkillParseError(msg) from exc
    if not isinstance(data, dict):
        msg = "frontmatter 顶层必须是映射（name/description 等键值对）"
        raise SkillParseError(msg)

    try:
        manifest = SkillManifest.model_validate(data)
    except Exception as exc:
        msg = f"frontmatter 字段校验失败：{exc}"
        raise SkillParseError(msg) from exc

    body = "\n".join(lines[end_index + 1 :]).strip()
    if not body:
        msg = "缺少指南正文（frontmatter 之后应有 markdown 内容）"
        raise SkillParseError(msg)
    return manifest, body


def _load_one(skill_dir: Path) -> SkillRecord:
    entry = skill_dir / SKILL_ENTRY_FILENAME
    text = entry.read_text(encoding="utf-8")
    manifest, body = parse_skill_markdown(text)
    if manifest.name != skill_dir.name:
        msg = f"frontmatter name（{manifest.name}）与目录名（{skill_dir.name}）不一致"
        raise SkillParseError(msg)

    scripts_dir = skill_dir / "scripts"
    scripts = (
        sorted(item.name for item in scripts_dir.iterdir() if item.suffix == ".py")
        if scripts_dir.is_dir()
        else []
    )
    return SkillRecord(
        name=manifest.name,
        description=manifest.description,
        directory=skill_dir,
        markdown=body,
        scripts=scripts,
        allowed_scripts=list(manifest.allowed_scripts),
        script_timeout=manifest.script_timeout,
    )


def load_skills(
    config: SkillsFileConfig | None = None,
    *,
    root: Path | None = None,
) -> SkillCatalog:
    """扫描技能目录：合法的进 records，非法/被白名单挡下的进 disabled。"""
    resolved = config or load_skills_config()
    skills_root = root or resolve_skills_root(resolved.dir)
    catalog = SkillCatalog(script_config=resolved.script, root=skills_root)
    if not resolved.enabled:
        return catalog
    if not skills_root.is_dir():
        logger.info("skills_dir_missing", root=str(skills_root))
        return catalog

    allow_set = {name.strip() for name in resolved.allow_list if name.strip()}
    seen: set[str] = set()
    for entry in sorted(skills_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        if allow_set and entry.name not in allow_set:
            catalog.disabled[entry.name] = "不在 allow_list 白名单内"
            continue
        try:
            record = _load_one(entry)
        except (SkillParseError, OSError, UnicodeDecodeError) as exc:
            catalog.disabled[entry.name] = f"解析失败：{exc}"
            continue
        if record.name in seen:
            catalog.disabled[entry.name] = "技能名重复"
            continue
        seen.add(record.name)
        catalog.records.append(record)

    if catalog.records or catalog.disabled:
        logger.info(
            "skills_loaded",
            count=len(catalog.records),
            disabled=list(catalog.disabled),
        )
    return catalog


__all__ = [
    "SKILLS_CONFIG_KEY",
    "load_skills",
    "load_skills_config",
    "parse_skill_markdown",
    "resolve_skills_root",
]
