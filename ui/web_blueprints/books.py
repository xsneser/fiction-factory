"""书库（图书列表/详情/删除） — 蓝图（自 ui/web_ui.py 按域拆分）。"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, Response, stream_with_context
from .ctx import *

bp = Blueprint("books", __name__)
logger = logging.getLogger("novel-engine.web")

# ═══════════════════════════════════════════
# 原有路由（保留兼容）
@bp.route("/books")
# ═══════════════════════════════════════════

def books():
    rows = _book_rows()
    return render_template("books.html", books=rows)


_book_rows_cache: dict = {}  # book_id -> (mtimes, row)

def _book_sig(bid: str):
    """books/{id} 三份关键文件的 mtime，用于判断行级缓存是否仍有效。"""
    paths = (f"books/{bid}/book.json", f"books/{bid}/timeline.json",
             f"books/{bid}/outline/outline.json")
    return tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in paths)


def _book_rows():
    """书库/写作台共用：每本书附带时间线/大纲元数据。
    行级缓存 key = 三份文件 mtime；后续导航只做 stat，不再 parse 大 JSON。
    除正式书 book_* 外，也列出「已生成完整故事线」的时间线草稿（tl_*/gen_* 且 phase=ready），
    它们在书库/写作台同样代表一本可继续规划 / 开始写作的书。"""
    from libraries.book_manager import BookConfig
    books = book_mgr.list_all()
    valid_ids = set()
    rows = []
    for b in books:
        valid_ids.add(b.book_id)
        sig = _book_sig(b.book_id)
        cached = _book_rows_cache.get(b.book_id)
        if cached and cached[0] == sig:
            rows.append(cached[1])
            continue
        tl = book_mgr.load_timeline(b.book_id)
        outline = book_mgr.get_outline(b.book_id)
        row = {
            "book": b,
            "is_timeline": False,
            "has_timeline": tl is not None,
            "timeline_outlines": len(tl.outlines) if tl else 0,
            "timeline_plots": len(tl.plots) if tl else 0,
            "outline_count": len((outline or {}).get("stages", [])) if outline else 0,
        }
        _book_rows_cache[b.book_id] = (sig, row)
        rows.append(row)

    # 已完成故事线的草稿时间线也进书库（可继续规划 / 开始写作）
    # 已被正式书 source_timeline_id 引用的草稿不再重复列出（避免"两本同名书"）
    imported_timeline_ids = {b.source_timeline_id for b in books if getattr(b, "source_timeline_id", "")}
    import glob as _glob
    for p in _glob.glob(os.path.join("books", "timelines", "*.json")):
        tl_id = os.path.splitext(os.path.basename(p))[0]
        if tl_id in valid_ids:
            continue
        if tl_id in imported_timeline_ids:
            _book_rows_cache.pop(tl_id, None)
            continue
        valid_ids.add(tl_id)
        try:
            tsig = os.path.getmtime(p)
        except OSError:
            continue
        cached = _book_rows_cache.get(tl_id)
        if cached and cached[0] == tsig:
            rows.append(cached[1])
            continue
        tl = load_timeline(p)
        if not tl or getattr(tl, "phase", "") != "ready":
            _book_rows_cache.pop(tl_id, None)
            continue
        total_ch = max((o.end_chapter for o in tl.outlines), default=0)
        bc = BookConfig(
            book_id=tl_id,
            title=tl.book_title or "(待定)",
            pen_name=tl.pen_name or "",
            genre=tl.genre or "",
            sub_genre=tl.sub_genre or "",
            current_chapter=0,
            chapter_count=total_ch or 0,
            status="ready",
        )
        row = {
            "book": bc,
            "is_timeline": True,
            "has_timeline": True,
            "timeline_outlines": len(tl.outlines),
            "timeline_plots": len(tl.plots),
            "outline_count": 0,
        }
        _book_rows_cache[tl_id] = (tsig, row)
        rows.append(row)

    for key in list(_book_rows_cache):
        if key not in valid_ids:
            del _book_rows_cache[key]
    return rows


def _basic_info_from_outline(outline, book):
    """无 timeline 的书（旧引擎路径）：从结构大纲尽力还原 basic_info 供详情页展示。"""
    bi = {"protagonist": {}, "world_building": {}, "supporting_cast": [],
          "tone": "", "target_audience": "", "synopsis": ""}
    if not outline:
        return bi
    bi["synopsis"] = outline.get("synopsis", "") or ""
    chars = outline.get("characters", []) or []
    if isinstance(chars, list):
        for c in chars:
            if not isinstance(c, dict):
                continue
            if not bi["protagonist"]:
                bi["protagonist"] = c
            else:
                bi["supporting_cast"].append(c)
    return bi


@bp.route("/books/<book_id>")
def book_detail(book_id):
    book = book_mgr.get(book_id)
    if not book: return "Not found", 404
    outline = book_mgr.get_outline(book_id)
    timeline = book_mgr.load_timeline(book_id)
    if timeline:
        basic_info = timeline.basic_info or {}
        basic_info["has_timeline"] = True
    else:
        basic_info = _basic_info_from_outline(outline, book)
        basic_info["has_timeline"] = False
    # 简介存在 outline.json，合并进 basic_info 供详情页显示
    if outline and (outline.get("synopsis") or ""):
        basic_info.setdefault("synopsis", outline["synopsis"])
    from core.text_utils import count_prose_units
    chapters = []
    for n in range(1, book.current_chapter + 2):
        ch = book_mgr.load_chapter(book_id, n)
        if ch:
            ch["word_count"] = count_prose_units(ch.get("content") or "")
            chapters.append(ch)
    cost_path = f"books/{book_id}/cost.json"
    cost = CostTracker.load(cost_path) if os.path.exists(cost_path) else CostTracker()
    csm = CharacterStateMachine()
    char_path = f"books/{book_id}/character_states.json"
    if os.path.exists(char_path): csm.load(char_path)
    # 进行中章节草稿（按桥段撰写中断时落盘；详情页展示未固化内容，写作台才有写入）
    draft = None
    draft_path = f"books/{book_id}/draft_chapter.json"
    if os.path.exists(draft_path):
        try:
            with open(draft_path, encoding="utf-8") as f:
                _d = json.load(f)
            _buf = _d.get("buffer") or []
            if _buf:
                draft = {
                    "chapter_num": _d.get("chapter_num", 0),
                    "text": "\n\n".join(_buf),
                    "words": count_prose_units("\n\n".join(_buf)),
                }
        except Exception as e:
            logger.warning("读取章节草稿失败: %s", e)
    return render_template("book_detail.html", book=book,
        outline=outline, chapters=chapters,
        timeline=timeline,
        basic_info=basic_info,
        cost=cost.summary(), characters=csm.characters, draft=draft)


@bp.route("/api/book/<book_id>/generate-meta", methods=["POST"])
def api_book_generate_meta(book_id):
    """书库详情页：手动生成书名+简介（基于第 1 章内容）。

    从"第 1 章写完自动触发"改为详情页独立动作。
    生成结果写 book.json（title）/ timeline.json（book_title）/ outline.json（synopsis）。
    """
    if not book_mgr.get(book_id):
        return jsonify({"ok": False, "error": "not found"}), 404
    ch1 = book_mgr.load_chapter(book_id, 1)
    if not ch1 or not ch1.get("content"):
        return jsonify({"ok": False, "error": "尚无第 1 章正文，请先写作再生成书名/简介"}), 400
    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置"}), 500
    try:
        from libraries.engine import NovelEngine
        engine = NovelEngine(llm_client=llm)
        engine.continue_book(book_id)   # 恢复 book/timeline（无 timeline 会报错）
        result = engine._generate_book_meta(ch1["content"])
        # 使 web_ui 的 book 缓存失效，下次详情页加载读到磁盘新值
        book_mgr._cache.pop(book_id, None)
        _engines.pop(f"cont_{book_id}", None)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/books/<book_id>/delete", methods=["POST"])
def delete_book(book_id):
    book_mgr.delete(book_id)
    return redirect(url_for("books.books"))


@bp.route("/timeline/<timeline_id>/delete", methods=["POST"])
def timeline_delete(timeline_id):
    """删除故事线草稿（tl_*/gen_*）。正式书 book_* 请走 /books/<id>/delete。"""
    if timeline_id.startswith("book_"):
        return jsonify({"ok": False, "error": "正式书请从书库删除"}), 400
    if not is_safe_timeline_id(timeline_id):
        return jsonify({"ok": False, "error": "非法故事线 ID"}), 400
    _timelines.pop(timeline_id, None)
    path = ensure_child_path("books/timelines", _timeline_filepath(timeline_id))
    try:
        if path.exists():
            os.remove(path)
        return jsonify({"ok": True})
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


