"""NovelEngine 共享 Agent 工具注册表 — 全链路操作（创建→上架）+ 导航/向导控制。

单一工具来源：MCP 服务器（mcp_server.py 适配层）与侧栏 dsh 桥
（libraries/dsh_bridge.py，经 MCP 驱动）都从这里取 TOOL_REGISTRY。

工具函数复用 ui.web_blueprints.ctx 单例：Web 进程内与 UI 共享同一份状态
（book_mgr/引擎会话 cont_<book_id>/storyline 缓存）；MCP 是独立进程，import 时
各建一份，通过 books/ 文件 JSON 协调，行为不回归。

工具返回约定：
  - navigate 返回特殊标记 {"__navigate__": url}，MCP 适配层据此落意图队列驱动浏览器。
"""
import os
import sys
import json
import time
import inspect
import typing
import functools

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

from ui.web_blueprints.ctx import (  # noqa: E402
    plot_lib, struct_lib, gag_lib, char_lib, profiles, book_mgr,
    get_llm, _engines, _resolve_storyline, _save_storyline,
    NovelEngine, BookStoryline, StorylineBuilder,
    ContentReviewer, DeAIEngine,
)
from core.text_utils import count_prose_units  # noqa: E402
from libraries.storyline import OutlineSlot, annotate_plot_roles, \
    get_mc, get_characters, normalize_basic_info  # noqa: E402
from libraries.book_lock import BookLock, BookBusyError  # noqa: E402
from libraries.tool_policy import _wrap_phase_gate  # noqa: E402


# ─── 基础辅助 ───

def _require_llm():
    llm = get_llm()
    if not llm:
        raise RuntimeError("LLM 未配置：请在设置页保存 API 配置（或 api.json 填 api_key）")
    return llm


def load_tl(book_id: str):
    """读故事线（走 ctx 缓存，与 Web 共享）。"""
    return _resolve_storyline(book_id)


def save_tl(book_id: str, tl) -> None:
    """写故事线（走 ctx 缓存 + 落盘，与 Web 共享）。"""
    _save_storyline(tl, book_id)


def _require_tl(book_id: str):
    tl = load_tl(book_id)
    if tl is None:
        raise RuntimeError(f"「{book_id}」无故事线（storyline.json）。"
                           "请先经「启动新书」向导建书 + save_basic_info / generate_world 生成设定，"
                           "再 generate_full_outline 生成大纲。")
    return tl


def get_engine(book_id: str):
    """进程内引擎会话，键 cont_<book_id>，与写作台共享同一 NovelEngine 实例。"""
    key = f"cont_{book_id}"
    if key not in _engines:
        e = NovelEngine(llm_client=_require_llm())
        try:
            e.continue_book(book_id)
        except ValueError as ex:
            raise RuntimeError(f"「{book_id}」无法进入写作：{ex}\n"
                               "请先用 save_basic_info / generate_world / "
                               "generate_full_outline 生成设定与大纲。") from ex
        _engines[key] = e
    return _engines[key]


def _drop_engine(book_id: str) -> None:
    """使该书引擎会话过期（规划/编辑类改动后调用）。"""
    _engines.pop(f"cont_{book_id}", None)


def _snapshot(book_id: str) -> dict:
    """book.json 当前进度快照（各写入工具返回前补上）。"""
    b = book_mgr.get(book_id)
    if not b:
        return {}
    return {
        "current_chapter": b.current_chapter,
        "chapter_count": b.chapter_count,
        "status": b.status,
        "total_words": b.total_words or 0,
    }


