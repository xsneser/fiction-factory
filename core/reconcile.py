"""
设定协调 + 大纲角色检查 + 全书优化 + SSE日志
对齐 show-me-the-story reconcile.go + outline_character.go + postprocess.go + sse/logger.go
"""
import json
import logging
from core.models import (
    Progress, ChapterStatus, ProjectSettings,
    OutlineCharacterReport, OutlineCharacterSuggestion,
    BookDiagnosis, BookDiagnosisItem
)
from core.llm_client import LLMClient, extract_json, render_prompt
from core import prompts

logger = logging.getLogger(__name__)


# ─── 大纲角色检查 ───

def check_outline_characters(client: LLMClient, state: Progress,
                              settings: ProjectSettings, story_cfg: dict) -> OutlineCharacterReport:
    """检查大纲角色与已登记角色的一致性"""
    registered = "\n".join(f"- {c.name}" for c in settings.characters) if settings.characters else "（无已登记角色）"
    outline = ""
    for ch in state.chapters:
        outline += f"第{ch.num}章《{ch.title}》：{ch.outline}\n"
    accepted = ""
    for ch in state.chapters:
        if ch.status == ChapterStatus.ACCEPTED and ch.summary:
            accepted += f"第{ch.num}章《{ch.title}》：{ch.summary}\n"

    user_prompt = render_prompt(prompts.outline_character_check, {
        "Title": state.title,
        "Outline": outline,
        "RegisteredCharacters": registered,
        "AcceptedSummaries": accepted or "尚无已确认章节。",
    })
    raw = client.call("你是一位严谨的小说设定编辑。请以JSON格式返回。",
                      user_prompt, temperature=0.3, max_tokens=2048)
    data = json.loads(extract_json(raw))

    report = OutlineCharacterReport(
        has_suggestions=data.get("has_suggestions", False),
        summary=data.get("summary", ""),
    )
    for s in data.get("suggestions", []):
        report.suggestions.append(OutlineCharacterSuggestion(
            name=s.get("name", ""), chapter_num=s.get("chapter_num", 0),
            description=s.get("description", ""), role=s.get("role", ""),
        ))
    return report


# ─── 设定协调 ───

def reconcile_settings(client: LLMClient, story_cfg: dict, state: Progress,
                       new_settings: dict, settings: ProjectSettings) -> str:
    """改设定后自动协调已写内容"""
    accepted = ""
    for ch in state.chapters:
        if ch.status == ChapterStatus.ACCEPTED and ch.summary:
            accepted += f"第{ch.num}章《{ch.title}》摘要: {ch.summary}\n"
    if not accepted:
        accepted = "尚无已确认章节。"

    user_prompt = render_prompt(prompts.settings_reconciliation, {
        "NewType": new_settings.get("type", ""),
        "NewWritingStyle": new_settings.get("writing_style", ""),
        "NewWritingPOV": new_settings.get("writing_pov", ""),
        "NewStorySynopsis": new_settings.get("story_synopsis", ""),
        "ExistingSummaries": accepted,
    })
    raw = client.call("你是一位资深小说编辑。请以JSON格式返回。",
                      user_prompt, temperature=0.5, max_tokens=2048)
    data = json.loads(extract_json(raw))
    return data.get("explanation", "")


# ─── 全书优化 ───

def book_diagnosis(client: LLMClient, state: Progress, settings: ProjectSettings,
                    story_cfg: dict) -> str:
    """全书诊断"""
    settings_text = _build_settings_text(settings, story_cfg)
    summary_index = _build_summary_index(state)
    full_text = _build_full_text(state)

    user_prompt = render_prompt(prompts.book_diagnosis, {
        "ModeNote": "请通读全文输出诊断报告。",
        "SettingsText": settings_text,
        "SummaryIndex": summary_index,
        "FullText": full_text,
    })
    return client.call("你是一位资深网文总编辑。", user_prompt,
                        temperature=0.5, max_tokens=8192)


def book_consistency_check(client: LLMClient, state: Progress, settings: ProjectSettings,
                            story_cfg: dict) -> str:
    """全书一致性检查"""
    settings_text = _build_settings_text(settings, story_cfg)
    summary_index = _build_summary_index(state)
    full_text = _build_full_text(state)

    user_prompt = render_prompt(prompts.book_consistency_check, {
        "VolumeNote": "",
        "SettingsText": settings_text,
        "SummaryIndex": summary_index,
        "FullText": full_text,
    })
    return client.call("你是一位严谨的小说事实核查员。", user_prompt,
                        temperature=0.3, max_tokens=8192)


def book_roadmap(client: LLMClient, diagnosis: str, consistency: str) -> BookDiagnosis:
    """生成可执行的修改工单"""
    user_prompt = render_prompt(prompts.book_roadmap, {
        "DiagnosisReport": diagnosis,
        "ConsistencyReport": consistency,
    })
    raw = client.call("你是一位资深小说编辑。请以JSON格式返回。",
                      user_prompt, temperature=0.3, max_tokens=4096)
    data = json.loads(extract_json(raw))

    roadmap = BookDiagnosis()
    for item in data.get("items", []):
        roadmap.items.append(BookDiagnosisItem(
            chapter_num=item.get("chapter_num", 0),
            type=item.get("type", ""),
            priority=item.get("priority", "P2"),
            feedback=item.get("feedback", ""),
            selected=item.get("selected", True),
        ))
    return roadmap


# ─── 辅助函数 ───

def _build_settings_text(settings: ProjectSettings, story_cfg: dict) -> str:
    story = story_cfg.get("story", story_cfg)
    lines = [f"类型: {story.get('type','')}", f"风格: {story.get('writing_style','')}",
             f"视角: {story.get('writing_pov','')}", f"梗概: {story.get('story_synopsis','')}"]
    for c in settings.characters:
        lines.append(f"角色: {c.name} - {c.background}")
    return "\n".join(lines)


def _build_summary_index(state: Progress) -> str:
    lines = []
    for ch in state.chapters:
        lines.append(f"第{ch.num}章《{ch.title}》[{ch.status.value}]: {ch.summary}")
    return "\n".join(lines)


def _build_full_text(state: Progress) -> str:
    texts = []
    for ch in state.chapters:
        if ch.content:
            texts.append(f"=== 第{ch.num}章 {ch.title} ===\n{ch.content}")
    return "\n\n".join(texts)


# ─── SSE 日志 ───
