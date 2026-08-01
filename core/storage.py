"""
存储层 — JSON 文件读写，对齐 show-me-the-story story/storage.go
"""
import json
import os
from pathlib import Path
from typing import Optional
from core.models import (
    Progress, ChapterState, ChapterStatus,
    ProjectSettings, Character, WorldviewEntry, Organization,
    APIConfig, StoryConfig, Arc, Foreshadow, Skill
)


def ensure_dir(path: str):
    Path(path).mkdir(parents=True, exist_ok=True)


def read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json(path: str, data, indent=2):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


# ─── API 配置 ───

def load_api_config(path: str) -> APIConfig:
    data = read_json(path)
    if not data:
        cfg = APIConfig()
        save_api_config(path, cfg)
        return cfg
    return APIConfig(
        api_key=data.get("api_key", ""),
        base_url=data.get("base_url", ""),
        url_strict=data.get("url_strict", False),
        model=data.get("model", ""),
        max_tokens=data.get("max_tokens", 0),
        http_timeout_seconds=data.get("http_timeout_seconds", 300),
        context_budget_tokens=data.get("context_budget_tokens", 300000),
        verify_ssl=data.get("verify_ssl", True),
    )


def save_api_config(path: str, cfg: APIConfig):
    write_json(path, {
        "api_key": cfg.api_key,
        "base_url": cfg.base_url,
        "url_strict": cfg.url_strict,
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "http_timeout_seconds": cfg.http_timeout_seconds,
        "verify_ssl": cfg.verify_ssl,
        "context_budget_tokens": cfg.context_budget_tokens,
    })


# ─── 项目配置 ───

def load_story_config(path: str) -> dict:
    """加载项目 config.json"""
    data = read_json(path)
    if not data:
        return _default_config()
    return data


def _default_config() -> dict:
    return {
        "project_format_version": 3,
        "language": "zh",
        "story": {
            "type": "", "title": "", "chapter_count": 12,
            "target_words_per_chapter": 3000,
            "writing_style": "", "writing_pov": "", "story_synopsis": ""
        },
        "skill_config": {"enabled_skills": {}}
    }


def save_story_config(path: str, config: dict):
    write_json(path, config)


# ─── 进度 ───

def load_progress(path: str) -> Progress:
    """加载 progress.json"""
    data = read_json(path)
    if not data:
        return Progress()
    p = Progress(
        phase=data.get("phase", "outline"),
        title=data.get("title", ""),
        core_prompt=data.get("core_prompt", ""),
        story_synopsis=data.get("story_synopsis", ""),
        current_chapter_index=data.get("current_chapter_index", 0),
        memory_max_tokens=data.get("memory_max_tokens", 0),
    )
    for ch in data.get("chapters", []):
        p.chapters.append(ChapterState(
            num=ch["num"], title=ch.get("title", ""),
            outline=ch.get("outline", ""), summary=ch.get("summary", ""),
            status=ChapterStatus(ch.get("status", "pending")),
            word_count=ch.get("word_count", 0),
        ))
    for arc in data.get("arcs", []):
        p.arcs.append(Arc(
            id=arc["id"], title=arc.get("title", ""),
            goal=arc.get("goal", ""), start_ch=arc.get("start_ch", 0),
            end_ch=arc.get("end_ch", 0), summary=arc.get("summary", ""),
        ))
    # 加载章节正文（从独立文件）
    _load_chapter_contents(path, p)
    return p


def _load_chapter_contents(progress_path: str, p: Progress):
    """从 chapters/ 目录加载章节正文"""
    chapters_dir = Path(progress_path).parent / "chapters"
    if not chapters_dir.exists():
        return
    for ch in p.chapters:
        ch_file = chapters_dir / f"{ch.num:02d}.json"
        if ch_file.exists():
            data = read_json(str(ch_file))
            if data:
                ch.content = data.get("content", "")


def save_progress(path: str, p: Progress):
    """保存进度（不含正文）"""
    _save_chapter_contents(path, p)
    meta = p.to_dict()
    meta["arcs"] = [
        {"id": a.id, "title": a.title, "goal": a.goal,
         "start_ch": a.start_ch, "end_ch": a.end_ch, "summary": a.summary}
        for a in p.arcs
    ]
    meta["foreshadows"] = [
        {"id": f.id, "name": f.name, "description": f.description,
         "plant_chapter": f.plant_chapter, "target_chapter": f.target_chapter,
         "status": f.status.value, "events": [{"chapter": e.chapter, "note": e.note} for e in f.events],
         "resolution": f.resolution}
        for f in p.foreshadows
    ]
    meta["memory_entries"] = [
        {"id": m.id, "content": m.content, "category": m.category,
         "chapter": m.chapter, "position": m.position}
        for m in p.memory_entries
    ]
    meta["memory_max_tokens"] = p.memory_max_tokens
    write_json(path, meta)