def _draft_read(book_id: str):
    p = os.path.join(_ROOT, "books", book_id, "draft_chapter.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _profile_for(tl):
    if tl and tl.pen_name:
        try:
            return profiles.get_by_name(tl.pen_name)
        except Exception:
            return None
    return None


def _max_id_suffix(ids) -> int:
    import re as _re
    max_n = 0
    for i in ids:
        m = _re.search(r"_(\d+)$", i or "")
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n


def _seed_builder_counter(builder, ids) -> None:
    builder._counter = _max_id_suffix(ids)


def _build_next_arc(builder, tl, mode="rule"):
    """故事线末尾追加下一段大纲弧（rule=模板循环；ai=单弧 LLM 再锚定）。镜像 storyline.py。"""
    if mode == "ai":
        seq = builder.build_outline_sequence(
            genre=tl.genre, sub_genre=tl.sub_genre,
            custom_context=(tl.basic_info or {}).get("world_building", {}).get("description", ""),
            max_outlines=1, mode="ai")
        if not seq:
            return None
        arc = seq[0]
        max_end = max((o.end_chapter for o in tl.outlines), default=0)
        span = max(arc.end_chapter - arc.start_chapter + 1, 20)
        arc.start_chapter = max_end + 1
        arc.end_chapter = arc.start_chapter + span - 1
        if tl.outlines:
            arc.predecessor = tl.outlines[-1].id
            tl.outlines[-1].successor = arc.id
        return arc

    structs = struct_lib.search(genre=tl.genre) or struct_lib.templates
    if not structs:
        return None
    idx = len(tl.outlines) % len(structs)
    tmpl = structs[idx]
    max_end = max((o.end_chapter for o in tl.outlines), default=0)
    start = max_end + 1
    span = min(tmpl.total_chapters, 60)
    arc = OutlineSlot(
        id=builder._next_id("outline"),
        template_id=tmpl.id,
        name=f"{tmpl.name}(第{len(tl.outlines) + 1}部分)",
        start_chapter=start,
        end_chapter=start + span - 1,
        stages=[{"name": s.name, "min_ch": s.min_chapters, "max_ch": s.max_chapters,
                 "events": s.key_events[:5]}
                for s in tmpl.stages],
        predecessor=tl.outlines[-1].id if tl.outlines else "",
        transition_type="sequential",
    )
    if tl.outlines:
        tl.outlines[-1].successor = arc.id
    return arc


# ─── 阻塞式消费生成器（把 SSE 流式改造成同步结果）───

def consume_dict_stream(gen):
    """迭代 yield-dict 生成器到完成；error 事件/异常转 RuntimeError。返回 (last_event, events)。"""
    last, events = None, []
    for evt in gen:
        if isinstance(evt, dict) and evt.get("type") == "error":
            raise RuntimeError(evt.get("message", "LLM 生成失败"))
        events.append(evt)
        last = evt
    return last, events


def consume_triple_stream(gen):
    """迭代 (event_type, message, data_dict) 生成器；error 事件转 RuntimeError。返回 (last_t, last_d, events)。"""
    last_t, last_d, events = None, None, []
    for t, msg, d in gen:
        if t == "error":
            raise RuntimeError(msg or "LLM 生成失败")
        events.append((t, msg, d))
        last_t, last_d = t, d
    return last_t, last_d, events


# ═══════════════════════════════════════════════════
# 只读 / 建书类（无 LLM，供上下文供给与测试）
# ═══════════════════════════════════════════════════

def list_books() -> list:
    """列出书库全部书籍的摘要（book_id/书名/流派/状态/进度）。"""
    rows = []
    for b in book_mgr.list_all():
        rows.append({
            "book_id": b.book_id,
            "title": b.title,
            "pen_name": b.pen_name,
            "genre": b.genre,
            "sub_genre": b.sub_genre,
            "status": b.status,
            "current_chapter": b.current_chapter,
            "chapter_count": b.chapter_count,
            "total_words": b.total_words or 0,
        })
    return rows


def get_book_state(book_id: str) -> dict:
    """读取一本书的完整状态：book 配置、故事线、结构大纲、章节摘要、进行中草稿。"""
    book = book_mgr.get(book_id)
    if not book:
        raise RuntimeError(f"书 {book_id} 不存在")
    tl = book_mgr.load_storyline(book_id)
    outline = book_mgr.get_outline(book_id)
    chapters = []
    for n in range(1, book.current_chapter + 2):
        ch = book_mgr.load_chapter(book_id, n)
        if ch:
            chapters.append({
                "num": n,
                "title": ch.get("title"),
                "summary": ch.get("summary"),
                "word_count": count_prose_units(ch.get("content") or ""),
            })
    return {
        "book": {
            "book_id": book.book_id, "title": book.title, "pen_name": book.pen_name,
            "genre": book.genre, "sub_genre": book.sub_genre, "platform": book.platform,
            "status": book.status, "current_chapter": book.current_chapter,
            "chapter_count": book.chapter_count, "total_words": book.total_words or 0,
        },
        "storyline": tl.to_dict() if tl else None,
        "outline": outline,
        "chapters": chapters,
        "draft": _draft_read(book_id),
    }


def get_storyline(book_id: str) -> dict:
    """读取一本书的故事线（timeline）JSON：大纲/桥段/线程/内涵/基础设定。"""
    tl = _require_tl(book_id)
    return tl.to_dict()




def borrow_preview(source_book_id: str) -> dict:
    """预览将借鉴源书的哪些设定（从已有书 basic_info 抽取种子）。"""
    src = load_tl(source_book_id)
    if not src:
        raise RuntimeError(f"源书 {source_book_id} 不存在或无故事线")
    from libraries.world_builder import WorldBuildingGenerator
    seed = WorldBuildingGenerator.extract_seed(src.basic_info)
    if not seed:
        raise RuntimeError("源书没有可借鉴的设定")
    return {"seed": seed,
            "source_title": src.book_title or src.pen_name or source_book_id,
            "source_genre": src.genre}


# ═══════════════════════════════════════════════════
# 信息工具层（P4，只读、无副作用，供外部 agent 选材/续写/上架决策）
# ═══════════════════════════════════════════════════

def get_book_detail(book_id: str) -> dict:
    """读取一本书的完整详情：书名/简介/角色/世界观/进度（供外部 agent 决策）。"""
    book = book_mgr.get(book_id)
    if not book:
        raise RuntimeError(f"书 {book_id} 不存在")
    tl = book_mgr.load_storyline(book_id)
    bi = (tl.basic_info or {}) if tl else {}
    chars = get_characters(bi) or []
    outline = book_mgr.get_outline(book_id) or {}
    return {
        "book_id": book.book_id, "title": book.title, "pen_name": book.pen_name,
        "genre": book.genre, "sub_genre": book.sub_genre, "platform": book.platform,
        "status": book.status, "current_chapter": book.current_chapter,
        "chapter_count": book.chapter_count, "total_words": book.total_words or 0,
        "synopsis": (outline.get("synopsis") or ""),
        "protagonist": get_mc(bi),
        "characters": chars[:10],
        "world_building": (bi or {}).get("world_building"),
        "tone": (bi or {}).get("tone"),
        "pov": (bi or {}).get("pov"),
        "outline_picks": (bi or {}).get("_outline_picks"),
        "phase": tl.phase if tl else "",
        "outlines": [{"id": o.id, "name": o.name}
                     for o in (tl.outlines or [])][:10] if tl else [],
        "plots": [{"id": p.id, "name": p.name}
                  for p in (tl.plots or [])][:20] if tl else [],
    }


def get_build_status() -> dict:
    """读取建书向导当前状态（浏览器 WZ 上报到 storage/build_status.json）。

    供 agent 在建书流程中感知进度：当前步 cur、是否已建书 created/book_id、
    候选是否已选 _picked、步 3 是否已填世界观/选材。drive_ui(submit) 非阻塞，
    agent 用本工具拿 book_id 再去 get_book_detail 校验。无记录时返回默认（cur=1）。
    """
    from libraries.build_status import get_build_status as _read
    return _read()


def query_structures(keyword: str = "", genre: str = "", sub_genre: str = "") -> dict:
    """查大纲库：按流派/子流派/关键词（名称）返回模板清单。"""
    kw = (keyword or "").strip()
    rows = struct_lib.search(genre=genre, sub_genre=sub_genre)
    if kw:
        rows = [t for t in rows if kw in (t.name or "")]
    return {"templates": [{
        "id": t.id, "name": t.name, "genre": t.genre, "sub_genre": t.sub_genre,
        "total_chapters": t.total_chapters,
        "stages": [s.name for s in (t.stages or [])[:5]],
    } for t in rows[:20]]}


def query_plots(category: str = "", context: str = "", keyword: str = "") -> dict:
    """查桥段库：按分类/场景/关键词（名称）返回桥段模板清单。"""
    kw = (keyword or "").strip()
    rows = plot_lib.search(category=category, context=context)
    if kw:
        rows = [t for t in rows if kw in (t.name or "")]
    return {"plots": [{
        "id": t.id, "name": t.name, "category": t.category,
        "sub_category": t.sub_category or "",
        "fit_contexts": list(getattr(t, "fit_contexts", None) or [])[:3],
        "template_structure": (t.template_structure or "")[:80],
    } for t in rows[:20]]}


def query_gags(category: str = "", scene: str = "", keyword: str = "") -> dict:
    """查笑点库：按分类/场景/关键词（名称）返回笑点模式清单。"""
    kw = (keyword or "").strip()
    rows = gag_lib.search(category=category, scene=scene)
    if kw:
        rows = [t for t in rows if kw in (t.name or "")]
    return {"gags": [{
        "id": t.id, "name": t.name, "category": getattr(t, "category", ""),
        "fit_scenes": list(getattr(t, "fit_scenes", None) or [])[:3],
        "pattern_description": (getattr(t, "pattern_description", "") or "")[:80],
    } for t in rows[:20]]}


def query_profiles(keyword: str = "") -> dict:
    """查笔名档案：返回现有笔名（预设 + 用户自建，含风格指纹摘要 + 平台注册状态），供外部 agent 选笔名/写作风格参考。

    platform_accounts 为每平台注册信息（registered/site_id/author_url/notes/last_published_at），
    由用户在 UI 登记（agent 只读）；registered_platforms 为已注册平台列表。
    """
    kw = (keyword or "").strip()
    rows = profiles.list_all()
    if kw:
        rows = [p for p in rows if kw in (p.pen_name or "") or kw in (p.description or "")]
    return {"profiles": [{
        "id": p.id, "pen_name": p.pen_name, "description": p.description,
        "assigned_books": list(p.assigned_books or [])[:10],
        "platform_accounts": p.platform_accounts or {},
        "registered_platforms": p.registered_platforms(),
        "style": {
            "sentence_length": (p.style_fingerprint or {}).get("sentence_length", ""),
            "dialogue_ratio": (p.style_fingerprint or {}).get("dialogue_ratio", 0),
            "paragraph_style": (p.style_fingerprint or {}).get("paragraph_style", ""),
            "humor_style": (p.style_fingerprint or {}).get("humor_style", ""),
            "action_style": (p.style_fingerprint or {}).get("action_style", ""),
            "scene_pacing": (p.tropes or {}).get("scene_pacing", ""),
            "chapter_hook_style": (p.tropes or {}).get("chapter_hook_style", ""),
        },
    } for p in rows[:30]]}


def query_characters(keyword: str = "", tag: str = "", genre: str = "") -> dict:
    """查角色原型库：按标签/适配流派/关键词返回启用原型，供外部 agent 选原型生成角色。"""
    kw = (keyword or "").strip()
    rows = char_lib.search(tag=tag, genre=genre, kw=kw)
    return {"archetypes": [a.to_dict() for a in rows if getattr(a, "enabled", True)][:20]}


# ═══════════════════════════════════════════════════
# 规划 / 编辑类（调 LLM，成功后使引擎会话过期）
# ═══════════════════════════════════════════════════

def save_basic_info(book_id: str, basic_info: dict) -> dict:
    """保存基础设定（人物/世界观/基调/目标读者，深合并保留已填值），可带 book_title。
    新 payload 传 characters 整体替换；旧 payload 传 protagonist/supporting_cast 兼容。"""
    tl = _require_tl(book_id)
    bi = dict(tl.basic_info or {})
    if isinstance(basic_info.get("characters"), list):
        bi["characters"] = basic_info["characters"]
        bi.pop("protagonist", None)
        bi.pop("supporting_cast", None)
    for section in ("protagonist", "world_building"):
        incoming = basic_info.get(section)
        if isinstance(incoming, dict):
            base = dict(bi.get(section, {}) or {})
            for k, v in incoming.items():
                if v not in (None, ""):
                    base[k] = v
            bi[section] = base
    for field in ("supporting_cast", "tone", "target_audience", "pov", "era_language"):
        if basic_info.get(field) not in (None, ""):
            bi[field] = basic_info[field]
    bi = normalize_basic_info(bi)
    tl.basic_info = bi
    if basic_info.get("book_title") not in (None, ""):
        tl.book_title = basic_info["book_title"]
        book = book_mgr.get(book_id)
        if book:
            book.title = basic_info["book_title"]
            book_mgr.update(book)
    tl.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    save_tl(book_id, tl)
    _drop_engine(book_id)
    return {"ok": True}


def generate_title(book_id: str) -> dict:
    """AI 生成书名（3-5 个候选，选第一个写入 book_title + book.json.title）。"""
    tl = _require_tl(book_id)
    llm = _require_llm()
    bi = tl.basic_info or {}
    protag = get_mc(bi)
    world = bi.get("world_building") or {}
    ctx = f"流派：{tl.genre}{'/' + tl.sub_genre if tl.sub_genre else ''}"
    if protag.get("name"):
        ctx += f"；主角：{protag.get('name')}（{protag.get('identity','')}）"
    if world.get("description"):
        ctx += f"；世界观：{world['description']}"
    if bi.get("tone"):
        ctx += f"；基调：{bi['tone']}"
    prompt = (f"为下面这本网络小说起书名（3-5 个，2-10 字，朗朗上口、有网文味）。\n\n{ctx}\n\n"
              '返回 JSON：{"titles": ["书名1", "书名2", "书名3"]}')
    from core.llm_client import extract_json
    raw = llm.call("你是网文书名策划。只返回JSON。", prompt,
                   temperature=0.8, max_tokens=1024)
    data = json.loads(extract_json(raw))
    titles = [t for t in (data.get("titles") or [])
              if isinstance(t, str) and t.strip()]
    if not titles:
        raise RuntimeError("书名生成失败（LLM 无有效候选）")
    tl.book_title = titles[0]
    save_tl(book_id, tl)
    book = book_mgr.get(book_id)
    if book:
        book.title = titles[0]
        book_mgr.update(book)
    _drop_engine(book_id)
    return {"titles": titles, "chosen": titles[0]}


def generate_outlines(book_id: str, mode: str = "ai", max_outlines: int = 5) -> dict:
    """生成大纲序列（mode=ai 用 LLM；rule 用流派模板确定性生成）。"""
    tl = _require_tl(book_id)
    llm = get_llm() if mode == "ai" else None
    builder = StorylineBuilder(structure_lib=struct_lib, plot_lib=plot_lib,
                               gag_lib=gag_lib, llm_client=llm)
    if mode == "rule":
        tl.outlines = builder.build_outline_sequence(genre=tl.genre, mode="rule",
                                                     max_outlines=max_outlines)
    else:
        tl.outlines = builder.build_outline_sequence(
            genre=tl.genre, sub_genre=tl.sub_genre,
            custom_context=(tl.basic_info or {}).get("world_building", {}).get("description", ""),
            max_outlines=max_outlines, mode="ai")
    tl.phase = "outlines"
    save_tl(book_id, tl)
    _drop_engine(book_id)
    return {"ok": True, "count": len(tl.outlines)}


def outline_material_candidates(book_id: str) -> dict:
    """选材决策点候选池：大纲库模板 + 桥段库（供外部 agent 预选后把 picks 传给 generate_full_outline）。

    返回的 plots 为扁平列表（{id,name,category}），可直接作 generate_full_outline 的
    picks["plots"]（扁平优先序：想先出现的桥段排前）。"""
    tl = _require_tl(book_id)
    candidates = struct_lib.search(genre=tl.genre, sub_genre=tl.sub_genre)
    if not candidates:
        candidates = struct_lib.templates[:5]
    templates = [{
        "id": t.id, "name": t.name, "total_chapters": t.total_chapters,
        "stages": [s.name for s in (t.stages or [])[:5]],
    } for t in (candidates or [])[:10]]
    plots = [{
        "id": t.id, "name": t.name, "category": t.category,
        "sub_category": t.sub_category or "",
    } for t in (plot_lib.templates or [])[:30]]
    return {"templates": templates, "plots": plots,
            "current_phase": getattr(tl, "phase", ""),
            "has_outline": bool(getattr(tl, "outlines", None))}


def generate_full_outline(book_id: str, picks: dict = None,
                          regenerate: bool = False) -> dict:
    """一键生成完整大纲（5 阶段：分析→大纲→桥段→内涵/吸睛→一致性），原地累加并逐步落盘。

    picks（可选，决策点预选）形如 {"templates": ["structure_id", ...],
    "plots": ["plot_id", ...]}——plots 为扁平优先序列表（与 outline_material_candidates
    返回的 plots 形状一致，语义=全书出现优先级）；兼容旧 dict 形态 {outline_id: [plot_id]}
    （已弃用，按值序展开）。外部 agent 先调 outline_material_candidates 看候选，选定后
    传入即按预选排布；不传则走管线内 AI/规则选材。
    regenerate：书已存在大纲时默认拒绝重跑（避免清空重排覆盖已有内容），确认重做须传 True。
    阻塞运行至完成（可能数分钟），返回最终 timeline 快照（含 phase=ready，完成时自动落盘）。
    """
    tl = _require_tl(book_id)
    if tl.outlines and not regenerate:
        raise RuntimeError(
            f"已有 {len(tl.outlines)} 条大纲（phase={tl.phase}）。确认重做请传 regenerate=True"
            "（会清空重排现有大纲），否则可 extend_outline 续写 / 直接写作。")
    llm = _require_llm()
    profile = _profile_for(tl)

    # 向导步 3 分阶段构建②选定的开篇大纲/桥段（_outline_picks）→ picks 未传时自动消费
    if picks is None:
        saved = (tl.basic_info or {}).get("_outline_picks")
        if isinstance(saved, dict) and (saved.get("templates") or saved.get("plots")):
            picks = saved

    from libraries.outline_generator import OutlineGenerator
    from libraries.prompt_harness import PromptHarness
    harness = PromptHarness(storyline=tl, profile=profile,
                            gag_lib=gag_lib, plot_lib=plot_lib)
    gen = OutlineGenerator(llm_client=llm, structure_lib=struct_lib,
                           plot_lib=plot_lib, gag_lib=gag_lib,
                           profile=profile, harness=harness)

    bi = tl.basic_info or {}
    world = bi.get("world_building", {}) or {}
    protag = get_mc(bi)
    ctx_parts = []
    if world.get("world_summary"):
        ctx_parts.append(f"世界观概述：{world['world_summary']}")
    if world.get("description"):
        ctx_parts.append(f"世界观：{world['description']}")
    if protag.get("name") or protag.get("identity"):
        ctx_parts.append(f"主角：{protag.get('name','')}（{protag.get('identity','')}）")
    if bi.get("storyline_hint"):
        ctx_parts.append(f"故事线想法：{bi['storyline_hint']}")
    custom_context = "；".join(ctx_parts) or (tl.book_title or "")

    last_t, last_d, events = consume_triple_stream(gen.generate(
        genre=tl.genre, sub_genre=tl.sub_genre,
        custom_context=custom_context, pen_name=tl.pen_name,
        words_per_chapter=tl.words_per_chapter,
        storyline=tl, on_save=lambda _tl: save_tl(book_id, _tl),
        skip_analyze=bool((tl.basic_info or {}).get("_world_generated")),
        agent_picks=picks))

    tl = load_tl(book_id)  # on_save 已逐步落盘，重新读取最终快照
    _drop_engine(book_id)
    return {"stats": last_d, "event_count": len(events),
            "timeline": tl.to_dict() if tl else None}


def extend_outline(book_id: str, mode: str = "ai") -> dict:
    """续写时扩展故事线：末尾追加新大纲弧 + 填充桥段 + 加料，同步 bump 章节总数。"""
    tl = _require_tl(book_id)
    if not tl.outlines:
        raise RuntimeError("尚无故事线大纲，请先 generate_full_outline 后再扩展")
    llm = get_llm() if mode == "ai" else None
    builder = StorylineBuilder(structure_lib=struct_lib, plot_lib=plot_lib,
                               gag_lib=gag_lib, llm_client=llm)
    _seed_builder_counter(builder,
                          [o.id for o in tl.outlines] + [p.id for p in tl.plots])

    new_arc = _build_next_arc(builder, tl, mode)
    if new_arc is None:
        raise RuntimeError("无可用大纲模板")
    tl.outlines.append(new_arc)
    new_plots = builder.fill_plots_for_outline(new_arc, tl)
    existing_ids = {p.id for p in tl.plots}
    added = [p for p in new_plots if p.id not in existing_ids]
    tl.plots.extend(added)
    builder.fill_themes_and_hooks(added, tl)
    annotate_plot_roles(tl)
    tl.phase = "ready"
    save_tl(book_id, tl)

    new_total = 0
    book = book_mgr.get(book_id)
    if book:
        new_total = max(book.chapter_count, new_arc.end_chapter)
        if new_total > book.chapter_count:
            book.chapter_count = new_total
            book_mgr.update(book)
    _drop_engine(book_id)
    return {"outline": {"id": new_arc.id, "name": new_arc.name,
                        "start_chapter": new_arc.start_chapter,
                        "end_chapter": new_arc.end_chapter},
            "plots_added": len(added), "total_chapters": new_total}


def confirm_outlines(book_id: str) -> dict:
    """确认大纲序列，进入桥段编排阶段（phase → plots）。"""
    tl = _require_tl(book_id)
    tl.phase = "plots"
    save_tl(book_id, tl)
    _drop_engine(book_id)
    return {"ok": True, "phase": tl.phase}


def fill_plots(book_id: str) -> dict:
    """给每个大纲填充桥段（LLM），返回新增数量与累计总量。"""
    tl = _require_tl(book_id)
    if not tl.outlines:
        raise RuntimeError("请先生成大纲序列（generate_outlines / generate_full_outline）")
    builder = StorylineBuilder(structure_lib=struct_lib, plot_lib=plot_lib,
                               gag_lib=gag_lib, llm_client=_require_llm())
    _seed_builder_counter(builder, [p.id for p in tl.plots])

    new_plots = []
    for o in tl.outlines:
        new_plots.extend(builder.fill_plots_for_outline(o, tl))

    existing_ids = {p.id for p in tl.plots}
    added = [p for p in new_plots if p.id not in existing_ids]
    tl.plots.extend(added)
    annotate_plot_roles(tl)
    save_tl(book_id, tl)
    _drop_engine(book_id)
    return {"plots_added": len(added), "total_plots": len(tl.plots)}


def fill_gags(book_id: str) -> dict:
    """给桥段挂载内涵（compatible_plots 规则）+ 标注吸睛点（规则，不调 LLM）。"""
    tl = _require_tl(book_id)
    builder = StorylineBuilder(structure_lib=struct_lib, plot_lib=plot_lib,
                               gag_lib=gag_lib)
    builder.fill_themes_and_hooks(tl.plots, tl)
    annotate_plot_roles(tl)
    tl.phase = "ready" if tl.plots else "gags"
    save_tl(book_id, tl)
    _drop_engine(book_id)
    return {"ok": True, "phase": tl.phase}


def outline_agent(book_id: str, message: str) -> dict:
    """大纲助手：用自然语言调整故事线（改桥段/加笑点/增删桥段/改大纲等），直接落盘。"""
    tl = _require_tl(book_id)
    if not message.strip():
        raise RuntimeError("消息为空")
    llm = _require_llm()
    from libraries.outline_agent import OutlineAgent
    agent = OutlineAgent(llm=llm, structure_lib=struct_lib, plot_lib=plot_lib,
                         gag_lib=gag_lib)
    result = agent.handle(tl, message)
    save_tl(book_id, tl)
    _drop_engine(book_id)
    return result


def generate_world(book_id: str, mode: str = "one", idea: str = "",
                   source_book_id: str = "", tweak: str = "") -> dict:
    """生成世界观设定（mode=one 一句话生成；borrow 从源书借鉴+微调），返回 basic_info。"""
    tl = _require_tl(book_id)
    llm = _require_llm()
    mode = mode or "one"
    idea = (idea or "").strip()
    source_book_id = (source_book_id or "").strip()
    tweak = (tweak or "").strip()

    seed = None
    if mode == "borrow" and source_book_id:
        src = load_tl(source_book_id)
        if src:
            from libraries.world_builder import WorldBuildingGenerator
            seed = WorldBuildingGenerator.extract_seed(src.basic_info)
    if mode == "borrow":
        idea = tweak
    if not idea:
        idea = str((tl.basic_info or {}).get("world_building", {}).get("description", "") or "").strip()

    profile = _profile_for(tl)
    from libraries.world_builder import WorldBuildingGenerator
    from libraries.prompt_harness import PromptHarness
    harness = PromptHarness(storyline=tl, profile=profile)
    gen = WorldBuildingGenerator(llm_client=llm, profile=profile, harness=harness)

    last_t, last_d, events = consume_triple_stream(gen.generate(
        genre=tl.genre, sub_genre=tl.sub_genre, idea=idea,
        pen_name=tl.pen_name, platform=tl.platform,
        seed_basic_info=seed, storyline=tl,
        on_save=lambda _tl: save_tl(book_id, _tl)))

    tl = load_tl(book_id)
    _drop_engine(book_id)
    return {"basic_info": tl.basic_info if tl else None,
            "event_count": len(events), "done_data": last_d}


_WIZARD_CAND_FILE = os.path.join(_ROOT, "storage", "wizard_candidates.json")


def _wizard_candidate_key(idea: str, tags: list) -> str:
    """候选持久化会话 key：一句话设定 + 题材标签（排序）唯一化一个建书会话。"""
    return json.dumps({
        "idea": (idea or "").strip(),
        "tags": sorted(str(t).strip() for t in (tags or []) if str(t).strip()),
    }, ensure_ascii=False)


def _wizard_existing_candidates(idea: str, tags: list) -> list:
    """读当前会话已积累的候选（按 idea+tags key）；key 不匹配视为新会话返回空。"""
    from core.json_store import read_json
    try:
        st = read_json(_WIZARD_CAND_FILE, None)
        if st and st.get("key") == _wizard_candidate_key(idea, tags) \
                and isinstance(st.get("candidates"), list):
            return st["candidates"]
    except Exception:
        pass
    return []


def _clear_wizard_candidates() -> None:
    """清空候选持久化（drive_ui(reset) 建书前调用，防跨会话残留）。"""
    from core.json_store import write_json_atomic
    try:
        write_json_atomic(_WIZARD_CAND_FILE, {"key": "", "candidates": []})
    except Exception:
        pass


def world_candidates(book_id: str = "", idea: str = "", genre: str = "",
                     sub_genre: str = "", tags: list = None) -> dict:
    """增量生成世界观候选并**自动填入**步 2（一次 1 个，给 LLM 充分思考空间）。

    book_id 为空 = 建书前调用（新书向导②）：每次调用只产出 1 个**新**候选并
    push add_candidate 自动填入浏览器步 2；候选按 (idea, tags) 持久化去重——
    连调 N 次即积累 N 张卡；侧栏「再来几个」复用同 idea/tags 续接（差异化基于
    已生成的候选）。book_id 非空 = 用该书的 genre/sub_genre（忽略传入 genre）。
    返回 {"candidate", "candidates"(全部累计), "total"}。
    """
    llm = _require_llm()
    idea = (idea or "").strip()
    from libraries.world_builder import WorldBuildingGenerator
    from libraries.prompt_harness import PromptHarness
    if book_id:
        tl = _require_tl(book_id)
        genre = genre or tl.genre
        sub_genre = sub_genre or tl.sub_genre
        profile = _profile_for(tl)
        harness = PromptHarness(storyline=tl, profile=profile)
        gen = WorldBuildingGenerator(llm_client=llm, profile=profile, harness=harness)
    else:
        # 无书（向导②）：generate_candidate 本就不读目标书
        harness = PromptHarness()   # storyline=None；tags 由 generate_candidate 透传
        gen = WorldBuildingGenerator(llm_client=llm, harness=harness)
        if not genre and tags:
            from libraries.world_tags import derive_genre
            genre = derive_genre(list(tags or []))
    existing = _wizard_existing_candidates(idea, tags)
    cand = gen.generate_candidate(genre=genre or "都市", sub_genre=sub_genre or "",
                                  idea=idea, tags=list(tags or []),
                                  existing_candidates=existing)
    if not cand:
        raise RuntimeError("示例候选生成失败，请重试")
    from core.json_store import write_json_atomic
    write_json_atomic(_WIZARD_CAND_FILE, {
        "key": _wizard_candidate_key(idea, tags),
        "candidates": existing + [cand],
    })
    from libraries.nav_intent import push_ui_command
    push_ui_command("add_candidate", {"candidate": cand})
    return {"candidate": cand, "candidates": existing + [cand],
            "total": len(existing) + 1}


def generate_characters(idea: str, genre: str = "", sub_genre: str = "",
                        tags: list = None, title: str = "",
                        archetype_ids: list = None,
                        core_conflict: str = "", factions: list = None,
                        outline_preview: str = "") -> dict:
    """生成角色候选（无书，建书向导步 3 用）：主角 + 配角，供 drive_ui(set_characters) 推给页面。

    分阶段构建④可带已定核心矛盾/势力/开篇大纲桥段上下文（core_conflict/factions/outline_preview），
    让角色与之自洽。原型选择：archetype_ids 非空则按 id 取；否则按 tags[0]→genre→启用原型回退
    （照旧 /api/world-builder/characters 端点逻辑）。返回 {"protagonists": [...], "supporting_cast": [...]}。
    """
    llm = _require_llm()
    from libraries.world_builder import WorldBuildingGenerator
    from libraries.prompt_harness import PromptHarness
    harness = PromptHarness()   # 无书：storyline=None
    gen = WorldBuildingGenerator(llm_client=llm, harness=harness)
    if archetype_ids:
        archetypes = [a.to_dict() for i in archetype_ids
                      if (a := char_lib.get_by_id(i)) is not None][:10]
    elif tags:
        archetypes = [a.to_dict() for a in char_lib.search(tag=str(tags[0]).strip())][:10]
    elif genre:
        archetypes = [a.to_dict() for a in char_lib.search(genre=genre)][:10]
    else:
        archetypes = [a.to_dict() for a in char_lib.archetypes if getattr(a, "enabled", True)][:10]
    result = gen.generate_characters(
        idea=idea or "", genre=genre, sub_genre=sub_genre,
        tags=list(tags or []), title=title or "", archetypes=archetypes,
        core_conflict=core_conflict or "", factions=list(factions or []),
        outline_preview=outline_preview or "")
    if not result:
        raise RuntimeError("角色候选生成失败，请重试")
    return result

def generate_core_conflict(idea: str, world_brief: str = "", tags: list = None,
                           genre: str = "", sub_genre: str = "",
                           pen_name: str = "") -> dict:
    """分阶段构建①（无书）：从一句话设定+题材标签推导故事主线核心矛盾。

    返回 {"core_conflict", "genre"}（genre 供②按大纲库查模板/桥段）。
    """
    llm = _require_llm()
    from libraries.world_builder import WorldBuildingGenerator
    from libraries.prompt_harness import PromptHarness
    from libraries.storyline import BookStoryline
    profile = None
    if pen_name:
        profile = _profile_for(BookStoryline(pen_name=pen_name))
    harness = PromptHarness(profile=profile)
    gen = WorldBuildingGenerator(llm_client=llm, profile=profile, harness=harness)
    if not genre and tags:
        from libraries.world_tags import derive_genre
        genre = derive_genre(list(tags or []))
    conflict = gen.generate_core_conflict(
        genre=genre, sub_genre=sub_genre, idea=world_brief or idea or "",
        tags=list(tags or []), pen_name=pen_name or "")
    if not conflict:
        raise RuntimeError("核心矛盾生成失败，请重试")
    return {"core_conflict": conflict, "genre": genre}


def generate_factions(idea: str, world_brief: str = "", core_conflict: str = "",
                      tags: list = None, genre: str = "", sub_genre: str = "") -> dict:
    """分阶段构建③（无书）：从一句话设定+核心矛盾发散世界里的势力派系。

    返回 {"factions": [{"name", "stance", "desc"}]}。
    """
    llm = _require_llm()
    from libraries.world_builder import WorldBuildingGenerator
    from libraries.prompt_harness import PromptHarness
    harness = PromptHarness()
    gen = WorldBuildingGenerator(llm_client=llm, harness=harness)
    if not genre and tags:
        from libraries.world_tags import derive_genre
        genre = derive_genre(list(tags or []))
    factions = gen.generate_factions(
        genre=genre, sub_genre=sub_genre, idea=world_brief or idea or "",
        core_conflict=core_conflict or "", tags=list(tags or []))
    if not factions:
        raise RuntimeError("势力生成失败，请重试")
    return {"factions": factions}


def generate_rest_world(idea: str, world_brief: str = "", core_conflict: str = "",
                        factions: list = None, outline_preview: str = "",
                        tags: list = None, genre: str = "", sub_genre: str = "",
                        pen_name: str = "") -> dict:
    """分阶段构建⑤（无书）：大纲确定后补全其余世界观维度 + 基调，保留 core_conflict/factions。

    返回 {"world_building": {era, power_system, geography, culture, history,
    social_structure, rules, world_summary}, "tone", "target_audience", "pov", "era_language"}。
    """
    llm = _require_llm()
    from libraries.world_builder import WorldBuildingGenerator
    from libraries.prompt_harness import PromptHarness
    from libraries.storyline import BookStoryline
    profile = None
    if pen_name:
        profile = _profile_for(BookStoryline(pen_name=pen_name))
    harness = PromptHarness(profile=profile)
    gen = WorldBuildingGenerator(llm_client=llm, profile=profile, harness=harness)
    if not genre and tags:
        from libraries.world_tags import derive_genre
        genre = derive_genre(list(tags or []))
    result = gen.generate_rest_world(
        genre=genre, sub_genre=sub_genre, idea=idea or "",
        world_brief=world_brief or "", core_conflict=core_conflict or "",
        factions=[f for f in (factions or []) if isinstance(f, dict)],
        outline_preview=outline_preview or "", tags=list(tags or []), pen_name=pen_name or "")
    if not result.get("world_building"):
        raise RuntimeError("世界观维度补全失败，请重试")
    return result


def confirm_world(book_id: str) -> dict:
    """确认世界观设定：basic_info 够充实则打标 _world_generated（后续大纲跳过 Phase 1 分析）。"""
    tl = _require_tl(book_id)
    tl.basic_info = tl.basic_info or {}
    from libraries.outline_generator import basic_info_is_rich
    if basic_info_is_rich(tl.basic_info):
        tl.basic_info["_world_generated"] = True
    else:
        tl.basic_info.pop("_world_generated", None)
    tl.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    save_tl(book_id, tl)
    _drop_engine(book_id)
    return {"ok": True, "world_generated": bool(tl.basic_info.get("_world_generated"))}


# ═══════════════════════════════════════════════════
# 写作 / 元数据类（调 LLM，保留引擎会话以支持断点续写）
# ═══════════════════════════════════════════════════

def write_next_bridge(book_id: str) -> dict:
    """写「下一个」桥段（按桥段撰写，阻塞至该桥段写完）：本章满字数自动切章。"""
    engine = get_engine(book_id)
    last, events = consume_dict_stream(engine._write_next_bridge_stream())
    snap = _snapshot(book_id)
    if last is None:
        return {"status": "noop", **snap}
    t = last.get("type")
    if t == "chapter_done":
        return {"status": "chapter_done", "chapter": last.get("chapter"),
                "word_count": last.get("word_count"), "beats": last.get("beats"),
                "review": last.get("review"), "cost": last.get("cost"), **snap}
    if t == "chapter_progress":
        return {"status": "bridge_written", "chapter": last.get("chapter"),
                "words": last.get("words"), "target": last.get("target"),
                "draft": _draft_read(book_id), **snap}
    if t == "complete":
        return {"status": "complete", "message": last.get("message"), **snap}
    if t == "budget_paused":
        return {"status": "budget_paused", "message": last.get("message"), **snap}
    # 桥段已写但未切章（或草稿收尾）：给出已产出的桥段数
    return {"status": "bridge_written", "last_event": t,
            "bridge_done_count": sum(1 for e in events if e.get("type") == "bridge_done"),
            "draft": _draft_read(book_id), **snap}


def write_chapter(book_id: str, chapter_num: int = 0) -> dict:
    """整章同步写作（按桥段驱动，一次写完一章），chapter_num=0 表示写下一章。"""
    from libraries.engine import Instruction, Op
    engine = get_engine(book_id)
    n = chapter_num or engine.state.current_chapter + 1
    result = engine.execute(Instruction(Op.WRITE_STORYLINE_CHAPTER, chapter_num=n))
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(result["error"])
    out = dict(result or {})
    out.update(_snapshot(book_id))
    return out


def generate_book_meta(book_id: str) -> dict:
    """基于第 1 章生成书名+简介并落盘（book.json / storyline.json / outline.json）。"""
    if not book_mgr.get(book_id):
        raise RuntimeError(f"书 {book_id} 不存在")
    ch1 = book_mgr.load_chapter(book_id, 1)
    if not ch1 or not ch1.get("content"):
        raise RuntimeError("尚无第 1 章正文，请先 write_next_bridge / write_chapter 写作")
    llm = _require_llm()
    engine = NovelEngine(llm_client=llm)
    engine.continue_book(book_id)
    result = engine._generate_book_meta(ch1["content"])
    book_mgr._cache.pop(book_id, None)   # 让后续读取看到新 title
    _drop_engine(book_id)
    return result


# ═══════════════════════════════════════════════════
# 上架 / 审查 / 去AI / 书管理（规则，补齐「创建→上架」最后一环）
# ═══════════════════════════════════════════════════

def publish_check(book_id: str) -> dict:
    """上架前检查（5 项免费规则：书名/简介/字数/审查/完本），返回 report。"""
    book = book_mgr.get(book_id)
    if not book:
        raise RuntimeError(f"书 {book_id} 不存在")
    from libraries.publisher import Publisher
    return Publisher(book_mgr).build_report(book).to_dict()


def mark_finished(book_id: str) -> dict:
    """标记完本（book.status=finished + finished_at）。"""
    from libraries.publisher import Publisher
    return Publisher(book_mgr).mark_finished(book_id)


def publish_book(book_id: str, force: bool = False) -> dict:
    """标记已上架（book.status=published）。未过上架检查时需 force=True。"""
    from libraries.publisher import Publisher
    return Publisher(book_mgr).publish(book_id, force=force)


def export_book(book_id: str) -> dict:
    """导出投稿包（逐章 txt + 合集 + zip），返回 manifest（含 zip_path）。"""
    from libraries.publisher import Publisher
    return Publisher(book_mgr).export_book(book_id)


def review_text(text: str, target_words: int = 3000) -> dict:
    """审查一段文本（规则层：字数/AI痕迹/段落节奏/对话占比/章末钩子），返回通过/评分/问题清单。"""
    r = ContentReviewer().review(text, chapter_num=0, target_words=target_words)
    return {
        "passed": r.passed, "score": r.score, "summary": r.summary,
        "issues": [{"severity": i.severity, "category": i.category,
                    "description": i.description, "location": i.location,
                    "suggestion": i.suggestion} for i in r.issues],
    }


def deai_text(text: str, style: str = "chatty") -> dict:
    """对文本做规则去 AI 味（词替换+段落节奏），返回处理结果与统计。"""
    r = DeAIEngine().process_rule_based(text, style=style)
    return {"processed": r.processed, "word_replacements": r.word_replacements,
            "sentences_split": r.sentences_split, "llm_rewritten": r.llm_rewritten,
            "processed_length": len(r.processed)}


def diagnose_retention(book_id: str, recent_n: int = 5) -> dict:
    """追读诊断：最近 N 章正文 → 章级钩子强度/掉读风险 + 建议（规则层，零成本）。"""
    book = book_mgr.get(book_id)
    if not book:
        raise RuntimeError(f"书 {book_id} 不存在")
    chapters = []
    start = max(1, book.current_chapter - recent_n + 1)
    for n in range(start, book.current_chapter + 1):
        ch = book_mgr.load_chapter(book_id, n)
        if ch:
            chapters.append({
                "num": n, "title": ch.get("title", ""),
                "content": ch.get("content", ""),
                "summary": ch.get("summary", ""),
            })
    if not chapters:
        return {"chapter_level": [], "suggestions": ["尚无已写章节"]}
    from libraries.retention import diagnose_chapters
    return diagnose_chapters(chapters)


def diagnose_promises(book_id: str) -> dict:
    """伏笔台账扫描：逾期/推进/停滞/近期回收（规则层，零成本）。

    复用 storyline.promises 台账（设局→pending、收局→fulfilled），
    写前先扫一眼「欠读者什么」：哪些承诺逾期了、哪些近期没推进。
    """
    tl = _require_tl(book_id)
    book = book_mgr.get(book_id)
    if not book:
        raise RuntimeError(f"书 {book_id} 不存在")
    chapters = []
    for n in range(1, book.current_chapter + 1):
        ch = book_mgr.load_chapter(book_id, n)
        if ch:
            chapters.append({"num": n, "content": ch.get("content", "")})
    from libraries.promise_ledger import scan_promises
    result = scan_promises(tl, chapters, book.current_chapter or 0)
    # 平铺计数键供 LoopGuard 摘要（书变化时计数变化，防误熔断）
    result["overdue_count"] = result["counts"]["overdue"]
    result["advanced_count"] = result["counts"]["advanced"]
    result["stalled_count"] = result["counts"]["stalled"]
    result["fulfilled_count"] = result["counts"]["fulfilled_recently"]
    return result


def tag_punch_points(book_id: str, chapter_num: int = 0) -> dict:
    """爽点标注：单章正文 → 爽点标签（打脸/升级/伏笔回收/装逼/甜宠/反转），
    chapter_num=0 用最近一章；结果落盘 books/<id>/tags.json。"""
    book = book_mgr.get(book_id)
    if not book:
        raise RuntimeError(f"书 {book_id} 不存在")
    n = chapter_num or book.current_chapter
    ch = book_mgr.load_chapter(book_id, n)
    if not ch or not ch.get("content"):
        raise RuntimeError(f"第 {n} 章无正文")
    from libraries.tag_generator import tag_chapter
    result = tag_chapter(ch["content"])
    tags_saved, tags_save_error = True, ""
    try:
        tags_path = os.path.join(_ROOT, "books", book_id, "tags.json")
        with open(tags_path, "w", encoding="utf-8") as _f:
            json.dump({"chapter": n, "tags": result["tags"]}, _f,
                      ensure_ascii=False, indent=2)
    except OSError as e:
        tags_saved, tags_save_error = False, str(e)
    return {"chapter": n, "tag_count": len(result["tags"]), "tags": result["tags"],
            "tags_saved": tags_saved, "tags_save_error": tags_save_error}


# ═══════════════════════════════════════════════════
# 导航 / 建书向导驱动（navigate 返回 {"__navigate__": url}，MCP 适配层据此落意图队列）
# ═══════════════════════════════════════════════════

def navigate(url: str, tab: str = "") -> dict:
    """浏览器页面跳转工具：把用户当前看到的页面切换到指定站内 URL（如 /books、/books/123、/books/123/continue、/publish）。

    用户明确要求「打开/跳转/去看看/进入」某页面时必须调用本工具切页。
    注意：切页与读取数据是两件事——即使已用 get_book_state 读过数据，只要用户要「打开页面」，
    就还要调用本工具让浏览器实际切过去。
    外部（MCP）调用时无 SSE 通道，本工具同时写入意图队列由浏览器轮询消费（tab 可切右侧工具日志页签）。"""
    url = (url or "").strip()
    if (not url.startswith("/") or url.startswith("//") or "://" in url
            or url.startswith("javascript:")):
        raise RuntimeError(f"仅允许站内路径，收到：{url}")
    from libraries.nav_intent import push_nav_intent
    push_nav_intent(url, tab=(tab or "").strip())
    return {"__navigate__": url}


# 建书向导命令白名单（cmd → 必填 args 键；空元组=无必填）。
# 命令桥安全护栏：只允许这些页面已声明的操作，禁止任意 DOM/JS 注入。
_WIZARD_CMDS = {
    "set_field": ("field", "value"),
    "set_tags": ("tags",),
    "set_characters": ("characters",),   # 角色列表整体替换（agent 生成后推送）
    "set_candidates": ("candidates",),   # 呈现候选卡（不选中，等用户在步 2 点选）；candidates=[{title, one_liner, world_brief}]
    "add_candidate": ("candidate",),   # 增量追加 1 张候选卡（world_candidates 合并工具自动 push；候选={title, one_liner, world_brief}）
    "pick_candidate": (),   # 兼容保留：candidate={title, world_brief, one_liner} 内嵌传入（idx 仅卡片高亮，可选）；新 skill 不用
    "set_world": ("world_building",),   # 分阶段内容构建：部分世界观 dict 合并进步 3 表单
    "set_picks": ("templates",),   # 开篇大纲/桥段选择（templates 或 plots 任一非空，drive_ui 特判）
    "next": (), "prev": (),
    "load_candidates": (), "skip_candidates": (),
    "fill_world": (),   # 步骤③世界观重新补全（Agent 兜底/重试）
    "reset": (),   # 清空向导 state（除 pen_name/库表外字段）——建书前先 reset，防残留干扰保真度
    "submit": (),
}


def drive_ui(cmd: str, args: dict = None) -> dict:
    """驱动「启动新书」向导 UI（命令桥）：set_field/set_tags/set_characters/set_candidates/
    pick_candidate/next/prev/load_candidates/skip_candidates/reset/submit。

    非阻塞：把命令写入意图队列，浏览器每 ~2.5s 轮询消费（start_book.html 的
    window.onnecommand 执行）。不入书锁（不写书）。
    建书仍走系统向导（/books/start POST）：agent 只驱动表单、点下一步/提交，
    **不能绕过向导直建**（护栏：无直建工具）。
    """
    cmd = (cmd or "").strip()
    if cmd not in _WIZARD_CMDS:
        raise RuntimeError(f"未知向导命令：{cmd}，可选 {sorted(_WIZARD_CMDS)}")
    args = dict(args or {})
    if cmd == "set_picks":   # templates 或 plots 任一非空即可（[] 会被通用校验误判为缺参）
        if not (args.get("templates") or args.get("plots")):
            raise RuntimeError(f"命令 {cmd} 缺少必填参数：templates 或 plots")
    elif cmd == "set_candidates":   # 呈现候选：非空 list、每项 dict 且含 title
        cands = args.get("candidates")
        if not (isinstance(cands, list) and cands
                and all(isinstance(c, dict) and c.get("title") for c in cands)):
            raise RuntimeError(f"命令 {cmd} 需 candidates 非空列表（每项 {{title, one_liner?, world_brief?}}）")
    elif cmd == "pick_candidate":   # candidate 内嵌传入为主；idx 仅卡片高亮，可选但至少给其一
        has_candidate = isinstance(args.get("candidate"), dict) and bool(args["candidate"])
        has_idx = isinstance(args.get("idx"), int)
        if not (has_candidate or has_idx):
            raise RuntimeError(f"命令 {cmd} 需 candidate 对象或 idx 至少其一（candidate={{title, world_brief, one_liner}}）")
    else:
        for k in _WIZARD_CMDS[cmd]:
            if not args.get(k):
                raise RuntimeError(f"命令 {cmd} 缺少必填参数：{k}")
    if cmd == "reset":
        _clear_wizard_candidates()   # 新会话清空候选持久化，防跨会话残留
    from libraries.nav_intent import push_ui_command
    push_ui_command(cmd, args)
    return {"__ui_command__": cmd, "cmd": cmd}


# ═══════════════════════════════════════════════════
# 工具注册表（自动从函数签名生成 JSON Schema，MCP 与 agent 循环共用）
# ═══════════════════════════════════════════════════

def _type_to_schema(t):
    origin = getattr(t, "__origin__", None)
    if origin is typing.Union:
        args = [a for a in t.__args__ if a is not type(None)]
        return _type_to_schema(args[0]) if args else {"type": "object"}
    if origin is list:
        return {"type": "array", "items": {}}
    if origin is dict:
        return {"type": "object"}
    name = getattr(t, "__name__", str(t))
    return {"type": {"str": "string", "int": "integer", "float": "number",
                     "bool": "boolean", "dict": "object", "list": "array"}.get(name, "string")}


def _func_to_schema(fn):
    sig = inspect.signature(fn)
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}
    properties, required = {}, []
    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        properties[name] = _type_to_schema(hints.get(name, str))
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "object", "properties": properties, "required": required}


