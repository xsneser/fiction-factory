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
    _engines, _resolve_storyline, _save_storyline,
    StorylineBuilder,
    ContentReviewer, DeAIEngine,
)
from core.text_utils import count_prose_units  # noqa: E402
from libraries.storyline import OutlineSlot, annotate_plot_roles, \
    get_mc, get_characters, normalize_basic_info  # noqa: E402
from libraries.book_lock import BookLock, BookBusyError  # noqa: E402
from libraries.tool_policy import _wrap_phase_gate  # noqa: E402


# ─── 基础辅助 ───

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
                           "请先经「启动新书」向导建书（步 3 内容随 submit 落库）。")
    return tl


def _drop_engine(book_id: str) -> None:
    """使该书引擎会话过期（规划/编辑类改动后调用）。"""
    _engines.pop(f"cont_{book_id}", None)


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


# ═══════════════════════════════════════════════════
# 只读 / 建书类（无 LLM，供上下文供给与测试）
# ═══════════════════════════════════════════════════

def _book_tags(book_id: str) -> list:
    """取一本书的题材标签（basic_info.world_building.tags；无则空）。genre 已移除，tags 是唯一题材来源。"""
    try:
        tl = book_mgr.load_storyline(book_id)
        if tl:
            wb = (tl.basic_info or {}).get("world_building") or {}
            return list(wb.get("tags") or [])
    except Exception:
        pass
    return []


def list_books() -> list:
    """列出书库全部书籍的摘要（book_id/书名/标签/状态/进度）。"""
    rows = []
    for b in book_mgr.list_all():
        rows.append({
            "book_id": b.book_id,
            "title": b.title,
            "pen_name": b.pen_name,
            "tags": _book_tags(b.book_id),
            "status": b.status,
            "current_chapter": b.current_chapter,
            "chapter_count": b.chapter_count,
            "total_words": b.total_words or 0,
        })
    return rows


def get_book_state(book_id: str) -> dict:
    """读取一本书的完整状态：book 配置、故事线、结构弧、章节摘要、进行中草稿。"""
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
            "tags": _book_tags(book_id), "platform": book.platform,
            "status": book.status, "current_chapter": book.current_chapter,
            "chapter_count": book.chapter_count, "total_words": book.total_words or 0,
        },
        "storyline": tl.to_dict() if tl else None,
        "outline": outline,
        "chapters": chapters,
        "draft": _draft_read(book_id),
    }


def get_writing_context(book_id: str) -> dict:
    """[薄工具] 一次返回写正文所需的完整上下文（书配置+故事线+角色/世界观+弧+最近章摘要+草稿）。

    复用 get_book_state 全量 payload（get_storyline / get_book_detail 是其子集/重叠），
    追加就地提取的扁平字段：synopsis（outline）、protagonist（get_mc）、
    next_bridge（第一个未写桥段 written_chapter==0，含 plot_id/name/roles/outline_id）。
    agent 逐桥段循环每轮只调本工具一次，避免重复读上下文。
    """
    payload = get_book_state(book_id)
    tl = book_mgr.load_storyline(book_id)
    outline = payload.get("outline") or {}
    # synopsis：outline.json 的 synopsis
    payload["synopsis"] = outline.get("synopsis") or ""
    # protagonist：basic_info.characters 中 role=主角 的第一个
    protagonist = None
    if tl and tl.basic_info:
        protagonist = get_mc(tl.basic_info)
    payload["protagonist"] = protagonist
    # next_bridge：第一个未写桥段（written_chapter==0）
    next_bridge = None
    if tl:
        for p in tl.plots:
            if not (getattr(p, "written_chapter", 0) or 0):
                next_bridge = {
                    "plot_id": p.id, "name": p.name, "roles": list(getattr(p, "roles", None) or []),
                    "outline_id": getattr(p, "outline_id", "") or "",
                }
                break
    payload["next_bridge"] = next_bridge
    # next_chapter：进行中草稿的章号优先，否则 current_chapter + 1（供写作任务卡显示「该写第几章」）
    book = payload.get("book") or {}
    draft = payload.get("draft")
    if draft and draft.get("chapter_num"):
        next_chapter = draft["chapter_num"]
    else:
        next_chapter = (book.get("current_chapter") or 0) + 1
    payload["next_chapter"] = next_chapter
    # pen_name + style_rules：注入笔名风格约束（生成前强注入，dsh/MCP 写作 agent 每轮必读必遵）
    payload["pen_name"] = (tl.pen_name if tl else "") or book.get("pen_name") or ""
    profile = _profile_for(tl) if tl else None
    if profile:
        payload["style_rules"] = profile.build_writing_prompt()
    else:
        # 无笔名档案也注入默认笔名规则兜底（禁句式/词表对所有书生效）
        from libraries.style_rules import StyleRuleLibrary
        payload["style_rules"] = StyleRuleLibrary().build_rules_block()
    return payload


def get_storyline(book_id: str) -> dict:
    """读取一本书的故事线（timeline）JSON：弧/桥段/线程/内涵/基础设定。"""
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
            "source_tags": ((src.basic_info or {}).get("world_building") or {}).get("tags") or []}


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
        "tags": _book_tags(book_id), "platform": book.platform,
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


def _stage_tree(s):
    """把弧模板的 StageNode（可 children 嵌套）压成紧凑 dict 树供 agent 参考（单弧可多层，字数表述）"""
    node = {
        "name": s.name,
        "description": (s.description or "")[:120],
        "min_words": s.min_words,
        "max_words": s.max_words,
        "key_events": (s.key_events or [])[:4],
    }
    if s.children:
        node["children"] = [_stage_tree(c) for c in s.children]
    return node


def query_arc_library(keyword: str = "", tags: str = "") -> dict:
    """查情节弧库：按标签/关键词（名称）返回模板清单（标签逗号/空格分隔，任一命中）。
    模板=单弧可多层：stages 即其子弧（可 children 嵌套，深度/分支按书定、不要求均匀）。
    注意 total_chapters 为模板参考章节数（非强制弧跨度），不要直接 × 每章字数当弧的 start_word/end_word。"""
    kw = (keyword or "").strip()
    tag_list = [x.strip() for x in (tags or "").replace("，", " ").replace(",", " ").split() if x.strip()]
    rows = struct_lib.search(tags=tag_list)
    if kw:
        rows = [t for t in rows if kw in (t.name or "")]
    return {"templates": [{
        "id": t.id, "name": t.name, "tags": t.tags,
        "total_words": t.total_words,
        "stages": [_stage_tree(s) for s in (t.stages or [])[:8]],
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
            "language": (p.language or "zh"),
            "sentence_length": (p.style_fingerprint or {}).get("sentence_length", ""),
            "dialogue_ratio": (p.style_fingerprint or {}).get("dialogue_ratio", 0),
            "paragraph_style": (p.style_fingerprint or {}).get("paragraph_style", ""),
            "humor_style": (p.style_fingerprint or {}).get("humor_style", ""),
            "action_style": (p.style_fingerprint or {}).get("action_style", ""),
            "scene_pacing": (p.tropes or {}).get("scene_pacing", ""),
            "chapter_hook_style": (p.tropes or {}).get("chapter_hook_style", ""),
        },
    } for p in rows[:30]]}


def query_characters(keyword: str = "", tag: str = "") -> dict:
    """查角色原型库：按标签/关键词返回启用原型，供外部 agent 选原型生成角色。"""
    kw = (keyword or "").strip()
    rows = char_lib.search(tag=tag, kw=kw)
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


