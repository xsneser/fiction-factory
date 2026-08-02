"""
写作引擎 — 对齐 show-me-the-story writing.go
"""
import json
import logging
from core.models import Progress, ChapterState, ChapterStatus, ProjectSettings, WritingConflict, ConflictActionOption
from core.llm_client import LLMClient, extract_json, render_prompt
from core.inject import (
    build_history_summary, build_previous_chapter_tail,
    build_outline_constraints, build_character_context,
    build_worldview_context, build_active_foreshadows,
    build_memory_context, strip_chapter_meta, count_prose_units
)
from core import prompts
from core.storage import save_progress, save_chapter_markdown

logger = logging.getLogger(__name__)


# ⚠️ 遗留路径：本文件是 show-me-the-story 移植的"逐章文本大纲"引擎（chapters[].outline 为每章一段文本）。
# 大纲的正确形态是"故事线"（BookTimeline 配置：大纲→桥段→笑点/内涵，一个 timeline.json），
# 由 libraries/outline_generator.py 生成、libraries/timeline_writer.py / engine._storyline_chapter_context 消费。
# 此路径仅作历史兼容保留，web_ui（主面板）不再使用。

def generate_outline(client: LLMClient, story_cfg: dict, settings: ProjectSettings,
                     state: Progress) -> tuple[str, str, str, list[ChapterState]]:
    """[遗留] 生成逐章文本大纲 → 返回 (标题, 核心提示词, 梗概, 章节列表)。已被故事线大纲替代。"""
    story = story_cfg.get("story", story_cfg)
    chapter_count = story.get("chapter_count", 12)
    target_words = story.get("target_words_per_chapter", 3000)

    user_prompt = render_prompt(prompts.outline_generation, {
        "StoryType": story.get("type", ""),
        "ChapterCount": str(chapter_count),
        "TargetWords": str(target_words),
        "WritingStyle": story.get("writing_style", ""),
        "WritingPOV": story.get("writing_pov", ""),
        "StorySynopsis": story.get("story_synopsis", ""),
        "CharacterList": _character_list_text(settings),
        "OutlineMinWords": "80", "OutlineMaxWords": "200",
    })

    system = "你是一位专业的小说策划编辑。请严格按照JSON格式返回，不要添加任何额外文字。"
    raw = client.call(system, user_prompt, temperature=0.8, max_tokens=8192)
    json_str = extract_json(raw)

    data = json.loads(json_str)
    title = data.get("title", "")
    core_prompt = data.get("core_prompt", "")
    synopsis = data.get("story_synopsis", "")
    chapters = []
    for i, ch in enumerate(data.get("chapters", []), 1):
        chapters.append(ChapterState(
            num=ch.get("num", i),
            title=ch.get("title", ""),
            outline=ch.get("outline", ""),
        ))
    return title, core_prompt, synopsis, chapters


def generate_chapter(client: LLMClient, story_cfg: dict, state: Progress,
                     settings: ProjectSettings, progress_path: str,
                     extra_constraints: str = "",
                     on_chunk=None) -> ChapterState:
    """生成一章正文"""
    idx = state.current_chapter_index
    ch = state.chapters[idx]
    story = story_cfg.get("story", story_cfg)
    lang = story_cfg.get("language", "zh")

    # 构建上下文
    history = build_history_summary(state, idx)
    prev_tail = build_previous_chapter_tail(state, idx)
    outline_constraints = build_outline_constraints(state, idx)
    char_context = build_character_context(settings, ch.outline)
    world_context = build_worldview_context(settings, ch.outline)
    foreshadow_ctx = build_active_foreshadows(state, ch.num)
    memory_ctx = build_memory_context(state)

    min_words = int(story.get("target_words_per_chapter", 3000) * 0.8)
    max_words = int(story.get("target_words_per_chapter", 3000) * 1.2)

    user_prompt = render_prompt(prompts.chapter_writing, {
        "Title": state.title or story.get("title", ""),
        "ChapterNum": str(ch.num),
        "CorePrompt": state.core_prompt,
        "StorySynopsis": state.story_synopsis or story.get("story_synopsis", ""),
        "HistorySummary": history,
        "PreviousEnding": prev_tail,
        "ChapterTitle": ch.title,
        "ChapterOutline": ch.outline,
        "WritingStyle": story.get("writing_style", ""),
        "WritingPOV": story.get("writing_pov", ""),
        "CharacterContext": char_context,
        "WorldviewContext": world_context,
        "TargetWords": str(story.get("target_words_per_chapter", 3000)),
        "TargetWordsMin": str(min_words),
        "TargetWordsMax": str(max_words),
        "Foreshadows": foreshadow_ctx,
        "Memory": memory_ctx,
        "OutlineConstraints": outline_constraints,
    })

    if extra_constraints:
        user_prompt += f"\n\n【补充写作约束（事实核查冲突调和）】\n{extra_constraints}"

    system = state.core_prompt or "你是一位专业的小说作者。请只输出小说正文，不要添加章节标题、章节号、'本章完'等任何元信息。"

    if on_chunk:
        content = client.call_stream(system, user_prompt, on_chunk=on_chunk,
                                     temperature=0.7, max_tokens=8192)
    else:
        content = client.call(system, user_prompt, temperature=0.7, max_tokens=8192)

    ch.content = strip_chapter_meta(content)
    ch.word_count = count_prose_units(ch.content)
    ch.status = ChapterStatus.REVIEW
    return ch