# 护栏：直建/直删工具不存在于注册表——建书走「启动新书」向导 UI、
# 删书走书库页手动，任何 agent（含 MCP 面）都拿不到建/删能力。

# 写类工具：进入前须拿书锁（防 Web / MCP 双进程同书撞写），退出释放。
_LOCKED_TOOLS = {
    "write_next_bridge", "write_chapter", "generate_full_outline",
    "generate_world", "world_candidates", "confirm_world",
    "outline_agent", "fill_plots", "fill_gags", "generate_book_meta",
}


def _wrap_book_lock(fn):
    """把写工具包上书锁：acquire 失败抛 BookBusyError（另一进程在操作），finally 释放。

    functools.wraps 保留原签名/__wrapped__，MCP 端 FastMCP 据此生成正确 JSON Schema
    （否则锁包装的 **kwargs 会让 inspect.signature 丢失 book_id 等参数，schema 错乱）。
    """
    @functools.wraps(fn)
    def wrapper(**kwargs):
        book_id = kwargs.get("book_id") or ""
        lock = BookLock(book_id) if book_id else None
        if lock is not None and not lock.acquire(timeout=30.0, purpose=fn.__name__):
            raise BookBusyError(f"另一进程正在操作这本书，请稍后再试：{book_id}")
        try:
            return fn(**kwargs)
        finally:
            if lock is not None:
                lock.release()
    return wrapper