def save_chapter_text(book_id: str, chapter_num: int, text: str,
                      title: str = "", summary: str = "",
                      bridge_segments: list | None = None) -> dict:
    """[薄工具] 保存整章正文（agent 自主生成后调用，内部不调 LLM）。

    agent 生成正文后，本工具负责纯规则副作用：去AI味 → 规则审查 → 章节落盘
    （含桥段）→ 书进度/字数 → 故事线 written_chapter 进度 → 角色状态 →
    读者承诺台账（规则）→ 清草稿。summary 由 agent 生成传入（语义摘要是 LLM
    职责，迁到 agent）。
    """
    book = book_mgr.get(book_id)
    if not book:
        raise RuntimeError(f"书 {book_id} 不存在")
    text = (text or "").strip()
    if not text:
        raise RuntimeError("正文为空，请先生成内容再调用")
    n = int(chapter_num or 0)
    if n < 1:
        raise RuntimeError("chapter_num 需 >= 1")

    # 1) 规则去AI味（词替换+段落节奏，无 LLM）；有桥段则逐段去并保持桥梁结构
    processed = text
    if bridge_segments:
        segs = []
        for b in bridge_segments:
            seg_text = (b.get("text") or "")
            try:
                seg_text = DeAIEngine().process_rule_based(seg_text).processed
            except Exception:
                pass
            segs.append({"plot_id": b.get("plot_id"), "plot_name": b.get("plot_name"), "text": seg_text})
        bridge_segments = segs
        processed = "\n\n".join(s["text"] for s in segs)
    else:
        try:
            processed = DeAIEngine().process_rule_based(text).processed
        except Exception:
            pass

    # 2) 规则审查（reviewer，无 LLM）→ 存 review
    review_dict = None
    try:
        target = int(getattr(book, "words_per_chapter", 0) or 3000)
        r = ContentReviewer().review(processed, chapter_num=n,
                                     chapter_title=title or f"第{n}章", target_words=target)
        review_dict = {"passed": r.passed, "score": r.score, "summary": r.summary,
                       "issues": [{"severity": i.severity, "category": i.category,
                                   "description": i.description, "location": i.location,
                                   "suggestion": i.suggestion} for i in (r.issues or [])]}
    except Exception:
        pass

    # 3) 落盘章节
    book_mgr.save_chapter(book_id, n, title or f"第{n}章", processed, summary or "",
                          review=review_dict, bridges=bridge_segments)

    # 4) 书进度/字数
    try:
        if book.current_chapter < n:
            book.current_chapter = n
            book.status = "writing"
            book.total_words = (book.total_words or 0) + count_prose_units(processed)
            book_mgr.update(book)
    except Exception:
        pass

    # 5) 故事线 written_chapter 进度（本桥段标记为已写）
    #    bridge_segments 缺失时回退草稿 bridges（agent 漏传 bridge_segments 也不会卡住 next_bridge）
    try:
        tl = book_mgr.load_storyline(book_id)
        if tl:
            written_plot_ids = {b.get("plot_id") for b in (bridge_segments or []) if b.get("plot_id")}
            if not written_plot_ids:
                try:
                    dp = os.path.join(str(book_mgr.dir), book_id, "draft_chapter.json")
                    if os.path.exists(dp):
                        with open(dp, encoding="utf-8") as f:
                            _d = json.load(f)
                        written_plot_ids = {b.get("plot_id") for b in (_d.get("bridges") or []) if b.get("plot_id")}
                except Exception:
                    pass
            for p in tl.plots:
                if not (getattr(p, "written_chapter", 0) or 0) and p.id in written_plot_ids:
                    p.written_chapter = n
            book_mgr.save_storyline(book_id, tl)
    except Exception:
        pass

    # 6) 角色状态（规则自动机）
    try:
        from libraries.character_state import CharacterStateMachine
        csm = CharacterStateMachine()
        csm_path = os.path.join(_ROOT, "books", book_id, "character_states.json")
        if os.path.exists(csm_path):
            csm.load(csm_path)
        csm.update_from_chapter(n, processed)
        csm.save(csm_path)
    except Exception:
        pass

    # 7) 读者承诺台账（规则：written_chapter 标记 + pending 承诺 op 分级演化）
    _update_promises_ledger_thin(book_id, n)

    # 8) 清进行中草稿（整章已落盘）
    try:
        dp = os.path.join(_ROOT, "books", book_id, "draft_chapter.json")
        if os.path.exists(dp):
            os.remove(dp)
    except Exception:
        pass

    return {"ok": True, "chapter": n, "word_count": count_prose_units(processed),
            "review": review_dict}


def _update_promises_ledger_thin(book_id: str, chapter_num: int) -> None:
    """薄工具用的读者承诺台账登记（规则层，无 LLM）。

    标记本章已写桥段、pending 承诺按 deadline 距离重定 op（seed→touch→pressure→payoff）。
    （完整设局/收局扫描复制 engine._update_promises_ledger，此处先做规则主路径。）
    """
    try:
        tl = book_mgr.load_storyline(book_id)
        if not tl:
            return
        from libraries.promise_ledger import promise_op
        changed = False
        promises = list(getattr(tl, "promises", None) or [])
        for q in promises:
            if q.get("status") == "pending" and q.get("op") != promise_op(q, chapter_num):
                q["op"] = promise_op(q, chapter_num)
                changed = True
        if changed:
            tl.promises = promises
            book_mgr.save_storyline(book_id, tl)
    except Exception:
        pass


