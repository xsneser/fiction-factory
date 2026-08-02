"""
伏笔系统 — 对齐 show-me-the-story foreshadow.go
"""
import json
import logging
from datetime import datetime
from core.models import (
    Progress, Foreshadow, ForeshadowStatus, ForeshadowEvent,
    ForeshadowOutlineReport, ForeshadowOutlineConflict
)
from core.llm_client import LLMClient, extract_json, render_prompt
from core import prompts
from core.inject import build_history_summary

logger = logging.getLogger(__name__)


def suggest_foreshadows(client: LLMClient, state: Progress, language: str = "zh") -> list[dict]:
    """AI 建议伏笔方案"""
    outline_text = ""
    for ch in state.chapters:
        outline_text += f"第{ch.num}章《{ch.title}》：{ch.outline}\n"

    user_prompt = render_prompt(prompts.foreshadow_planning, {
        "Title": state.title,
        "CorePrompt": state.core_prompt,
        "StorySynopsis": state.story_synopsis,
        "Outline": outline_text,
    })
    system = "你是一位资深的小说叙事架构师。请以JSON格式返回：{\"foreshadows\": [...]}"
    raw = client.call(system, user_prompt, temperature=0.7, max_tokens=4096)
    data = json.loads(extract_json(raw))
    return data.get("foreshadows", [])


def update_foreshadows_after_chapter(client: LLMClient, state: Progress,
                                     chapter_idx: int, language: str = "zh"):
    """更新伏笔状态（章节写完调用）"""
    if not state.foreshadows:
        return
    ch = state.chapters[chapter_idx]
    fs_text = _format_foreshadows_for_prompt(state.foreshadows)

    user_prompt = render_prompt(prompts.foreshadow_update, {
        "Title": state.title,
        "ChapterNum": str(ch.num),
        "ChapterTitle": ch.title,
        "ChapterContent": ch.content,
        "HistorySummary": build_history_summary(state, chapter_idx),
        "Foreshadows": fs_text,
    })
    system = "你是一位严谨的小说伏笔追踪员。请以JSON格式返回：{\"updates\": [...]}"
    raw = client.call(system, user_prompt, temperature=0.3, max_tokens=2048)
    try:
        data = json.loads(extract_json(raw))
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("伏笔更新输出解析失败，跳过本章更新: %s", e)
        return
    _apply_foreshadow_updates(state, data.get("updates", []), ch.num)


def _apply_foreshadow_updates(state: Progress, updates: list[dict], chapter_num: int):
    update_map = {u["id"]: u for u in updates if "id" in u}
    for fs in state.foreshadows:
        u = update_map.get(fs.id)
        if not u:
            continue
        if u.get("event"):
            fs.events.append(ForeshadowEvent(chapter=chapter_num, note=u["event"]))
        if u.get("status"):
            try:
                fs.status = ForeshadowStatus(u["status"])
            except ValueError as e:
                logger.warning("非法伏笔状态 %r 已忽略: %s", u["status"], e)
        if u.get("resolution"):
            fs.resolution = u["resolution"]


def check_foreshadow_outline_consistency(client: LLMClient, state: Progress,
                                         story_cfg: dict) -> ForeshadowOutlineReport:
    """检查伏笔与大纲的一致性"""
    if not state.foreshadows:
        return ForeshadowOutlineReport(summary="无伏笔")

    outline = ""
    for ch in state.chapters:
        outline += f"第{ch.num}章《{ch.title}》：{ch.outline}\n"
    accepted = ""
    for ch in state.chapters:
        if ch.status.value == "accepted" and ch.summary:
            accepted += f"第{ch.num}章《{ch.title}》：{ch.summary}\n"

    user_prompt = render_prompt(prompts.foreshadow_outline_consistency, {
        "Title": state.title,
        "Outline": outline,
        "Foreshadows": _format_foreshadows_for_prompt(state.foreshadows),
        "AcceptedSummaries": accepted or "尚无已确认章节。",
    })
    system = "你是一位严谨的小说编辑。请以JSON格式返回。"
    raw = client.call(system, user_prompt, temperature=0.3, max_tokens=2048)
    data = json.loads(extract_json(raw))

    report = ForeshadowOutlineReport(
        has_conflicts=data.get("has_conflicts", False),
        summary=data.get("summary", ""),
    )
    for c in data.get("conflicts", []):
        report.conflicts.append(ForeshadowOutlineConflict(
            foreshadow_id=c.get("foreshadow_id", 0),
            foreshadow_name=c.get("foreshadow_name", ""),
            conflict_type=c.get("conflict_type", ""),
            description=c.get("description", ""),
            suggested_fix=c.get("suggested_fix", ""),
        ))
    return report


