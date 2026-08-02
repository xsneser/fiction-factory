"""
卷/Arc 系统 — 对齐 show-me-the-story arcs.go
超长篇分卷：卷骨架 → 逐卷生成章纲 → 卷摘要压缩 → 追加新卷
"""
import json
import logging
from core.models import Progress, Arc, ChapterState, ProjectSettings
from core.llm_client import LLMClient, extract_json, render_prompt
from core import prompts

logger = logging.getLogger(__name__)


def arc_index_by_id(state: Progress, arc_id: int) -> int:
    for i, a in enumerate(state.arcs):
        if a.id == arc_id:
            return i
    return -1


def _arc_for_chapter_num(state: Progress, num: int) -> Arc | None:
    for a in state.arcs:
        if a.start_ch <= num <= a.end_ch:
            return a
    return None


def _build_previous_arc_context(state: Progress, start_ch: int) -> str:
    lines = []
    for i, arc in enumerate(state.arcs):
        if arc.end_ch >= start_ch or not arc.summary:
            continue
        lines.append(f"【第{i+1}卷《{arc.title}》（第{arc.start_ch}~{arc.end_ch}章）卷摘要】\n{arc.summary}\n")
    # 前卷末尾章节摘要
    tail = []
    for ch in state.chapters:
        if ch.num >= start_ch or ch.num < start_ch - 5:
            continue
        if ch.summary:
            tail.append(f"[第{ch.num}章摘要] {ch.summary}")
        elif ch.outline.strip():
            tail.append(f"第{ch.num}章《{ch.title}》：{ch.outline}")
    if tail:
        lines.append("\n".join(tail))
    return "\n".join(lines) if lines else "当前为故事开端，无历史前情。"


def _build_future_arcs_block(state: Progress, arc_idx: int) -> str:
    lines = []
    for i in range(arc_idx + 1, len(state.arcs)):
        a = state.arcs[i]
        lines.append(f"第{i+1}卷《{a.title}》（第{a.start_ch}~{a.end_ch}章）：{a.goal}")
    return "\n".join(lines) if lines else "（本卷为最后一卷）"


def generate_arc_skeleton(client: LLMClient, story_cfg: dict, state: Progress,
                           settings: ProjectSettings) -> list[Arc]:
    """生成卷级骨架"""
    story = story_cfg.get("story", story_cfg)
    char_list = "\n".join(f"- {c.name}" for c in settings.characters[:20]) if settings.characters else "（无）"

    user_prompt = render_prompt(prompts.arc_skeleton, {
        "StoryType": story.get("type", ""),
        "ChapterCount": str(story.get("chapter_count", 30)),
        "TargetWords": str(story.get("target_words_per_chapter", 3000)),
        "WritingStyle": story.get("writing_style", ""),
        "WritingPOV": story.get("writing_pov", ""),
        "StorySynopsis": story.get("story_synopsis", ""),
        "CharacterList": char_list,
    })
    system = "你是一位资深小说策划编辑。请以JSON格式返回。"
    raw = client.call(system, user_prompt, temperature=0.8, max_tokens=8192)
    data = json.loads(extract_json(raw))

    arcs_data = data.get("arcs", [])
    counts = [max(a.get("chapter_count", 10), 1) for a in arcs_data]
    total = story.get("chapter_count", 30)
    # 最后卷吸收漂移
    drift = total - sum(counts)
    counts[-1] += drift
    if counts[-1] < 1:
        counts[-1] = 1

    cur = 1
    arcs = []
    for i, a in enumerate(arcs_data):
        end = cur + counts[i] - 1
        arcs.append(Arc(id=i + 1, title=a.get("title", f"第{i+1}卷"),
                        goal=a.get("goal", ""), start_ch=cur, end_ch=end))
        cur = end + 1
    return arcs


def generate_arc_outline(client: LLMClient, story_cfg: dict, state: Progress,
                         settings: ProjectSettings, arc_id: int,
                         requirements: str = "") -> list[ChapterState]:
    """为指定卷生成逐章大纲"""
    ai = arc_index_by_id(state, arc_id)
    if ai < 0:
        raise ValueError(f"卷 {arc_id} 不存在")
    arc = state.arcs[ai]
    story = story_cfg.get("story", story_cfg)

    user_prompt = render_prompt(prompts.arc_chapter_outline, {
        "Title": state.title or story.get("title", ""),
        "StoryType": story.get("type", ""),
        "CorePrompt": state.core_prompt,
        "StorySynopsis": state.story_synopsis or story.get("story_synopsis", ""),
        "WritingStyle": story.get("writing_style", ""),
        "WritingPOV": story.get("writing_pov", ""),
        "PreviousContext": _build_previous_arc_context(state, arc.start_ch),
        "ArcIndex": str(ai + 1),
        "ArcTitle": arc.title,
        "ArcGoal": arc.goal,
        "FutureArcs": _build_future_arcs_block(state, ai),
        "UserRequirements": requirements or "（无）",
        "NewChapterCount": str(arc.end_ch - arc.start_ch + 1),
        "StartNum": str(arc.start_ch),
        "EndNum": str(arc.end_ch),
        "CharacterList": "\n".join(f"- {c.name}: {c.background}" for c in settings.characters[:20]) if settings.characters else "（无）",
        "OutlineMinWords": "80", "OutlineMaxWords": "200",
    })
    system = "你是一位专业的小说策划编辑。请以JSON格式返回。"
    raw = client.call(system, user_prompt, temperature=0.8, max_tokens=8192)
    data = json.loads(extract_json(raw))

    chapters = []
    for i, ch_data in enumerate(data.get("chapters", [])):
        num = arc.start_ch + i
        if num > arc.end_ch:
            break
        chapters.append(ChapterState(
            num=num, title=ch_data.get("title", ""),
            outline=ch_data.get("outline", ""),
        ))
    return chapters


def append_arc(client: LLMClient, story_cfg: dict, state: Progress,
               settings: ProjectSettings, title: str, goal: str,
               chapter_count: int = 20) -> Arc:
    """追加新卷并生成章纲"""
    start_ch = state.arcs[-1].end_ch + 1 if state.arcs else (
        state.chapters[-1].num + 1 if state.chapters else 1
    )
    max_id = max((a.id for a in state.arcs), default=0)
    arc = Arc(id=max_id + 1, title=title or f"第{len(state.arcs)+1}卷",
              goal=goal, start_ch=start_ch, end_ch=start_ch + chapter_count - 1)
    state.arcs.append(arc)
    chapters = generate_arc_outline(client, story_cfg, state, settings, arc.id, goal)
    # 保留已有章节，替换该卷范围
    kept = [c for c in state.chapters if c.num < arc.start_ch or c.num > arc.end_ch]
    kept.extend(chapters)
    kept.sort(key=lambda c: c.num)
    state.chapters = kept
    return arc