def save_bridge_draft(book_id: str, chapter_num: int, plot_id: str,
                      plot_name: str, text: str) -> dict:
    """[薄工具] 保存单个桥段到进行中草稿（draft_chapter.json，断点续写保底）。

    agent 逐桥段生成后调用：规则去AI味 → 追加进草稿（含 buffer/words/bridges），
    章满后用 save_chapter_text 落盘并清草稿。
    """
    if not (book_id and text and (text or "").strip()):
        raise RuntimeError("book_id 与 text 必填")
    text = (text or "").strip()
    try:
        text = DeAIEngine().process_rule_based(text).processed
    except Exception:
        pass
    dp = os.path.join(_ROOT, "books", book_id, "draft_chapter.json")
    draft = {}
    try:
        if os.path.exists(dp):
            with open(dp, encoding="utf-8") as f:
                draft = json.load(f)
    except Exception:
        draft = {}
    cur_ch = int(draft.get("chapter_num") or 0)
    bridges = list(draft.get("bridges") or [])
    if cur_ch != chapter_num:
        # 新章节草稿：重置
        bridges = []
        cur_ch = chapter_num
    bridges.append({"plot_id": plot_id or "", "plot_name": plot_name or "", "text": text})
    buffer = [b.get("text", "") for b in bridges]
    words = sum(count_prose_units(b) for b in buffer)
    try:
        os.makedirs(os.path.dirname(dp) or ".", exist_ok=True)
        with open(dp, "w", encoding="utf-8") as f:
            json.dump({"chapter_num": chapter_num, "buffer": buffer, "words": words,
                       "bridges": bridges}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        raise RuntimeError(f"保存桥段草稿失败: {e}")
    return {"ok": True, "chapter": chapter_num, "bridges": len(bridges), "words": words}


def save_outlines(book_id: str, outlines: list | None = None,
                  plots: list | None = None, threads: list | None = None,
                  themes: list | None = None, mode: str = "replace") -> dict:
    """[薄工具] 保存弧/桥段/线程/内涵（agent 生成后调用，内部不调 LLM）。

    接受 agent 生成的结构化 dict 列表，反序列化为 OutlineSlot / PlotSlot 落盘；
    mode=replace 整体替换 | append 续写追加。含 plots 则 phase=plots，否则 outlines。
    """
    tl = _require_tl(book_id)
    from libraries.storyline import OutlineSlot, PlotSlot, reconcile_outline
    if mode == "replace":
        tl.outlines = []
        tl.plots = []
    if outlines:
        base = len(tl.outlines)
        for i, o in enumerate(outlines):
            _sw = o.get("start_word"); _ew = o.get("end_word")
            _sc = o.get("start_chapter"); _ec = o.get("end_chapter")
            tl.outlines.append(OutlineSlot(
                id=o.get("id") or f"outline_{base + i + 1:04d}",
                template_id=o.get("template_id", ""),
                name=o.get("name") or "未命名弧",
                start_chapter=int(_sc) if _sc is not None else None,
                end_chapter=int(_ec) if _ec is not None else None,
                start_word=int(_sw) if _sw is not None else None,
                end_word=int(_ew) if _ew is not None else None,
                stages=o.get("stages") or [],
                predecessor=o.get("predecessor", ""),
                successor=o.get("successor", ""),
                transition_type=o.get("transition_type", "sequential"),
                parent_arc_id=o.get("parent_arc_id", ""),
                narrative=o.get("narrative", "chronological"),
                narrative_target=o.get("narrative_target", ""),
            ))
        for _o in tl.outlines:
            reconcile_outline(_o, tl.words_per_chapter or 3000)
    if plots:
        base = len(tl.plots)
        for i, p in enumerate(plots):
            tl.plots.append(PlotSlot(
                id=p.get("id") or f"plot_{base + i + 1:04d}",
                template_id=p.get("template_id", ""),
                name=p.get("name") or "未命名桥段",
                category=p.get("category", ""),
                sub_category=p.get("sub_category", ""),
                outline_id=p.get("outline_id", ""),
                stage_index=int(p.get("stage_index") or 0),
                order=int(p.get("order") or 0),
                thread_id=p.get("thread_id", "主线"),
                resolves_plot_id=p.get("resolves_plot_id", ""),
                resolves_name=p.get("resolves_name", ""),
                roles=p.get("roles") or [],
            ))
    if threads:
        tl.threads = threads
    if themes:
        tl.themes = themes
    tl.phase = "plots" if plots else "outlines"
    tl.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    save_tl(book_id, tl)
    _drop_engine(book_id)
    return {"ok": True, "outlines": len(tl.outlines), "plots": len(tl.plots),
            "phase": tl.phase}


def save_book_meta(book_id: str, title: str = "", synopsis: str = "") -> dict:
    """[薄工具] 保存书名/简介（agent 生成后调用，内部不调 LLM）。

    书名写 book.title + storyline.book_title；简介写 outline.json 的 synopsis。
    """
    tl = _require_tl(book_id)
    book = book_mgr.get(book_id)
    if title:
        tl.book_title = title
        if book:
            book.title = title
    if synopsis:
        od = book_mgr.get_outline(book_id) or {}
        od["synopsis"] = synopsis
        try:
            book_mgr.save_outline(book_id, od)
        except Exception:
            pass
    if book:
        book_mgr.update(book)
    tl.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    save_tl(book_id, tl)
    return {"ok": True, "title": title or tl.book_title, "synopsis": synopsis}


def arc_material_candidates(book_id: str) -> dict:
    """选材决策点候选池：情节弧库模板 + 桥段库（供外部 agent 预选弧模板作参考，再在自身上下文生成弧+桥段）。

    返回 {templates, plots}：templates 为弧级模板清单（单弧可多层，stages 含 children 层级），
    plots 为桥段库候选（{id,name,category,sub_category}）。"""
    tl = _require_tl(book_id)
    _tags = ((tl.basic_info or {}).get("world_building") or {}).get("tags") or []
    candidates = struct_lib.search(tags=_tags)
    if not candidates:
        candidates = struct_lib.templates[:5]
    templates = [{
        "id": t.id, "name": t.name, "total_words": t.total_words,
        "stages": [_stage_tree(s) for s in (t.stages or [])[:8]],
    } for t in (candidates or [])[:10]]
    plots = [{
        "id": t.id, "name": t.name, "category": t.category,
        "sub_category": t.sub_category or "",
    } for t in (plot_lib.templates or [])[:30]]
    return {"templates": templates, "plots": plots,
            "current_phase": getattr(tl, "phase", ""),
            "has_outline": bool(getattr(tl, "outlines", None))}


def confirm_outlines(book_id: str) -> dict:
    """确认弧序列，进入桥段编排阶段（phase → plots）。"""
    tl = _require_tl(book_id)
    tl.phase = "plots"
    save_tl(book_id, tl)
    _drop_engine(book_id)
    return {"ok": True, "phase": tl.phase}


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


_WIZARD_CAND_FILE = os.path.join(_ROOT, "storage", "wizard_candidates.json")


def _clear_wizard_candidates() -> None:
    """清空候选持久化（drive_ui(reset) 建书前调用，防跨会话残留）。"""
    from core.json_store import write_json_atomic
    try:
        write_json_atomic(_WIZARD_CAND_FILE, {"key": "", "candidates": []})
    except Exception:
        pass


def _outline_preview_text(outline_data: dict) -> str:
    """把 generate_outline_preview 产出的弧+桥段序列化为 prompt 预览文本。"""
    if not outline_data:
        return ""
    lines = []
    for o in (outline_data.get("outlines") or [])[:5]:
        lines.append(f"· {o.get('name', '')}（第{o.get('start_chapter', 1)}-{o.get('end_chapter', 30)}章）")
        for s in (o.get("stages") or [])[:4]:
            evs = "、".join((s.get("events") or [])[:3])
            lines.append(f"  - {s.get('name', '')}：{evs}")
    plots = (outline_data.get("plots") or [])[:15]
    if plots:
        lines.append("桥段：" + "、".join(p.get("name", "") for p in plots))
    return "\n".join(lines)


def confirm_world(book_id: str) -> dict:
    """确认世界观设定：basic_info 够充实则打标 _world_generated（后续弧跳过 Phase 1 分析）。"""
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


def extract_style_asset(text: str, pen_name: str = "", enabled: dict = None) -> dict:
    """从文本提取写法资产（规则层：句长/对话比/段落风格/高频词/禁用词/句首/动作节拍），
    可选写入笔名档案 style_assets（AI-NWA 写法引擎最小可用版）。
    enabled：特征池逐项开关 {feature: bool}（缺省全启用），供「按启用集重编译」。"""
    from libraries.style_assets import (extract_style_features, default_enabled,
                                        STYLE_ASSET_FEATURES)
    features = extract_style_features(text)
    if not pen_name:
        return {"features": features, "saved": False, "message": "未指定笔名，仅返回特征"}
    profile = profiles.get_by_name(pen_name)
    if not profile:
        return {"features": features, "saved": False, "message": f"笔名「{pen_name}」不存在"}
    features["enabled"] = default_enabled()
    if enabled:
        for k, v in enabled.items():
            if k in STYLE_ASSET_FEATURES:
                features["enabled"][k] = bool(v)
    profile.style_assets = features
    profiles.update(profile)
    return {"features": features, "saved": True, "profile": pen_name}


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


def diagnose_continuity(book_id: str, recent_n: int = 5) -> dict:
    """连续性扫描：系统绑定重复/人称性别/数值单次/时间过渡/角色离线/伏笔逾期
    （规则层，零成本）。返回逐项 checks 与可行性标注（partial=弱启发仅供参考）。"""
    book = book_mgr.get(book_id)
    if not book:
        raise RuntimeError(f"书 {book_id} 不存在")
    chapters = []
    start = max(1, book.current_chapter - recent_n + 1)
    for n in range(start, book.current_chapter + 1):
        ch = book_mgr.load_chapter(book_id, n)
        if ch:
            chapters.append({"num": n, "content": ch.get("content", "")})
    if not chapters:
        return {"issue_count": 0, "issues": [], "suggestions": ["尚无已写章节"],
                "scanned_chapters": 0, "checks": {}}
    tl = _require_tl(book_id)
    from libraries.character_state import CharacterStateMachine
    csm = CharacterStateMachine()
    csm_path = os.path.join(_ROOT, "books", book_id, "character_states.json")
    if os.path.exists(csm_path):
        csm.load(csm_path)
    from libraries.continuity import ContinuityChecker
    return ContinuityChecker().check_all(tl, chapters, book.current_chapter or 0, csm)


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


def chapter_quality_gate(book_id: str, chapter_num: int = 0, recent_n: int = 5) -> dict:
    """完整章节质量门禁（规则层，零成本）：一次聚合 审查/连续性/追读/伏笔/爽点 五项，
    返回统一门禁报告。chapter_num=0 用最近一章。只报告不修复——问题作 decision_points
    决策点由 agent/用户定夺，不自动改正文。单项异常该项 skipped 不阻断。

    聚合 diagnose_retention / diagnose_continuity / diagnose_promises / review_text /
    tag_chapter（爽点只读标注不落盘，保门禁零写副作用）。返回紧凑报告（<8KB，
    summary/计数/decision_points 置前）；全量明细请按需调独立 diagnose_* 深挖。
    """
    book = book_mgr.get(book_id)
    if not book:
        raise RuntimeError(f"书 {book_id} 不存在")
    cur = int((book.current_chapter if book else 0) or 0)
    n = int(chapter_num or 0) or cur
    if n < 1 or n > cur:
        if cur < 1:
            raise RuntimeError("尚无已写章节，请先 write_next_bridge / write_chapter 写作")
        raise RuntimeError(f"第 {n} 章不存在（当前写到第 {cur} 章）")
    ch = book_mgr.load_chapter(book_id, n)
    content = (ch or {}).get("content") or ""
    if not content:
        raise RuntimeError(f"第 {n} 章无正文")
    target_words = int(getattr(book, "words_per_chapter", 0) or 3000)

    checks = {}
    skipped = []
    decision_points = []

    def _push(check, severity, description, location="", suggestion=""):
        if len(decision_points) >= 20:
            return
        decision_points.append({
            "check": check, "severity": severity,
            "description": str(description)[:40],
            "location": str(location)[:40],
            "suggestion": str(suggestion)[:40],
        })

    # 1. 审查（规则层；score>=60 视为过）
    try:
        r = ContentReviewer().review(content, chapter_num=n,
                                     chapter_title=f"第{n}章", target_words=target_words)
        top_issues = [{"severity": it.severity, "category": it.category,
                       "description": str(it.description)[:40]}
                      for it in (r.issues or [])[:3]]
        checks["review"] = {"passed": bool(r.passed), "score": r.score,
                            "issues_count": len(r.issues or []), "top_issues": top_issues}
    except Exception as e:
        skipped.append("review")
        checks["review"] = {"passed": None, "skipped": True, "error": str(e)[:60]}

    # 2. 连续性
    try:
        c = diagnose_continuity(book_id, recent_n=recent_n)
        warnings = [it for it in (c.get("issues") or []) if it.get("severity") == "warning"]
        status = {k: (v.get("status") if isinstance(v, dict) else str(v))
                  for k, v in (c.get("checks") or {}).items()}
        checks["continuity"] = {"passed": not warnings, "issue_count": len(warnings),
                                "scanned_chapters": c.get("scanned_chapters", 0),
                                "check_status": status}
    except Exception as e:
        skipped.append("continuity")
        checks["continuity"] = {"passed": None, "skipped": True, "error": str(e)[:60]}

    # 3. 追读
    try:
        ret = diagnose_retention(book_id, recent_n=recent_n)
        drops = [cl for cl in (ret.get("chapter_level") or [])
                 if int(cl.get("drop_risk") or 0) >= 7]
        rows = [{"chapter": cl.get("chapter"), "hook_strength": cl.get("hook_strength"),
                 "drop_risk": cl.get("drop_risk")}
                for cl in (ret.get("chapter_level") or [])[-5:]]
        checks["retention"] = {"passed": not drops, "issues_count": len(drops),
                               "chapters": rows}
    except Exception as e:
        skipped.append("retention")
        checks["retention"] = {"passed": None, "skipped": True, "error": str(e)[:60]}

    # 4. 伏笔台账
    try:
        p = diagnose_promises(book_id)
        counts = p.get("counts") or {}
        top = [str(it.get("desc") or it.get("name") or it)[:40]
               for it in ((p.get("overdue") or []) + (p.get("stalled") or []))[:3]]
        checks["promises"] = {"passed": int(counts.get("overdue") or 0) == 0,
                              "counts": {"overdue": counts.get("overdue", 0),
                                         "stalled": counts.get("stalled", 0),
                                         "advanced": counts.get("advanced", 0),
                                         "fulfilled_recently": counts.get("fulfilled_recently", 0)},
                              "issues_count": len(p.get("overdue") or []) + len(p.get("stalled") or []),
                              "top_issues": top}
    except Exception as e:
        skipped.append("promises")
        checks["promises"] = {"passed": None, "skipped": True, "error": str(e)[:60]}

    # 5. 爽点标注（只读，不落盘）
    try:
        from libraries.tag_generator import tag_chapter
        tags = tag_chapter(content)
        tag_list = [t.get("tag") for t in (tags.get("tags") or []) if t.get("tag")]
        checks["punch_points"] = {"passed": True, "tag_count": len(tag_list),
                                  "tags": tag_list[:10]}
    except Exception as e:
        skipped.append("punch_points")
        checks["punch_points"] = {"passed": None, "skipped": True, "error": str(e)[:60]}

    # 决策点聚合：硬问题（review/连续性/追读）优先，伏笔为提示。
    # review 的 warning/error 级问题无论是否通过都进决策点（如 AI 味词提示，供用户定夺去 AI 味）
    rv = checks.get("review") or {}
    for it in (rv.get("top_issues") or []):
        if it.get("severity") in ("warning", "error"):
            _push("review", it.get("severity") or "warning", it.get("description"))
    cc = checks.get("continuity") or {}
    if cc.get("passed") is False:
        _push("continuity", "warning", f"连续性 {cc.get('issue_count')} 项 warning 级问题")
    rt = checks.get("retention") or {}
    if rt.get("passed") is False:
        _push("retention", "warning", f"{rt.get('issues_count')} 章掉读风险高（drop_risk≥7）")
    pp = checks.get("promises") or {}
    if pp.get("passed") is False:
        for it in (pp.get("top_issues") or []):
            _push("promises", "info", "伏笔未推进", suggestion=it)

    complete = not skipped
    ran = [k for k in ("review", "continuity", "retention", "promises", "punch_points")
           if (checks.get(k) or {}).get("skipped") is not True]
    all_passed = bool(ran) and all((checks[k] or {}).get("passed") is True for k in ran)
    passed = complete and all_passed
    issue_count = sum(int((checks[k] or {}).get("issues_count") or (checks[k] or {}).get("issue_count") or 0)
                      for k in ("review", "continuity", "retention", "promises", "punch_points"))
    review_score = (rv.get("score") if isinstance(rv.get("score"), (int, float)) else 0)

    if skipped:
        summary = (f"第{n}章质量门禁：{len(skipped)} 项异常({','.join(skipped)})，"
                   f"其余{'通过' if all_passed else '有未过项'}，{len(decision_points)} 个决策点")
    elif passed:
        summary = f"第{n}章质量门禁：通过，review {review_score} 分，共 {issue_count} 项提示，{len(decision_points)} 个决策点"
    else:
        summary = f"第{n}章质量门禁：未通过，review {review_score} 分，{issue_count} 项问题，{len(decision_points)} 个决策点"

    return {
        "book_id": book_id, "chapter": n, "word_count": count_prose_units(content),
        "target_words": target_words, "passed": passed, "complete": complete,
        "summary": summary, "issue_count": issue_count, "review_score": review_score,
        "overdue_count": (checks.get("promises") or {}).get("counts", {}).get("overdue", 0),
        "stalled_count": (checks.get("promises") or {}).get("counts", {}).get("stalled", 0),
        "drop_risk_count": (checks.get("retention") or {}).get("issues_count", 0),
        "decision_points": decision_points,
        "checks": checks,
    }


def validate_storyline(book_id: str = "", outlines: list | None = None,
                       plots: list | None = None, words_per_chapter: int = 3000) -> dict:
    """故事线校验（规则层，零成本）：检查两条硬规则——①顶层弧完整覆盖故事线纵轴（无叙事空白）、
    ②桥段仅挂最底层弧（不包含其他弧的弧）。只报告不修复，问题作 decision_points 由 agent/用户补弧或移桥段。

    双模式：传 book_id 校验已落盘书；或步3 未建书时传 outlines/plots dict（内联模式，agent 提交前自查用，
    因为步3 时 book 尚未创建、get_book_detail/get_storyline 不可用）。返回 compact 报告：
    passed/issue_count/summary 置前 + coverage/leaf_arcs 明细 + decision_points。"""
    from libraries.storyline import OutlineSlot, PlotSlot, reconcile_outline

    # 数据源解析：book_id 模式从落盘读（字段已 reconcile）；内联模式用传入 dict
    if book_id:
        tl = book_mgr.load_storyline(book_id)
        if tl is None:
            raise RuntimeError(f"书 {book_id} 无故事线")
        _arcs = list(tl.outlines or [])
        _plots = list(tl.plots or [])
        wpc = int((tl.words_per_chapter or 0) or words_per_chapter or 3000)
    else:
        _arcs = []
        wpc = int(words_per_chapter or 3000)
        for o in (outlines or []):
            _sw = o.get("start_word"); _ew = o.get("end_word")
            _sc = o.get("start_chapter"); _ec = o.get("end_chapter")
            slot = OutlineSlot(
                id=o.get("id") or "", template_id=o.get("template_id") or "",
                name=o.get("name") or "",
                start_chapter=int(_sc) if _sc is not None else None,
                end_chapter=int(_ec) if _ec is not None else None,
                start_word=int(_sw) if _sw is not None else None,
                end_word=int(_ew) if _ew is not None else None,
                parent_arc_id=o.get("parent_arc_id") or "",
                stages=o.get("stages") or [],
            )
            reconcile_outline(slot, wpc)
            _arcs.append(slot)
        _plots = plots or []

    if not _arcs and not _plots:
        return {"ok": True, "book_id": book_id, "passed": True, "issue_count": 0,
                "summary": "无弧/桥段，无需校验", "total_words": 0,
                "top_arc_count": 0, "leaf_arc_count": 0, "plot_count": 0,
                "coverage": {"passed": True, "total_words": 0, "leading_gap": False, "gaps": [], "issues": []},
                "leaf_arcs": {"passed": True, "violations": [], "issues": []},
                "decision_points": [], "suggestions": []}

    # ① 覆盖：顶层弧按 start_word 排序；total = max(end_word)；报 leading_gap 与中间 gaps（重叠允许）
    _cov_issues = []
    _gaps = []
    top = [a for a in _arcs if not (getattr(a, "parent_arc_id", "") or "")]
    top_sorted = sorted(top, key=lambda a: (getattr(a, "start_word", 0) or 0))
    total = max([(getattr(a, "end_word", 0) or 0) for a in _arcs] or [0])
    leading_gap = bool(top_sorted and (top_sorted[0].start_word or 0) > 0)
    if leading_gap:
        _cov_issues.append(f"顶层弧「{top_sorted[0].name}」从 {top_sorted[0].start_word} 字才开始，开头 {top_sorted[0].start_word} 字为叙事空白")
    if not top_sorted:
        _cov_issues.append("没有顶层弧，故事线纵轴完全空白")
    for i in range(1, len(top_sorted)):
        prev, nxt = top_sorted[i - 1], top_sorted[i]
        if (nxt.start_word or 0) > (prev.end_word or 0):
            _gaps.append({"start_word": prev.end_word, "end_word": nxt.start_word,
                          "gap_words": (nxt.start_word or 0) - (prev.end_word or 0),
                          "before": prev.name, "after": nxt.name})
            _cov_issues.append(f"顶层弧「{prev.name}」结束于 {prev.end_word}，「{nxt.name}」始于 {nxt.start_word}，间隔 {(nxt.start_word or 0) - (prev.end_word or 0)} 字叙事空白")
    _cov_passed = not _cov_issues

    # ② 叶弧：parents = 有子弧的弧 id；桥段 outline_id 须存在且非 parents
    _leaf_issues = []
    _viol = []
    parents = {getattr(a, "parent_arc_id", "") for a in _arcs if getattr(a, "parent_arc_id", "")}
    _by_id = {getattr(a, "id", ""): a for a in _arcs}
    for p in _plots:
        pid = getattr(p, "id", None) if not isinstance(p, dict) else p.get("id")
        pname = getattr(p, "name", "") if not isinstance(p, dict) else p.get("name") or ""
        oid = getattr(p, "outline_id", "") if not isinstance(p, dict) else p.get("outline_id") or ""
        _oname = getattr(_by_id.get(oid), "name", "") if oid in _by_id else ""
        if not oid or oid not in _by_id:
            _leaf_issues.append(f"桥段「{pname or pid}」的 outline_id={oid or '空'} 悬空，不属于任何弧")
            _viol.append({"plot_id": pid, "plot_name": pname, "outline_id": oid, "outline_name": "", "reason": "悬空"})
        elif oid in parents:
            _leaf_issues.append(f"桥段「{pname or pid}」挂在非最底层弧「{_oname}」({oid})——仅最底层弧可拥有桥段")
            _viol.append({"plot_id": pid, "plot_name": pname, "outline_id": oid,
                          "outline_name": _oname, "reason": "非最底层弧"})
    _leaf_passed = not _leaf_issues

    # ③ 弧内覆盖：顶层弧跨度 vs 其叶弧后代桥段 planned_words 之和（warning 级，不计硬失败）
    def _pw_of(p):
        beats = 4
        beats = int(p.get("cover_beats") or 4) if isinstance(p, dict) else int(getattr(p, "cover_beats", 4) or 4)
        beats = max(beats, 2)
        return min(beats * 200, 1200)

    def _collect_leaves(arc_id):
        leaves = set()
        for a in _arcs:
            if (getattr(a, "parent_arc_id", "") or "") == arc_id:
                aid = getattr(a, "id", "")
                if aid in parents:
                    leaves |= _collect_leaves(aid)   # 递归：子弧还有子弧
                else:
                    leaves.add(aid)
        return leaves

    _pw_by_oid = {}
    for p in _plots:
        oid = getattr(p, "outline_id", "") if not isinstance(p, dict) else p.get("outline_id") or ""
        _pw_by_oid[oid] = _pw_by_oid.get(oid, 0) + _pw_of(p)
    _fill_issues = []
    _fill_arcs = []
    for a in top_sorted:
        span = (a.end_word or 0) - (a.start_word or 0)
        leaves = _collect_leaves(getattr(a, "id", ""))
        content = sum(_pw_by_oid.get(l, 0) for l in leaves)
        if not leaves:
            content = _pw_by_oid.get(getattr(a, "id", ""), 0)   # 顶层弧自身即叶弧时挂桥段
        gap_words = max(span - content, 0)
        ratio = (span / content) if content > 0 else 999.0
        _fill_arcs.append({"id": getattr(a, "id", ""), "name": a.name, "span": span,
                           "planned_words": content, "gap_words": gap_words,
                           "ratio": round(ratio, 1)})
        if gap_words > wpc:   # 弧内空白超一章即报（收紧：去掉 ratio>3 宽松条件）
            _fill_issues.append(f"顶层弧「{a.name}」跨度 {span} 字、桥段 planned 仅 {content} 字，约 {gap_words} 字空白（ratio {ratio:.1f}），建议拆子弧/缩弧跨度/补桥段")
    _fill_passed = not _fill_issues

    passed = _cov_passed and _leaf_passed and _fill_passed
    parts = []
    if _cov_issues:
        parts.append(f"弧树覆盖 {len(_cov_issues)} 处问题（{len(_gaps)} 处叙事空白）")
    if _leaf_issues:
        parts.append(f"桥段叶弧 {len(_leaf_issues)} 处问题")
    if _fill_issues:
        parts.append(f"弧内空白 {len(_fill_issues)} 处（跨度远超桥段内容）")
    if not parts:
        parts.append("弧树覆盖、桥段叶弧与弧内填充均通过")
    decision_points = [{"check": "storyline", "severity": "warning",
                        "description": str(s)[:60], "location": "", "suggestion": ""}
                       for s in (_cov_issues + _leaf_issues + _fill_issues)[:20]]
    suggestions = []
    if leading_gap:
        suggestions.append("在故事线开头补一个从 0 字开始的顶层弧，或把首个顶层弧 start_word 调到 0")
    for g in _gaps[:5]:
        suggestions.append(f"在「{g['before']}」与「{g['after']}」之间补弧或扩展前者 end_word 消除 {g['gap_words']} 字空白")
    for v in _viol[:5]:
        suggestions.append(f"把桥段「{v['plot_name']}」移到其所属弧的最底层子弧，或把「{v['outline_name']}」拆出子弧")
    for f in _fill_arcs[:5]:
        if f["gap_words"] > wpc:
            suggestions.append(f"顶层弧「{f['name']}」跨度 {f['span']} 字但桥段仅 {f['planned_words']} 字，拆出足够子弧/桥段填满，或把 end_word 缩到与内容匹配")
    return {
        "ok": True, "book_id": book_id, "passed": passed,
        "issue_count": len(_cov_issues) + len(_leaf_issues) + len(_fill_issues),
        "summary": "；".join(parts), "total_words": total,
        "top_arc_count": len(top),
        "leaf_arc_count": len([a for a in _arcs if (getattr(a, "id", "") or "") not in parents]),
        "plot_count": len(_plots),
        "coverage": {"passed": _cov_passed, "total_words": total, "leading_gap": leading_gap,
                     "gaps": _gaps, "issues": _cov_issues},
        "leaf_arcs": {"passed": _leaf_passed, "violations": _viol, "issues": _leaf_issues},
        "arc_fill": {"passed": _fill_passed, "arcs": _fill_arcs, "issues": _fill_issues},
        "decision_points": decision_points,
        "suggestions": suggestions,
    }


def validate_world(book_id: str = "", basic_info: dict | None = None) -> dict:
    """世界观校验（规则层，零成本）：检查势力/人物一致性——①势力名唯一（剥离括号描述后去重）、
    ②每个势力至少 1 个对应人物、③人物 faction 归属某个势力（无孤儿人物）。只报告不修复。

    双模式：传 book_id 读已落盘书 basic_info；或步3 未建书时传 basic_info dict
    （set_world 的 world_building + set_characters 的 characters 合并 payload）。"""
    if book_id:
        tl = book_mgr.load_storyline(book_id)
        if tl is None:
            raise RuntimeError(f"书 {book_id} 无故事线")
        bi = tl.basic_info or {}
    else:
        bi = basic_info or {}
    wb = bi.get("world_building") or {}
    factions_raw = wb.get("factions") or []
    chars = [c for c in (bi.get("characters") or []) if isinstance(c, dict)]

    def _norm(name):
        """剥离括号描述取规范名：『联邦远征军（人类主战力量）』→『联邦远征军』。"""
        s = str(name or "").strip()
        for open_c, close_c in (("（", "）"), ("(", ")")):
            if open_c in s and close_c in s:
                s = s[:s.index(open_c)].strip()
        return s

    f_norm = []
    for f in factions_raw:
        fname = f if isinstance(f, str) else (f or {}).get("name") or ""
        f_norm.append(_norm(fname))

    seen = {}
    duplicates = []
    for fn in f_norm:
        if fn in seen and fn and fn not in duplicates:
            duplicates.append(fn)
        else:
            seen[fn] = True

    char_factions = [_norm((c or {}).get("faction") or "") for c in chars]
    without_char = [fn for fn in f_norm if fn and fn not in char_factions]

    orphan = []
    for c in chars:
        cf = _norm((c or {}).get("faction") or "")
        if cf and cf not in f_norm:
            orphan.append({"name": (c or {}).get("name") or "", "faction": (c or {}).get("faction") or ""})

    issues = []
    for d in duplicates:
        issues.append(f"势力重复：{d}")
    for w in without_char:
        issues.append(f"势力「{w}」无对应人物")
    for o in orphan:
        issues.append(f"人物「{o['name']}」的势力「{o['faction']}」不属于任何势力")
    passed = not issues
    return {
        "ok": True, "book_id": book_id, "passed": passed, "issue_count": len(issues),
        "summary": "；".join(issues) if issues else "势力与人物一致性通过",
        "factions": {"count": len(f_norm), "duplicates": duplicates, "without_characters": without_char},
        "orphan_characters": orphan,
        "decision_points": [{"check": "world", "severity": "warning",
                             "description": str(s)[:60], "location": "", "suggestion": ""}
                            for s in issues[:20]],
        "suggestions": [],
    }


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
    "set_picks": ("templates",),   # 开篇弧/桥段选择（templates 或 plots 任一非空，drive_ui 特判）
    "set_outline": ("outlines", "plots"),   # 步3②生成的弧+桥段（generate_outline_preview 产出，submit 随书落库）
    "next": (), "prev": (),
    "load_candidates": (), "skip_candidates": (),
    "fill_world": (),   # 步骤③世界观重新补全（Agent 兜底/重试）
    "reset": (),   # 清空向导 state（除 pen_name/库表外字段）——建书前先 reset，防残留干扰保真度
    "submit": (),
}

