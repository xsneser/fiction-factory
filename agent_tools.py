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
import logging
import time
import inspect
import typing
import functools
import hashlib

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

_log = logging.getLogger("agent_tools")


class ChapterCommittedStateError(RuntimeError):
    """章节正文/文件已落盘，但**权威状态**（故事线 written_chapter / storyline_revision）未更新。

    这类失败既不能当成「章节提交失败」（章确实在盘上），更不能静默：不更新 written_chapter
    会让下一轮 `_next_plot` 重写同一情节段；不 bump revision 会让旧 replan preview 看起来
    仍然新鲜、CAS 放行。调用方（finalize_draft_chapter → FSM）必须把它回报给用户。
    """

from ui.web_blueprints.ctx import (  # noqa: E402
    plot_lib, struct_lib, gag_lib, char_lib, profiles, book_mgr,
    _engines, _resolve_storyline, _save_storyline,
    StorylineBuilder,
    ContentReviewer, DeAIEngine,
)
from core.text_utils import count_prose_units  # noqa: E402
from libraries.reviewer import HARD_MIN_RATIO  # noqa: E402
from libraries.storyline import OutlineSlot, annotate_plot_roles, \
    get_mc, get_characters, normalize_basic_info, \
    outline_payload_problems  # noqa: E402
from libraries.book_lock import BookLock, BookBusyError  # noqa: E402
from libraries.tool_policy import _wrap_phase_gate  # noqa: E402
from libraries import style_md  # noqa: E402  # 样本驱动:styles/<pen>.md 与 STYLE REFERENCE 样本
from libraries import style_samples  # noqa: E402  # 样文池 samples.json + 预算选样注入
from libraries.planning_state import (  # noqa: E402
    detect_story_boundary, load_planning_state, merge_state, planning_path,
    save_planning_state, validate_patch,
)
from libraries.agent_tool_router import check_ui_command, selected_profile, tool_metadata  # noqa: E402


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


def _text_metrics(text: str) -> dict:
    """唯一的正文计量口径：发布/门禁用 prose units，展示同时给 Unicode code points。"""
    text = text or ""
    return {"actual_prose_units": count_prose_units(text), "raw_codepoints": len(text)}


def _draft_metrics(draft: dict | None) -> dict:
    bridges = list((draft or {}).get("bridges") or [])
    content = "\n\n".join(str(b.get("text") or "") for b in bridges)
    metrics = _text_metrics(content)
    metrics["bridge_count"] = len(bridges)
    return metrics