def generate_summary(client: LLMClient, content: str, language: str = "zh") -> str:
    """生成章节摘要"""
    user_prompt = render_prompt(prompts.chapter_summary, {"ChapterContent": content})
    system = "你是一位精准的小说叙事状态分析师。"
    raw = client.call(system, user_prompt, temperature=0.3, max_tokens=1024)
    return raw.strip()


def fact_check_chapter(client: LLMClient, state: Progress, idx: int,
                       content: str, story_cfg: dict) -> tuple[bool, str]:
    """事实核查 → (是否通过, 问题描述)"""
    ch = state.chapters[idx]
    history = build_history_summary(state, idx)
    outline_constraints = build_outline_constraints(state, idx)
    memory_ctx = build_memory_context(state)

    user_prompt = render_prompt(prompts.fact_check, {
        "ChapterContent": content,
        "HistorySummary": history,
        "ChapterOutline": ch.outline,
        "OutlineConstraints": outline_constraints,
        "Memory": memory_ctx,
        "CorePrompt": "",
    })

    system = "你是一位严谨的小说事实核查员。请以JSON格式返回：{\"result\": \"PASS\"} 或 {\"result\": \"FAIL\", \"issues\": [\"问题\"]}"
    raw = client.call(system, user_prompt, temperature=0.2, max_tokens=1024)

    try:
        data = json.loads(extract_json(raw))
        result = data.get("result", "PASS").upper()
        if result == "FAIL":
            issues = "；".join(data.get("issues", []))
            return False, issues
    except json.JSONDecodeError:
        if "FAIL" in raw:
            return False, raw[:300]
    return True, ""


def analyze_writing_conflict(client: LLMClient, state: Progress, idx: int,
                              content: str, issues: list[str],
                              failed_issues: list[str]) -> WritingConflict:
    """分析写作冲突根因"""
    ch = state.chapters[idx]
    history = build_history_summary(state, idx)
    outline_constraints = build_outline_constraints(state, idx)

    user_prompt = render_prompt(prompts.writing_conflict_analysis, {
        "ChapterNum": str(ch.num), "ChapterTitle": ch.title,
        "ChapterOutline": ch.outline, "HistorySummary": history,
        "OutlineConstraints": outline_constraints,
        "Foreshadows": build_active_foreshadows(state, ch.num),
        "FailedIssues": "\n".join(failed_issues),
        "ContentExcerpt": content[:2000],
    })

    system = "你是一位资深小说编辑。请以JSON格式返回。"
    raw = client.call(system, user_prompt, temperature=0.5, max_tokens=2048)

    try:
        data = json.loads(extract_json(raw))
    except json.JSONDecodeError:
        data = {"reconcilable": False, "summary": raw[:200], "root_cause": "other"}

    return WritingConflict(
        chapter_index=idx, chapter_num=ch.num, chapter_title=ch.title,
        issues=issues,
        summary=data.get("summary", ""),
        root_cause=data.get("root_cause", "other"),
        reconcilable=data.get("reconcilable", False),
        extra_constraints=data.get("extra_constraints", ""),
        suggested_actions=[
            ConflictActionOption(id=a.get("id", ""), label=a.get("label", ""),
                                 description=a.get("description", ""))
            for a in data.get("suggested_actions", [])
        ],
    )


def check_outline_consistency(client: LLMClient, state: Progress, idx: int,
                              story_cfg: dict) -> bool:
    """写前大纲一致性检查"""
    ch = state.chapters[idx]
    if not ch.outline.strip():
        return False

    prev_tail = build_previous_chapter_tail(state, idx)
    history = build_history_summary(state, idx)

    user_prompt = render_prompt(prompts.outline_consistency_check, {
        "ChapterNum": str(ch.num), "ChapterTitle": ch.title,
        "ChapterOutline": ch.outline, "HistorySummary": history,
        "PreviousEnding": prev_tail,
    })
    system = "你是一位严谨的小说策划编辑。请以JSON格式返回。"
    raw = client.call(system, user_prompt, temperature=0.3, max_tokens=2048)

    try:
        data = json.loads(extract_json(raw))
        if data.get("conflict", False):
            revised = data.get("revised_outline", "")
            if revised.strip():
                ch.outline = revised.strip()
                return True
    except json.JSONDecodeError:
        pass
    return False