# 步敏感命令 → 需求向导步（步 2 候选 / 步 3 内容构建）。
# drive_ui 执行前据此校验当前步——把浏览器「错误步静默丢弃/静默失败命令」变成「真错误」（agent 能收到拒绝信息）。
# 激活条件：build_status.updated_at 非空（浏览器上报过真实向导状态）；空状态（向导未启动/测试）走宽松阀跳过。
# 步 3 内容命令锁步 3：防候选未选/未进步 3 时提前写内容或建书（submit）。
# set_field/set_tags 不入表：表单字段全在 DOM，步 3 跨步改书名/笔名/标签合法。
# next/prev/reset 不入表（跨步移动/重置任何时候都允许）。
_WIZARD_STEP_GATE = {
    "set_candidates": 2, "add_candidate": 2, "pick_candidate": 2,
    "load_candidates": 2, "skip_candidates": 2,
    "set_world": 3, "set_characters": 3, "set_picks": 3,
    "set_outline": 3, "fill_world": 3, "submit": 3,
}


def drive_ui(cmd: str, args: dict = None) -> dict:
    """驱动「启动新书」向导 UI（命令桥）：set_field/set_tags/set_characters/set_candidates/
    add_candidate/pick_candidate/set_world/set_picks/set_outline/next/prev/load_candidates/
    skip_candidates/fill_world/reset/submit。

    非阻塞：把命令写入意图队列，浏览器每 ~2.5s 轮询消费（start_book.html 的
    window.onnecommand 执行）。不入书锁（不写书）。
    建书仍走系统向导（/books/start POST）：agent 只驱动表单、点下一步/提交，
    **不能绕过向导直建**（护栏：无直建工具）。

    必填 args（cmd → 必填键，缺则报错）：
    - set_field: {field, value}   field ∈ idea/pen/title/words/borrow_source/borrow_tweak
    - set_tags: {tags: [str]}
    - set_characters: {characters: [{name, role, importance, identity, personality, golden_finger,
      gender, catchphrase, brief, title, age, death_year, faction, relations}]}（整体替换）
      role 只取 主角/配角/反派/其他；importance 必传（主角=1）；**relations 必须 [{name, relation}] 数组**
      （传字符串会让前端渲染中断、后续角色全丢）
    - set_candidates: {candidates: [{title, one_liner?, world_brief?}]}   title 必填
    - add_candidate: {candidate: {title, one_liner?, world_brief?}}   title 必填，增量追加 1 张候选卡
    - pick_candidate: {candidate: {title, world_brief, one_liner}} 或 {idx: int}（至少其一）
    - set_world: {world_building: {era?, power_system?, geography?, culture?, history?,
      social_structure?, core_conflict?, rules?, world_summary?, factions?},
      tone?, target_audience?, pov?, era_language?}   **顶层键必须叫 world_building**（部分维可分批提交，合并进表单不覆盖已填）；
      **rules 必须数组**（传字符串会被忽略）
    - set_picks: {templates: [id|{id,name}]} 或 {plots: [id|{id,name}]}（任一非空）
    - set_outline: {outlines: [非空列表], plots: [list], threads?, themes?}   步3②弧+桥段，submit 随书落库
      outlines 每项 {id, name, start_word, end_word, parent_arc_id?, notes, stages?}（id 唯一必填、备注用 notes 非
      description、start_word/end_word 为 0 基字数坐标（start 含/end 不含，权威；可同时传 start_chapter/end_chapter
      兼容）、parent_arc_id 指向父弧 id 支持弧树嵌套）；弧=树状目标节点（定义见 NOVEL_AGENT.md 1.1），
      字数跨度由剧情结构决定、不设固定章数；仅最底层弧可拥有桥段；
      plots 每项 {id, name, outline_id, order, category?, thread_id?, roles?, template_structure?}
      （id 唯一必填、outline_id 必填指向所属弧 id（须为最底层弧）、order 弧内序号）——缺 id/outline_id 故事线桥段不显示
    - submit: {}  **⚠️ 建书即创建书目并跳书详情页，调用前必须先向用户汇报设定概要并取得确认**
    - next / prev / reset / load_candidates / skip_candidates / fill_world: {} 无必填
    """
    cmd = (cmd or "").strip()
    if cmd not in _WIZARD_CMDS:
        raise RuntimeError(f"未知向导命令：{cmd}，可选 {sorted(_WIZARD_CMDS)}")
    args = dict(args or {})
    if cmd == "set_picks":   # templates 或 plots 任一非空即可（[] 会被通用校验误判为缺参）
        if not (args.get("templates") or args.get("plots")):
            raise RuntimeError(f"命令 {cmd} 缺少必填参数：templates 或 plots")
    elif cmd == "set_outline":   # outlines 非空列表；plots 允许空列表（不能走通用缺参校验）
        outs = args.get("outlines")
        if not (isinstance(outs, list) and outs):
            raise RuntimeError(f"命令 {cmd} 需 outlines 非空列表")
        if not isinstance(args.get("plots"), list):
            raise RuntimeError(f"命令 {cmd} 需 plots 列表")
    elif cmd == "set_candidates":   # 呈现候选：非空 list、每项 dict 且含 title
        cands = args.get("candidates")
        if not (isinstance(cands, list) and cands
                and all(isinstance(c, dict) and c.get("title") for c in cands)):
            raise RuntimeError(f"命令 {cmd} 需 candidates 非空列表（每项 {{title, one_liner?, world_brief?}}）")
    elif cmd == "add_candidate":   # 增量追加 1 张候选卡：candidate 需 dict 且 title 非空，否则浏览器端会静默丢弃（agent 误以为成功）
        cand = args.get("candidate")
        if not (isinstance(cand, dict) and (cand.get("title") or "").strip()):
            raise RuntimeError(f"命令 {cmd} 需 candidate={{title, one_liner?, world_brief?}}，title 必填")
    elif cmd == "pick_candidate":   # candidate 内嵌传入为主；idx 仅卡片高亮，可选但至少给其一
        has_candidate = isinstance(args.get("candidate"), dict) and bool(args["candidate"])
        has_idx = isinstance(args.get("idx"), int)
        if not (has_candidate or has_idx):
            raise RuntimeError(f"命令 {cmd} 需 candidate 对象或 idx 至少其一（candidate={{title, world_brief, one_liner}}）")
    else:
        for k in _WIZARD_CMDS[cmd]:
            if not args.get(k):
                raise RuntimeError(f"命令 {cmd} 缺少必填参数：{k}")
    # 步校验：把「浏览器错误步静默丢弃命令」变成「真错误」（agent 能收到拒绝信息）。
    # 宽松阀：build_status 无真实记录（updated_at 空 = 浏览器从未上报向导状态）时跳过，
    # 避免误伤向导未启动 / 测试场景（mcp_smoke 等）。
    req_step = _WIZARD_STEP_GATE.get(cmd)
    if req_step is not None:
        try:
            from libraries.build_status import get_build_status as _read_st
            st = _read_st() or {}
        except Exception:
            st = {}
        if st.get("updated_at"):
            if st.get("created"):
                raise RuntimeError(
                    f"书已创建(book_id={st.get('book_id')})，建书流程已结束，不能执行 {cmd}")
            cur = st.get("cur")
            if cur is not None and cur != req_step:
                raise RuntimeError(
                    f"向导当前在步 {cur}，{cmd} 需在步 {req_step}；"
                    "请先 get_build_status 确认当前步，或 drive_ui(next/prev) 对齐后再操作")
    if cmd == "reset":
        _clear_wizard_candidates()   # 新会话清空候选持久化，防跨会话残留
    from libraries.nav_intent import push_ui_command
    # submit 半同步：推送前快照 submit_error，只对「新错误」反应，规避陈旧错误误判
    _read_st = None
    _old_err = ""
    if cmd == "submit":
        try:
            from libraries.build_status import get_build_status as _read_st
            _old_err = (_read_st() or {}).get("submit_error") or ""
        except Exception:
            pass
    push_ui_command(cmd, args)
    if cmd == "submit":
        # 等真实建书结果：浏览器 ~2.5s 轮询消费 submit → WZ.createBook() → POST /books/start
        # → reportStatus 写 book_id(成功)或 submit_error(失败)。成功返回 book_id,失败 raise(工具卡红叉),
        # 超时返回 pending(不 raise,防 agent 重复 submit)。
        _deadline = time.time() + 15
        while time.time() < _deadline:
            time.sleep(0.5)
            st = (_read_st() or {}) if _read_st else {}
            if st.get("created") and st.get("book_id"):
                return {"ok": True, "book_id": st.get("book_id"), "__ui_command__": "submit"}
            err = st.get("submit_error") or ""
            if err and err != _old_err:
                raise RuntimeError(f"建书失败：{err}")
        return {"ok": False, "pending": True,
                "message": "建书仍在进行/超时,请 get_build_status 确认 submit_error", "__ui_command__": "submit"}
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
    "confirm_world", "fill_gags",
    # 薄工具（agent 生成后落盘，同样需书锁防并发）
    "save_chapter_text", "save_bridge_draft", "save_outlines", "save_book_meta",
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
            # 决策点落库前快照（commit 语义：写工具改前先留底，供 preview_diff/rollback）
            if book_id:
                try:
                    from libraries.book_snapshot import snapshot as _book_snap
                    _book_snap(book_id, fn.__name__)
                except Exception:
                    pass  # 快照失败不阻断写操作
            return fn(**kwargs)
        finally:
            if lock is not None:
                lock.release()
    return wrapper


