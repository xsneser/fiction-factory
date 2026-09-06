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
from libraries.reviewer import HARD_MIN_RATIO  # noqa: E402
from libraries.storyline import OutlineSlot, annotate_plot_roles, \
    get_mc, get_characters, normalize_basic_info  # noqa: E402
from libraries.book_lock import BookLock, BookBusyError  # noqa: E402
from libraries.tool_policy import _wrap_phase_gate  # noqa: E402
from libraries import style_md  # noqa: E402  # 样本驱动:styles/<pen>.md 与 STYLE REFERENCE 样本
from libraries import style_samples  # noqa: E402  # 样文库 samples.json + 预算选样注入


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
    next_plot（第一个未写情节段 written_chapter==0，含 plot_id/name/roles/outline_id）。
    agent 逐情节段循环每轮只调本工具一次，避免重复读上下文。
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
    # next_plot：第一个未写情节段（written_chapter==0）
    next_plot = None
    if tl:
        for p in tl.plots:
            if not (getattr(p, "written_chapter", 0) or 0):
                next_plot = {
                    "plot_id": p.id, "name": p.name, "roles": list(getattr(p, "roles", None) or []),
                    "outline_id": getattr(p, "outline_id", "") or "",
                }
                break
    payload["next_plot"] = next_plot
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
    return payload


def get_storyline(book_id: str) -> dict:
    """读取一本书的故事线（timeline）JSON：弧/情节段/线程/内涵/基础设定。"""
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


def get_pen_style(book_id: str = "", profile_id: str = "", query: dict = None,
                  k: int = 3, avoid: list = None) -> dict:
    """读一个笔名的完整写作风格（句式风格+禁止内容+语言习惯+通用纪律），写作 agent 动笔前必读。

    book_id 与 profile_id 至少其一：book_id 优先按书绑定的笔名解析；否则按 profile_id；
    都无则默认笔名（枫落）。query=多维权表(英文键)，如 {"scene":["investigation"],
    "cast":"solo","dramatic_state":"uneasy"}：给则 STYLE REFERENCE 用加权随机从词条池抽 ≤k 条
    (硬过滤→软加权→加权随机→自动避重，k 默认 3)；不给则多样封顶注入。avoid 可追加指定避开 id。
    返回 prose style_rules（权威）+ 结构化 style/forbidden 列表 + samples(维度视图)，
    供逐条遵守/精确引用。信息不足时优先用本工具重读（独立薄工具，不纠缠全量上下文）。
    """
    from libraries.style_rules import StyleRuleLibrary, DEFAULT_PROFILE_ID
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
    rules = [r for r in StyleRuleLibrary().rules_for(profile.id) if r.enabled]
    # 样本驱动:存在 styles/<pen>.md → style_rules = 该 md(负约束/原则);
    # 其后若存在 storage/style_refs/<pen>.reference.txt → 追加为 STYLE REFERENCE 人工样本(最高风格来源)。
    # 无 md → 回退规则拼装(legacy,数组照旧)。每次现读文件,手改 md/样本即刻生效。
    style_md_text = style_md.read_style_md(profile)
    # STYLE REFERENCE:全局词条库。给 query → 加权随机抽 ≤k 条(自动近期避重);无 query → 多样封顶。
    # md 单独保留、不参与裁剪;无 JSON → 旧文件兜底。
    _avoid = [x for x in (list(avoid or []) + list(_STYLE_RECENT)) if x]
    ref_text, ref_meta = style_samples.build_ref_text_for_profile(
        profile, query=query, k=int(k or 3), avoid=_avoid)
    if query and ref_meta and ref_meta.get("mode") == "pick":
        for _sid in (ref_meta.get("selected") or []):
            if _sid not in _STYLE_RECENT:
                _STYLE_RECENT.append(_sid)
        del _STYLE_RECENT[:-_STYLE_RECENT_MAX]
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
    _cur = style_samples.load_samples()  # 全局样文库词条
    if _cur:
        _sel_ids = [x for x in (getattr(profile, 'sample_ids', None) or []) if x]
        _meta_cur = [s for s in _cur if (not _sel_ids or s.id in _sel_ids)]
        meta_samples = [{"id": s.id, "title": s.title, "scene_tags": s.scene_tags, "dims": s.dims,
                         "word_count": s.word_count, "source": s.source} for s in _meta_cur]
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
    """(兼容壳)样文库现为**全局词条库**(不分笔名);profile_id 仅占位,不再按笔名解析文件。"""
    return None, None, style_samples.load_samples() or []