def _character_list_text(settings: ProjectSettings) -> str:
    if not settings or not settings.characters:
        return "(尚无已登记角色)"
    return "\n".join(f"- {c.name}: {c.background}" for c in settings.characters[:20])


def generate_chapter_full_pipeline(client: LLMClient, story_cfg: dict,
                                    state: Progress, settings: ProjectSettings,
                                    progress_path: str, project_dir: str,
                                    on_chunk=None) -> dict:
    """完整的章节生成管线：一致性检查 → 写作 → 摘要 → 事实核查（最多3次重试）"""
    idx = state.current_chapter_index
    ch = state.chapters[idx]
    ch.status = ChapterStatus.WRITING
    save_progress(progress_path, state)

    # 1. 写前一致性检查
    if idx > 0:
        check_outline_consistency(client, state, idx, story_cfg)
        save_progress(progress_path, state)

    max_retries = 3
    issues_accumulated = []
    extra_constraints = ""

    for attempt in range(max_retries + 1):
        # 2. 生成正文
        ch = generate_chapter(client, story_cfg, state, settings,
                              progress_path, extra_constraints, on_chunk)
        logger.info(f"第{ch.num}章正文生成完成，{ch.word_count}字")

        # 3. 生成摘要
        ch.summary = generate_summary(client, ch.content)
        logger.info(f"第{ch.num}章摘要提取完成")

        # 4. 事实核查
        passed, issues = fact_check_chapter(client, state, idx, ch.content, story_cfg)

        if passed:
            logger.info(f"第{ch.num}章事实核查通过")
            break

        issues_accumulated.append(issues)
        if attempt < max_retries:
            logger.warning(f"第{ch.num}章事实核查失败（第{attempt+1}次），重试中...")
            continue

        # 全部失败，分析冲突
        logger.warning(f"第{ch.num}章事实核查{max_retries}次失败，分析冲突...")
        conflict = analyze_writing_conflict(client, state, idx, ch.content,
                                            issues_accumulated, issues_accumulated)
        state.pending_writing_conflict = conflict

        # 调和可行且 LLM 给出了补充约束：带着约束重写一次正文
        if conflict.reconcilable and conflict.extra_constraints:
            ch = generate_chapter(client, story_cfg, state, settings,
                                  progress_path, conflict.extra_constraints, on_chunk)
            ch.summary = generate_summary(client, ch.content)
            passed, _ = fact_check_chapter(client, state, idx, ch.content, story_cfg)
            if passed:
                break

    state.current_chapter_index = idx
    save_progress(progress_path, state)
    save_chapter_markdown(project_dir, ch, state.title)

    return {"chapter": ch, "conflict": state.pending_writing_conflict}


def confirm_chapter(state: Progress, progress_path: str):
    """确认当前章节"""
    idx = state.current_chapter_index
    if idx >= len(state.chapters):
        return
    ch = state.chapters[idx]
    if ch.status != ChapterStatus.REVIEW:
        return
    ch.status = ChapterStatus.ACCEPTED
    state.current_chapter_index = idx + 1
    save_progress(progress_path, state)

def revise_chapter(client: LLMClient, story_cfg: dict, state: Progress,
                   settings: ProjectSettings, progress_path: str,
                   chapter_num: int, feedback: str) -> ChapterState:
    """按用户反馈定向修订一章：仅修改涉及部分，其余原文保持不变。

    使用 prompts.chapter_revision 模板（局部修订，不是整章重写）。
    """
    idx = next((i for i, c in enumerate(state.chapters) if c.num == chapter_num), -1)
    if idx < 0:
        raise ValueError(f"章节 {chapter_num} 不存在")
    ch = state.chapters[idx]
    if not ch.content:
        raise ValueError(f"章节 {chapter_num} 无正文可修订")

    story = story_cfg.get("story", story_cfg)
    user_prompt = render_prompt(prompts.chapter_revision, {
        "ChapterNum": str(ch.num),
        "ChapterTitle": ch.title,
        "CorePrompt": state.core_prompt,
        "HistorySummary": build_history_summary(state, idx),
        "WritingStyle": story.get("writing_style", ""),
        "WritingPOV": story.get("writing_pov", ""),
        "CharacterContext": build_character_context(settings, ch.outline),
        "WorldviewContext": build_worldview_context(settings, ch.outline),
        "OriginalContent": ch.content,
        "UserFeedback": feedback,
    })
    system = state.core_prompt or "你是一位专业的小说作者。请只输出修订后的章节正文。"
    content = client.call(system, user_prompt, temperature=0.5, max_tokens=8192)

    ch.content = strip_chapter_meta(content)
    ch.word_count = count_prose_units(ch.content)
    ch.status = ChapterStatus.REVIEW
    save_progress(progress_path, state)
    return ch