def preview_diff(book_id: str, snapshot_id: str) -> dict:
    """预览某次快照与当前书状态的差异（决策点落库前的 diff 审查，只读）。"""
    from libraries.book_snapshot import preview_diff as _pd
    return _pd(book_id, snapshot_id)


def rollback_book(book_id: str, snapshot_id: str) -> dict:
    """把书全量回滚到某次快照（恢复至快照时刻状态，谨慎使用）。"""
    from libraries.book_snapshot import rollback as _rb
    return _rb(book_id, snapshot_id)


def list_snapshots(book_id: str) -> dict:
    """列出某书的全部快照（新→旧）。"""
    from libraries.book_snapshot import list_snapshots as _ls
    return {"book_id": book_id, "snapshots": _ls(book_id)}


def fetch_novel(title: str = "", book_id: str = "", chapters: int = 30,
                download_delay: float = 1.0) -> dict:
    """抓取番茄小说：按书名或 book_id 搜索→下载指定章数→保存到 storage/novels/fanqie/。

    纯抓取、无需 LLM（复用 FanqieCrawler + novel_storage.save_novel）。进度实时写入
    storage/crawl_progress.json（/scout 页轮询展示）。返回 {ok, title, author,
    saved_chapters, folder, platform}。
    """
    if not title and not book_id:
        raise RuntimeError("请提供书名 title 或 book_id")
    from libraries.crawl_progress import write_crawl_progress
    from plugins.fanqie_scout import FanqieCrawler
    import re as _re
    import time as _time
    crawler = FanqieCrawler()

    def on_progress(phase, current, total, message):
        write_crawl_progress("running", phase, current, total, message)

    try:
        novel = (crawler._get_novel_from_page(book_id) if book_id
                 else crawler.search_novel(title))
        if not novel:
            raise RuntimeError(f"未找到：{title or book_id}")
        on_progress("search", 1, 1, f"找到: {novel.title}")

        chapter_list = crawler.get_chapter_list(novel.book_id, chapters)
        total_ch = len(chapter_list)
        downloaded = []
        for i, ch in enumerate(chapter_list):
            content = crawler.download_chapter(novel.book_id, ch["id"])
            if content.strip():
                downloaded.append({
                    "index": ch["index"], "title": ch["title"],
                    "content": content,
                    "word_count": len(_re.findall(r"[一-鿿]", content)),
                })
            on_progress("download", i + 1, total_ch, ch["title"][:30])
            if i < total_ch - 1:
                _time.sleep(download_delay)   # 礼貌爬取间隔

        from plugins.novel_storage import save_novel
        folder = save_novel("fanqie", {
            "title": novel.title, "author": novel.author,
            "book_id": novel.book_id, "url": novel.url,
            "genre": novel.genre, "chapter_count": novel.chapter_count,
        }, downloaded)
        write_crawl_progress("done", "download", len(downloaded), len(downloaded),
                             f"下载完成 {len(downloaded)}章")
        return {"ok": True, "title": novel.title, "author": novel.author,
                "saved_chapters": len(downloaded), "folder": folder,
                "platform": "fanqie"}
    except Exception as e:
        write_crawl_progress("error", "", 0, 0, str(e))
        raise