def _context_fingerprint(book_id: str, tl, plot_run: dict | None, profile=None,
                         style_snapshot: dict | None = None,
                         version_vector: dict | None = None) -> str:
    """Hash the canonical semantic inputs observed by one Plot Run."""
    payload = {
        "book_id": book_id,
        "storyline_revision": int(getattr(tl, "storyline_revision", 0) or 0),
        "run": (plot_run or {}).get("run") or {},
        "plot": (plot_run or {}).get("plot") or {},
        "style_profile_id": getattr(profile, "id", "") or "",
        "style_snapshot": style_snapshot or {},
        "version_vector": version_vector or {},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _semantic_digest(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _json_source_digest(path: str) -> str:
    """Digest a JSON source without invoking a resolver or mutating it."""
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, TypeError, ValueError):
        return ""
    return _semantic_digest(value)


def _context_version_vector(book_id: str, tl, draft: dict | None,
                            style_snapshot: dict | None = None) -> dict:
    """Return revisions for semantic sources used by a prepared Plot.

    This intentionally hashes canonical source state, not the rendered packet:
    display metadata or resolver ordering cannot make a valid run stale.
    """
    from libraries.plot_run_state import load_staged
    draft = draft or {}
    staged = load_staged(book_id)
    book_dir = os.path.join(_ROOT, "books", book_id)
    return {
        "storyline_revision": int(getattr(tl, "storyline_revision", 0) or 0),
        "draft_revision": _semantic_digest({
            "chapter_num": draft.get("chapter_num", 0),
            "bridges": draft.get("bridges") or [],
        }),
        "staged_revision": _semantic_digest({
            "chapter_num": staged.get("chapter_num", 0),
            "plot_deltas": staged.get("plot_deltas") or [],
        }),
        "character_state_revision": _json_source_digest(
            os.path.join(book_dir, "character_states.json")),
        "planning_revision": _json_source_digest(
            os.path.join(book_dir, "planning_state.json")),
        "memory_source_revision": _json_source_digest(
            os.path.join(book_dir, "story_memory.json")),
        "style_digest": (style_snapshot or {}).get("digest", ""),
        "sample_id": (style_snapshot or {}).get("sample_id", ""),
        "sample_content_digest": (style_snapshot or {}).get("sample_content_digest", ""),
    }


def _committed_metrics(book_id: str, book=None) -> tuple[int, int]:
    """已落盘章节的 (正文字数, 原始码点数)。

    优先累加 `save_chapter` 落盘的 `actual_prose_units`/`raw_codepoints`（v2 起每章必写），
    避免每次 prepare / 每轮 FSM 评估都把全书正文重读一遍再正则计字（O(章节数×字数)）；
    只有 v2 迁移前写入、缺这两个字段的旧章节才按正文补算一次。
    """
    units = codepoints = 0
    for n in range(1, int(getattr(book, "current_chapter", 0) or 0) + 1):
        ch = book_mgr.load_chapter(book_id, n) or {}
        if not ch:
            continue
        u, c = ch.get("actual_prose_units"), ch.get("raw_codepoints")
        if isinstance(u, int) and isinstance(c, int):
            units += u
            codepoints += c
            continue
        m = _text_metrics(ch.get("content") or "")
        units += m["actual_prose_units"]
        codepoints += m["raw_codepoints"]
    return units, codepoints


def _runtime_written_words(book_id: str, tl=None, book=None, draft=None) -> int:
    """「已写字数」的**唯一口径**：已落盘章节正文计字 + 当前草稿计字。

    绝对轴坐标（与 `committed_until_word` 同轴），所以边界判定只能用这个值；
    章内判定（本章是否够 `words_per_chapter`）请用 `write_flow.chapter_status` 的章内 `words`。
    """
    return int(_runtime_projection(book_id, tl, book, draft)["display_written_words"])


def _runtime_projection(book_id: str, tl=None, book=None, draft=None) -> dict:
    """storyline + draft 的单一运行态投影；planning_state 只提供 forecast，不做当前事实源。"""
    tl = tl if tl is not None else book_mgr.load_storyline(book_id)
    book = book if book is not None else book_mgr.get(book_id)
    draft = _draft_read(book_id) if draft is None else draft
    draft = draft or {}
    next_p = _next_plot(tl, draft) if tl else None
    committed_units, committed_codepoints = _committed_metrics(book_id, book) if book else (0, 0)
    dmetrics = _draft_metrics(draft)
    planned = int(getattr(next_p, "words", 0) or 0) if next_p else 0
    return {
        "next_plot": next_p,
        "current_plot": ({"id": next_p.id, "name": next_p.name, "outline_id": next_p.outline_id,
                          "thread_id": getattr(next_p, "thread_id", "") or ""} if next_p else None),
        "planned_prose_units": planned,
        "committed_words": committed_units,
        "draft_words": dmetrics["actual_prose_units"],
        "display_written_words": committed_units + dmetrics["actual_prose_units"],
        "committed_raw_codepoints": committed_codepoints,
        "draft_raw_codepoints": dmetrics["raw_codepoints"],
        "draft": draft,
    }


def _commit_journal_path(book_id: str) -> str:
    return os.path.join(_ROOT, "books", book_id, "chapter_commit.pending.json")


def _write_commit_journal(book_id: str, payload: dict) -> None:
    """跨文件章节提交的恢复线索；正常完成会删除，异常保留给下一次上下文/运维检查。"""
    from core.json_store import write_json_atomic
    write_json_atomic(_commit_journal_path(book_id), payload)


def _profile_for(tl):
    if tl and tl.pen_name:
        try:
            return profiles.get_by_name(tl.pen_name)
        except Exception:
            return None
    return None


def _default_style_card() -> str:
    """无笔名档案时的默认风格卡（默认笔名 枫落 的规则兜底）。"""
    from libraries.style_rules import DEFAULT_PROFILE_ID
    p = profiles.get(DEFAULT_PROFILE_ID)
    return p.build_style_card() if p else "笔名：默认（中文）"


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
            metrics = _text_metrics(ch.get("content") or "")
            chapters.append({
                "num": n,
                "title": ch.get("title"),
                "summary": ch.get("summary"),
                "word_count": metrics["actual_prose_units"],
                "actual_prose_units": metrics["actual_prose_units"],
                "raw_codepoints": metrics["raw_codepoints"],
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


# ─── PlotRunContext：把「写这一个情节段」所需输入收敛成一个块 ───
def _draft_plot_ids(draft):
    """当前草稿里已写入的情节段 id 集合（draft 段落键兼容 bridges/plots）。"""
    segs = []
    if draft:
        segs = draft.get("plots")
        if segs is None:
            segs = draft.get("bridges")
    return {str(s.get("plot_id")) for s in (segs or []) if (s or {}).get("plot_id")}


def _outline_name_chain(tl, oid):
    """弧路径（顶层→叶）名称串，供 agent 感知本情节段所处弧位。"""
    by = {o.id: o for o in tl.outlines}
    names, cur, guard = [], by.get(oid), 0
    while cur and guard < 8:
        names.append(cur.name or "")
        cur = by.get(cur.parent_arc_id) if getattr(cur, "parent_arc_id", "") else None
        guard += 1
    return " / ".join(x for x in reversed(names) if x)


def _char_states_path(book_id: str) -> str:
    return os.path.join(_ROOT, "books", book_id, "character_states.json")


def _load_char_states(book_id: str):
    """读 books/<id>/character_states.json；缺失/损坏返回空机（不抛）。"""
    from libraries.character_state import CharacterStateMachine
    csm = CharacterStateMachine()
    p = _char_states_path(book_id)
    if os.path.exists(p):
        try:
            csm.load(p)
        except Exception:
            pass
    return csm


def _draft_char_events(book_id: str) -> list:
    """从进行中草稿收集逐情节段上报的 character_events（save_chapter_text 漏传时的兜底）。"""
    try:
        dp = os.path.join(_ROOT, "books", book_id, "draft_chapter.json")
        if not os.path.exists(dp):
            return []
        with open(dp, encoding="utf-8") as f:
            d = json.load(f)
        evs = []
        for b in (d.get("bridges") or []):
            if isinstance(b, dict):
                evs.extend(b.get("character_events") or [])
        return evs
    except Exception:
        return []


def _segment_char_events(plot_segments) -> list:
    """把 plot_segments 各条携带的 character_events 展平（只留有 name 的项）。"""
    out = []
    for b in (plot_segments or []):
        if not isinstance(b, dict):
            continue
        for e in (b.get("character_events") or []):
            if isinstance(e, dict) and str(e.get("name") or "").strip():
                out.append(e)
    return out


# ─── cast_pack：按出场分级预解析的角色紧凑卡（plot_run 一块，谁在写） ───
def _clip(s, n=60) -> str:
    s = str(s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def _rel_of(c, mc_name) -> str:
    for r in (c.get("relations") or []):
        if isinstance(r, dict) and str(r.get("name", "") or "").strip() == mc_name:
            return str(r.get("relation", "") or "")
    return str(c.get("relation", "") or "")


def _dyn_of(csm, name) -> dict:
    """csm 动态态（只给 agent 可写/核心量；推断字段 mood/conflict/secret 不给，防噪音）。"""
    if csm is None:
        return {}
    cs = csm.get(name)
    if cs is None:
        return {}
    out = {}
    for k in ("goal", "power_level", "relationship_to_mc", "arc_stage", "location"):
        v = getattr(cs, k, "")
        if v is not None and str(v).strip():
            out[k] = str(v).strip()
    if int(getattr(cs, "offline_chapters", 0) or 0) > 0:
        out["offline_chapters"] = int(cs.offline_chapters)
    return out


def _speech_compact(c) -> str:
    """speech_profile 压缩成一句（语言倾向）；空则回退 catchphrase 作「情绪高点点缀」。"""
    sp = c.get("speech_profile") or {}
    parts = []
    if str(sp.get("rhythm", "") or "").strip():
        parts.append("句长倾向:" + str(sp["rhythm"]).strip())
    if str(sp.get("tone", "") or "").strip():
        parts.append("语气:" + str(sp["tone"]).strip())
    habits = [str(x).strip() for x in (sp.get("habits") or []) if str(x).strip()]
    if habits:
        parts.append("说话习惯:" + "、".join(habits[:3]))
    forb = [str(x).strip() for x in (sp.get("forbidden") or []) if str(x).strip()]
    if forb:
        parts.append("绝不说:" + "、".join(forb[:2]))
    if parts:
        return "；".join(parts)
    if str(c.get("catchphrase", "") or "").strip():
        return "口癖(仅情绪高点一次点缀):" + str(c["catchphrase"]).strip()
    return ""


def _dev_goal_of(c) -> str:
    dev = c.get("development_plan")
    if isinstance(dev, dict):
        return str((dev or {}).get("growth_target", "") or "").strip()
    return str(dev or "").strip()


def _build_cast_pack(tl, p, csm=None) -> dict:
    """plot_run.cast_pack：本情节段出场角色的分级紧凑卡（决定「谁在写」）。

    protagonists = role==主角 的角色（全卡，含 behavior/speech_profile/faction/relations）
    active        = plot.roles 里其余出场角色（中卡：行为/语言/性格/关系/动态态）
    referenced    = 情节段文本命中的非出场角色 ≤2（极简，含与在场角色关系）
    空则对应空数组；dyn 来自 character_states（未落账 → 纯静态退化）。
    全表仍在 storyline.basic_info.characters 作 bible 反查，本块只给本段要用的，控制体积。
    """
    empty = {"protagonists": [], "active": [], "referenced": []}
    bi = (tl.basic_info or {}) if tl else {}
    chars = bi.get("characters") or []
    if not chars or p is None:
        return empty
    by_name = {}
    for c in chars:
        nm = str(c.get("name", "") or "").strip()
        if nm:
            by_name[nm] = c
    mc_name = ""
    for c in chars:
        if str(c.get("role", "") or "").strip() == "主角" and str(c.get("name", "") or "").strip():
            mc_name = str(c["name"]).strip()
            break
    if not mc_name:
        for c in chars:
            if str(c.get("name", "") or "").strip() and int(c.get("importance") or 0) == 1:
                mc_name = str(c["name"]).strip()
                break

    def _card(c, full=False) -> dict:
        nm = str(c.get("name", "") or "")
        d = _dyn_of(csm, nm)
        card = {
            "name": nm,
            "role": c.get("role", "") or "",
            "importance": int(c.get("importance") or 2),
            "identity": _clip(c.get("identity"), 40),
            "title": _clip(c.get("title"), 30),
            "personality": _clip(c.get("personality"), 60),
            "brief": _clip(c.get("brief"), 60),
            "speech": _speech_compact(c),
            "relation_to_mc": _rel_of(c, mc_name),
            "behavior": c.get("behavior") or {},
            "speech_profile": c.get("speech_profile") or {},
        }
        if d:
            card["dyn"] = d
        dg = _dev_goal_of(c)
        if dg:
            card["dev_goal"] = dg
        if full:
            card["gender"] = c.get("gender", "") or ""
            card["faction"] = c.get("faction", "") or ""
            card["golden_finger"] = _clip(c.get("golden_finger"), 60)
            rels = [{"name": str(r.get("name", "")), "relation": str(r.get("relation", "") or "")}
                    for r in (c.get("relations") or []) if isinstance(r, dict) and r.get("name")]
            if rels:
                card["relations"] = rels[:3]
        return card

    protagonists = [_card(by_name[nm], full=True) for nm in by_name
                    if str(by_name[nm].get("role", "") or "").strip() == "主角"]
    pro_names = {c["name"] for c in protagonists}

    active = []
    for nm in (getattr(p, "roles", None) or []):
        nm = str(nm or "").strip()
        if not nm or nm in pro_names or nm not in by_name:
            continue
        active.append(_card(by_name[nm]))
    act_names = {c["name"] for c in active}

    # referenced：情节段文本命中（name 出现在标题/分类/骨架/节点/hook 里）的非出场角色，≤2
    hay = " ".join([str(getattr(p, "name", "") or ""),
                    str(getattr(p, "category", "") or ""),
                    str(getattr(p, "sub_category", "") or ""),
                    str(getattr(p, "template_structure", "") or "")] + list(getattr(p, "hook_points", None) or []))
    referenced = []
    for nm in by_name:
        if nm in pro_names or nm in act_names:
            continue
        if nm in hay and len(referenced) < 2:
            c = by_name[nm]
            rel = _rel_of(c, mc_name)
            for an in act_names or pro_names:
                if not rel:
                    for r in (c.get("relations") or []):
                        if isinstance(r, dict) and str(r.get("name", "") or "").strip() in (act_names | pro_names):
                            rel = str(r.get("relation", "") or "")
                            break
            referenced.append({
                "name": nm, "role": c.get("role", "") or "",
                "identity": _clip(c.get("identity"), 30) or _clip(c.get("brief"), 40),
                "relation_to_mc": rel,
            })
    return {"protagonists": protagonists, "active": active, "referenced": referenced}


def _build_plot_run(tl, p, csm=None, draft=None, facts_by_plot=None):
    """组装一次情节段运行 plot_run：run 生命周期 + plot 全量 + 弧目标 + 线程状态 + 承诺 + 字数余量。

    全从已 load 的 tl 内存对象现算，不新增磁盘读；样文/场景判定不在此（走 get_pen_style）。
    draft/facts_by_plot 可选，用于派生 run 生命周期状态（prepare_plot_run/desk 三处同源共用）。
    """
    run = {
        "run": plot_run_lifecycle(tl, p, draft, facts_by_plot),
        "plot": {
            "id": p.id, "name": p.name, "category": p.category or "",
            "sub_category": getattr(p, "sub_category", "") or "",
            "words": getattr(p, "words", None),
            "cover_beats": getattr(p, "cover_beats", 0) or 0,
            "template_structure": getattr(p, "template_structure", "") or "",
            "thread_id": getattr(p, "thread_id", "") or "",
            "order": getattr(p, "order", 0) or 0,
            "resolves_plot_id": getattr(p, "resolves_plot_id", "") or "",
            "resolves_name": getattr(p, "resolves_name", "") or "",
            "hook_points": list(getattr(p, "hook_points", None) or []),
            "theme_hints": list(getattr(p, "theme_hints", None) or []),
            "roles": list(getattr(p, "roles", None) or []),
            "no_named_cast": bool(getattr(p, "no_named_cast", False)),
            "protocol_version": int(getattr(p, "protocol_version", 1) or 1),
            "planned_prose_units": int(getattr(p, "words", 0) or 0),
            "expected_facts": list(getattr(p, "expected_facts", None) or []),
            "outline_id": getattr(p, "outline_id", "") or "",
            "written_chapter": getattr(p, "written_chapter", 0) or 0,
            "is_payoff": bool(getattr(p, "resolves_plot_id", "") or ""),
        }
    }
    run["execution_brief"] = dict(getattr(p, "execution_brief", None) or {})
    run["character_impact"] = list(getattr(p, "character_impact", None) or [])
    # 弧目标
    arc = next((o for o in tl.outlines if o.id == getattr(p, "outline_id", "")), None)
    arc_goal = {"arc_id": "", "name": "", "notes": "", "arc_path": "",
                "stage_index": 0, "stage_name": "", "stage_desc": "", "stage_events": [],
                "word_span": [], "words_left_in_arc": 0}
    if arc:
        arc_goal.update(arc_id=arc.id, name=arc.name or "", notes=getattr(arc, "notes", "") or "",
                        arc_path=_outline_name_chain(tl, arc.id),
                        word_span=[getattr(arc, "start_word", 0) or 0,
                                   getattr(arc, "end_word", 0) or 0])
        si = int(getattr(p, "stage_index", 0) or 0)
        stages = list(getattr(arc, "stages", None) or [])
        if stages and 0 <= si < len(stages):
            st = stages[si]
            arc_goal["stage_index"] = si
            arc_goal["stage_name"] = st.get("name", "") if isinstance(st, dict) else ""
            arc_goal["stage_desc"] = (st.get("description", "") if isinstance(st, dict) else "")
            arc_goal["stage_events"] = list(st.get("events") or []) if isinstance(st, dict) else []
        oid = arc.id
        left = 0
        for q in tl.plots:
            if getattr(q, "outline_id", "") == oid and not (getattr(q, "written_chapter", 0) or 0):
                left += int(getattr(q, "words", None) or 0) or 0
        arc_goal["words_left_in_arc"] = left
    run["arc_goal"] = arc_goal
    # 线程状态
    tid = getattr(p, "thread_id", "") or ""
    tdef = {}
    for x in (tl.threads or []):
        xid = x.get("id") if isinstance(x, dict) else getattr(x, "id", "")
        if xid == tid:
            tdef = x if isinstance(x, dict) else {}
            break
    written, unwritten = [], []
    for q in tl.plots:
        if (getattr(q, "thread_id", "") or "") != tid:
            continue
        qc = getattr(q, "written_chapter", 0) or 0
        if qc:
            role = "收局" if (getattr(q, "resolves_plot_id", "") or "") else ""
            if not role:
                role = "设局" if any((getattr(r, "resolves_plot_id", "") or "") == q.id for r in tl.plots) else ""
            written.append({"id": q.id, "name": q.name or "", "chapter": int(qc), "role": role})
        else:
            unwritten.append(q.id)
    written.sort(key=lambda x: x["chapter"])
    run["thread"] = {
        "id": tid, "name": tdef.get("name", "") if isinstance(tdef, dict) else "",
        "desc": tdef.get("desc", "") if isinstance(tdef, dict) else "",
        "same_thread_written": written,
        "same_thread_unwritten": unwritten,
        "chain_note": f"{tid or '主线'} 已写 {len(written)} 条 / 未写 {len(unwritten)} 条",
    }
    # 承诺状态（只列设局/收局指向本情节段的项）
    prom = []
    for it in (tl.promises or []):
        if isinstance(it, dict) and (it.get("setup_plot_id") == p.id or it.get("payoff_plot_id") == p.id):
            prom.append({k: it.get(k) for k in ("id", "type", "desc", "status", "op",
                                                "setup_plot_id", "payoff_plot_id", "deadline_chapter")
                         if k in it})
    run["promise_state"] = prom
    # 场景/样文 query：服务端按当前情节段内容字段确定性推导（plot_dims.infer_plot_query），
    # 写作 agent 直接据此选样（pick_plot_sample），不再每轮手判。线程/承诺不参与推导。
    from libraries.plot_dims import infer_plot_query
    run["style_query"] = infer_plot_query(p, tl) if tl is not None else {}
    # 出场角色分级包（谁在写）：protagonists/active/referenced；dyn 由 csm 注入（未落账则静态退化）
    run["cast_pack"] = _build_cast_pack(tl, p, csm)
    return run


def _thread_projection(thread: dict | None) -> dict:
    """把 `_build_plot_run` 里的线程态压成 Writer 快照用的小投影。"""
    t = thread if isinstance(thread, dict) else {}
    written = [w for w in (t.get("same_thread_written") or []) if isinstance(w, dict)]
    unwritten = [str(x) for x in (t.get("same_thread_unwritten") or [])]
    return {"id": t.get("id", ""), "name": t.get("name", ""), "desc": t.get("desc", ""),
            "chain_note": t.get("chain_note", ""),
            "recent_written": written[-3:],
            "unwritten_count": len(unwritten), "unwritten_next": unwritten[:5]}


def _ordered_plots(tl) -> list:
    """按「弧顺序 → 阶段 → 次序 → 原列表序」排情节段。

    与 outline_agent/storyline_writer 的既有排序口径一致（它们都用
    `大纲位置→stage_index→order`），只多一个「原列表序」兜底，保证遗留数据
    （order 全为 0）仍按写入顺序稳定可复现。写作顺序、horizon、cast 预测共用它。
    """
    outline_pos = {getattr(o, "id", ""): i for i, o in enumerate(getattr(tl, "outlines", None) or [])}
    plots = list(getattr(tl, "plots", None) or [])
    index = {id(p): i for i, p in enumerate(plots)}
    return sorted(plots, key=lambda p: (
        outline_pos.get(getattr(p, "outline_id", ""), 9999),
        int(getattr(p, "stage_index", 0) or 0),
        int(getattr(p, "order", 0) or 0),
        index.get(id(p), 0)))


def _next_plot(tl, draft):
    """下一个待写情节段（written_chapter==0 且不在当前草稿内）——draft-aware，防章中途重复返回同一首。

    顺序取 `_ordered_plots`（弧→阶段→次序），不再直接用 JSON 里的 `tl.plots` 列表序——
    续规划 append 后列表序未必等于叙事序。
    """
    indraft = _draft_plot_ids(draft)
    for p in _ordered_plots(tl):
        if getattr(p, "written_chapter", 0) or 0:
            continue
        if p.id in indraft:
            continue
        return p
    return None


def _run_id_for(plot_id: str, based_revision: int) -> str:
    """PlotRun 身份：`plot_id@based_storyline_revision`。

    Plot 是稳定规划节点；PlotRun 是基于某次 storyline revision 的执行实例。同一未完成
    plot 在 revision 变更后（写期间被 replan/改设定）即产生新的 run，故 id 必须带 revision。
    """
    return f"{plot_id or 'plot'}@{int(based_revision or 0)}"


def plot_run_lifecycle(tl, plot, draft=None, facts_by_plot=None) -> dict:
    """派生 Plot Run 生命周期 Created → Drafting → Committed → Reconciled（WS1）。

    不建独立 run 文件：draft bridge 含该 plot=Drafting；plot.written_chapter>0 且该章
    bridge 已有 facts=Reconciled、无 facts=Committed（待对账）；否则 Created。
    run.id=plot_id@based_revision（revision 变更即新 run）；based_on_storyline_revision 供
    commit 时 refresh→compare→reconcile（WS2）。
    """
    pid = getattr(plot, "id", "") or ""
    based = int(getattr(tl, "storyline_revision", 0) or 0)
    if pid in _draft_plot_ids(draft):
        status, label = "Drafting", "正在写作"
    else:
        wc = int(getattr(plot, "written_chapter", 0) or 0)
        if wc:
            if (facts_by_plot or {}).get(pid):
                status, label = "Reconciled", "已对账"
            else:
                status, label = "Committed", "已提交"
        else:
            status, label = "Created", "待开始"
    return {
        "id": _run_id_for(pid, based),
        "plot_id": pid,
        "status": status,
        "label": label,
        "based_on_storyline_revision": based,
    }


def _deprecated_get_writing_context(book_id: str) -> dict:
    """已退役的全量上下文实现，仅保留在模块内供历史迁移审计，绝不注册为 Agent 工具。

    复用 get_book_state 全量 payload（get_storyline / get_book_detail 是其子集/重叠），
    追加就地提取的扁平字段：synopsis（outline）、protagonist（get_mc）、
    next_plot（第一个未写且不在草稿内的情节段，draft-aware，含 plot_id/name/roles/outline_id）、
    plot_run（该次情节段运行的收敛上下文：plot 全量/所在弧目标/线程状态/承诺/字数余量——
    场景判定与样文仍走 get_pen_style(query, k=1)，此处不替 agent 判场景、不带样文正文）。
    agent 逐情节段循环每轮只调本工具一次，避免重复读上下文；返回的是调用时的
    一致性快照，写入正文后若要继续规划，必须重新读取本工具或 get_story_state。
    style_card = 本笔名精简风格提醒（位于 payload 尾部，必读，防风格漂移）；
    完整风格用 get_pen_style 按需取。
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
    # next_plot：第一个未写情节段（draft-aware：written_chapter==0 且不在当前草稿内，防章中途重复返回同一首）
    draft = payload.get("draft") or {}
    runtime = _runtime_projection(book_id, tl, book_mgr.get(book_id), draft)
    next_p = runtime["next_plot"]
    if next_p is not None:
        payload["next_plot"] = {
            "plot_id": next_p.id, "name": next_p.name,
            "roles": list(getattr(next_p, "roles", None) or []),
            "outline_id": getattr(next_p, "outline_id", "") or "",
        }
        payload["plot_run"] = _build_plot_run(tl, next_p, _load_char_states(book_id), draft=draft)
    else:
        payload["next_plot"] = None
        payload["plot_run"] = None
    # next_chapter：进行中草稿的章号优先，否则 current_chapter + 1（供写作任务卡显示「该写第几章」）
    book = payload.get("book") or {}
    draft = payload.get("draft")
    if draft and draft.get("chapter_num"):
        next_chapter = draft["chapter_num"]
    else:
        next_chapter = (book.get("current_chapter") or 0) + 1
    payload["next_chapter"] = next_chapter
    # pen_name + style_card：注入精简风格卡（每轮提醒防漂移；完整规则走 get_pen_style）
    payload["pen_name"] = (tl.pen_name if tl else "") or book.get("pen_name") or ""
    profile = _profile_for(tl) if tl else None
    payload["style_card"] = profile.build_style_card() if profile else _default_style_card()
    payload["runtime"] = {k: v for k, v in runtime.items() if k not in ("next_plot", "draft")}
    payload["context_fingerprint"] = _context_fingerprint(book_id, tl, payload.get("plot_run"), profile) if next_p else ""
    pending = _commit_journal_path(book_id)
    if os.path.exists(pending):
        payload["commit_recovery"] = {"pending": True, "path": pending,
                                      "action": "检测到未完成章节提交；请先核对并恢复后继续写作"}
    if tl:
        ps = load_planning_state(book_id, tl, book_mgr.get(book_id))
        remaining = sum(1 for p in (tl.plots or [])
                        if not (getattr(p, "written_chapter", 0) or 0) and p.id not in _draft_plot_ids(draft or {}))
        boundary = detect_story_boundary(
            written_until_word=int(runtime["display_written_words"]),
            committed_until_word=int(ps.get("committed_until_word") or 0),
            remaining_plots=remaining,
            words_per_batch=int(tl.words_per_chapter or 3000),
            storyline_revision=int(getattr(tl, "storyline_revision", 0) or 0),
            last_replan=ps.get("last_replan") or {},
        )
        payload["planning"] = {
            "target_word_budget": ps.get("target_word_budget", 0),
            "committed_until_word": ps.get("committed_until_word", 0),
            "written_until_word": ps.get("written_until_word", 0),
            "display_written_words": runtime["display_written_words"],
            "committed_words": runtime["committed_words"],
            "draft_words": runtime["draft_words"],
            "storyline_revision": getattr(tl, "storyline_revision", 0),
            "boundary": boundary,
            # 与写作台规划面板共享同一导航层；这些字段是方向提示，
            # 不会把 forecast 误当成可直接写入的正式 plot。
            "horizon": ps.get("horizon") or {},
            "future_intents": ps.get("future_intents") or [],
            "story_questions": ps.get("story_questions") or [],
            "character_intents": ps.get("character_intents") or [],
            "last_replan": ps.get("last_replan") or {},
        }
    return payload


def _staged_cast_projection(cast: dict, staged: dict) -> dict:
    """把本章已 reconcile 的人物变化覆盖到下一段的运行态，不落正式角色状态机。"""
    from libraries.character_state import _EVENT_FIELD
    result = json.loads(json.dumps(cast or {}, ensure_ascii=False))
    dynamic = {}
    for delta in (staged.get("plot_deltas") or []):
        for row in (delta.get("character_changes") or []):
            name = str((row or {}).get("name") or "").strip()
            for event in ((row or {}).get("events") or []):
                field = _EVENT_FIELD.get(str((event or {}).get("type") or ""))
                if name and field and (event or {}).get("to") is not None:
                    dynamic.setdefault(name, {})[field] = str(event["to"])
    for group in result.values():
        if not isinstance(group, list):
            continue
        for card in group:
            if isinstance(card, dict) and card.get("name") in dynamic:
                card.setdefault("dyn", {}).update(dynamic[card["name"]])
                card["state_source"] = "staged_fact"
    return result


def prepare_plot_run(book_id: str) -> dict:
    """准备当前唯一待写 Plot，返回 Writer 输入快照和提交 receipt。

    成功后请使用返回的 token 提交一次；服务端负责 Plot 顺序和章节编排。
    """
    from libraries.plot_run_state import load_staged, previous_change, retrieved_memory
    from libraries.plot_commit_tokens import issue as issue_commit_token, find_prepared
    from libraries.write_flow import chapter_status
    tl = _require_tl(book_id)
    book = book_mgr.get(book_id)
    draft = _draft_read(book_id) or {}
    runtime = _runtime_projection(book_id, tl, book, draft)
    p = runtime["next_plot"]
    profile = _profile_for(tl)
    if p is None:
        return {"ok": True, "run": None, "reason": "no_pending_plot"}
    staged = load_staged(book_id)
    raw_run = _build_plot_run(tl, p, _load_char_states(book_id), draft=draft)
    run = raw_run["run"]
    style_profile = profile
    if style_profile is None:
        try:
            style_profile = _resolve_profile(book_id)
        except Exception:
            style_profile = None
    style_card = (style_profile.build_style_card() if style_profile
                  else _default_style_card())
    # Materialize planning state before deriving the retry key; first and
    # subsequent prepares must observe the same source set.
    ps = load_planning_state(book_id, tl, book)
    from libraries.style_snapshot import (build_snapshot, selected_sample_digest,
                                           snapshot_matches)
    base_style_snapshot = build_snapshot(style_profile, style_card)
    base_version_vector = _context_version_vector(book_id, tl, draft, base_style_snapshot)
    run["context_fingerprint"] = _context_fingerprint(
        book_id, tl, raw_run, profile, style_snapshot=base_style_snapshot,
        version_vector=base_version_vector)
    prepared_key = run["context_fingerprint"]
    flow_id = os.environ.get("NOVEL_WRITE_FLOW_ID", "adhoc")
    child_run_id = os.environ.get("NOVEL_WRITE_CHILD_RUN_ID", run["id"])
    # Retry the same prepared Plot from its immutable snapshot. This check must
    # happen before sample selection because picking mutates avoidance history.
    existing = find_prepared(
        book_id=book_id,
        flow_id=flow_id,
        plot_id=p.id,
        storyline_revision=int(getattr(tl, "storyline_revision", 0) or 0),
        prepared_key=prepared_key,
    )
    if existing:
        snapshot = existing[1].get("prepared_snapshot")
        saved_style = ((snapshot or {}).get("style") or {}).get("snapshot")
        sample_id = (saved_style or {}).get("sample_id", "")
        current_digest = selected_sample_digest(style_profile, sample_id) if style_profile else ""
        if isinstance(snapshot, dict) and saved_style and current_digest is not None and snapshot_matches(
                style_profile, style_card, saved_style, current_digest):
            return snapshot
    execution = dict(raw_run["plot"])
    # 新数据 roles 是严格契约；旧数据仅用结构化字段补齐，不从正文猜角色。
    protocol = int(execution.get("protocol_version") or 1)
    role_warnings = []
    if protocol >= 2 and not execution.get("no_named_cast") and not execution.get("roles"):
        raise RuntimeError("roles_v2：当前 Plot 缺少出场角色；请先补齐角色或显式标记 no_named_cast")
    resolution = {"mode": "v2_strict" if protocol >= 2 else "legacy_fallback", "confidence": "high", "warnings": role_warnings}
    if protocol < 2 and not execution.get("roles"):
        candidates = []
        for row in (raw_run.get("character_impact") or []):
            if isinstance(row, dict) and row.get("name"):
                candidates.append(str(row["name"]))
        for row in ((raw_run.get("execution_brief") or {}).get("actors") or []):
            if isinstance(row, str): candidates.append(row)
        execution["roles"] = list(dict.fromkeys(candidates))
        resolution.update(confidence="medium", warnings=["legacy Plot 的角色来自结构化规划回退" if candidates else "legacy Plot 未提供结构化出场角色"])
    execution.update({"brief": raw_run.get("execution_brief") or {},
                      "arc": raw_run.get("arc_goal") or {},
                      "must_happen": list((raw_run.get("execution_brief") or {}).get("must_happen") or []),
                      "must_not_happen": list((raw_run.get("execution_brief") or {}).get("must_not_happen") or [])})
    bridges = list(draft.get("bridges") or [])
    recent = []
    for i, bridge in enumerate(reversed(bridges[-2:])):
        text = str(bridge.get("text") or "")
        recent.append({"distance": i + 1, "plot_id": bridge.get("plot_id"),
                       "plot_name": bridge.get("plot_name"),
                       "summary": (bridge.get("facts") or {}),
                       "ending": text[-800:] if i == 0 else text[-500:]})
    terms = list(getattr(p, "roles", None) or []) + [getattr(p, "thread_id", ""), getattr(p, "name", "")]
    # horizon 与剩余计数都走 _ordered_plots（弧→阶段→次序），与写作顺序同一口径
    indraft = _draft_plot_ids(draft)
    pending = [q for q in _ordered_plots(tl)
               if q.id != p.id and not getattr(q, "written_chapter", 0) and q.id not in indraft]
    horizon_plots = pending[:1]
    remaining = sum(1 for q in _ordered_plots(tl)
                    if not getattr(q, "written_chapter", 0) and q.id not in indraft)
    boundary = detect_story_boundary(
        written_until_word=runtime["display_written_words"],
        committed_until_word=int(ps.get("committed_until_word") or 0), remaining_plots=remaining,
        words_per_batch=int(tl.words_per_chapter or 3000),
        storyline_revision=int(getattr(tl, "storyline_revision", 0) or 0),
        last_replan=ps.get("last_replan") or {})
    status = chapter_status(book_id, tl, draft, needs_replan=bool(boundary.get("needs_replan")))
    # 风格卡已在幂等检查前解析；本次只在没有可恢复快照时选一篇样文。
    sample = {"text": "", "receipt": {}, "meta": {}}
    sample_problem = ""
    try:
        picked = pick_plot_sample(book_id, query=raw_run.get("style_query") or {})
        if picked.get("ok") and (picked.get("text") or "").strip():
            sample = {"text": picked.get("text") or "", "receipt": picked.get("sample_receipt") or {},
                      "meta": picked.get("sample") or {}}
        else:
            sample_problem = str(picked.get("error") or picked.get("message") or "样文池未返回可用样文")
    except Exception as exc:  # noqa: BLE001
        sample_problem = f"取样异常：{exc}"
    if sample_problem:
        # 设计取舍：样文池不可用时**仍允许写作**（style card 是保底，样文本就是可选参考），
        # 但绝不假装取到了样文——快照里显式标 unavailable、回执不记 digest，并留日志，
        # 让「这次没有样文」在审计面可辨（此前静默降级成空样文 + 空回执）。
        sample["receipt"] = {"status": "unavailable", "reason": sample_problem}
        _log.warning("Plot 取样不可用，按无样文继续（style card 保底）book=%s plot=%s: %s",
                     book_id, p.id, sample_problem)
    # pick_plot_sample 的独立旧入口不带动态 cast；prepare 是唯一 Writer 入口，
    # 所以在这里以完整七 Packet 的指纹重新绑定 receipt。
    style_snapshot = build_snapshot(style_profile, style_card, sample["receipt"])
    version_vector = _context_version_vector(book_id, tl, draft, style_snapshot)
    run["context_fingerprint"] = _context_fingerprint(
        book_id, tl, raw_run, profile, style_snapshot=style_snapshot,
        version_vector=version_vector)
    sample["receipt"]["context_fingerprint"] = run["context_fingerprint"]
    brief = raw_run.get("execution_brief") or {}
    must = list(brief.get("must_happen") or [])
    should = list(brief.get("should_happen") or [])
    may = list(brief.get("may_happen") or [])
    execution.update({"entry_state": brief.get("entry_state") or {}, "success_criteria": brief.get("success_criteria") or must,
                      "must": must, "should": should, "may": may,
                      "chapter_progress": {"target_words": status["target_words"], "written_words": status["written_words"],
                                           "plot_index": len(bridges) + 1, "is_likely_last_plot": bool(status["chapter_ready"] or not status["has_next_committed_plot"])}})
    configured_memory_budget = int(os.environ.get("WRITE_MEMORY_BUDGET", "2500") or 2500)
    context_budget = int(os.environ.get("WRITE_CONTEXT_BUDGET", "12000") or 12000)
    output_reserve = int(os.environ.get("WRITE_OUTPUT_RESERVE", "3000") or 3000)
    fixed_packets = {"execution": execution, "cast": raw_run.get("cast_pack") or {},
                     "style_card": style_card, "sample": sample.get("text") or ""}
    fixed_cost = len(json.dumps(fixed_packets, ensure_ascii=False)) // 2
    # 2500 是软上限；当本 Plot 的角色/样文较大时自动给 memory 让路。
    memory_budget = max(400, min(configured_memory_budget, context_budget - fixed_cost - output_reserve))
    # 近段尾部按预算收缩；previous_change 单列，不占此预算。
    recent = recent[:2]
    used = sum(len(str(x.get("ending") or "")) // 2 for x in recent)
    retrieved = retrieved_memory(book_id, terms, limit=max(1, min(6, (memory_budget - min(memory_budget, used)) // 250 or 1)))
    prepared = {
        "ok": True,
        "run": {"id": run["id"], "expires_in_seconds": 20 * 60,
                "cast_resolution": resolution,
                "context_version_vector": version_vector},
        "execution": execution,
        "previous_change": previous_change(book_id),
        "cast": {**_staged_cast_projection(raw_run.get("cast_pack") or {}, staged), "resolution": resolution},
        "memory": {"budget_tokens": memory_budget, "recent": recent, "retrieved": retrieved},
        "horizon": {"preserve": list(getattr(horizon_plots[0], "expected_facts", None) or []) if horizon_plots else [],
                    "do_not_resolve_yet": [], "next_function": (horizon_plots[0].category or horizon_plots[0].name) if horizon_plots else None,
                    "arc_destination": (raw_run.get("arc_goal") or {}).get("notes", ""), "boundary": boundary,
                    "chapter_status": status},
        # 当前线程态（最小契约要求 current_thread 明示；只投影最近已写 + 未写计数/前几个 id，
        # 不把整条线程的 id 列表塞进快照——那是导航信息，不是上下文主体）。
        "thread": _thread_projection(raw_run.get("thread")),
        "style": {"card": style_card, "sample": sample, "snapshot": style_snapshot},
    }
    token = issue_commit_token(
        book_id=book_id, flow_id=flow_id, child_run_id=child_run_id,
        plot_id=p.id, storyline_revision=int(getattr(tl, "storyline_revision", 0) or 0),
        context_fingerprint=run["context_fingerprint"], sample_receipt=sample["receipt"],
        prepared_key=prepared_key, version_vector=version_vector,
        prepared_snapshot=prepared,
    )
    # 令牌已在 issue 时写进快照（账本一次写入即可），无需再 attach 一次全量重写。
    prepared["run"]["commit_token"] = token
    return prepared


def get_storyline(book_id: str) -> dict:
    """读取一本书的故事线（timeline）JSON：弧/情节段/线程/内涵/基础设定。"""
    tl = _require_tl(book_id)
    return tl.to_dict()


def get_story_state(book_id: str) -> dict:
    """聚合故事状态：事实摘要、当前承诺、远期意图、线程/承诺、人物动态及规划边界。"""
    tl = book_mgr.load_storyline(book_id)
    if tl is None:
        raise RuntimeError(f"「{book_id}」无故事线（storyline.json）")
    book = book_mgr.get(book_id)
    state = load_planning_state(book_id, tl, book)
    draft = _draft_read(book_id) or {}
    runtime = _runtime_projection(book_id, tl, book, draft)
    draft_ids = _draft_plot_ids(draft)
    unwritten = [p for p in (tl.plots or [])
                 if not (getattr(p, "written_chapter", 0) or 0) and p.id not in draft_ids]
    current = runtime["next_plot"]
    boundary = detect_story_boundary(
        written_until_word=int(runtime["display_written_words"]),
        committed_until_word=int(state.get("committed_until_word") or 0),
        remaining_plots=len(unwritten),
        words_per_batch=int(tl.words_per_chapter or 3000),
        storyline_revision=int(getattr(tl, "storyline_revision", 0) or 0),
        last_replan=state.get("last_replan") or {},
    )
    csm = _load_char_states(book_id)
    char_dyn = []
    for cs in getattr(csm, "characters", []):
        name = str(getattr(cs, "name", "") or "")
        if name:
            char_dyn.append({"name": name, **_dyn_of(csm, name)})
    return {
        "ok": True,
        "book_id": book_id,
        "storyline_revision": int(getattr(tl, "storyline_revision", 0) or 0),
        "facts": {
            "written_until_word": int(state.get("written_until_word") or 0),
            "committed_words": runtime["committed_words"],
            "draft_words": runtime["draft_words"],
            "display_written_words": runtime["display_written_words"],
            "raw_codepoints": runtime["committed_raw_codepoints"] + runtime["draft_raw_codepoints"],
            "current_chapter": int(getattr(book, "current_chapter", 0) or 0) if book else 0,
            "completed_plot_ids": [p.id for p in (tl.plots or []) if getattr(p, "written_chapter", 0)],
            "character_dynamics": char_dyn,
        },
        "staged_facts": _staged_story_state_for_api(book_id),
        "committed": {
            "until_word": int(state.get("committed_until_word") or 0),
            "current_arc_id": getattr(current, "outline_id", "") if current else "",
            "current_plot": ({"id": current.id, "name": current.name,
                              "outline_id": current.outline_id, "words": current.words}
                             if current else None),
            "remaining_plot_count": len(unwritten),
        },
        "forecast": {
            "future_intents": state.get("future_intents") or [],
            "character_intents": state.get("character_intents") or [],
            "decision_points": state.get("decision_points") or [],
        },
        "story_questions": state.get("story_questions") or [],
        "active_threads": state.get("active_threads") or tl.threads or [],
        "critical_promises": state.get("critical_promises") or tl.promises or [],
        "target_word_budget": int(state.get("target_word_budget") or 0),
        "boundary": boundary,
        "last_replan": state.get("last_replan") or {},
        "runtime": {k: v for k, v in runtime.items() if k not in ("next_plot", "draft")},
    }


def _staged_story_state_for_api(book_id: str) -> dict:
    """Planning Agent 可见的暂存事实与 forecast 分离，绝不混入正式 facts。"""
    from libraries.plot_run_state import load_staged
    staged = load_staged(book_id)
    return {"chapter_num": staged.get("chapter_num", 0),
            "plot_deltas": staged.get("plot_deltas") or []}




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


def _arc_item(t) -> dict:
    """平级独立弧 → 紧凑 dict 供 agent 参考。"""
    return {
        "id": t.id,
        "name": t.name,
        "tags": t.tags,
        "total_words": t.total_words,
        "description": (t.description or "")[:200],
        "min_words": t.min_words,
        "max_words": t.max_words,
    }


def query_arc_library(keyword: str = "", tags: str = "") -> dict:
    """查情节弧库：按标签（任一命中）/关键词（名称）返回**平级独立弧模板**清单。
    弧库无父子层级（每行一个弧，各带 tags/描述/内涵，可单独挑选）。total_words = 该弧
    整段字数量，别直接 × 每章字数当弧的 start_word/end_word。"""
    kw = (keyword or "").strip()
    tag_list = [x.strip() for x in (tags or "").replace("，", " ").replace(",", " ").split() if x.strip()]
    rows = struct_lib.search(tags=tag_list)
    if kw:
        rows = [t for t in rows if kw in (t.name or "")]
    return {"templates": [_arc_item(t) for t in rows[:40]]}


def query_plots(category: str = "", context: str = "", keyword: str = "") -> dict:
    """查情节段库：按分类/场景/关键词（名称）返回情节段模板清单。"""
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
    """查笔名档案：返回现有笔名（含风格摘要 + 平台注册状态），供外部 agent 选笔名/写作风格参考。

    style 从规则库统计（句式风格/禁止内容条数）+ build_style_card 精简摘要；sentence_length 等
    结构化字段已随写法资产废弃，不再返回。platform_accounts 为每平台注册信息（agent 只读）。
    """
    from libraries.style_rules import StyleRuleLibrary
    kw = (keyword or "").strip()
    rows = profiles.list_all()
    if kw:
        rows = [p for p in rows if kw in (p.pen_name or "") or kw in (p.description or "")]
    srl = StyleRuleLibrary()
    return {"profiles": [{
        "id": p.id, "pen_name": p.pen_name, "description": p.description,
        "assigned_books": list(p.assigned_books or [])[:10],
        "platform_accounts": p.platform_accounts or {},
        "registered_platforms": p.registered_platforms(),
        "style": {
            "language": (p.language or "zh"),
            "humor_style": (p.style_fingerprint or {}).get("humor_style", ""),
            "action_style": (p.style_fingerprint or {}).get("action_style", ""),
            "style_rules_count": sum(1 for r in srl.rules_for(p.id)
                                     if r.kind == "prefer" and r.enabled),
            "forbidden_count": sum(1 for r in srl.rules_for(p.id)
                                   if r.kind == "ban" and r.enabled),
            "summary": p.build_style_card(),
        },
    } for p in rows[:30]]}


_STYLE_RECENT = []   # 进程内近期已注入的样文 id(自动避重,dsh 单任务内跨情节段生效)
_STYLE_RECENT_MAX = 12


def _resolve_profile(book_id: str = "", profile_id: str = ""):
    """book_id(按书绑笔名) 优先 → profile_id → 默认笔名(枫落)；无可用则抛错。"""
    from libraries.style_rules import DEFAULT_PROFILE_ID
    profile = None
    if book_id:
        try:
            tl = book_mgr.load_storyline(book_id)
            profile = _profile_for(tl) if tl else None
        except Exception:
            profile = None
    if profile is None and profile_id:
        profile = profiles.get(profile_id)
    if profile is None:
        profile = profiles.get(DEFAULT_PROFILE_ID)
    if profile is None:
        raise RuntimeError("没有可用的笔名档案")
    return profile


def _load_avoid(profile_id: str, extra=None) -> list:
    """近期避重 = 进程内(_STYLE_RECENT, 单任务内) ∪ 持久化历史(按笔名,跨任务/会话);去重保序。"""
    _persist = style_samples.load_pick_history(profile_id)
    return list(dict.fromkeys([x for x in (list(extra or []) + list(_STYLE_RECENT) + _persist) if x]))


def _record_pick(profile_id: str, sample_ids) -> None:
    """把**实际注入过**的样文记入进程内近期 + 持久化历史。

    任一注入路径都记(不只 query 取样):多样封顶/legacy/兜底若真实注入了词条,同样进避重,
    否则「最常见的注入方式」会绕过近期避重,同篇反复被灌。
    """
    sample_ids = [x for x in (sample_ids or []) if x]
    if not sample_ids:
        return
    for _sid in sample_ids:
        if _sid not in _STYLE_RECENT:
            _STYLE_RECENT.append(_sid)
    del _STYLE_RECENT[:-_STYLE_RECENT_MAX]
    style_samples.record_pick_history(profile_id, sample_ids)


def get_pen_style(book_id: str = "", profile_id: str = "", query: dict = None,
                  k: int = 3, avoid: list = None, no_ref: bool = False) -> dict:
    """读一个笔名的完整写作风格（句式风格+禁止内容+语言习惯+通用纪律），写作 agent 动笔前必读。

    book_id 与 profile_id 至少其一：book_id 优先按书绑定的笔名解析；否则按 profile_id；
    都无则默认笔名（枫落）。query=多维权表(英文键)，如 {"scene":["investigation"],
    "cast":"solo","dramatic_state":"uneasy"}：给则 STYLE REFERENCE 用加权随机从词条池抽 ≤k 条
    (硬过滤→软加权→加权随机→自动避重，k 默认 3)；不给则多样封顶注入。avoid 可追加指定避开 id。
    no_ref=True 只取 md/rules、不注入任何样文（写作流程每 Plot Run 的**唯一**样文请用
    pick_plot_sample，避免多样预注入与单篇并存稀释「每段只参考一篇」）。任何真实注入都会记避重历史。
    返回 prose style_rules（权威）+ 结构化 style/forbidden 列表 + samples(维度视图)，
    供逐条遵守/精确引用。信息不足时优先用本工具重读（独立薄工具，不纠缠全量上下文）。
    """
    from libraries.style_rules import StyleRuleLibrary
    profile = _resolve_profile(book_id, profile_id)
    rules = [r for r in StyleRuleLibrary().rules_for(profile.id) if r.enabled]
    # 样本驱动:存在 styles/<pen>.md → style_rules = 该 md(负约束/原则);
    # 其后若存在 storage/style_refs/<pen>.reference.txt → 追加为 STYLE REFERENCE 人工样本(最高风格来源)。
    # 无 md → 回退规则拼装(legacy,数组照旧)。每次现读文件,手改 md/样本即刻生效。
    style_md_text = style_md.read_style_md(profile)
    ref_text = ref_meta = None
    if not no_ref:
        # STYLE REFERENCE:全局词条库。给 query → 加权随机抽 ≤k 条(自动近期避重);无 query → 多样封顶。
        # md 单独保留、不参与裁剪;无 JSON → 旧文件兜底。有注入即记避重(见 _record_pick)。
        ref_text, ref_meta = style_samples.build_ref_text_for_profile(
            profile, query=query, k=int(k or 3), avoid=_load_avoid(profile.id, avoid))
        if ref_meta and ref_meta.get("selected"):
            _record_pick(profile.id, ref_meta.get("selected") or [])
    sample_driven = style_md_text is not None
    if sample_driven:
        style_rules = style_md_text
        if ref_text:
            style_rules = style_rules + "\n\n" + ref_text
        # md 模式结构性数组清空:① 与「不建禁词表/不机械规避」哲学一致;② 使本工具结果 <8KB
        # 免被 dsh 裁剪(否则 md+样本+大数组超限,style_rules 中段被裁)。非 md 笔名不受影响。
        style_list, forbidden = [], {"words": [], "patterns": []}
        ref_summary = _ref_summary(ref_meta)
    else:
        style_rules = profile.build_writing_prompt()
        style_list = [r.pattern for r in rules if r.kind == "prefer" and r.pattern]
        forbidden = {
            "words": [{"word": r.pattern, "replacement": "、".join(r.replacements or []), "desc": r.desc}
                      for r in rules if r.kind in ("ban", "word") and r.replacements and r.pattern],
            "patterns": [{"pattern": r.pattern, "desc": r.desc, "severity": r.severity}
                         for r in rules if r.kind == "ban" and not r.replacements and r.pattern],
        }
        ref_summary = ""
    # 场景标签结构化视图(无正文,保持薄):写作 agent 按 id/title/scene_tags 就近参考
    # 对应场景的样本(正文已注入;需要单条全文用 get_style_sample)。
    # 笔名已选样文 → 只列所选;未选 → 全量(兼容旧行为)。
    meta_samples = []
    _pool = style_samples.pool_for(profile)  # 已按 sample_books/sample_ids 收窄到笔名可用池
    if _pool:
        meta_samples = [{"id": s.id, "title": s.title, "scene_tags": s.scene_tags, "dims": s.dims,
                         "word_count": s.word_count, "source": s.source,
                         "source_book": s.source_book} for s in _pool]
    return {
        "pen_name": profile.pen_name,
        "language": profile.language or "zh",
        "profile_id": profile.id,
        "style_rules": style_rules,
        "style": style_list,
        "forbidden": forbidden,
        "language_hint": profile.build_language_hints(),
        "discipline": "【通用写作纪律】" + "；".join(profile.discipline_items()),
        # 注入观测:实际注入哪些样文/共多少条(ref_summary);samples = 场景标签结构化
        # 视图(id/title/scene_tags/字数,无正文),想聚焦某场景再 get_style_sample 取全文。
        "ref_summary": ref_summary,
        "samples": meta_samples,
    }


def pick_plot_sample(book_id: str = "", query: dict = None, profile_id: str = "") -> dict:
    """[Plot Run] 给「当前这一个情节段」抽**恰好 1 篇**样文并记避重历史。

    服务端确定性链路：_next_plot(第一个未写且不在草稿内) → plot_run.style_query
    (plot_dims.infer_plot_query 按情节段内容推导，见 _build_plot_run) → 加权随机 k=1 →
    _record_pick。query 可显式覆盖（缺省用推导值）。线程/承诺只作上下文、不参与选样。

    写作 agent 每情节段运行调一次，text 就是本段唯一 STYLE REFERENCE 单篇样文（已含
    `# 场景:` 头 + 引导语），语言参考随运行自然漂移；同场景其余样文用 get_style_sample 备查。
    """
    tl = book_mgr.load_storyline(book_id)
    if tl is None:
        raise RuntimeError(f"「{book_id}」无故事线（storyline.json）")
    draft = _draft_read(book_id)
    p = _next_plot(tl, draft)
    if p is None:
        return {"ok": False, "error": "没有待写的情节段（已全部写完，或全部落在进行中草稿里）。"}
    run = _build_plot_run(tl, p)
    q = dict(query) if query else (run.get("style_query") or {})
    profile = _resolve_profile(book_id, profile_id)
    pool = style_samples.pool_for(profile)
    if not pool:
        return {"ok": False, "error": "全局样文池无词条，无法单篇取样；请先在 /samples 入库人工样文。"}
    picked, meta = style_samples.pick_samples(pool, query=q, k=1, avoid=_load_avoid(profile.id))
    # 注：空 query 时 _score_samples 对全池同分(declared 空) → 轮盘在整池上随机 1 条，不落多样分支。
    if not picked:
        return {"ok": False, "error": "k=1 取样为空（无可取样本）。"}
    _record_pick(profile.id, meta.get("selected") or [s.id for s in picked])
    s = picked[0]
    rendered = style_samples.render_reference([s])
    from libraries.style_snapshot import build_snapshot, rendered_sample_digest
    cast_names = [x.get("name") for group in (run.get("cast_pack") or {}).values()
                  if isinstance(group, list) for x in group if isinstance(x, dict) and x.get("name")]
    sample_receipt = {"sample_id": s.id, "profile_id": profile.id,
                      "plot_id": p.id, "cast_names": cast_names,
                      "content_digest": rendered_sample_digest(rendered)}
    style_snapshot = build_snapshot(profile, profile.build_style_card(), sample_receipt)
    fingerprint = _context_fingerprint(book_id, tl, run, profile,
                                        style_snapshot=style_snapshot)
    return {
        "ok": True,
        "plot": {"id": p.id, "name": p.name},
        "query": q,
        "sample": {"id": s.id, "title": s.title or s.id, "word_count": s.word_count},
        "text": rendered,
        "ref_summary": _ref_summary(meta),
        "sample_receipt": {**sample_receipt, "context_fingerprint": fingerprint,
                           "style_snapshot": style_snapshot},
    }


def _ref_summary(meta) -> str:
    """把样文注入 meta 压成一行,供 agent 观测预算使用情况。"""
    if not meta:
        return ""
    mode = meta.get("mode", "")
    sel = meta.get("selected") or []
    total = meta.get("total", 0)
    chars = meta.get("chars", 0)
    max_chars = meta.get("max_chars")
    if mode == "pick":
        ids = ",".join(sel) if sel else "-"
        fb = " [兜底]" if meta.get("fallback") else ""
        return (f"STYLE REFERENCE(按场景): 抽取 {len(sel)} 条 [{ids}] "
                f"≈{chars} 字符{fb}")
    if mode == "samples":
        ids = ",".join(sel) if sel else "-"
        budget = "多样封顶≤{0}".format(max_chars) if max_chars else "多样(不限)"
        return f"STYLE REFERENCE: 注入样文 {len(sel)}/{total} 条 [{ids}] ≈{chars} 字符 {budget}"
    return f"STYLE REFERENCE(legacy 旧文件): 注入 {meta.get('count', 0)}/{total} 段 ≈{chars} 字符"


def add_style_rule(profile_id: str, kind: str, pattern: str, desc: str = "",
                   replacements: list = None, severity: str = "warning") -> dict:
    """给笔名加一条风格规则：kind=prefer 句式风格（正向指令，如「句长偏短」）| ban 禁止内容
    （replacements 有值=AI高频词自动去AI味替换，空=硬禁句式审查检测）。供 agent 写完发现
    AI 味词或想调整句式时自行维护该笔名风格。"""
    from libraries.style_rules import StyleRule, StyleRuleLibrary
    kind = (kind or "").strip()
    pattern = (pattern or "").strip()
    if kind not in ("ban", "prefer") or not pattern:
        raise RuntimeError("kind 必须是 prefer/ban，pattern 必填")
    if not profiles.get(profile_id):
        raise RuntimeError(f"笔名 {profile_id} 不存在")
    srl = StyleRuleLibrary()
    n = 1
    existing = {r.id for r in srl.rules}
    while f"{kind}_{n}" in existing:
        n += 1
    rule = StyleRule(id=f"{kind}_{n}", kind=kind, profile_id=profile_id,
                     pattern=pattern, desc=desc, severity=severity,
                     replacements=[str(x) for x in (replacements or []) if str(x).strip()])
    srl.rules.append(rule)
    srl._save()
    return {"ok": True, "rule": rule.to_dict()}


def delete_style_rule(rule_id: str) -> dict:
    """删除一条笔名风格规则（按规则 id）。"""
    from libraries.style_rules import StyleRuleLibrary
    srl = StyleRuleLibrary()
    before = len(srl.rules)
    srl.rules = [r for r in srl.rules if r.id != rule_id]
    if len(srl.rules) == before:
        return {"ok": False, "error": f"规则 {rule_id} 不存在"}
    srl._save()
    return {"ok": True, "deleted": rule_id}


def _samples_ctx(profile_id: str = ""):
    """(兼容壳)样文池现为**全局词条库**(不分笔名);profile_id 仅占位,不再按笔名解析文件。"""
    return None, None, style_samples.load_samples() or []


def add_style_sample(profile_id: str, text: str, title: str = "", scene_tags: list = None,
                     source: str = "", note: str = "", replace_id: str = "",
                     no_warn: bool = None, dims: dict = None,
                     source_book: str = "") -> dict:
    """给**全局样文池**加/替换一个词条(STYLE REFERENCE 人工样文,不分笔名,各笔名写作共享)。

    供 agent 把参考书里的**完整连续场景**按段截取入库(不拆技巧、不润色、保留普通解释句;
    别单喂金句/纯高潮)。scene_tags 为过渡期中文标签(可空);dims=多维权表(英文键,8 维:scene/
    narrative_action 列表 + dramatic_state/cast/dialogue_density/information_density/pace/pov 单值,
    缺维=选择器通配;非法值被丢弃);replace_id 给出则替换该条否则追加;no_warn=True 人工确认保留。
    source_book=机器可读来源书键(如 `十日终焉` / `冰河末世，我囤积了百亿物资`,与书库书名一致),
    空则自动从 source 的《》书名推导——供「全局池按来源书隔离」与 /samples 来源分组。
    profile_id 仅向后兼容占位。服务端算字数并再生 reference.txt 镜像;注入按 query 加权随机。
    """
    cur = style_samples.load_samples() or []
    txt = (text or "").strip()
    if not txt:
        raise RuntimeError("样文文本不能为空(应是一段完整连续场景原文)")
    sb = (source_book or "").strip() or style_samples._book_from_source(source)
    records = [s.to_dict() for s in cur]
    meta = {"title": (title or "").strip(),
            "scene_tags": [str(t).strip() for t in (scene_tags or []) if str(t).strip()],
            "source": (source or "").strip(), "source_book": sb, "note": (note or "").strip(),
            "no_warn": bool(no_warn)}
    if dims is not None:
        meta["dims"] = dims
    rid = (replace_id or "").strip()
    if rid:
        for r in records:
            if r.get("id") == rid:
                if no_warn is None:
                    meta.pop("no_warn")  # 替换但未指定 → 保留原 no_warn
                if dims is None:
                    meta.pop("dims", None)  # 替换未指定 → 保留原 dims
                if not (source or "").strip() and not (source_book or "").strip():
                    meta.pop("source_book", None)  # 替换未给来源 → 保留原 source_book
                r.update(meta)
                r["text"] = txt
                break
        else:
            meta.update({"id": rid, "text": txt})
            records.append(meta)
    else:
        meta["text"] = txt
        records.append(meta)
    style_samples.save_samples(samples=records)
    final = style_samples.load_samples() or []
    target = next((s.to_dict() for s in final if s.id == (rid or final[-1].id)), None)
    return {"ok": True, "sample": target, "total": len(final),
            "warnings": style_samples.duplicate_warnings(final)}


def delete_style_sample(profile_id: str, sample_id: str) -> dict:
    """删除全局样文池的一个词条(按 id,如 s1)。"""
    cur = style_samples.load_samples() or []
    if not cur or not any(s.id == sample_id for s in cur):
        return {"ok": False, "error": f"样文 {sample_id} 不存在"}
    cur = [s for s in cur if s.id != sample_id]
    style_samples.save_samples(samples=cur)
    return {"ok": True, "deleted": sample_id, "total": len(cur)}


def list_style_samples(profile_id: str = "") -> dict:
    """列全局样文池**元数据**(id/标题/场景标签/字数/来源/备注,不含正文,保持薄)。

    供 agent 看当前有哪些词条、各属什么场景;想聚焦某场景时调 get_style_sample 取全文。"""
    cur = style_samples.load_samples() or []
    rows = [{"id": s.id, "title": s.title, "scene_tags": s.scene_tags, "dims": s.dims,
             "source": s.source, "source_book": s.source_book, "note": s.note,
             "word_count": s.word_count} for s in cur]
    return {"ok": True, "scope": "global", "count": len(rows), "samples": rows}


def get_style_sample(profile_id: str = "", sample_id: str = "") -> dict:
    """取全局样文池一个词条**全文**;sample_id 空则只回目录(与 list 同)。"""
    cur = style_samples.load_samples() or []
    if not sample_id:
        return {"ok": True, "scope": "global", "count": len(cur),
                "samples": [{"id": s.id, "title": s.title, "scene_tags": s.scene_tags, "dims": s.dims,
                             "source_book": s.source_book, "word_count": s.word_count} for s in cur]}
    for s in cur:
        if s.id == sample_id:
            return {"ok": True, "sample": s.to_dict()}
    return {"ok": False, "error": f"样文 {sample_id} 不存在"}


def query_characters(keyword: str = "", tag: str = "") -> dict:
    """查角色原型库：按标签/关键词返回启用原型，供外部 agent 选原型生成角色。"""
    kw = (keyword or "").strip()
    rows = char_lib.search(tag=tag, kw=kw)
    return {"archetypes": [a.to_dict() for a in rows if getattr(a, "enabled", True)][:20]}


# ═══════════════════════════════════════════════════
# 规划 / 编辑类（调 LLM，成功后使引擎会话过期）
# ═══════════════════════════════════════════════════

def save_basic_info(book_id: str, basic_info: dict, expected_revision: int | None = None) -> dict:
    """保存基础设定（人物/世界观/基调/目标读者，深合并保留已填值），可带 book_title。
    新 payload 传 characters 整体替换；旧 payload 传 protagonist/supporting_cast 兼容。

    expected_revision 省略=不校验；给则与磁盘 storyline_revision 不一致返 stale_storyline
    （storyline_revision 代表所有影响下次故事规划的事实状态，见 R3 修订）。
    """
    tl = book_mgr.load_storyline(book_id)
    if tl is None:
        raise RuntimeError(f"「{book_id}」无故事线（storyline.json）。"
                           "请先经「启动新书」向导建书（步 3 内容随 submit 落库）。")
    _cur = int(getattr(tl, "storyline_revision", 0) or 0)
    if expected_revision is not None and int(expected_revision) != _cur:
        return {"ok": False, "error": "stale_storyline", "expected": int(expected_revision),
                "actual": _cur, "action": "refresh_and_replan"}
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
    tl.storyline_revision = int(getattr(tl, "storyline_revision", 0) or 0) + 1
    tl.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    save_tl(book_id, tl)
    _drop_engine(book_id)
    return {"ok": True, "storyline_revision": tl.storyline_revision}


def _apply_chapter_planning_patch(book_id: str, tl, book, planning_patch: dict | None) -> dict | None:
    """章末把 agent 上报的增量（story_questions/character_intents）语义合并进 planning_state。

    - story_questions：稳定 id 或同文去重 → 更新状态；否则新开（默认 open）。
      已 answered/superseded 的问题默认保持终态（upsert_story_questions 内保证）。
    - character_intents：按人物 upsert，不按章无限 append。
    - 其余白名单键走 merge_state 覆盖。
    返回 None=无 patch/无变更；{"ok": False,...}=patch 非法（不阻塞正文落盘，仅回传）。
    """
    if not planning_patch:
        return None
    from libraries.planning_state import (validate_patch, merge_state, load_planning_state,
                                          save_planning_state, upsert_story_questions,
                                          upsert_character_intents)
    try:
        patch = validate_patch(planning_patch)
    except ValueError as e:
        return {"ok": False, "error": "planning_patch_invalid", "message": str(e)}
    if not patch:
        return None
    try:
        state = load_planning_state(book_id, tl, book, persist=False)
        changed = False
        for _key, _fn in (("story_questions", upsert_story_questions),
                          ("character_intents", upsert_character_intents)):
            if patch.get(_key) is not None:
                merged = _fn(state.get(_key), patch.get(_key))
                if merged != state.get(_key):
                    state[_key] = merged
                    changed = True
        rest = {k: v for k, v in patch.items() if k not in ("story_questions", "character_intents")}
        if rest:
            state = merge_state(state, rest)
            changed = True
        if changed:
            state["storyline_revision"] = int(getattr(tl, "storyline_revision", 0) or 0)
            state["written_until_word"] = int(getattr(book, "total_words", 0) or 0) if book else 0
            save_planning_state(book_id, state)
        return {"ok": True, "changed": changed}
    except Exception:
        return None


def save_chapter_text(book_id: str, chapter_num: int, text: str,
                      title: str = "", summary: str = "",
                      plot_segments: list | None = None,
                      expected_revision: int | None = None,
                      planning_patch: dict | None = None) -> dict:
    """[薄工具] 保存整章正文（agent 自主生成后调用，内部不调 LLM）。

    agent 生成正文后，本工具负责纯规则副作用：去AI味 → 规则审查 → 章节落盘
    （含情节段）→ 书进度/字数 → 故事线 written_chapter 进度 → 角色状态 →
    读者承诺台账（规则）→ 清草稿。summary 由 agent 生成传入（语义摘要是 LLM
    职责，迁到 agent）。

    expected_revision（可选）：省略=不校验；给则与磁盘 storyline_revision 不一致时
    直接返 stale_storyline、不落任何内容。
    planning_patch（可选）：章末把 story_questions / character_intents 等增量
    语义合并进 planning_state（稳定 id 去重、按人物 upsert），失败不阻塞正文落盘。
    每次成功落盘一章都 bump storyline_revision（该值代表所有影响下次故事规划的
    事实状态版本，使旧 replan preview 在 commit 时被正确判 stale）。
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

    # 0) 版本校验（expected_revision 省略=不校验；给则对比磁盘最新修订）
    if expected_revision is not None:
        _tl0 = book_mgr.load_storyline(book_id)
        _rev0 = int(getattr(_tl0, "storyline_revision", 0) or 0) if _tl0 else 0
        if int(expected_revision) != _rev0:
            return {"ok": False, "error": "stale_storyline", "expected": int(expected_revision),
                    "actual": _rev0, "action": "refresh_and_replan"}

    # 0.5) 收集 agent 按情节段上报的剧情人物变化事件（plot_segments 优先；漏传回退草稿）
    char_events = _segment_char_events(plot_segments)
    if not char_events:
        char_events = _draft_char_events(book_id)

    # 0.55) 读进行中草稿的 per-plot facts/run 快照（agent 经 save_plot_draft(outcome=) 上报的
    #       **结构化结果**）。commit 原样搬进 chapter bridge——平台不推断正文，只搬运 structured facts（修订 2）。
    _draft_by_plot = {}
    try:
        _dp0 = os.path.join(str(book_mgr.dir), book_id, "draft_chapter.json")
        if os.path.exists(_dp0):
            with open(_dp0, encoding="utf-8") as f:
                _dd = json.load(f)
            for _b in (_dd.get("bridges") or []):
                if isinstance(_b, dict) and _b.get("plot_id"):
                    _draft_by_plot[str(_b["plot_id"])] = _b
    except Exception:
        _draft_by_plot = {}

    # protocol v2 的草稿是章节 canonical source：提交参数只能确认段集合，不能静默覆盖已签收文本。
    _v2_draft = [b for b in _draft_by_plot.values() if int(b.get("protocol_version", 1) or 1) >= 2]
    if _v2_draft:
        submitted_ids = [str((b or {}).get("plot_id") or "") for b in (plot_segments or [])]
        expected_ids = [str(b.get("plot_id") or "") for b in _v2_draft]
        if not plot_segments or set(submitted_ids) != set(expected_ids):
            raise RuntimeError("protocol_v2：save_chapter_text 必须提交与已保存草稿完全一致的 plot_segments")
        plot_segments = [{"plot_id": b.get("plot_id"), "plot_name": b.get("plot_name"),
                          "text": b.get("text") or ""} for b in _v2_draft]
        text = "\n\n".join(b["text"] for b in plot_segments)

    # 1) 规则去AI味（词替换+段落节奏，无 LLM）；有情节段则逐段去并保持桥梁结构。
    #    防静默丢字：plot_segments 必须覆盖 text（总长 ≥ text 70%）。只列了部分情节段时以
    #    text 为正文源落盘、不挂情节段，并回传 segment_warning（提示 agent 把每情节段都列入）。
    processed = text
    segment_warning = None
    plot_spans = []   # WS5：content 内各 plot 的 [start,end)（Python 串下标，段间以 2 个换行连接）
    if plot_segments:
        raw_cover = sum(len((b.get("text") or "")) for b in plot_segments)
        if raw_cover < len(text) * 0.7:
            raise RuntimeError(f"plot_segments 总长 {raw_cover} ＜ 正文 {len(text)}；拒绝静默降级，请提交完整桥段")
        else:
            segs = []
            by_pid = {}   # 非空 plot_id → 在 segs 中的下标（同 id 重写时原位替换，防重复情节段）
            for b in plot_segments:
                seg_text = (b.get("text") or "")
                try:
                    seg_text = DeAIEngine().process_rule_based(seg_text).processed
                except Exception:
                    pass
                seg = {"plot_id": b.get("plot_id"), "plot_name": b.get("plot_name"), "text": seg_text}
                # 携带 per-plot facts/run 快照：优先来自草稿（save_plot_draft 结构化上报），
                # plot_segments 自带亦可。facts 永不从正文推断。
                _db = _draft_by_plot.get(str(seg.get("plot_id") or "")) if seg.get("plot_id") else None
                if _db is not None:
                    seg["facts"] = _db.get("facts") or {}
                    seg["expected_facts"] = _db.get("expected_facts") or []
                    seg["run_id"] = _db.get("run_id") or ""
                    seg["based_on_storyline_revision"] = _db.get("based_on_storyline_revision") or 0
                    seg["character_events"] = _db.get("character_events") or []
                    seg["context_fingerprint"] = _db.get("context_fingerprint") or ""
                    seg["sample_receipt"] = _db.get("sample_receipt") or {}
                else:
                    _sf = dict(b.get("facts") or {}) if isinstance(b.get("facts"), dict) else {}
                    if not _sf and isinstance(b.get("outcome"), dict):
                        _sf = {k: list((b["outcome"].get(k) or [])) for k in
                               ("choices_made", "information_revealed", "relationship_changes",
                                "resource_changes", "promise_updates", "new_story_questions")}
                    if _sf or b.get("character_events"):
                        _sf = dict(_sf)
                        _sf.setdefault("character_events", list(b.get("character_events") or []))
                        seg["facts"] = _sf
                    if b.get("expected_facts"):
                        seg["expected_facts"] = b["expected_facts"]
                    if b.get("run_id"):
                        seg["run_id"] = b["run_id"]
                    if b.get("based_on_storyline_revision") is not None:
                        seg["based_on_storyline_revision"] = b["based_on_storyline_revision"]
                    seg["character_events"] = list(b.get("character_events") or [])
                pid = seg.get("plot_id")
                seg.update(_text_metrics(seg_text))
                if pid and pid in by_pid:
                    segs[by_pid[pid]] = seg        # 同 plot_id 重写：替换旧条目保持原位置，最后写入胜出
                else:
                    if pid:
                        by_pid[pid] = len(segs)
                    segs.append(seg)
            plot_segments = segs
            processed = "\n\n".join(s["text"] for s in segs)
            # WS5 plot_spans：与 processed 同序逐段累加（start 含/end 不含；段间 "\n\n" 2 字符）
            _off = 0
            for s in segs:
                _t = s.get("text") or ""
                plot_spans.append({"plot_id": s.get("plot_id"), "plot_name": s.get("plot_name"),
                                   "run_id": s.get("run_id") or "", "start": _off, "end": _off + len(_t),
                                   **_text_metrics(_t)})
                _off += len(_t) + 2
    if plot_segments is None:
        try:
            processed = DeAIEngine().process_rule_based(text).processed
        except Exception:
            pass

    # 2) 规则审查（reviewer，无 LLM）→ 存 review
    target = int(getattr(book, "words_per_chapter", 0) or 3000)
    review_dict = None
    try:
        r = ContentReviewer().review(processed, chapter_num=n,
                                     chapter_title=title or f"第{n}章", target_words=target)
        review_dict = {"passed": r.passed, "score": r.score, "summary": r.summary,
                       "issues": [{"severity": i.severity, "category": i.category,
                                   "description": i.description, "location": i.location,
                                   "suggestion": i.suggestion} for i in (r.issues or [])]}
    except Exception:
        pass

    # 2.5) 硬门禁：正文低于字数下限 → 拒绝落盘（保留进行中草稿），逼 agent 续写满章。
    #      必须**独立于审查器**：审查器异常会让 review_dict=None，早期实现据此整段跳过下限检查
    #      → 过短正文照样落盘（章质量与后续 reconcile 都被污染）。
    actual_units = count_prose_units(processed)
    if actual_units < int(target * HARD_MIN_RATIO):
        raise RuntimeError(
            f"第 {n} 章正文 {actual_units} 字，低于本章下限 "
            f"{int(target * HARD_MIN_RATIO)} 字，正文不完整，未落盘。"
            f"请继续写满本章（逐情节段补全全部未写情节段）后，再调用 save_chapter_text。"
        )

    _write_commit_journal(book_id, {"schema_version": 1, "book_id": book_id, "chapter_num": n,
                                    "title": title or f"第{n}章", "started_at": time.time(),
                                    "plot_ids": [s.get("plot_id") for s in (plot_segments or [])],
                                    "phase": "prevalidated"})

    # 3) 落盘章节
    book_mgr.save_chapter(book_id, n, title or f"第{n}章", processed, summary or "",
                          review=review_dict, bridges=plot_segments, plot_spans=plot_spans or None)

    # 4) 书进度/字数（累加章节已存的计量字段，不再把全书正文重读一遍计字）
    try:
        book.current_chapter = max(int(book.current_chapter or 0), n)
        book.status = "writing"
        book.total_words = _committed_metrics(book_id, book)[0]
        book_mgr.update(book)
    except Exception as e:
        _log.warning("书进度/字数更新失败 book=%s chapter=%s: %s", book_id, n, e)

    # 5) 故事线 written_chapter 进度 + storyline_revision 递增
    #    plot_segments 缺失时回退草稿 bridges（agent 漏传 plot_segments 也不会卡住 next_plot）
    #    storyline_revision = 影响下次故事规划的事实状态版本（R3/R5 修订）：每成功落盘一章 +1，
    #    使旧 replan preview（expected_revision=旧值）在 commit 时被判 stale。
    #
    #    ⚠️ 这一段是**权威状态**：静默失败会让「正文已写、情节段仍算未写」（下一轮重写同一
    #    情节段）并且 revision 不 bump（旧 replan preview 看起来仍新鲜、CAS 放行）。因此这里
    #    重试一次后显式抛 ChapterCommittedStateError，由 finalize_draft_chapter 转成「章已提交
    #    但状态待修复」的诊断，绝不当作章节提交失败、也绝不静默。
    tl = None
    last_state_error = None
    for attempt in (1, 2):
        try:
            tl = book_mgr.load_storyline(book_id)
            if tl is None:
                raise RuntimeError("故事线缺失")
            written_plot_ids = {b.get("plot_id") for b in (plot_segments or []) if b.get("plot_id")}
            if not written_plot_ids:
                try:
                    dp = os.path.join(str(book_mgr.dir), book_id, "draft_chapter.json")
                    if os.path.exists(dp):
                        with open(dp, encoding="utf-8") as f:
                            _d = json.load(f)
                        written_plot_ids = {b.get("plot_id") for b in (_d.get("bridges") or []) if b.get("plot_id")}
                except Exception as e:
                    _log.warning("回退草稿取情节段失败 book=%s chapter=%s: %s", book_id, n, e)
            for p in tl.plots:
                if not (getattr(p, "written_chapter", 0) or 0) and p.id in written_plot_ids:
                    p.written_chapter = n
            tl.storyline_revision = int(getattr(tl, "storyline_revision", 0) or 0) + 1
            tl.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
            book_mgr.save_storyline(book_id, tl)
            last_state_error = None
            break
        except Exception as e:  # noqa: BLE001
            last_state_error = e
            _log.warning("故事线进度更新失败（第 %s 次尝试）book=%s chapter=%s: %s",
                         attempt, book_id, n, e)
    if last_state_error is not None:
        raise ChapterCommittedStateError(
            f"第{n}章已落盘，但故事线进度/版本更新失败：{last_state_error}；"
            "需修复 storyline.json（written_chapter / storyline_revision）后再继续写作") from last_state_error
    # 5.5) 章末规划增量上报（reader_question/character_intents 语义合并；失败不阻塞落盘）
    if planning_patch:
        try:
            _apply_chapter_planning_patch(book_id, tl, book, planning_patch)
        except Exception as e:  # noqa: BLE001
            _log.warning("章末规划增量合并失败（不阻塞正文）book=%s chapter=%s: %s", book_id, n, e)

    # 5.6) Prediction→Fact reconcile（修订 2/3）：只比较 structured expected_facts vs actual facts，
    #      fact 永远优先；漂移转 DecisionPoint，apply_fact_intents 以事实校正 character_intents；
    #      全程 try/except，失败不阻塞正文已成功落盘、绝不回写正文。
    reconcile_result = None
    try:
        from libraries.reconcile import reconcile_chapter, apply_fact_intents
        from libraries.decision_feed import dp_from_reconcile
        tl_r = book_mgr.load_storyline(book_id)
        if tl_r is not None and plot_spans:
            rc = reconcile_chapter(book_id, n, tl_r)
            runs = rc.get("runs") or []
            apply_fact_intents(book_id, tl_r, book, runs)
            points = []
            for r in runs:
                points.extend(dp_from_reconcile(r))
            reconcile_result = {
                "ok": True, "chapter": n, "runs": len(runs),
                "kinds": [r.get("kind") for r in runs],
                "drift_count": sum(len(r.get("drifts") or []) for r in runs),
                "unpredicted_count": sum(len(r.get("unpredicted") or []) for r in runs),
                "stale_count": sum(1 for r in runs if r.get("stale")),
                "decision_points": points,
            }
    except Exception as e:  # noqa: BLE001
        _log.warning("章末 reconcile 失败（正文与章已落地，仅预测对账缺失）book=%s chapter=%s: %s",
                     book_id, n, e)
        reconcile_result = None

    # 6) 角色状态（规则自动机 + agent 上报的剧情人物变化事件落账）
    try:
        csm = _load_char_states(book_id)
        bible = []
        try:
            _tl = book_mgr.load_storyline(book_id)
            if _tl and _tl.basic_info:
                bible = (_tl.basic_info or {}).get("characters") or []
        except Exception:
            pass
        csm.ensure_registered(bible)          # 修 csm 空机：每章先按 bible 就地注册
        if char_events:                        # agent 按情节段上报 → 剧情变化落账
            csm.apply_events(char_events, n, bible)
        csm.update_from_chapter(n, processed)  # 出场/离线计数
        csm.save(_char_states_path(book_id))
    except Exception as e:  # noqa: BLE001
        _log.warning("角色状态落账失败（不阻塞正文）book=%s chapter=%s: %s", book_id, n, e)

    # 7) 读者承诺台账（规则：written_chapter 标记 + pending 承诺 op 分级演化）
    try:
        _update_promises_ledger_thin(book_id, n)
    except Exception as e:  # noqa: BLE001
        _log.warning("读者承诺台账更新失败（不阻塞正文）book=%s chapter=%s: %s", book_id, n, e)

    # 7.5) 暂存事实升级为章节正式历史；过程只聚合 Agent 已上报的结构化 delta，
    # 不读取正文推断语义。写入失败不应清空 staged ledger，便于恢复。
    chapter_delta = None
    try:
        from libraries.plot_run_state import build_chapter_delta, commit_chapter_delta
        chapter_delta = commit_chapter_delta(book_id, build_chapter_delta(book_id, n))
    except Exception as e:  # noqa: BLE001
        _log.warning("暂存事实升级失败（保留 staged ledger 便于恢复）book=%s chapter=%s: %s",
                     book_id, n, e)
        chapter_delta = None

    # 8) 清进行中草稿（整章已落盘）
    try:
        dp = os.path.join(_ROOT, "books", book_id, "draft_chapter.json")
        if os.path.exists(dp):
            os.remove(dp)
    except Exception as e:  # noqa: BLE001
        _log.warning("清理章节草稿失败（章已落盘）book=%s chapter=%s: %s", book_id, n, e)
    try:
        journal = _commit_journal_path(book_id)
        if os.path.exists(journal):
            os.remove(journal)
    except Exception as e:  # noqa: BLE001
        _log.warning("清理提交日志失败（章已落盘）book=%s chapter=%s: %s", book_id, n, e)

    metrics = _text_metrics(processed)
    return {"ok": True, "chapter": n, "word_count": metrics["actual_prose_units"],
            **metrics,
            "review": review_dict, "segment_warning": segment_warning,
            "plot_spans": plot_spans or None, "reconcile": reconcile_result,
            "chapter_delta": chapter_delta}


def finalize_draft_chapter(book_id: str, flow_id: str = "") -> dict:
    """服务端从已签收的 draft 提交章节 + 执行质量门禁（不暴露给 Writer）。

    顺序与失败语义（W8）：
      1. `save_chapter_text` 是**不可回退**的落盘点：它失败（章未确认落盘）→ 抛异常，
         由 FSM 的失败出口统一转 FAILED + 释放租约；
      2. 一旦章落地，后续**全部是提交后诊断/收尾**——质量门禁异常只标 `quality_gate.error`，
         flow 一定走到 DONE 并释放租约，绝不出现「章已提交却报『章节提交失败』且租约不释放」；
      3. 若权威状态（故事线进度/revision）在章落地后更新失败，`save_chapter_text` 抛
         `ChapterCommittedStateError`，这里转成 `state_error` 诊断返回（章确实已提交），
         由 FSM 提示用户修复后继续。
    """
    draft = _draft_read(book_id) or {}
    bridges = list(draft.get("bridges") or [])
    if not bridges:
        raise RuntimeError("没有可提交的章节草稿")
    chapter_num = int(draft.get("chapter_num") or 0)
    if chapter_num < 1:
        raise RuntimeError("草稿缺少章节号")
    text = "\n\n".join(str(x.get("text") or "") for x in bridges)
    # Plot summary 不进入事实层，只做面向读者的章节摘要来源（取前若干段拼接，不硬截断句子）。
    summaries = [str(x.get("plot_summary") or "").strip() for x in bridges if x.get("plot_summary")]
    summary = "；".join(summaries[:3])
    state_error = None
    try:
        result = save_chapter_text(book_id, chapter_num, text, title=f"第{chapter_num}章",
                                   summary=summary, plot_segments=[{"plot_id": x.get("plot_id"),
                                   "plot_name": x.get("plot_name"), "text": x.get("text") or ""} for x in bridges])
    except ChapterCommittedStateError as exc:
        _log.error("章已落盘但权威状态未更新 book=%s chapter=%s: %s", book_id, chapter_num, exc)
        state_error = str(exc)
        result = {"ok": True, "chapter": chapter_num, "state_error": state_error,
                  **_text_metrics(text)}
    # 提交后诊断：门禁读的是已落盘章节，异常只作诊断，不改「章已提交」这一事实。
    try:
        gate = chapter_quality_gate(book_id, chapter_num)
        result["quality_gate"] = gate if isinstance(gate, dict) else {"ok": True, "report": gate}
    except Exception as exc:  # noqa: BLE001
        _log.warning("章质量门禁异常（章已提交）book=%s chapter=%s: %s", book_id, chapter_num, exc)
        result["quality_gate"] = {"ok": False, "error": str(exc), "skipped": True}
    if flow_id:
        from libraries.write_flow import release_lease, transition
        try:
            transition(book_id, flow_id, "QUALITY_GATE")
        except Exception as exc:  # noqa: BLE001
            _log.warning("flow 置 QUALITY_GATE 失败 book=%s flow=%s: %s", book_id, flow_id, exc)
        try:
            transition(book_id, flow_id, "DONE", chapter_num=chapter_num,
                       error=state_error)
        except Exception as exc:  # noqa: BLE001
            _log.warning("flow 置 DONE 失败 book=%s flow=%s: %s", book_id, flow_id, exc)
        finally:
            try:
                release_lease(book_id, flow_id)     # 收章完成无论如何都要释放租约
            except Exception as exc:  # noqa: BLE001
                _log.warning("租约释放失败 book=%s flow=%s: %s", book_id, flow_id, exc)
    return result


def _update_promises_ledger_thin(book_id: str, chapter_num: int) -> None:
    """薄工具用的读者承诺台账登记（规则层，无 LLM）。

    标记本章已写情节段、pending 承诺按 deadline 距离重定 op（seed→touch→pressure→payoff）。
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


def _save_plot_draft_legacy(book_id: str, chapter_num: int, plot_id: str,
                      plot_name: str, text: str,
                      character_events: list | None = None, outcome: dict | None = None,
                      expected_facts: list | None = None, run_id: str = "",
                      based_on_storyline_revision: int | None = None,
                      context_fingerprint: str = "", sample_receipt: dict | None = None,
                      plot_summary: str = "", verified_record: dict | None = None) -> dict:
    """[薄工具] 保存单个情节段到进行中草稿（draft_chapter.json，断点续写保底）。

    agent 逐情节段生成后调用：规则去AI味 → 追加进草稿（含 buffer/words/bridges），
    章满后用 save_chapter_text 落盘并清草稿。

    character_events（可选）：本情节段剧情造成的人物变化事件，随草稿落账，
    章满 save_chapter_text 时并入角色状态机。每项 {name, events:[{type, from?, to?, reason?}]}，
    type ∈ goal_shift|power_shift|location_shift|arc_stage|relationship|trust_change|note。
    只报剧情造成的**变化**，不报 mood/secret 等推断字段（禁 agent 直写）。

    outcome（可选，修订 2）：本段**结构化结果**（平台不做任何 NLP 推断，禁止指望从正文猜语义）：
      {choices_made[], information_revealed[], relationship_changes[], resource_changes[],
       promise_updates[], new_story_questions[]}。这些与 character_events 一起作为本 run 的 facts。
    expected_facts（可选，修订 3）：本 run 可机器比较的预测 [{subject, type, expected_to, strength}]；
      省略时 commit 的 reconcile 回退读 PlotSlot.expected_facts；execution_brief 只供阅读、不参与比较。
    run_id / based_on_storyline_revision（可选）：本次 Plot Run 身份与所基于故事线版本；缺省由
      plot_id@当前 storyline_revision 派生（修订 1：revision 变更即新 run）。
    """
    if not (book_id and text and (text or "").strip()):
        raise RuntimeError("book_id 与 text 必填")
    from libraries.plot_run_state import validate_outcome, make_plot_delta, stage_delta
    try:
        checked_outcome = validate_outcome(outcome)
    except ValueError as e:
        raise RuntimeError(str(e))
    tl_now = book_mgr.load_storyline(book_id)
    draft_now = _draft_read(book_id) or {}
    current = _next_plot(tl_now, draft_now) if tl_now else None
    if current is None or current.id != plot_id:
        raise RuntimeError("当前 plot 已变化；请重新读取 prepare_plot_run 后再保存")
    expected_run = _run_id_for(plot_id, int(getattr(tl_now, "storyline_revision", 0) or 0))
    is_v2 = int(getattr(current, "protocol_version", 1) or 1) >= 2
    if is_v2:
        if verified_record:
            # The commit token already represents the immutable prepared input.
            # Never rebuild the resolver here: it may reorder memory or pick a
            # different sample and would make retries spuriously stale.
            context_fingerprint = verified_record.get("context_fingerprint") or context_fingerprint
            sample_receipt = dict(verified_record.get("sample_receipt") or sample_receipt or {})
        else:
            # Private legacy callers still get the old strict check; the public
            # token path always supplies verified_record.
            expected_fp = _context_fingerprint(book_id, tl_now,
                                               _build_plot_run(tl_now, current, draft=draft_now), _profile_for(tl_now))
            if not context_fingerprint or context_fingerprint != expected_fp:
                raise RuntimeError("protocol_v2：context_fingerprint 缺失或已失效，请重新读取 prepare_plot_run")
        if run_id != expected_run or based_on_storyline_revision is None or int(based_on_storyline_revision) != int(getattr(tl_now, "storyline_revision", 0) or 0):
            raise RuntimeError("protocol_v2：run_id 或 storyline_revision 不匹配，请刷新上下文")
        if not isinstance(sample_receipt, dict) or sample_receipt.get("context_fingerprint") != context_fingerprint:
            raise RuntimeError("protocol_v2：sample_receipt 缺失或与上下文不匹配")
    text = (text or "").strip()
    try:
        text = DeAIEngine().process_rule_based(text).processed
    except Exception:
        pass
    # Plot Run 快照：基于哪个 story revision、以什么身份进入 Drafting（修订 1：plot_id@rev）。
    if based_on_storyline_revision is None:
        _tl = book_mgr.load_storyline(book_id)
        based_on_storyline_revision = int(getattr(_tl, "storyline_revision", 0) or 0) if _tl else 0
    based_on_storyline_revision = int(based_on_storyline_revision or 0)
    if not run_id:
        run_id = _run_id_for(plot_id or "plot", based_on_storyline_revision)
    _facts = {}
    for _k in ("choices_made", "information_revealed", "relationship_changes",
               "resource_changes", "promise_updates", "new_story_questions"):
        _facts[_k] = checked_outcome[_k]
    _facts["character_events"] = list(character_events or []) if isinstance(character_events, list) else []
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
    entry = {"plot_id": plot_id or "", "plot_name": plot_name or "", "text": text,
             "character_events": (character_events or []) if isinstance(character_events, list) else []}
    entry["plot_summary"] = str(plot_summary or "").strip()
    entry["facts"] = _facts
    entry["expected_facts"] = list(expected_facts or []) if isinstance(expected_facts, list) else []
    entry["run_id"] = run_id
    entry["based_on_storyline_revision"] = based_on_storyline_revision
    entry["context_fingerprint"] = context_fingerprint
    entry["sample_receipt"] = dict(sample_receipt or {})
    entry["protocol_version"] = 2 if is_v2 else 1
    entry.update(_text_metrics(text))
    if plot_id:
        # 同 plot_id 重写：替换旧条目而非追加（防同一情节段被反复生成导致重复渲染/高亮），最后写入胜出
        replaced = False
        for i, b in enumerate(bridges):
            if b.get("plot_id") == plot_id:
                bridges[i] = entry
                replaced = True
                break
        if not replaced:
            bridges.append(entry)
    else:
        # 空 plot_id 情节段不参与去重（可能代表不同的非故事线内容），直接追加
        bridges.append(entry)
    buffer = [b.get("text", "") for b in bridges]
    words = sum(count_prose_units(b) for b in buffer)
    try:
        os.makedirs(os.path.dirname(dp) or ".", exist_ok=True)
        with open(dp, "w", encoding="utf-8") as f:
            json.dump({"chapter_num": chapter_num, "buffer": buffer, "words": words,
                       "bridges": bridges}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        raise RuntimeError(f"保存情节段草稿失败: {e}")
    # Plot 即事务边界：仅用结构化 facts reconcile，通过后进入可恢复 staged ledger，
    # 下一次 prepare_plot_run 立即能看到它；正式角色状态仍待章节 commit。
    from libraries.reconcile import reconcile_run
    reconcile = reconcile_run(plot=current, bridge=entry,
                              based_on_storyline_revision=based_on_storyline_revision,
                              current_revision=int(getattr(tl_now, "storyline_revision", 0) or 0),
                              chapter_num=chapter_num)
    try:
        staged = stage_delta(book_id, make_plot_delta(
            plot_id=plot_id, plot_name=plot_name, chapter_num=chapter_num, facts=_facts,
            text=text, run_id=run_id, reconcile=reconcile, plot_summary=plot_summary,
            arc_id=getattr(current, "outline_id", "") or ""))
    except ValueError as e:
        raise RuntimeError(f"暂存事实落账失败: {e}")
    return {"ok": True, "chapter": chapter_num, "bridges": len(bridges), "words": words,
            "actual_prose_units": words, "raw_codepoints": sum(len(b) for b in buffer),
            "protocol": "v2" if is_v2 else "shadow", "reconcile": reconcile,
            "staged_fact_count": len(staged.get("plot_deltas") or [])}


def save_plot_draft(commit_token: str, text: str, plot_summary: str = "",
                    outcome: dict | None = None, character_events: list | None = None) -> dict:
    """提交当前 Plot 正文及实际结构化变化。

    token 必须来自 prepare_plot_run；成功后暂存事实生效且当前 Writer Run 结束。
    outcome 只能包含 choices_made、information_revealed、relationship_changes、
    resource_changes、promise_updates、new_story_questions，且每项都是 list；
    character_events 只上报正文造成的变化，格式为 [{name, events:[{type, from?, to?, reason?}]}]，
    type 仅可用 goal_shift、power_shift、location_shift、arc_stage、relationship、trust_change、note。
    plot_summary 仅供展示/检索/章节摘要，长度 50～120 字，不是事实源。
    """
    from libraries.plot_commit_tokens import accepted_result
    if not (text or "").strip():
        raise RuntimeError("text 必填")
    summary = str(plot_summary or "").strip()
    summary_problems = []
    if not summary:
        summary_problems.append("plot_summary 为空（契约 50-120 字，仅作展示/检索用）")
    elif not 50 <= len(summary) <= 120:
        # Summary 只作展示/检索元数据，不是事实源：不为一次格式不合格让整轮 LLM 重写，
        # 但必须**如实记下问题**（此前只截断不给信号）。
        summary_problems.append(f"plot_summary 长度 {len(summary)} 不在 50-120")
        _log.warning("plot_summary 长度 %d 不在 50-120，按展示字段容错保存", len(summary))
        if len(summary) > 120:
            summary = summary[:120]
    if isinstance(outcome, dict) and outcome.get("relationship_changes"):
        for row in character_events or []:
            if any((event or {}).get("type") == "relationship" for event in (row or {}).get("events") or []):
                raise RuntimeError("关系变化只能由 outcome.relationship_changes 提交，禁止双源写入")
    # token 表按书分片，当前 Plot 无需由 Writer 提交；子进程只带回 commit_token，
    # 归属书由服务端经 NOVEL_WRITE_BOOK_ID 注入 → 只查一本书的账本（O(1)）。
    # 无提示（手工/测试调用）才回退全库扫描；有提示但账本里没有该令牌 = 子进程上下文错，
    # 直接失败而不是去翻别的书（fail-closed）。
    from libraries.plot_commit_tokens import resolve_book_id
    book_id = resolve_book_id(commit_token, os.environ.get("NOVEL_WRITE_BOOK_ID", ""))
    if not book_id:
        raise RuntimeError("commit_token 无效；请重新 prepare")
    # accepted token 是幂等返回，允许网络重试。
    accepted = accepted_result(book_id, commit_token)
    if accepted:
        return {**accepted, "idempotent": True, "writer_run_complete": True}
    # W1：本函数签名不含 book_id（Writer 只带回 commit_token），`_wrap_book_lock` 取不到锁目标，
    # 因此在这里解析出归属书后**显式**加锁，覆盖「版本校验 → 落草稿/staged → 记账本 → 推进 flow」
    # 全过程：令牌只防重放，不防与 Web 进程 / 另一 MCP 客户端同书并发写盘。
    # 注意：BookLock 不可重入，故本函数不得再出现在 _LOCKED_TOOLS 里（否则 wrapper 会二次加锁死锁）。
    from libraries.book_lock import BookBusyError, BookLock
    lock = BookLock(book_id)
    if not lock.acquire(timeout=30.0, purpose="save_plot_draft"):
        raise BookBusyError(f"另一进程正在操作这本书，请稍后再试：{book_id}")
    try:
        return _commit_plot_draft_locked(book_id, commit_token, text, summary,
                                         outcome, character_events, summary_problems)
    finally:
        lock.release()


def _finish_plot_commit(book_id: str, commit_token: str, record: dict, plot,
                        chapter_num: int, result: dict) -> dict:
    """Plot 提交收尾（正常提交与崩溃恢复共用）：写身份 + 记账本 + 推进 flow。

    flow 推进失败只记日志（Plot 已生效，不该回滚），并顺手把续规划连败计数归零（有推进）。
    """
    from libraries.plot_commit_tokens import accept
    flow_id = record.get("flow_id") or "adhoc"
    if flow_id == "adhoc":
        # 旧入口没有提前创建 Flow；首次成功提交时补建可恢复 Flow，后续子 Run 都继承它。
        from libraries.write_flow import active_flow_id, start_flow
        flow_id = active_flow_id(book_id)
        if not flow_id:
            flow_id = start_flow(book_id, chapter_num)["flow_id"]
    result.update(book_id=book_id, plot_id=plot.id, writer_run_complete=True,
                  flow_id=flow_id, child_run_id=record.get("child_run_id"))
    accept(book_id, commit_token, result)
    if flow_id:
        try:
            from libraries.write_flow import load_flow, transition
            flow = load_flow(book_id, flow_id) or {}
            completed = list(flow.get("completed_plot_ids") or [])
            if plot.id not in completed:
                completed.append(plot.id)
            transition(book_id, flow_id, "EVALUATING", current_plot_id=plot.id,
                       completed_plot_ids=completed, replan_state={"reason": "", "attempts": 0})
        except Exception as e:  # noqa: BLE001
            # 子 Run 审计记录失败不该回滚已生效的 Plot 提交，但必须留痕（此前静默吞掉）。
            _log.warning("flow 推进失败（Plot 已提交）book=%s flow=%s: %s", book_id, flow_id, e)
    return result


def _recover_partial_plot_commit(book_id: str, commit_token: str, tl, draft: dict):
    """崩溃恢复：草稿里已有本令牌对应 Plot 的桥 → 补齐 staged 事实/记账本后幂等返回。

    Plot 提交跨三处（写 draft → stage_delta → accept token），中间崩溃会留下：
    draft 已含该 Plot（`_next_plot` 已跳过它，正常校验只会以「token 与当前 Plot 不匹配」
    失败），该 Plot 的 staged 事实永远缺失，FSM 的草稿恢复还会把「部分完成的桥」当成已完成。
    桥里已存 facts/run_id/based_on_storyline_revision/context_fingerprint，因此可以确定性重放
    `reconcile_run` + `stage_delta`（按 plot_id 幂等）+ `accept`，不需要读正文推断语义。

    返回补齐后的 result；没有需要恢复的东西返回 None（继续走正常提交路径）。
    """
    from libraries.plot_commit_tokens import _load
    record = ((_load(book_id).get("tokens") or {}).get(commit_token)) or {}
    if not record or record.get("accepted"):
        return None
    plot_id = str(record.get("plot_id") or "")
    if not plot_id:
        return None
    fingerprint = record.get("context_fingerprint") or ""
    bridge = None
    for b in (draft.get("bridges") or []):
        if str(b.get("plot_id") or "") != plot_id:
            continue
        if fingerprint and b.get("context_fingerprint") != fingerprint:
            continue
        bridge = b
        break
    if bridge is None:
        return None
    if tl is None:
        return None
    plot = next((p for p in (tl.plots or []) if p.id == plot_id), None)
    if plot is None:
        return None
    chapter_num = int(draft.get("chapter_num") or 0)
    text = str(bridge.get("text") or "")
    try:
        from libraries.plot_run_state import make_plot_delta, stage_delta
        from libraries.reconcile import reconcile_run
        reconcile = reconcile_run(
            plot=plot, bridge=bridge,
            based_on_storyline_revision=int(bridge.get("based_on_storyline_revision") or 0),
            current_revision=int(getattr(tl, "storyline_revision", 0) or 0),
            chapter_num=chapter_num)
        stage_delta(book_id, make_plot_delta(
            plot_id=plot_id, plot_name=bridge.get("plot_name") or "", chapter_num=chapter_num,
            facts=bridge.get("facts") or {}, text=text, run_id=bridge.get("run_id") or "",
            reconcile=reconcile, plot_summary=bridge.get("plot_summary") or "",
            arc_id=getattr(plot, "outline_id", "") or ""))
    except Exception as e:  # noqa: BLE001
        _log.warning("补齐中断的 Plot 提交失败（保留草稿，留待下次重试）book=%s plot=%s: %s",
                     book_id, plot_id, e)
        return None
    metrics = _text_metrics(text)
    _log.warning("检测到中断的 Plot 提交，已按草稿桥补齐 staged 事实 book=%s plot=%s",
                 book_id, plot_id)
    return _finish_plot_commit(book_id, commit_token, record, plot, chapter_num, {
        "ok": True, "chapter": chapter_num, "bridges": len(draft.get("bridges") or []),
        "actual_prose_units": metrics["actual_prose_units"],
        "raw_codepoints": metrics["raw_codepoints"],
        "protocol": "v2" if int(bridge.get("protocol_version") or 1) >= 2 else "shadow",
        "reconcile": reconcile, "recovered_from_draft": True,
    })


def _commit_plot_draft_locked(book_id: str, commit_token: str, text: str, summary: str,
                              outcome: dict | None, character_events: list | None,
                              summary_problems: list | None = None) -> dict:
    """save_plot_draft 的锁内主体：调用方负责持有 `books/<id>/.lock`。"""
    from libraries.plot_commit_tokens import verify
    tl = book_mgr.load_storyline(book_id)
    draft = _draft_read(book_id) or {}
    # 先看有没有「上次提交中断」的痕迹：draft 里已有本令牌的桥 → 补齐并幂等返回。
    recovered = _recover_partial_plot_commit(book_id, commit_token, tl, draft)
    if recovered is not None:
        return recovered
    current = _next_plot(tl, draft) if tl else None
    if not current:
        raise RuntimeError("当前没有可提交的 Plot")
    try:
        # Verify only authoritative identity/version fields. The token carries
        # the prepared snapshot; rebuilding PlotRun here would be side-effectful
        # and could change memory ordering or sample selection.
        record = verify(book_id, commit_token, plot_id=current.id,
                        storyline_revision=int(getattr(tl, "storyline_revision", 0) or 0),
                        context_fingerprint=None)
    except ValueError as e:
        raise RuntimeError(str(e))
    prepared_snapshot = record.get("prepared_snapshot") or {}
    saved_style = ((prepared_snapshot.get("style") or {}).get("snapshot")
                   if isinstance(prepared_snapshot, dict) else None)
    if saved_style:
        from libraries.style_snapshot import selected_sample_digest, snapshot_matches
        profile = _profile_for(tl)
        if profile is None:
            try:
                profile = _resolve_profile(book_id)
            except Exception:
                pass
        current_card = profile.build_style_card() if profile else _default_style_card()
        sample_id = saved_style.get("sample_id", "")
        current_sample_digest = selected_sample_digest(profile, sample_id) if profile else ""
        if current_sample_digest is None or not snapshot_matches(
                profile, current_card, saved_style, current_sample_digest):
            raise RuntimeError("commit_token 风格上下文已失效；请重新 prepare_plot_run")
    recorded_vector = record.get("version_vector") or {}
    if recorded_vector:
        current_vector = _context_version_vector(book_id, tl, draft, saved_style or {})
        if current_vector != recorded_vector:
            raise RuntimeError("commit_token 权威上下文已变化；请重新 prepare_plot_run")
    chapter_num = int((draft or {}).get("chapter_num") or ((getattr(book_mgr.get(book_id), "current_chapter", 0) or 0) + 1))
    result = _save_plot_draft_legacy(
        book_id, chapter_num, current.id, current.name, text,
        character_events=character_events, outcome=outcome,
        expected_facts=list(getattr(current, "expected_facts", None) or []),
        run_id=f"{current.id}@{int(getattr(tl, 'storyline_revision', 0) or 0)}",
        based_on_storyline_revision=int(getattr(tl, "storyline_revision", 0) or 0),
        context_fingerprint=record.get("context_fingerprint") or "",
        sample_receipt=record.get("sample_receipt") or {},
        plot_summary=summary, verified_record=record)
    if summary_problems:
        # 非阻断的契约问题如实回报（摘要超长会被截断为 120，问题清单里保留原长）
        result["summary_problems"] = list(summary_problems)
    return _finish_plot_commit(book_id, commit_token, record, current, chapter_num, result)


def save_outlines(book_id: str, outlines: list | None = None,
                  plots: list | None = None, threads: list | None = None,
                  themes: list | None = None, mode: str = "replace",
                  expected_revision: int | None = None,
                  planning_patch: dict | None = None,
                  validate: bool = True) -> dict:
    """[薄工具] 保存弧/情节段/线程/内涵（agent 生成后调用，内部不调 LLM）。

    接受 agent 生成的结构化 dict 列表，反序列化为 OutlineSlot / PlotSlot 落盘；
    mode=replace 整体替换 | append 续写追加。
    含 plots 则 phase=plots（草案待确认）否则 outlines；但**已 ready 书保持 ready**
    （续写/扩写追加弧后不降级——ready 翻转只由用户在书详情 UI 确认，见 confirm-storyline）。
    弧的 `notes`（设计意图/偏离库模板点）随 OutlineSlot 落盘，供蓝图过目复核。
    结构必填（硬规则，缺则 raise 拒收、不自动换算兜底）：每条弧 id 非空唯一 + name +
    一组完整跨度（start_word&end_word 成对整数 0<=start<end，或 start_chapter&end_chapter
    成对整数 1<=start<=end；半组/全缺直接报错）；每个情节段 id 非空唯一 + outline_id 指向
    存在的最底层（叶）弧；append/续写可只传 plots 挂到已落盘弧（outlines 留空）。
    """
    # 直接读磁盘而非进程缓存：Web/MCP 双进程下 expected_revision 必须对比最新版本。
    tl = book_mgr.load_storyline(book_id)
    if tl is None:
        raise RuntimeError(f"「{book_id}」无故事线（storyline.json）")
    from libraries.storyline import BookStoryline, OutlineSlot, PlotSlot, reconcile_outline
    from core.json_store import read_json, write_json_atomic
    current_revision = int(getattr(tl, "storyline_revision", 0) or 0)
    from libraries.planning_state import enabled as _planning_enabled
    if (_planning_enabled("STORYLINE_REVISION_CHECK", True)
            and expected_revision is not None and int(expected_revision) != current_revision):
        return {"ok": False, "error": "stale_storyline", "expected": int(expected_revision),
                "actual": current_revision, "action": "refresh_and_replan"}
    try:
        checked_patch = validate_patch(planning_patch)
    except ValueError as e:
        raise RuntimeError(str(e))
    # 在副本上完成全部结构变更和校验，任何失败都不触碰正式故事线。
    candidate = BookStoryline.from_dict(tl.to_dict())
    # 结构校验（硬规则）：known 只对 append 生效（续写可把 plots 挂到已落盘弧）；replace 整体替换须自洽。
    if mode not in {"replace", "append"}:
        raise RuntimeError("mode 只允许 replace 或 append")
    _known_arcs = [] if mode == "replace" else (candidate.outlines or [])
    _known_plots = [] if mode == "replace" else (candidate.plots or [])
    _probs = outline_payload_problems(outlines or [], plots or [],
                                      known_outlines=_known_arcs, known_plots=_known_plots)
    if _probs:
        raise RuntimeError("save_outlines 拒绝：outlines/plots 结构不完整——" + "；".join(_probs))
    if mode == "replace":
        candidate.outlines = []
        candidate.plots = []
    if outlines:
        base = len(candidate.outlines)
        for i, o in enumerate(outlines):
            _sw = o.get("start_word"); _ew = o.get("end_word")
            _sc = o.get("start_chapter"); _ec = o.get("end_chapter")
            candidate.outlines.append(OutlineSlot(
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
                notes=o.get("notes", ""),
            ))
        for _o in candidate.outlines:
            reconcile_outline(_o, candidate.words_per_chapter or 3000)
    if plots:
        base = len(candidate.plots)
        for i, p in enumerate(plots):
            candidate.plots.append(PlotSlot(
                id=p.get("id") or f"plot_{base + i + 1:04d}",
                template_id=p.get("template_id", ""),
                name=p.get("name") or "未命名情节段",
                category=p.get("category", ""),
                sub_category=p.get("sub_category", ""),
                outline_id=p.get("outline_id", ""),
                stage_index=int(p.get("stage_index") or 0),
                order=int(p.get("order") or 0),
                cover_beats=int(p.get("cover_beats") or 4),
                words=int(p.get("words")) if p.get("words") is not None else None,
                thread_id=p.get("thread_id", "主线"),
                resolves_plot_id=p.get("resolves_plot_id", ""),
                resolves_name=p.get("resolves_name", ""),
                roles=p.get("roles") or [],
                no_named_cast=bool(p.get("no_named_cast", False)),
                protocol_version=int(p.get("protocol_version", 2) or 2),
                execution_brief=p.get("execution_brief") or {},
                character_impact=p.get("character_impact") or [],
                expected_facts=p.get("expected_facts") or [],
            ))
        known_names = {str(c.get("name") or "") for c in ((candidate.basic_info or {}).get("characters") or [])}
        for p in candidate.plots:
            unknown = [name for name in (p.roles or []) if name not in known_names]
            if unknown:
                raise RuntimeError(f"save_outlines 拒绝：情节段「{p.name}」包含未知角色：{'、'.join(unknown)}")
            # 世界观/角色尚未建立的早期兼容书不在这里卡死；有角色 bible 的新规划必须显式声明 cast。
            if p.protocol_version >= 2 and known_names and not p.no_named_cast and not p.roles:
                raise RuntimeError(f"save_outlines 拒绝：情节段「{p.name}」缺 roles；无具名角色请显式 no_named_cast=true")
    if threads:
        candidate.threads = threads
    if themes:
        candidate.themes = themes
    candidate.phase = candidate.phase if candidate.phase == "ready" else ("plots" if plots else "outlines")
    candidate.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    if validate and candidate.outlines:
        report = globals()["validate_storyline"](
            outlines=candidate.to_dict().get("outlines") or [],
            plots=candidate.to_dict().get("plots") or [],
            words_per_chapter=candidate.words_per_chapter,
        )
        if not report.get("passed"):
            raise RuntimeError("save_outlines 校验失败：" + str(report.get("summary") or report))

    book = book_mgr.get(book_id)
    old_state = load_planning_state(book_id, tl, book, persist=False)
    new_state = merge_state(old_state, checked_patch)
    candidate.storyline_revision = current_revision + 1
    new_state["book_id"] = book_id
    new_state["storyline_revision"] = candidate.storyline_revision
    if checked_patch.get("last_replan"):
        new_state.setdefault("last_replan", {})["result_revision"] = candidate.storyline_revision
        new_state["last_replan"].setdefault("at", time.strftime("%Y-%m-%d %H:%M:%S"))
    new_state["written_until_word"] = int(getattr(book, "total_words", 0) or 0) if book else 0
    new_state["committed_until_word"] = max(
        int(new_state.get("committed_until_word") or 0),
        max((int(getattr(o, "end_word", 0) or 0) for o in candidate.outlines), default=0),
    )
    if int(new_state.get("target_word_budget") or 0) <= 0:
        new_state["target_word_budget"] = new_state["committed_until_word"]
    story_path = os.path.join(_ROOT, "books", book_id, "storyline.json")
    plan_path = planning_path(book_id)
    old_story_raw = read_json(story_path)
    old_plan_raw = read_json(plan_path) if plan_path.exists() else None
    try:
        save_tl(book_id, candidate)
        save_planning_state(book_id, new_state)
    except Exception:
        if isinstance(old_story_raw, dict):
            write_json_atomic(story_path, old_story_raw)
            from ui.web_blueprints.ctx import _storylines, _storyline_lock
            with _storyline_lock:
                _storylines[book_id] = BookStoryline.from_dict(old_story_raw)
        if isinstance(old_plan_raw, dict):
            write_json_atomic(plan_path, old_plan_raw)
        elif plan_path.exists():
            plan_path.unlink()
        raise
    _drop_engine(book_id)
    return {"ok": True, "outlines": len(candidate.outlines), "plots": len(candidate.plots),
            "phase": candidate.phase, "storyline_revision": candidate.storyline_revision,
            "planning": {"target_word_budget": new_state.get("target_word_budget", 0),
                         "committed_until_word": new_state.get("committed_until_word", 0),
                         "written_until_word": new_state.get("written_until_word", 0)}}


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
    """选材决策点候选池：情节弧库**平级独立弧模板** + 情节段库（供外部 agent 预选弧模板作参考，再在自身上下文生成弧+情节段）。

    返回 {templates, plots}：templates 为平级独立弧清单（无父子层级，各带 id/name/tags/
    description/min-max），plots 为情节段库候选（{id,name,category,sub_category}）。"""
    tl = _require_tl(book_id)
    _tags = ((tl.basic_info or {}).get("world_building") or {}).get("tags") or []
    candidates = struct_lib.search(tags=_tags)
    if not candidates:
        candidates = struct_lib.roots()[:20]
    templates = [_arc_item(t) for t in (candidates or [])[:30]]
    plots = [{
        "id": t.id, "name": t.name, "category": t.category,
        "sub_category": t.sub_category or "",
    } for t in (plot_lib.templates or [])[:30]]
    return {"templates": templates, "plots": plots,
            "current_phase": getattr(tl, "phase", ""),
            "has_outline": bool(getattr(tl, "outlines", None))}



_WIZARD_CAND_FILE = os.path.join(_ROOT, "storage", "wizard_candidates.json")


def _clear_wizard_candidates() -> None:
    """清空候选持久化（drive_ui(reset) 建书前调用，防跨会话残留）。"""
    from core.json_store import write_json_atomic
    try:
        write_json_atomic(_WIZARD_CAND_FILE, {"key": "", "candidates": []})
    except Exception:
        pass


def _outline_preview_text(outline_data: dict) -> str:
    """把 generate_outline_preview 产出的弧+情节段序列化为 prompt 预览文本。"""
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
        lines.append("情节段：" + "、".join(p.get("name", "") for p in plots))
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
    """从文本提取句式风格（规则层：句长/对话比/段落风格/高频词/禁用词/句首/动作节拍），
    转成 prefer/ban 规则写入该笔名 style_rules（kind 合一：句式风格=规则列表，不再写 profile.style_assets）。
    enabled：特征池逐项开关 {feature: bool}（缺省全启用）。"""
    from libraries.style_assets import (extract_style_features, default_enabled,
                                        STYLE_ASSET_FEATURES, features_to_rules)
    from libraries.style_rules import StyleRuleLibrary
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
    srl = StyleRuleLibrary()
    new_rules = features_to_rules(features, profile.id)
    if new_rules:
        srl.rules.extend(new_rules)
        srl._save()
    return {"features": features, "saved": bool(new_rules), "rules_added": len(new_rules),
            "profile": pen_name}


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
            raise RuntimeError("尚无已写章节，请先逐情节段写正文（save_plot_draft → 章满 save_chapter_text）")
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
        from libraries.decision_feed import dp as _dp, SOURCE_BY_CHECK as _SRC
        decision_points.append(_dp(
            source=_SRC.get(check) or "reviewer",
            kind=check, message=str(description)[:40], severity=severity,
            subject_id=str(location)[:40], anchor=f"ch{n}", chapter=n,
            suggested_action=str(suggestion)[:40],
            location=str(location)[:40], suggestion=str(suggestion)[:40],
        ))

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

    # 6. Future consumption：仅比较结构化事实与未执行 Plot 的 expected_facts，
    # 不从正文猜剧情。命中表示本章可能提前消耗后续明确承诺，交由用户/规划 Agent 决策。
    try:
        from libraries.plot_run_state import history_path
        from core.json_store import read_json
        from libraries.reconcile import actual_fact_entries
        history = read_json(history_path(book_id)) if history_path(book_id).exists() else {}
        current_delta = next((x for x in reversed((history or {}).get("chapters") or [])
                              if int(x.get("chapter_num") or 0) == n), {})
        actual = []
        for delta in (current_delta.get("plot_deltas") or []):
            actual.extend(actual_fact_entries(delta.get("facts") or {}))
        tl = book_mgr.load_storyline(book_id)
        consumed = []
        for p in (tl.plots or []) if tl else []:
            if getattr(p, "written_chapter", 0):
                continue
            for expected in (getattr(p, "expected_facts", None) or []):
                if not isinstance(expected, dict):
                    continue
                for fact in actual:
                    if (fact.get("subject") == expected.get("subject") and fact.get("type") == expected.get("type")
                            and fact.get("actual_to") == expected.get("expected_to")):
                        consumed.append({"future_plot_id": p.id, "future_plot_name": p.name,
                                         "subject": fact.get("subject"), "type": fact.get("type")})
        checks["future_consumption"] = {"passed": not consumed, "issues_count": len(consumed),
                                        "violations": consumed[:10]}
    except Exception as e:
        skipped.append("future_consumption")
        checks["future_consumption"] = {"passed": None, "skipped": True, "error": str(e)[:60]}

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
    fc = checks.get("future_consumption") or {}
    for it in (fc.get("violations") or [])[:3]:
        _push("future_consumption", "warning", "可能提前消耗后续情节", suggestion=it.get("future_plot_name", ""))

    complete = not skipped
    ran = [k for k in ("review", "continuity", "retention", "promises", "punch_points", "future_consumption")
           if (checks.get(k) or {}).get("skipped") is not True]
    all_passed = bool(ran) and all((checks[k] or {}).get("passed") is True for k in ran)
    passed = complete and all_passed
    issue_count = sum(int((checks[k] or {}).get("issues_count") or (checks[k] or {}).get("issue_count") or 0)
                      for k in ("review", "continuity", "retention", "promises", "punch_points", "future_consumption"))
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
    ②情节段仅挂最底层弧（不包含其他弧的弧）。只报告不修复，问题作 decision_points 由 agent/用户补弧或移情节段。

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
                "summary": "无弧/情节段，无需校验", "total_words": 0,
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

    # ② 叶弧：parents = 有子弧的弧 id；情节段 outline_id 须存在且非 parents
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
            _leaf_issues.append(f"情节段「{pname or pid}」的 outline_id={oid or '空'} 悬空，不属于任何弧")
            _viol.append({"plot_id": pid, "plot_name": pname, "outline_id": oid, "outline_name": "", "reason": "悬空"})
        elif oid in parents:
            _leaf_issues.append(f"情节段「{pname or pid}」挂在非最底层弧「{_oname}」({oid})——仅最底层弧可拥有情节段")
            _viol.append({"plot_id": pid, "plot_name": pname, "outline_id": oid,
                          "outline_name": _oname, "reason": "非最底层弧"})
    _leaf_passed = not _leaf_issues

    # ③ 弧内覆盖：顶层弧跨度 vs 其叶弧后代情节段 planned_words 之和（warning 级，不计硬失败）
    # 口径与 libraries/storyline_writer.planned_words 一致：plot.words(agent 目标字数) 优先，
    # 未给回退 cover_beats×200 封顶 1200（两处同步，勿单改）。
    def _pw_of(p):
        words = 0
        if isinstance(p, dict):
            words = int(p.get("words") or 0)
            beats = int(p.get("cover_beats") or 4)
        else:
            words = int(getattr(p, "words", 0) or 0)
            beats = int(getattr(p, "cover_beats", 4) or 4)
        if words > 0:
            return max(200, min(words, 3000))
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
            content = _pw_by_oid.get(getattr(a, "id", ""), 0)   # 顶层弧自身即叶弧时挂情节段
        gap_words = max(span - content, 0)
        ratio = (span / content) if content > 0 else 999.0
        _fill_arcs.append({"id": getattr(a, "id", ""), "name": a.name, "span": span,
                           "planned_words": content, "gap_words": gap_words,
                           "ratio": round(ratio, 1)})
        if gap_words > wpc:   # 弧内空白超一章即报（收紧：去掉 ratio>3 宽松条件）
            _fill_issues.append(f"顶层弧「{a.name}」跨度 {span} 字、情节段 planned 仅 {content} 字，约 {gap_words} 字空白（ratio {ratio:.1f}），建议拆子弧/缩弧跨度/补情节段")
    _fill_passed = not _fill_issues

    passed = _cov_passed and _leaf_passed and _fill_passed
    parts = []
    if _cov_issues:
        parts.append(f"弧树覆盖 {len(_cov_issues)} 处问题（{len(_gaps)} 处叙事空白）")
    if _leaf_issues:
        parts.append(f"情节段叶弧 {len(_leaf_issues)} 处问题")
    if _fill_issues:
        parts.append(f"弧内空白 {len(_fill_issues)} 处（跨度远超情节段内容）")
    if not parts:
        parts.append("弧树覆盖、情节段叶弧与弧内填充均通过")
    decision_points = [{"check": "storyline", "severity": "warning",
                        "description": str(s)[:60], "location": "", "suggestion": ""}
                       for s in (_cov_issues + _leaf_issues + _fill_issues)[:20]]
    suggestions = []
    if leading_gap:
        suggestions.append("在故事线开头补一个从 0 字开始的顶层弧，或把首个顶层弧 start_word 调到 0")
    for g in _gaps[:5]:
        suggestions.append(f"在「{g['before']}」与「{g['after']}」之间补弧或扩展前者 end_word 消除 {g['gap_words']} 字空白")
    for v in _viol[:5]:
        suggestions.append(f"把情节段「{v['plot_name']}」移到其所属弧的最底层子弧，或把「{v['outline_name']}」拆出子弧")
    for f in _fill_arcs[:5]:
        if f["gap_words"] > wpc:
            suggestions.append(f"顶层弧「{f['name']}」跨度 {f['span']} 字但情节段仅 {f['planned_words']} 字，拆出足够子弧/情节段填满，或把 end_word 缩到与内容匹配")

    # ④ 结构软提示（uniform/印刷感，不改 passed——只作决策点供 agent/步3 L2 消除或说明）
    _lattice = []
    leaf_arcs = [a for a in _arcs
                 if (getattr(a, "id", "") or "") not in parents
                 and getattr(a, "start_word", None) is not None
                 and getattr(a, "end_word", None) is not None]
    leaf_spans = sorted({(getattr(a, "end_word", 0) or 0) - (getattr(a, "start_word", 0) or 0)
                         for a in leaf_arcs})
    _uniform_leaf = len(leaf_arcs) >= 3 and len(leaf_spans) == 1
    if _uniform_leaf:
        _lattice.append(f"叶弧跨度全相等（{len(leaf_arcs)} 个叶弧均 {leaf_spans[0]} 字，常见=按章/words_per_chapter 均分），疑似把叶弧=章而非剧情结构，建议按各弧目标重排跨度")
    _bridge_words = sorted({_pw_of(p) for p in _plots})
    _uniform_bridge = len(_plots) >= 6 and len(_bridge_words) == 1
    if _uniform_bridge:
        _lattice.append(f"情节段目标字数全相同（{len(_plots)} 个情节段均 {_bridge_words[0]} 字，常见=全默认 cover_beats=4→800），疑似模板印刷，建议按场景浓淡差异化（words 300~2500）")

    # ⑤ 深度下探建议（可选，不强制、不改 passed）：两层树 + 存在 ≥3×wpc 的大叶弧 → 提示可拆第三层
    _by_arc_id = {(getattr(a, "id", "") or ""): a for a in _arcs}

    def _arc_depth(aid, seen=None):
        if not aid or aid not in _by_arc_id:
            return 1
        par = getattr(_by_arc_id[aid], "parent_arc_id", "") or ""
        if not par:
            return 1
        seen = seen or set()
        if aid in seen:      # 防环（异常数据）：环上按 1 截断
            return 1
        return 1 + _arc_depth(par, seen | {aid})

    _max_depth = max((_arc_depth(getattr(a, "id", "") or "") for a in _arcs), default=0)
    _big_leaves = []
    if _max_depth == 2:
        for a in leaf_arcs:
            _span = (getattr(a, "end_word", 0) or 0) - (getattr(a, "start_word", 0) or 0)
            if _span >= 3 * wpc:
                _big_leaves.append({"id": getattr(a, "id", ""), "name": getattr(a, "name", ""), "span": _span})
        _big_leaves = _big_leaves[:5]
    structure_hints = {"uniform_leaf_spans": _uniform_leaf, "uniform_bridge_words": _uniform_bridge,
                       "leaf_spans_set": leaf_spans[:10], "bridge_words_set": _bridge_words[:10],
                       "max_arc_depth": _max_depth,
                       "deep_split_suggested": bool(_big_leaves),
                       "deep_leaf_ids": [b["id"] for b in _big_leaves]}
    if _lattice:
        for h in _lattice:
            suggestions.append(h)
            decision_points.append({"check": "structure", "severity": "info", "description": h[:120],
                                    "location": "", "suggestion": "消除或向用户说明"})
        _tags = []
        if _uniform_leaf:
            _tags.append("叶弧跨度均一")
        if _uniform_bridge:
            _tags.append("情节段字数均一")
        parts.append("结构提示：" + "、".join(_tags) + "（软提示，见 suggestions/decision_points）")
    if _big_leaves:
        _big_names = "、".join(f"「{b['name']}」({b['span']}字≈{max(1, round(b['span'] / wpc))}章)"
                               for b in _big_leaves)
        suggestions.append(f"叶弧 {_big_names} 跨度较大：若内含 2+ 可独立排序的子目标，可拆出第三层（中弧→小叶弧）；"
                           f"无则保持两层即可——可选，不强制（structure_hints.deep_split_suggested）")
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
        "structure_hints": structure_hints,
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
        if not isinstance(bi, dict):
            return {"ok": False, "error": "invalid_input_shape", "field": "basic_info",
                    "received": type(bi).__name__, "action": "pass an object"}
        if "factions" in bi and not ((bi.get("world_building") or {}).get("factions")):
            return {"ok": False, "error": "invalid_input_shape",
                    "field": "basic_info.world_building.factions",
                    "received": "basic_info.factions",
                    "action": "move_factions_under_world_building"}
        if "world_building" in bi and not isinstance(bi.get("world_building"), dict):
            return {"ok": False, "error": "invalid_input_shape", "field": "basic_info.world_building",
                    "received": type(bi.get("world_building")).__name__, "action": "pass an object"}
        if "characters" in bi and not isinstance(bi.get("characters"), list):
            return {"ok": False, "error": "invalid_input_shape", "field": "basic_info.characters",
                    "received": type(bi.get("characters")).__name__, "action": "pass an array"}
    wb = bi.get("world_building") or {}
    factions_raw = wb.get("factions") or []
    if not isinstance(factions_raw, list):
        return {"ok": False, "error": "invalid_input_shape",
                "field": "basic_info.world_building.factions",
                "received": type(factions_raw).__name__, "action": "pass an array"}
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
    "set_picks": ("templates",),   # 开篇弧/情节段选择（templates 或 plots 任一非空，drive_ui 特判）
    "set_outline": ("outlines", "plots"),   # 步3②生成的弧+情节段（generate_outline_preview 产出，submit 随书落库）
    "next": (), "prev": (),
    "load_candidates": (), "skip_candidates": (),
    "fill_world": (),   # 步骤③世界观重新补全（Agent 兜底/重试）
    "reset": (),   # 清空向导 state（除 pen_name/库表外字段）——建书前先 reset，防残留干扰保真度
    "submit": (),
    "set_review": ("title",),   # 提取页：呈现五库候选审查卡（非建书命令，不入步门控）
    "set_replan_preview": ("book_id", "expected_revision", "diagnosis", "directions",
                            "selected_direction_id", "outlines", "plots", "planning_patch"),
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
    skip_candidates/fill_world/reset/submit/set_replan_preview。

    非阻塞：把命令写入意图队列，浏览器每 ~2.5s 轮询消费（start_book.html 的
    window.onnecommand 执行）。不入书锁（不写书）。
    建书仍走系统向导（/books/start POST）：agent 只驱动表单和步骤，提交由用户点击，
    **不能绕过向导直建**（护栏：无直建工具）。

    必填 args（cmd → 必填键，缺则报错）：
    - set_field: {field, value}   field ∈ idea/pen/title/words/borrow_source/borrow_tweak
    - set_tags: {tags: [str]}
    - set_characters: {characters: [{name, role, importance, identity, personality, golden_finger,
      gender, catchphrase, brief, title, age, death_year, faction, relations,
      behavior?, speech_profile?, development_plan?}]}（整体替换）
      role 只取 主角/配角/反派/其他；importance 必传（主角=1）；**relations 必须 [{name, relation}] 数组**
      （传字符串会让前端渲染中断、后续角色全丢）；
      behavior 可选 {decision_style:{under_pressure,danger,betrayal},
      communication_style:{stranger,friend,enemy}, emotion_expression:{anger,fear,sadness}}（情境→一贯反应，每格 1-3 短句；
      人物稳定感=不同刺激下反应一致）；
      speech_profile 可选 {rhythm,tone:str, habits:[句式/表达倾向], forbidden:[绝不说]}（语言倾向，非固定口头禅复读；
      缺省把 catchphrase 视作 habits 之一）；
      development_plan 可选（一句成长方向，如「从独行者成为领导者」，或 {growth_target,notes}）——只规划不绑 Storyline
    - set_candidates: {candidates: [{title, one_liner?, world_brief?}]}   title 必填
    - add_candidate: {candidate: {title, one_liner?, world_brief?}}   title 必填，增量追加 1 张候选卡
    - pick_candidate: {candidate: {title, world_brief, one_liner}} 或 {idx: int}（至少其一）
    - set_world: {world_building: {era?, power_system?, geography?, culture?, history?,
      social_structure?, core_conflict?, rules?, world_summary?, factions?},
      tone?, target_audience?, pov?, era_language?}   **顶层键必须叫 world_building**（部分维可分批提交，合并进表单不覆盖已填）；
      **rules 必须数组**（传字符串会被忽略）
    - set_picks: {templates: [id|{id,name}]} 或 {plots: [id|{id,name}]}（任一非空）
    - set_outline: {outlines: [非空列表], plots: [list], threads?, themes?}   步3②弧+情节段，submit 随书落库
      **结构必填（缺则命令被拒、不自动换算兜底）**——outlines 每项须 id 非空唯一 + name 非空 + 一组完整跨度：
      `start_word/end_word` 成对整数（0 基、start 含/end 不含、0<=start_word<end_word，权威）**或**
      `start_chapter/end_chapter` 成对整数（1<=start_chapter<=end_chapter）；半组（如只有 end_word）/全缺直接报错；
      备注用 notes 非 description、parent_arc_id 指向父弧 id 支持弧树嵌套（缺省=顶层弧）；弧=树状目标节点
      （定义见 NOVEL_AGENT.md 1.1），字数跨度由剧情结构决定、不设固定章数；仅最底层弧可拥有情节段；
      plots 每项须 id 非空唯一 + outline_id 非空且指向存在的最底层（叶）弧，其余可选
      （order 弧内序号；words=目标字数 0 基整数、按场景浓淡 300~2500，未给回退 cover_beats×200；
      cover_beats=节拍数 2~6；category/thread_id/roles/template_structure 可选）——缺 id/outline_id/叶弧归属也会被拒
    - set_review: {title, platform?, folder?, downloaded_chapters?, profile_id?, profile_name?,
      plots?, structures?, gags?, characters?, style_rules?}   **侦察/提取合并页**：把 agent 提炼的五库候选
      呈现成可勾选审查卡（drive_ui 命令，非建书命令，不套步门控）。至少一类非空才可提交；
      style_rules 每项 {kind(prefer|ban), pattern, desc?, severity?, replacements?}。
      审查数据字段对齐 NOVEL_AGENT.md 1.2，用户确认后由页面 POST /api/scout/ingest 落库。
    - set_replan_preview: {book_id, expected_revision, diagnosis, directions(2-3),
      selected_direction_id, outlines, plots(3-8), planning_patch, threads?, themes?}。
      只暂存续规划预览，不修改正式故事线；最终提交只能由用户在写作台抽屉确认。
    - submit: {}  **禁止 Agent 调用；建书只能由用户在页面点击提交**
    - next / prev / reset / load_candidates / skip_candidates / fill_world: {} 无必填
    """
    cmd = (cmd or "").strip()
    if cmd not in _WIZARD_CMDS:
        raise RuntimeError(f"未知向导命令：{cmd}，可选 {sorted(_WIZARD_CMDS)}")
    check_ui_command(cmd, selected_profile(sys.argv))
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
        # 结构校验（硬规则）：弧缺完整跨度/半组跨度、情节段挂非叶/悬空弧 → 直接拒绝，浏览器不代填兜底。
        _probs = outline_payload_problems(outs, args["plots"])
        if _probs:
            raise RuntimeError(f"命令 {cmd} 拒绝：outlines/plots 结构不完整——" + "；".join(_probs))
        try:
            from libraries.build_status import get_build_status as _build_status
            from libraries.planning_state import save_build_session
            _sid = (_build_status() or {}).get("build_session_id") or ""
            if _sid:
                _committed = max((int(o.get("end_word") or 0) for o in outs), default=0)
                _planning = dict(args.get("planning") or {})
                _planning.setdefault("mode", "open")
                _planning.setdefault("committed_until_word", _committed)
                _planning.setdefault("target_word_budget", max(120000, _committed))
                save_build_session(_sid, _planning)
        except ValueError as e:
            raise RuntimeError(f"命令 {cmd} planning 无效：{e}")
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
    elif cmd == "set_replan_preview":
        book_id = str(args.get("book_id") or "").strip()
        tl = book_mgr.load_storyline(book_id)
        if tl is None:
            raise RuntimeError(f"命令 {cmd}：书 {book_id} 无故事线")
        if isinstance(args.get("expected_revision"), bool) or not isinstance(args.get("expected_revision"), int):
            raise RuntimeError(f"命令 {cmd} 需 expected_revision 整数")
        # 预览必须基于**当前**故事线版本：陈旧预览要到 commit 才被 CAS 拒绝，
        # 而那时 auto 路径已经中断整轮写作。这里早失败，让计划器重读 get_story_state。
        current_revision = int(getattr(tl, "storyline_revision", 0) or 0)
        if int(args["expected_revision"]) != current_revision:
            raise RuntimeError(
                f"命令 {cmd}：expected_revision={args['expected_revision']} 与当前故事线版本 "
                f"{current_revision} 不一致（预览会过期）；请重新 get_story_state 后按最新版本生成")
        directions = args.get("directions")
        if not (isinstance(directions, list) and 2 <= len(directions) <= 3
                and all(isinstance(x, dict) and x.get("id") and x.get("title") for x in directions)):
            raise RuntimeError(f"命令 {cmd} 需 2-3 个含 id/title 的 directions")
        if not isinstance(args.get("diagnosis"), dict):
            raise RuntimeError(f"命令 {cmd} 需 diagnosis 对象")
        outs, plots = args.get("outlines"), args.get("plots")
        # 批大小与「剩余多少就续规划」的阈值同源（planning_state.REPLAN_BATCH_*），
        # 否则一次补太少 + 阈值太高 → 每 1–7 段就烧一整轮计划器会话。
        from libraries.planning_state import REPLAN_BATCH_MAX, REPLAN_BATCH_MIN
        if not isinstance(outs, list) or not (isinstance(plots, list)
                                             and REPLAN_BATCH_MIN <= len(plots) <= REPLAN_BATCH_MAX):
            raise RuntimeError(f"命令 {cmd} 需 outlines 数组及 "
                               f"{REPLAN_BATCH_MIN}-{REPLAN_BATCH_MAX} 个 plots")
        problems = outline_payload_problems(outs, plots,
                                            known_outlines=tl.outlines or [],
                                            known_plots=tl.plots or [])
        try:
            checked_patch = validate_patch(args.get("planning_patch"))
        except ValueError as e:
            problems.append(str(e))
            checked_patch = {}
        selected = str(args.get("selected_direction_id") or "")
        if selected not in {str(x.get("id")) for x in directions}:
            problems.append("selected_direction_id 未指向 directions 中的方向")
        if not problems:
            combined = tl.to_dict()
            combined["outlines"] = list(combined.get("outlines") or []) + outs
            combined["plots"] = list(combined.get("plots") or []) + plots
            report = globals()["validate_storyline"](
                outlines=combined["outlines"], plots=combined["plots"],
                words_per_chapter=tl.words_per_chapter,
            )
            if not report.get("passed"):
                problems.append(str(report.get("summary") or report))
        preview = {
            "expected_revision": int(args["expected_revision"]),
            "diagnosis": args["diagnosis"], "directions": directions,
            "selected_direction_id": selected,
            "outlines": outs, "plots": plots, "threads": args.get("threads") or [],
            "themes": args.get("themes") or [], "planning_patch": checked_patch,
            "validation": {"passed": not problems, "problems": problems},
        }
        from libraries.planning_state import save_replan_preview
        saved = save_replan_preview(book_id, preview)
        args = {"book_id": book_id, "preview_id": saved["preview_id"]}
    elif cmd == "set_review":   # 提取页：呈现五库候选审查卡（title 必填、至少一类非空、数组类型校验）
        if not (args.get("title") or "").strip():
            raise RuntimeError(f"命令 {cmd} 需 title 必填")
        if not args.get("platform"):
            args["platform"] = "fanqie"
        five = ["plots", "structures", "gags", "characters", "style_rules"]
        for k in five:
            v = args.get(k)
            if v is not None and not isinstance(v, list):
                raise RuntimeError(f"命令 {cmd} 需 {k} 为数组")
        if not any(args.get(k) for k in five):
            raise RuntimeError(f"命令 {cmd} 需 plots/structures/gags/characters/style_rules 至少一类非空")
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
# 这些工具的签名里有 book_id，wrapper 才能据此取锁目标（functools.wraps 保留签名）。
_LOCKED_TOOLS = {
    "confirm_world",
    # 薄工具（agent 生成后落盘，同样需书锁防并发）
    "save_chapter_text", "save_outlines", "save_book_meta",
    "save_basic_info",     # 会 bump storyline_revision（影响规划），必须与写作/续规划互斥
    # 首次读取会 lazy bootstrap planning_state，故也需同书锁（不做快照）。
    "get_story_state", "prepare_plot_run",
}

# 自持锁工具：签名里没有 book_id（只有 commit_token），wrapper 取不到锁目标，
# 由函数体在解析出归属书后自行 BookLock（见 save_plot_draft / _commit_plot_draft_locked）。
# 仅用于工具元数据如实标注 locked，切勿加进 _LOCKED_TOOLS（BookLock 不可重入 → 双重加锁死锁）。
_SELF_LOCKED_TOOLS = {"save_plot_draft"}


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
            if book_id and fn.__name__ not in {"get_story_state", "prepare_plot_run"}:
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


def fetch_novel(title: str = "", book_id: str = "", chapters: int = 30,
                start_chapter: int = 1, end_chapter: int = 0,
                download_delay: float = 1.0) -> dict:
    """抓取番茄小说：按书名或 book_id 搜索→下载指定章区间→保存到统一书库 storage/novels/。

    章节区间按**真实章号**：start_chapter=100, end_chapter=130 下载第 100~130 章
    （存为 0100..0130.json）；只给 chapters 时默认从 start_chapter(缺省 1) 起 N 章。
    幂等：已落盘章跳过不重下（already=True）；SVIP 锁定章预览不当正文落盘。
    纯抓取、无需 LLM（复用 FanqieCrawler + novel_storage）。进度实时写入
    storage/crawl_progress.json（/scout 页轮询展示）。返回 {ok, title, author,
    saved_chapters, folder, already, platform}。
    """
    if not title and not book_id:
        raise RuntimeError("请提供书名 title 或 book_id")
    start_chapter = max(1, int(start_chapter or 1))
    effective_end = int(end_chapter or 0)
    if effective_end <= 0:
        effective_end = start_chapter + int(chapters or 0) - 1
    if effective_end < start_chapter:
        raise RuntimeError(f"结束章 {effective_end} 小于起始章 {start_chapter}")
    from libraries.crawl_progress import write_crawl_progress
    from plugins.fanqie_scout import FanqieCrawler
    import time as _time
    crawler = FanqieCrawler()
    _task_id = f"mcp_fetch_novel_{int(_time.time() * 1000)}"
    _task_title = title or book_id or "番茄小说"

    def on_progress(phase, current, total, message):
        write_crawl_progress("running", phase, current, total, message,
                             task_id=_task_id, title=_task_title)

    try:
        novel = (crawler._get_novel_from_page(book_id) if book_id
                 else crawler.search_novel(title))
        if not novel:
            raise RuntimeError(f"未找到：{title or book_id}")
        on_progress("search", 1, 1, f"找到: {novel.title}")

        # 目录拉到 effective_end，再按真实章号过滤出 [start_chapter, effective_end]
        catalog = crawler.get_chapter_list(novel.book_id, effective_end)
        chapter_list = [c for c in catalog
                        if start_chapter <= int(c.get("index") or 0) <= effective_end]
        if not chapter_list:
            raise RuntimeError(f"起始章 {start_chapter} 超出该书可下载范围（目录 {len(catalog)} 章）")
        total_ch = len(chapter_list)
        from plugins.novel_storage import (save_novel, save_chapter, NOVELS_DIR,
                                           _safe_name)
        from plugins.fanqie_scout import FANQIE_FREE_MIN_CHARS
        folder = _safe_name(novel.title)

        # 幂等：跳过已落盘**且 content 完整**的章（短预览/空占位视为缺章 → 下次重下修复）
        from plugins.novel_storage import existing_complete_chapters
        existing = existing_complete_chapters(folder, min_units=FANQIE_FREE_MIN_CHARS)
        pending = [c for c in chapter_list if int(c.get("index") or 0) not in existing]
        skipped = total_ch - len(pending)
        if not pending:
            write_crawl_progress("done", "download", 0, 0,
                                 f"已是最新（{len(existing)} 章）",
                                 task_id=_task_id, title=_task_title,
                                 extra={"folder": folder, "platform": "fanqie"})
            return {"ok": True, "title": novel.title, "author": novel.author,
                    "saved_chapters": 0, "folder": folder, "already": True,
                    "platform": "fanqie"}

        # info.json：仅当该书尚未落盘时创建（增量/续传保留原来源元数据）
        if not (NOVELS_DIR / folder / "info.json").exists():
            save_novel("fanqie", {
                "title": novel.title, "author": novel.author,
                "book_id": novel.book_id, "url": novel.url,
                "genre": novel.genre, "chapter_count": novel.chapter_count,
                "cover": novel.cover, "intro": novel.intro,
            }, [])

        downloaded = 0
        locked = 0
        from core.text_utils import cjk_char_count as _cjkc   # 锁章门用纯 CJK 口径
        for i, ch in enumerate(pending):
            content = crawler.download_chapter(novel.book_id, ch["id"])
            units = count_prose_units(content or "")
            if content and content.strip() and _cjkc(content or "") >= FANQIE_FREE_MIN_CHARS:
                save_chapter("fanqie", folder, {
                    "index": ch["index"], "title": ch["title"],
                    "content": content, "word_count": units,
                })
                downloaded += 1
            elif content and content.strip():
                locked += 1   # 锁章预览：不当正文落盘
            on_progress("download", i + 1, len(pending), ch["title"][:30])
            if i < len(pending) - 1:
                _time.sleep(download_delay)   # 礼貌爬取间隔

        _msg = f"下载完成 {downloaded}章"
        if skipped:
            _msg += f"（跳过 {skipped}）"
        if locked:
            _msg += f"，{locked} 章锁定预览未落盘（需解锁/镜像全文）"
        write_crawl_progress("done", "download", downloaded, len(pending), _msg,
                             task_id=_task_id, title=_task_title,
                             extra={"folder": folder, "platform": "fanqie"})
        return {"ok": True, "title": novel.title, "author": novel.author,
                "saved_chapters": downloaded, "folder": folder,
                "already": False, "locked": locked, "platform": "fanqie"}
    except Exception as e:
        write_crawl_progress("error", "", 0, 0, str(e), task_id=_task_id, title=_task_title)
        raise


def fetch_webnovel(site: str = "bookszw", url: str = "", book_id: str = "",
                   chapters: int = 0, start_chapter: int = 1, end_chapter: int = 0,
                   download_delay: float = 0.5) -> dict:
    """抓取网页镜像站小说（番茄锁定章需 SVIP 时的替代全文源，如 bookszw 零点看书）。

    按书籍 URL 或 book_id 下载→保存到统一书库 storage/novels/<书名>/。章节按列表序号（第1章=1）；
    chapters<=0（默认）全文下载（可按站点配置过滤番外）；chapters>0 按区间。
    纯抓取、无需 LLM（复用 plugins.webnovel_scraper.download_webnovel + novel_storage）。
    进度实时写入 storage/crawl_progress.json（/scout 页轮询展示）。返回 {ok, title,
    author, saved_chapters, folder, platform, site}。
    """
    if not url and not book_id:
        raise RuntimeError("请提供书籍 URL 或 book_id")
    import time as _time
    from libraries.crawl_progress import write_crawl_progress
    from plugins.webnovel_scraper import download_webnovel
    _task_id = f"mcp_fetch_webnovel_{int(_time.time() * 1000)}"
    _task_title = (url or book_id or "网页镜像站小说")[:40]

    def on_progress(phase, current, total, message):
        write_crawl_progress("running", phase, current, total, message,
                             task_id=_task_id, title=_task_title)

    try:
        info, dl = download_webnovel(
            site=site, url=url, book_id=book_id, chapters=chapters,
            start_chapter=start_chapter, end_chapter=end_chapter,
            download_delay=download_delay, on_progress=on_progress, platform="web")
        n = dl["chapters"]
        msg = (f"已是最新（{dl.get('skipped', 0)} 章）" if dl.get("already")
               else f"下载完成 {n}章")
        write_crawl_progress("done", "download", n, n, msg,
                             task_id=_task_id, title=_task_title,
                             extra={"folder": dl["folder"], "platform": "web", "site": site})
        return {"ok": True, "title": info["title"], "author": info["author"],
                "saved_chapters": n, "folder": dl["folder"], "already": dl.get("already", False),
                "platform": "web", "site": site}
    except Exception as e:
        write_crawl_progress("error", "", 0, 0, str(e), task_id=_task_id, title=_task_title)
        raise


def fetch_book(title: str = "", url: str = "", book_id: str = "", site: str = "bookszw",
               chapters: int = 0, start_chapter: int = 1, end_chapter: int = 0,
               download_delay: float = 0.5) -> dict:
    """综合抓取一本书（番茄元数据+权威目录 + 镜像站全文 → 统一书库一本）。

    输入书名 / 番茄 book_id / 镜像站 URL 任一即可：番茄解析元数据（书名/作者/简介 intro/
    封面 cover + 章节目录权威，番茄目录为准），镜像站（默认 bookszw 零点看书）提供全文，
    按番茄目录合并；番茄比镜像多的章节落空占位。番茄解析不到 → 回退镜像站元数据。
    chapters<=0（默认）全书；>0 按区间。进度实时写 storage/crawl_progress.json
    （/scout 页轮询展示）。返回 {ok, title, author, intro, cover, saved_chapters,
    folder, already, platform, sources}。
    """
    from libraries.crawl_progress import write_crawl_progress
    import time as _time
    from plugins.book_fetch import download_book_merged
    _task_id = f"mcp_fetch_book_{int(_time.time() * 1000)}"
    _task_title = (title or url or book_id or "综合抓取")[:40]
    _state = {"phase": "", "cur": 0, "total": 0}   # 供 on_step 复用进度条位置（防跳 0）
    _tname = {"title": _task_title}                # 顶部标题：解析番茄成功后自动换成书名

    def on_progress(phase, current, total, message):
        _state.update(phase=phase, cur=current, total=total)
        write_crawl_progress("running", phase, current, total, message,
                             task_id=_task_id, title=_tname["title"])

    def on_step(label, status="running", detail=""):
        # 分步清单：写 crawl_progress steps（/scout 页轮询渲染）
        if label == "解析番茄" and detail:
            _nm = detail.split(" · ")[0].strip()
            if _nm:
                _tname["title"] = _nm
        write_crawl_progress("running", _state["phase"], _state["cur"], _state["total"],
                             detail or label, task_id=_task_id, title=_tname["title"],
                             step={"label": label, "status": status, "detail": detail})

    try:
        meta, dl = download_book_merged(
            title=title, url=url, book_id=book_id, site=site,
            chapters=chapters, start_chapter=start_chapter, end_chapter=end_chapter,
            download_delay=download_delay, on_progress=on_progress, on_step=on_step)
        n = dl["chapters"]
        msg = (f"已是最新（{dl.get('skipped', 0)} 章）" if dl.get("already")
               else f"下载完成 {n}章")
        # 最终顶标题以解析出的书名为准（兜底：未走 on_step 的路径也用 meta title）
        _tname["title"] = meta.get("title") or _tname["title"]
        write_crawl_progress("done", "download", n, n, msg,
                             task_id=_task_id, title=_tname["title"],
                             extra={"folder": dl["folder"], "platform": "merged", "site": site})
        return {"ok": True, "title": meta["title"], "author": meta["author"],
                "intro": meta.get("intro", ""), "cover": meta.get("cover", ""),
                "saved_chapters": n, "folder": dl["folder"], "already": dl.get("already", False),
                "platform": meta.get("platform", "merged"), "sources": dl.get("sources", "merged")}
    except Exception as e:
        write_crawl_progress("error", "", 0, 0, str(e), task_id=_task_id, title=_tname["title"])
        raise


def discover_hot(platform: str = "fanqie", key: str = "", count: int = 10) -> dict:
    """侦察小说热榜（多平台）：返回热门书列表（排名/书名/作者/题材/热度/简介）。

    platform 默认 fanqie（番茄）；key 为榜单分类 id（如 258 传统玄幻）或题材中文名
    （如"玄幻""都市"，空/'全部'=聚合综合热榜）；count 默认 10。
    返回 {"ok", "platform", "count", "novels": [{platform, rank, book_id, title,
    author, category, word_count, chapter_count, hot_score, intro, url}]}。
    """
    from plugins.hot_ranks import discover as hot_discover
    novels = hot_discover(platform, key=key, count=count) or []
    return {"ok": True, "platform": platform, "count": len(novels), "novels": novels}


def list_rankings(platform: str = "fanqie", gender: str = "male") -> dict:
    """查平台热榜榜单/分类清单（供挑题材/参考爆款时选榜单）。

    platform 默认 fanqie；gender male/female（男频/女频）。
    返回 {"ok", "platform", "gender", "rankings": [{id, name}]}。
    """
    from plugins.hot_ranks import list_rankings as hr_list_rankings
    rankings = hr_list_rankings(platform, gender=gender) or []
    return {"ok": True, "platform": platform, "gender": gender, "rankings": rankings}


def list_crawled_novels(platform: str = "") -> dict:
    """列出已下载的小说库（统一书库：每书一个文件夹，platform 为 info 字段）。

    复用 novel_storage.list_novels；platform 非空时按 info.json 的 platform 字段过滤
    （fanqie/merged/web...），空=全部。返回 {"ok", "count", "novels": [{title, author,
    platform, book_id, genre, chapter_count, saved_chapters, folder, intro?}]}。
    """
    from plugins.novel_storage import list_novels
    novels = list_novels(platform)
    return {"ok": True, "count": len(novels), "novels": novels}


def read_crawled_novel(platform: str = "", folder: str = "",
                       chapter: int = 0, start_chapter: int = 0,
                       end_chapter: int = 0, max_chapters: int = 25) -> dict:
    """读已下载小说内容。三种模式互斥（folder 为唯一路径 key，platform 仅作兼容/过滤）：

    - chapter=0（默认）：只返回元数据 + 章节目录 {chapters:[{index,title,word_count}]}，正文不进。
    - chapter>0：只返回该章正文 {chapter:{index,title,content,word_count}}——**不再回带整份章节目录**
      （目录开头 chapter=0 读一次即可；单章走 O(1) 懒加载，不整本扫盘）。
    - start_chapter>0：成批顺序读 [start_chapter, end_chapter]（end 缺省 = start + max_chapters - 1，
      max_chapters 默认 25 防单次工具结果过大触发裁剪），返回紧凑
      {chapters:[{index,title,content,word_count}]}（**无目录回声**）——整本扫读分窗一次取若干章，
      免每章一次工具往返。

    返回 {ok, title, author, platform, folder, chapter_count, chapters|chapter, ...}。
    """
    from plugins.novel_storage import (load_novel, read_chapter,
                                       read_chapter_range, read_novel_info)
    if not folder:
        raise RuntimeError("请提供 folder（书名目录，来自 list_crawled_novels）")

    # ── 成批顺序读（start_chapter>0）——只回窗口正文，不回带目录 ──
    if start_chapter:
        lo = int(start_chapter)
        hi = (int(end_chapter) if (end_chapter and int(end_chapter) >= lo)
              else lo + int(max_chapters) - 1)
        if hi < lo:
            hi = lo
        chapters = read_chapter_range(folder, lo, hi)
        info = read_novel_info(folder) or {}
        return {
            "ok": True, "title": info.get("title", ""), "author": info.get("author", ""),
            "platform": info.get("platform", platform or ""), "folder": folder,
            "chapter_count": info.get("chapter_count", 0),
            "read_start": lo, "read_end": hi,
            "chapters": [{"index": c.get("index"), "title": c.get("title", ""),
                          "word_count": c.get("word_count", 0),
                          "content": c.get("content", "")} for c in chapters],
        }

    # ── 单章（chapter>0）——只回该章正文，不回带目录 ──
    if chapter:
        c = read_chapter(platform, folder, int(chapter))
        if not c:
            raise RuntimeError(f"章节不存在：第{chapter}章")
        info = read_novel_info(folder) or {}
        return {
            "ok": True, "title": info.get("title", ""), "author": info.get("author", ""),
            "platform": info.get("platform", platform or ""), "folder": folder,
            "chapter_count": info.get("chapter_count", 0),
            "chapter": {"index": c.get("index"), "title": c.get("title", ""),
                        "word_count": c.get("word_count", 0), "content": c.get("content", "")},
        }

    # ── 目录（chapter=0）——元数据 + 章节目录（with_content=False，正文不进）──
    data = load_novel(platform, folder, with_content=False)
    if not data:
        raise RuntimeError(f"未找到已下载小说：{folder}")
    info, chapters = data["info"], data["chapters"]
    return {
        "ok": True,
        "title": info.get("title", ""), "author": info.get("author", ""),
        "platform": info.get("platform", platform or ""), "genre": info.get("genre", ""),
        "intro": info.get("intro", ""), "cover": info.get("cover", ""),
        "book_id": info.get("book_id", ""), "folder": folder,
        "chapter_count": len(chapters),
        "chapters": [{"index": c.get("index", i + 1), "title": c.get("title", ""),
                      "word_count": c.get("word_count", 0)}
                     for i, c in enumerate(chapters)],
    }


def extract_state(folder: str = "", action: str = "load",
                  mode: str = "summary", state: dict | None = None) -> dict:
    """读/存「整本扫读」提取的断点工作状态（storage/extract_work/<folder>.json，每书一个文件）。

    整本顺序通读式提取分多段跑（dsh 会话内无上下文压缩），段间靠本文件续：
    每段读到自己判断的窗口末尾 → 把「压缩记忆 digest + 游标 cursor + 已入库名称」save 落盘 →
    下一段 load(summary) 从断点续读（只带 digest、不带旧章原文）。

    action=load（默认）：读状态。mode=summary 只回 {book, cursor, status, memory,
      committed_counts/names, style_rules_profile}（精简防 dsh 8KB 裁剪；无记录 exists=false）；
      mode=full 回全部（含 segments_log）。重复读不消费。
    action=save：需 folder + state（state schema：book{cursor,status,memory{digest,open_segments,
      people,unresolved},committed{plots,structures,gags,characters},style_rules_profile,
      segments_log}；缺省补默认）。整体覆盖，返回 {ok, folder, cursor, status}。
    action=clear：删除该书扫读状态（重新扫读/归档），返回 {ok, cleared}。

    状态文件纯规则读写，无 LLM。
    """
    from libraries.extract_state import (clear_extract_state, load_extract_state,
                                         save_extract_state)
    if not folder:
        raise RuntimeError("请提供 folder（书名目录，来自 list_crawled_novels）")
    if action == "clear":
        return clear_extract_state(folder)
    if action == "save":
        return save_extract_state(folder, state)
    return load_extract_state(folder, mode=mode)


def ingest_library_assets(plots: list | None = None, structures: list | None = None,
                          gags: list | None = None, characters: list | None = None,
                          source: str = "fanqie", gate: bool = True) -> dict:
    """提取入库底层工具：把已审查确认的情节段/弧/笑点/角色写入四库。

    novel-scout 流程禁止 agent 直接调用本工具；必须先用 drive_ui(set_review)
    呈现候选，由用户在 /extract 页面确认后再由页面调用入库接口。

    纯规则落盘、无 LLM（复用 FanqieScoutAgent.ingest_selected，角色走新增
    _add_character）。字段格式——plot {name, category, sub_category, structure,
    slots[{name, options}], notes, word_range}；structure 为**平级独立弧**（每条 = 一个
    弧 dict，字段 {name, description, min_words, max_words, key_events, foreshadow_
    opportunities, tags}，无父子层级、逐条判定去重入库）；
    gag {name, category, pattern_description, fit_scenes, examples}；character {name,
    personality, description, archetypes, examples, catchphrases, tags, fit_tags}。

    gate=True（默认）：入库前经 extract_judge 闸门——结构不完整 / 库内机制级近似 /
    自评书级专用的候选**不写入四库**，返回 judge 报告（incomplete 缺字段 /
    duplicate 库内近似 / book_archive 书级专用），agent 据报告修正或记入
    extract_state.digest。候选可带自评字段 `_book_specific=true` / `_reusable=false`
    主动标书级专用。
    gate=False：维持旧行为（仅 scout_{source}_{name} 精确 id 去重，全量写入）。

    返回 {ok, source, plots, structures, gags, characters, judge?}。
    """
    if not any([plots, structures, gags, characters]):
        raise RuntimeError("至少提供 plots/structures/gags/characters 之一")
    from plugins.fanqie_scout import FanqieScoutAgent
    from libraries.extract_judge import judge_all, split_by_decision

    cands = {"plots": plots or [], "structures": structures or [],
             "gags": gags or [], "characters": characters or []}
    filtered = cands
    report = {}
    if gate:
        judge = judge_all(plots=cands["plots"], structures=cands["structures"],
                          gags=cands["gags"], characters=cands["characters"],
                          plot_lib=plot_lib, struct_lib=struct_lib,
                          gag_lib=gag_lib, char_lib=char_lib)
        filtered = {}
        dropped = {}
        for kind, src in cands.items():
            keep, drop = split_by_decision(src, judge.get(kind, []))
            filtered[kind] = keep
            for dec, items in drop.items():
                dropped.setdefault(dec, []).extend(items)
        report["judge"] = {
            "gated": bool(dropped),
            "incomplete": dropped.get("incomplete", []),
            "duplicate": dropped.get("duplicate", []),
            "book_archive": dropped.get("book_archive", []),
        }

    scout = FanqieScoutAgent(plot_lib=plot_lib, struct_lib=struct_lib,
                             gag_lib=gag_lib, char_lib=char_lib)
    stats = scout.ingest_selected(plots=filtered["plots"], structures=filtered["structures"],
                                  gags=filtered["gags"], characters=filtered["characters"],
                                  source=source)
    return {"ok": True, "source": source, **stats, **report}


def judge_extraction(plots: list | None = None, structures: list | None = None,
                     gags: list | None = None, characters: list | None = None) -> dict:
    """入库判断闸门（预检，不写库）：对候选执行「什么能进四库」的确定性判定。

    每条候选返回 decision ∈ four_lib（可进四库）/ duplicate（库内已有机制级近似，
    跳过或差异化改名）/ incomplete（结构不完整，缺字段，需补全或落书级档案）/
    book_archive（自评书级专用，不进四库，可记 extract_state）。附 reasons 与
    overlap_with（与库内哪条近似）。

    判据：①结构完整性（情节段需 structure 箭头骨架 + slots、弧需可复用 description、笑点需
    pattern_description、角色需 personality）②库内 bigram 机制级近似（含本批
    已过闸候选）③候选自评 `_book_specific=true` / `_reusable=false`。
    structures 传**平级独立弧**（每条 = 一个弧 dict，无父子层级），逐条判定。
    纯规则无 LLM。入库请用 ingest_library_assets（gate=True 应用同一闸门自动过滤）。
    """
    from libraries.extract_judge import judge_all
    return {"ok": True, **judge_all(plots=plots, structures=structures,
                                    gags=gags, characters=characters,
                                    plot_lib=plot_lib, struct_lib=struct_lib,
                                    gag_lib=gag_lib, char_lib=char_lib)}


def _build_registry():
    # 顺序有讲究：导航/建书向导驱动排最前（flash 对列表前部工具更敏感，能保证
    # "打开页面"请求正确触发 navigate），其次只读摸底，再创作链/上架/工具。
    fns = [
        # 导航 / 建书向导驱动（用户高频意图，必须前置）
        navigate, drive_ui,
        # 只读摸底
        list_books, get_book_state, prepare_plot_run, get_storyline, get_story_state,
        get_book_detail, get_build_status, query_arc_library, query_plots, query_gags, query_profiles, query_characters,
        get_pen_style, pick_plot_sample,
        # 规划（薄工具：agent 生成后落盘；旧工具内 LLM 生成已由 agent 自主生成接管）
        save_basic_info,
        save_outlines, save_book_meta,
        arc_material_candidates,
        # 写作 / 元数据（薄工具：agent 生成后落盘）
        save_plot_draft, save_chapter_text,
        add_style_rule, delete_style_rule,
        add_style_sample, delete_style_sample, list_style_samples, get_style_sample,
        # 上架 / 质量门禁 / 校验
        publish_check, mark_finished, publish_book, export_book,
        chapter_quality_gate, validate_storyline, validate_world,
        # 抓取 / 侦察 / 提取入库（进度写 crawl_progress.json，/scout 页轮询展示；
        # fetch_book = 综合抓取（番茄元数据+权威目录 + 镜像站全文，统一书库）；fetch_novel 番茄专用；
        # fetch_webnovel 镜像站专用）
        fetch_book, fetch_novel, fetch_webnovel, discover_hot, list_rankings, list_crawled_novels, read_crawled_novel,
        extract_state, judge_extraction, ingest_library_assets,
    ]
    seen = set()
    entries = []
    for fn in fns:
        name = fn.__name__
        if name in seen:
            raise RuntimeError(f"工具注册表去重失败：{name} 出现两次")
        seen.add(name)
        # 包装顺序：phase 门控最外层（phase 不对就不等锁）→ 书锁 → 原函数。
        # 门控对未收录工具原样返回；锁只对 _LOCKED_TOOLS 生效（自持锁工具见
        # _SELF_LOCKED_TOOLS，其函数体自己加锁，只影响元数据标注）。functools.wraps
        # 逐层保留 __name__/__wrapped__，MCP 端 schema 不受影响。
        wrapped = _wrap_book_lock(fn) if name in _LOCKED_TOOLS else fn
        wrapped = _wrap_phase_gate(wrapped)
        from libraries.tool_policy import PHASE_GATES
        entries.append({
            "name": name,
            "description": (inspect.getdoc(fn) or "").strip(),
            "input_schema": _func_to_schema(fn),
            "func": wrapped,
            **tool_metadata(name, allowed_phases=PHASE_GATES.get(name),
                            locked=name in _LOCKED_TOOLS or name in _SELF_LOCKED_TOOLS),
        })
    return entries


TOOL_REGISTRY = _build_registry()
