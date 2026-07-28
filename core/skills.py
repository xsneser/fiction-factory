"""
技能系统 — 对齐 show-me-the-story skills.go + embeds/skills/*.md
"""
import os
import logging
from pathlib import Path
from core.models import Skill

logger = logging.getLogger(__name__)
SKILLS_DIR = Path(__file__).parent / "embeds" / "skills"


def load_builtin_skills() -> list[Skill]:
    skills = []
    if not SKILLS_DIR.exists():
        return skills
    for f in sorted(SKILLS_DIR.glob("*.md")):
        skill = _parse_skill_file(f.read_text(encoding='utf-8'), "builtin")
        if skill:
            skills.append(skill)
    return skills


def load_project_skills(project_dir: str) -> list[Skill]:
    pd = Path(project_dir) / "skills"
    if not pd.exists():
        return []
    skills = []
    for f in sorted(pd.glob("*.md")):
        skill = _parse_skill_file(f.read_text(encoding='utf-8'), "project")
        if skill:
            skills.append(skill)
    return skills


def _parse_skill_file(content: str, source: str) -> Skill | None:
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    frontmatter = parts[1].strip()
    body = parts[2].strip()
    skill = Skill(source=source, content=body)
    for line in frontmatter.split("\n"):
        line = line.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "id": skill.id = value
        elif key == "name": skill.name = value
        elif key == "description": skill.description = value
        elif key == "category": skill.category = value
        elif key == "lang": skill.lang = value
    if not skill.id:
        return None
    return skill


def merge_skills(builtin: list[Skill], project: list[Skill]) -> list[Skill]:
    return builtin + project


def load_all_skills(project_dir: str, language: str = "zh") -> list[Skill]:
    builtin = load_builtin_skills()
    proj = load_project_skills(project_dir)
    merged = merge_skills(builtin, proj)
    return [s for s in merged if not s.lang or s.lang == language]


def get_enabled_skills(skills: list[Skill], enabled_ids: dict[str, bool]) -> list[Skill]:
    return [s for s in skills if enabled_ids.get(s.id, False)]


def get_enabled_skills_by_category(skills: list[Skill], enabled: dict[str, bool], category: str) -> list[Skill]:
    return [s for s in skills if enabled.get(s.id, False) and s.category == category]


def format_skills_content(skills: list[Skill]) -> str:
    if not skills:
        return ""
    lines = ["以下技能规则在创作时必须严格遵守：", ""]
    for s in skills:
        lines.append(f"## {s.name}")
        lines.append(s.content)
        lines.append("")
    return "\n".join(lines)