def discover_hot(genre: str = "", count: int = 10) -> dict:
    """侦察番茄小说热榜：返回热门书列表（书名/作者/题材/字数/章数/热度/简介）。

    genre 为题材中文名（如"玄幻""都市"，空=全站热榜）；count 默认 10。
    返回 {"ok", "count", "novels": [{book_id,title,author,genre,sub_genre,
    word_count,chapter_count,hot_score,intro,url}]}。
    """
    from plugins.fanqie_scout import FanqieCrawler
    crawler = FanqieCrawler()
    genre_id = 0
    if genre:
        genre_id = {v: k for k, v in FanqieCrawler.GENRE_MAP.items()}.get(genre.strip(), 0)
    novels = crawler.discover_hot(genre_id=genre_id, count=count)
    return {"ok": True, "count": len(novels),
            "novels": [n.__dict__ for n in novels]}


def list_crawled_novels(platform: str = "fanqie") -> dict:
    """列出已抓取/下载的小说库（元数据+已存章数），供 agent 选书借鉴。

    复用 novel_storage.list_novels。返回 {"ok", "count",
    "novels": [{title, author, platform, book_id, genre, chapter_count,
    saved_chapters, folder}]}。
    """
    from plugins.novel_storage import list_novels
    novels = list_novels(platform)
    return {"ok": True, "count": len(novels), "novels": novels}


