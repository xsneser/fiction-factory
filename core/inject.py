"""
上下文注入 — 对齐 show-me-the-story inject.go
将项目状态（前情、大纲、角色、伏笔、记忆）注入写作 prompt
"""
from core.models import Progress, ProjectSettings, ChapterState, ForeshadowStatus


def build_history_summary(state: Progress, idx: int) -> str:
    """前情提要：最近 5 章摘要"""
    start = max(0, idx - 5)
    lines = []
    for i in range(start, idx):
        ch = state.chapters[i]
        if ch.summary:
            lines.append(f"[第{ch.num}章摘要]: {ch.summary}")
    return "\n".join(lines) if lines else "当前为故事开端，无历史前情。"


def build_previous_chapter_tail(state: Progress, idx: int, max_chars: int = 800) -> str:
    """上一章结尾原文（800字，用于无缝承接）"""
    if idx <= 0 or idx > len(state.chapters):
        return ""
    prev = state.chapters[idx - 1]
    if not prev.content:
        return ""
    tail = prev.content[-max_chars:]
    # 对齐到段落边界
    if "\n" in tail and len(tail) > 100:
        tail = tail.split("\n", 1)[-1] if tail[0] == "\n" else "\n".join(tail.split("\n")[1:])
    return f"【上一章结尾原文（仅供无缝承接场景与情绪，禁止复述或改写）】\n{tail.strip()}\n\n"


def build_outline_constraints(state: Progress, idx: int, future_window: int = 10) -> str:
    """全书章节脉络（反向约束）"""
    past, future = [], []
    end = min(idx + 1 + future_window, len(state.chapters))
    for i in range(idx):
        ch = state.chapters[i]
        if ch.outline.strip():
            past.append(f"第{ch.num}章《{ch.title}》：{ch.outline}")
    for i in range(idx + 1, end):
        ch = state.chapters[i]
        if ch.outline.strip():
            future.append(f"第{ch.num}章《{ch.title}》：{ch.outline}")
    if not past and not future:
        return ""
    parts = ["【全书章节脉络（反向约束，必须严格遵守）】"]
    if future:
        parts.append("◆ 后续章节安排——以下人物登场、初遇、身份揭示等事件已安排在对应章节，"
                     "本章严禁提前发生，也不得以任何形式暗示或剧透：")
        parts.extend(future)
    if past:
        parts.append("◆ 前文已发生——以下事件已经发生，本章不得将其作为新事件重复发生"
                     "（尤其是初次见面、身份揭示等一次性事件，只能作为既成事实延续）：")
        parts.extend(past)
    return "\n".join(parts) + "\n"


def build_character_context(settings: ProjectSettings, chapter_outline: str) -> str:
    """注入相关角色设定"""
    if not settings or not settings.characters:
        return ""
    # 筛选大纲中提到的角色
    relevant = [c for c in settings.characters if c.name in chapter_outline]
    if not relevant:
        relevant = settings.characters
    parts = []
    for c in relevant:
        lines = [f"【{c.name}】"]
        if c.age:
            lines.append(f"  年龄:{c.age}")
        for label, val in [("外貌", c.appearance), ("性格", c.personality),
                           ("背景", c.background), ("动机", c.motivation),
                           ("能力", c.abilities), ("备注", c.notes)]:
            if val:
                lines.append(f"  {label}: {val}")
        parts.append("\n".join(lines) + "\n")
    return "\n".join(parts)


def build_worldview_context(settings: ProjectSettings, chapter_outline: str) -> str:
    """注入世界观和组织设定"""
    if not settings:
        return ""
    parts = []
    for w in settings.worldview:
        if w.name in chapter_outline or not chapter_outline.strip():
            parts.append(f"【{w.name}】({w.category})\n  {w.description}\n")
    for o in settings.organizations:
        if o.name in chapter_outline or not chapter_outline.strip():
            parts.append(f"【组织:{o.name}】({o.type})\n  {o.description}")
            if o.members:
                parts[-1] += f"\n  成员IDs: {', '.join(o.members)}"
            parts[-1] += "\n"
    return "\n".join(parts)


def build_active_foreshadows(state: Progress, chapter_num: int) -> str:
    """注入活跃伏笔"""
    active = [f for f in state.foreshadows
              if f.status in (ForeshadowStatus.PLANTED, ForeshadowStatus.PROGRESSING)]
    if not active:
        return ""
    parts = ["【活跃伏笔（写作时必须注意推进或回收）】"]
    for f in active:
        parts.append(f'#{f.id} "{f.name}" [第{f.plant_chapter}章埋设'
                     + (f'，预计第{f.target_chapter}章回收' if f.target_chapter else '')
                     + ']')
        parts.append(f"   描述: {f.description}")
        if f.events:
            parts.append("   已有进展:")
            for e in f.events:
                parts.append(f"   - 第{e.chapter}章: {e.note}")
        # 超期告警
        if f.target_chapter and chapter_num >= f.target_chapter:
            parts.append(f"   ⚠️ 该伏笔已超过预计回收章节，本章应优先考虑回收")
        elif f.target_chapter and chapter_num >= f.target_chapter - 2:
            parts.append(f"   → 接近预计回收节点，可开始收束")
        parts.append("")
    return "\n".join(parts)


def build_memory_context(state: Progress) -> str:
    """注入叙事记忆"""
    if not state.memory_entries:
        return ""
    parts = ["【叙事记忆——早期章节的关键叙事细节】"]
    for m in state.memory_entries:
        parts.append(f"[第{m.chapter}章] {m.content}")
    return "\n".join(parts) + "\n"


def strip_chapter_meta(content: str) -> str:
    """去除 AI 生成的元信息（"本章完""欲知后事如何"等）"""
    lines = content.strip().split("\n")
    meta_exact = {"本章完", "本章终", "待续", "未完待续", "（完）", "(完)", "完",
                  "——", "—", "***", "---"}
    while lines and (lines[0].strip() in meta_exact or
                     lines[0].strip().startswith("第") and "章" in lines[0]):
        lines.pop(0)
    while lines and (lines[-1].strip() in meta_exact or
                     "欲知后事" in lines[-1] or "下章预告" in lines[-1]):
        lines.pop()
    return "\n".join(lines).strip()


def count_prose_units(text: str) -> int:
    """中文字数统计（中文按字，英文按词）"""
    import re
    chinese = len(re.findall(r'[\u4e00-\u9fff]', text))
    english_words = len(re.findall(r'[a-zA-Z]+', text))
    return chinese + english_words