def add_style_sample(profile_id: str, text: str, title: str = "", scene_tags: list = None,
                     source: str = "", note: str = "", replace_id: str = "",
                     no_warn: bool = None, dims: dict = None) -> dict:
    """给**全局样文库**加/替换一个词条(STYLE REFERENCE 人工样文,不分笔名,各笔名写作共享)。

    供 agent 把参考书里的**完整连续场景**按段截取入库(不拆技巧、不润色、保留普通解释句;
    别单喂金句/纯高潮)。scene_tags 为过渡期中文标签(可空);dims=多维权表(英文键,8 维:scene/
    narrative_action 列表 + dramatic_state/cast/dialogue_density/information_density/pace/pov 单值,
    缺维=选择器通配;非法值被丢弃);replace_id 给出则替换该条否则追加;no_warn=True 人工确认保留。
    profile_id 仅向后兼容占位。服务端算字数并再生 reference.txt 镜像;注入按 query 加权随机。
    """
    cur = style_samples.load_samples() or []
    txt = (text or "").strip()
    if not txt:
        raise RuntimeError("样文文本不能为空(应是一段完整连续场景原文)")
    records = [s.to_dict() for s in cur]
    meta = {"title": (title or "").strip(),
            "scene_tags": [str(t).strip() for t in (scene_tags or []) if str(t).strip()],
            "source": (source or "").strip(), "note": (note or "").strip(),
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
    """删除全局样文库的一个词条(按 id,如 s1)。"""
    cur = style_samples.load_samples() or []
    if not cur or not any(s.id == sample_id for s in cur):
        return {"ok": False, "error": f"样文 {sample_id} 不存在"}
    cur = [s for s in cur if s.id != sample_id]
    style_samples.save_samples(samples=cur)
    return {"ok": True, "deleted": sample_id, "total": len(cur)}


def list_style_samples(profile_id: str = "") -> dict:
    """列全局样文库**元数据**(id/标题/场景标签/字数/来源/备注,不含正文,保持薄)。

    供 agent 看当前有哪些词条、各属什么场景;想聚焦某场景时调 get_style_sample 取全文。"""
    cur = style_samples.load_samples() or []
    rows = [{"id": s.id, "title": s.title, "scene_tags": s.scene_tags, "dims": s.dims,
             "source": s.source, "note": s.note, "word_count": s.word_count} for s in cur]
    return {"ok": True, "scope": "global", "count": len(rows), "samples": rows}


def get_style_sample(profile_id: str = "", sample_id: str = "") -> dict:
    """取全局样文库一个词条**全文**;sample_id 空则只回目录(与 list 同)。"""
    cur = style_samples.load_samples() or []
    if not sample_id:
        return {"ok": True, "scope": "global", "count": len(cur),
                "samples": [{"id": s.id, "title": s.title, "scene_tags": s.scene_tags, "dims": s.dims,
                             "word_count": s.word_count} for s in cur]}
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
                      plot_segments: list | None = None) -> dict:
    """[薄工具] 保存整章正文（agent 自主生成后调用，内部不调 LLM）。

    agent 生成正文后，本工具负责纯规则副作用：去AI味 → 规则审查 → 章节落盘
    （含情节段）→ 书进度/字数 → 故事线 written_chapter 进度 → 角色状态 →
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

    # 1) 规则去AI味（词替换+段落节奏，无 LLM）；有情节段则逐段去并保持桥梁结构。
    #    防静默丢字：plot_segments 必须覆盖 text（总长 ≥ text 70%）。只列了部分情节段时以
    #    text 为正文源落盘、不挂情节段，并回传 segment_warning（提示 agent 把每情节段都列入）。
    processed = text
    segment_warning = None
    if plot_segments:
        raw_cover = sum(len((b.get("text") or "")) for b in plot_segments)
        if raw_cover < len(text) * 0.7:
            segment_warning = (f"plot_segments 总长 {raw_cover} ＜ 正文 {len(text)}，疑似只列了部分情节段；"
                              f"已按整章正文落盘、未挂情节段。请把本章每个情节段都列入 plot_segments")
            plot_segments = None
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
                pid = seg.get("plot_id")
                if pid and pid in by_pid:
                    segs[by_pid[pid]] = seg        # 同 plot_id 重写：替换旧条目保持原位置，最后写入胜出
                else:
                    if pid:
                        by_pid[pid] = len(segs)
                    segs.append(seg)
            plot_segments = segs
            processed = "\n\n".join(s["text"] for s in segs)
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

    # 2.5) 硬门禁：正文低于字数下限 → 拒绝落盘（保留进行中草稿），逼 agent 续写满章
    if review_dict and not review_dict.get("passed"):
        short = any(i.get("severity") == "error" and i.get("category") == "word_count"
                    for i in (review_dict.get("issues") or []))
        if short:
            raise RuntimeError(
                f"第 {n} 章正文 {count_prose_units(processed)} 字，低于本章下限 "
                f"{int(target * HARD_MIN_RATIO)} 字，正文不完整，未落盘。"
                f"请继续写满本章（逐情节段补全全部未写情节段）后，再调用 save_chapter_text。"
            )

    # 3) 落盘章节
    book_mgr.save_chapter(book_id, n, title or f"第{n}章", processed, summary or "",
                          review=review_dict, bridges=plot_segments)

    # 4) 书进度/字数
    try:
        if book.current_chapter < n:
            book.current_chapter = n
            book.status = "writing"
            book.total_words = (book.total_words or 0) + count_prose_units(processed)
            book_mgr.update(book)
    except Exception:
        pass

    # 5) 故事线 written_chapter 进度（本情节段标记为已写）
    #    plot_segments 缺失时回退草稿 bridges（agent 漏传 plot_segments 也不会卡住 next_plot）
    try:
        tl = book_mgr.load_storyline(book_id)
        if tl:
            written_plot_ids = {b.get("plot_id") for b in (plot_segments or []) if b.get("plot_id")}
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
            "review": review_dict, "segment_warning": segment_warning}


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


def save_plot_draft(book_id: str, chapter_num: int, plot_id: str,
                      plot_name: str, text: str) -> dict:
    """[薄工具] 保存单个情节段到进行中草稿（draft_chapter.json，断点续写保底）。

    agent 逐情节段生成后调用：规则去AI味 → 追加进草稿（含 buffer/words/bridges），
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
    entry = {"plot_id": plot_id or "", "plot_name": plot_name or "", "text": text}
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
    return {"ok": True, "chapter": chapter_num, "bridges": len(bridges), "words": words}