def read_crawled_novel(platform: str = "fanqie", folder: str = "",
                       chapter: int = 0) -> dict:
    """读已抓取小说的内容：chapter=0 返回元数据+章节目录（标题/字数）；
    chapter>0 返回该章正文。供 agent 抓取参考书后借鉴设定/写法。

    复用 novel_storage.load_novel。返回 {ok, title, author, platform, genre,
    chapter_count, chapters:[{index,title,word_count}], chapter:{...}}。
    """
    from plugins.novel_storage import load_novel
    if not folder:
        raise RuntimeError("请提供 folder（书名目录，来自 list_crawled_novels）")
    data = load_novel(platform, folder)
    if not data:
        raise RuntimeError(f"未找到已抓取小说：{platform}/{folder}")
    info, chapters = data["info"], data["chapters"]
    result = {
        "ok": True,
        "title": info.get("title", ""), "author": info.get("author", ""),
        "platform": platform, "genre": info.get("genre", ""),
        "book_id": info.get("book_id", ""), "chapter_count": len(chapters),
        "chapters": [{"index": c.get("index", i + 1), "title": c.get("title", ""),
                      "word_count": c.get("word_count", 0)}
                     for i, c in enumerate(chapters)],
    }
    if chapter:
        for c in chapters:
            if c.get("index") == chapter:
                result["chapter"] = {"index": c.get("index"), "title": c.get("title", ""),
                                     "content": c.get("content", "")}
                return result
        raise RuntimeError(f"章节不存在：第{chapter}章")
    return result