def _save_chapter_contents(path: str, p: Progress):
    """保存章节正文到独立文件"""
    chapters_dir = Path(path).parent / "chapters"
    ensure_dir(str(chapters_dir))
    for ch in p.chapters:
        if ch.content:
            write_json(str(chapters_dir / f"{ch.num:02d}.json"), {
                "num": ch.num, "title": ch.title, "content": ch.content,
            })


def save_chapter_markdown(project_dir: str, ch: ChapterState, title: str):
    """导出章节为 Markdown"""
    content = f"# 第 {ch.num} 章: {ch.title}\n\n> **本章摘要**：{ch.summary}\n\n---\n\n{ch.content}"
    md_path = Path(project_dir) / f"Chapter_{ch.num:02d}.md"
    md_path.write_text(content, encoding='utf-8')


# ─── 设定 ───

def load_settings(path: str) -> ProjectSettings:
    data = read_json(path)
    if not data:
        return ProjectSettings()
    settings = ProjectSettings()
    for c in data.get("characters", []):
        settings.characters.append(Character(
            id=c.get("id", ""), name=c.get("name", ""),
            age=c.get("age", ""), appearance=c.get("appearance", ""),
            personality=c.get("personality", ""), background=c.get("background", ""),
            motivation=c.get("motivation", ""), abilities=c.get("abilities", ""),
            notes=c.get("notes", ""),
        ))
    for w in data.get("worldview", []):
        settings.worldview.append(WorldviewEntry(
            id=w.get("id", ""), name=w.get("name", ""),
            category=w.get("category", ""), description=w.get("description", ""),
            tags=w.get("tags", ""),
        ))
    for o in data.get("organizations", []):
        settings.organizations.append(Organization(
            id=o.get("id", ""), name=o.get("name", ""),
            type=o.get("type", ""), description=o.get("description", ""),
            members=o.get("members", []),
        ))
    return settings


def save_settings(path: str, settings: ProjectSettings):
    write_json(path, {
        "characters": [
            {"id": c.id, "name": c.name, "age": c.age,
             "appearance": c.appearance, "personality": c.personality,
             "background": c.background, "motivation": c.motivation,
             "abilities": c.abilities, "notes": c.notes}
            for c in settings.characters
        ],
        "worldview": [
            {"id": w.id, "name": w.name, "category": w.category,
             "description": w.description, "tags": w.tags}
            for w in settings.worldview
        ],
        "organizations": [
            {"id": o.id, "name": o.name, "type": o.type,
             "description": o.description, "members": o.members}
            for o in settings.organizations
        ],
    })


# ─── 项目列表 ───

def list_projects(storys_dir: str) -> list[dict]:
    """列出所有项目"""
    storys = Path(storys_dir)
    ensure_dir(str(storys))
    projects = []
    for d in sorted(storys.iterdir()):
        if not d.is_dir():
            continue
        config_path = d / "config.json"
        if config_path.exists():
            cfg = read_json(str(config_path)) or {}
            story = cfg.get("story", {})
            projects.append({
                "name": d.name,
                "title": story.get("title", ""),
                "language": cfg.get("language", "zh"),
            })
    return projects


def create_project(storys_dir: str, name: str, language: str = "zh") -> bool:
    project_dir = Path(storys_dir) / name
    if project_dir.exists():
        return False
    ensure_dir(str(project_dir))
    config = _default_config()
    config["language"] = language
    save_story_config(str(project_dir / "config.json"), config)
    write_json(str(project_dir / "progress.json"), {"phase": "outline"})
    write_json(str(project_dir / "settings.json"), {"characters": [], "worldview": [], "organizations": []})
    ensure_dir(str(project_dir / "sessions"))
    return True


def delete_project(storys_dir: str, name: str) -> bool:
    import shutil
    project_dir = Path(storys_dir) / name
    if not project_dir.exists():
        return False
    shutil.rmtree(str(project_dir))
    return True


def reset_progress(progress_path: str):
    """重置进度文件"""
    write_json(progress_path, {"phase": "outline"})
    chapters_dir = Path(progress_path).parent / "chapters"
    if chapters_dir.exists():
        import shutil
        shutil.rmtree(str(chapters_dir))