def save_outlines(book_id: str, outlines: list | None = None,
                  plots: list | None = None, threads: list | None = None,
                  themes: list | None = None, mode: str = "replace") -> dict:
    """[薄工具] 保存弧/情节段/线程/内涵（agent 生成后调用，内部不调 LLM）。

    接受 agent 生成的结构化 dict 列表，反序列化为 OutlineSlot / PlotSlot 落盘；
    mode=replace 整体替换 | append 续写追加。
    含 plots 则 phase=plots（草案待确认）否则 outlines；但**已 ready 书保持 ready**
    （续写/扩写追加弧后不降级——ready 翻转只由用户在书详情 UI 确认，见 confirm-storyline）。
    弧的 `notes`（设计意图/偏离库模板点）随 OutlineSlot 落盘，供蓝图过目复核。
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
                notes=o.get("notes", ""),
            ))
        for _o in tl.outlines:
            reconcile_outline(_o, tl.words_per_chapter or 3000)
    if plots:
        base = len(tl.plots)
        for i, p in enumerate(plots):
            tl.plots.append(PlotSlot(
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
            ))
    if threads:
        tl.threads = threads
    if themes:
        tl.themes = themes
    tl.phase = tl.phase if tl.phase == "ready" else ("plots" if plots else "outlines")
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
    "set_picks": ("templates",),   # 开篇弧/情节段选择（templates 或 plots 任一非空，drive_ui 特判）
    "set_outline": ("outlines", "plots"),   # 步3②生成的弧+情节段（generate_outline_preview 产出，submit 随书落库）
    "next": (), "prev": (),
    "load_candidates": (), "skip_candidates": (),
    "fill_world": (),   # 步骤③世界观重新补全（Agent 兜底/重试）
    "reset": (),   # 清空向导 state（除 pen_name/库表外字段）——建书前先 reset，防残留干扰保真度
    "submit": (),
    "set_review": ("title",),   # 提取页：呈现五库候选审查卡（非建书命令，不入步门控）
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
    - set_outline: {outlines: [非空列表], plots: [list], threads?, themes?}   步3②弧+情节段，submit 随书落库
      outlines 每项 {id, name, start_word, end_word, parent_arc_id?, notes, stages?}（id 唯一必填、备注用 notes 非
      description、start_word/end_word 为 0 基字数坐标（start 含/end 不含，权威；可同时传 start_chapter/end_chapter
      兼容）、parent_arc_id 指向父弧 id 支持弧树嵌套）；弧=树状目标节点（定义见 NOVEL_AGENT.md 1.1），
      字数跨度由剧情结构决定、不设固定章数、叶弧不要求=1 章（可跨多章、同父下不必相等）；仅最底层弧可拥有情节段；
      plots 每项 {id, name, outline_id, order, category?, thread_id?, roles?, words?, cover_beats?, template_structure?}
      （id 唯一必填、outline_id 必填指向所属弧 id（须为最底层弧）、order 弧内序号；words=该情节段目标字数
      0 基整数、按场景浓淡给（300~2500，同弧/全书不要全部相等），未给回退 cover_beats×200；cover_beats=节拍数 2~6 可选）——缺 id/outline_id 故事线情节段不显示
    - set_review: {title, platform?, folder?, downloaded_chapters?, profile_id?, profile_name?,
      plots?, structures?, gags?, characters?, style_rules?}   **侦察/提取合并页**：把 agent 提炼的五库候选
      呈现成可勾选审查卡（drive_ui 命令，非建书命令，不套步门控）。至少一类非空才可提交；
      style_rules 每项 {kind(prefer|ban), pattern, desc?, severity?, replacements?}。
      审查数据字段对齐 NOVEL_AGENT.md 1.2，用户确认后由页面 POST /api/scout/ingest 落库。
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
_LOCKED_TOOLS = {
    "confirm_world",
    # 薄工具（agent 生成后落盘，同样需书锁防并发）
    "save_chapter_text", "save_plot_draft", "save_outlines", "save_book_meta",
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
        list_books, get_book_state, get_writing_context, get_storyline,
        get_book_detail, get_build_status, query_arc_library, query_plots, query_gags, query_profiles, query_characters,
        get_pen_style,
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