def ingest_library_assets(plots: list | None = None, structures: list | None = None,
                          gags: list | None = None, characters: list | None = None,
                          source: str = "fanqie") -> dict:
    """提取入库：把 agent 从参考书/已抓取书提炼的桥段/弧/笑点/角色写入四库。

    纯规则落盘、无 LLM（复用 FanqieScoutAgent.ingest_selected，角色走新增
    _add_character）。字段格式——plot {name, category, sub_category, structure,
    slots[{name, options}], notes, word_range}；structure {name, total_words,
    tags?, description?, stages[{name, description, min_words, max_words,
    key_events, children?[{…}]}]}（弧模板=单弧，stages 为子弧，可 children 嵌套多层，
    深度/分支按书里真实结构定、不要求均匀；只表述字数，不含章数）；gag {name, category,
    pattern_description, fit_scenes, examples}；character {name, personality,
    description, archetypes, examples, catchphrases, tags, fit_tags}。
    返回 {ok, source, plots, structures, gags, characters}。
    """
    if not any([plots, structures, gags, characters]):
        raise RuntimeError("至少提供 plots/structures/gags/characters 之一")
    from plugins.fanqie_scout import FanqieScoutAgent
    scout = FanqieScoutAgent(plot_lib=plot_lib, struct_lib=struct_lib,
                             gag_lib=gag_lib, char_lib=char_lib)
    stats = scout.ingest_selected(plots=plots, structures=structures,
                                  gags=gags, characters=characters, source=source)
    return {"ok": True, "source": source, **stats}


def _build_registry():
    # 顺序有讲究：导航/建书向导驱动排最前（flash 对列表前部工具更敏感，能保证
    # "打开页面"请求正确触发 navigate），其次只读摸底，再创作链/上架/工具。
    fns = [
        # 导航 / 建书向导驱动（用户高频意图，必须前置）
        navigate, drive_ui,
        # 只读摸底
        list_books, get_book_state, get_writing_context, get_storyline,
        get_book_detail, get_build_status, query_arc_library, query_plots, query_gags, query_profiles, query_characters,
        # 规划（薄工具：agent 生成后落盘；旧工具内 LLM 生成已由 agent 自主生成接管）
        save_basic_info,
        save_outlines, save_book_meta,
        fill_gags, arc_material_candidates,
        # 写作 / 元数据（薄工具：agent 生成后落盘）
        save_bridge_draft, save_chapter_text,
        # 上架 / 质量门禁 / 校验
        publish_check, mark_finished, publish_book, export_book,
        chapter_quality_gate, validate_storyline, validate_world,
        # 抓取 / 侦察 / 提取入库（番茄小说；fetch_novel 进度写 crawl_progress.json，/scout 页轮询展示）
        fetch_novel, discover_hot, list_crawled_novels, read_crawled_novel,
        ingest_library_assets,
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