def _build_registry():
    # 顺序有讲究：导航/建书向导驱动排最前（flash 对列表前部工具更敏感，能保证
    # "打开页面"请求正确触发 navigate），其次只读摸底，再创作链/上架/工具。
    fns = [
        # 导航 / 建书向导驱动（用户高频意图，必须前置）
        navigate, drive_ui,
        # 只读摸底
        list_books, get_book_state, get_storyline, borrow_preview,
        get_book_detail, get_build_status, query_structures, query_plots, query_gags, query_profiles, query_characters,
        # 规划
        save_basic_info,
        generate_title, generate_outlines, generate_full_outline,
        extend_outline, confirm_outlines, fill_plots, fill_gags, outline_agent,
        outline_material_candidates,
        generate_world, world_candidates, generate_characters, confirm_world,
        generate_core_conflict, generate_factions, generate_rest_world,
        # 写作 / 元数据
        write_next_bridge, write_chapter, generate_book_meta,
        # 上架 / 审查 / 去AI / 质量分析
        publish_check, mark_finished, publish_book, export_book,
        review_text, deai_text,
        diagnose_retention, tag_punch_points,
        diagnose_promises,
    ]
    seen = set()
    entries = []
    for fn in fns:
        name = fn.__name__
        if name in seen:
            raise RuntimeError(f"工具注册表去重失败：{name} 出现两次")
        seen.add(name)
        # 包装顺序：phase 门控最外层（phase 不对就不等锁）→ 书锁 → 原函数。
        # 门控对未收录工具原样返回；锁只对 _LOCKED_TOOLS 生效。functools.wraps
        # 逐层保留 __name__/__wrapped__，MCP 端 schema 不受影响。
        wrapped = _wrap_book_lock(fn) if name in _LOCKED_TOOLS else fn
        wrapped = _wrap_phase_gate(wrapped)
        entries.append({
            "name": name,
            "description": (inspect.getdoc(fn) or "").strip(),
            "input_schema": _func_to_schema(fn),
            "func": wrapped,
        })
    return entries


TOOL_REGISTRY = _build_registry()