def _format_foreshadows_for_prompt(foreshadows: list[Foreshadow]) -> str:
    if not foreshadows:
        return "无"
    lines = []
    for fs in foreshadows:
        lines.append(f"#{fs.id} [{fs.status.value}] {fs.name}")
        lines.append(f"   描述: {fs.description}")
        lines.append(f"   埋设于: 第{fs.plant_chapter}章"
                     + (f"，预计回收: 第{fs.target_chapter}章" if fs.target_chapter else ""))
        if fs.events:
            lines.append("   已有进展:")
            for e in fs.events:
                lines.append(f"   - 第{e.chapter}章: {e.note}")
        if fs.resolution:
            lines.append(f"   回收方式: {fs.resolution}")
        lines.append("")
    return "\n".join(lines)


def next_foreshadow_id(foreshadows: list[Foreshadow]) -> int:
    max_id = max((f.id for f in foreshadows), default=0)
    return max_id + 1


def build_foreshadow_roadmap(state: Progress) -> str:
    """生成伏笔路线图 Markdown"""
    title = state.title or "未命名小说"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"# 伏笔路线图 — 《{title}》", f"> 更新时间：{now}", ""]

    if not state.foreshadows:
        lines.append("当前尚无伏笔记录。")
        return "\n".join(lines)

    active = sum(1 for f in state.foreshadows
                 if f.status in (ForeshadowStatus.PLANTED, ForeshadowStatus.PROGRESSING))
    resolved = sum(1 for f in state.foreshadows if f.status == ForeshadowStatus.RESOLVED)
    abandoned = sum(1 for f in state.foreshadows if f.status == ForeshadowStatus.ABANDONED)

    lines.append("## 概览")
    lines.append(f"- 总计 **{len(state.foreshadows)}** 条 | 活跃 **{active}** | 已回收 **{resolved}** | 已放弃 **{abandoned}**")
    lines.append("")

    # 按章节时间线
    max_ch = max(
        max((f.plant_chapter for f in state.foreshadows), default=0),
        max((f.target_chapter for f in state.foreshadows), default=0),
        max((e.chapter for f in state.foreshadows for e in f.events), default=0),
    )
    if max_ch:
        lines.append("## 按章节时间线")
        for ch_num in range(1, max_ch + 1):
            events = []
            for fs in state.foreshadows:
                if fs.plant_chapter == ch_num:
                    events.append(f"- 🔵 **#{fs.id} {fs.name}** — 埋设（{_status_label(fs.status)}）")
                if fs.target_chapter == ch_num:
                    events.append(f"- 🎯 **#{fs.id} {fs.name}** — 预计回收")
                for e in fs.events:
                    if e.chapter == ch_num:
                        events.append(f"- 📌 **#{fs.id} {fs.name}** — {e.note}")
            if events:
                lines.append(f"### 第 {ch_num} 章")
                lines.extend(events)
                lines.append("")

    lines.append("## 伏笔详情")
    for fs in state.foreshadows:
        lines.append(f"### #{fs.id} {fs.name} [{_status_label(fs.status)}]")
        lines.append(f"{fs.description}")
        lines.append(f"- 埋设章节：第 **{fs.plant_chapter}** 章")
        if fs.target_chapter:
            lines.append(f"- 预计回收：第 **{fs.target_chapter}** 章")
        if fs.events:
            lines.append("- 进展记录：")
            for e in fs.events:
                lines.append(f"  - 第 {e.chapter} 章：{e.note}")
        if fs.resolution:
            lines.append(f"- 回收方式：{fs.resolution}")
        lines.append("")

    return "\n".join(lines)


def _status_label(status: ForeshadowStatus) -> str:
    return {ForeshadowStatus.PLANTED: "已埋设", ForeshadowStatus.PROGRESSING: "推进中",
            ForeshadowStatus.RESOLVED: "已回收", ForeshadowStatus.ABANDONED: "已放弃"}.get(status, status.value)
