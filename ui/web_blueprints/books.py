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
    """books/{id} 关键文件 + cost.json + 章节文件 mtime，用于判断行级缓存是否仍有效。

    chapters 目录取全部章节文件的最大 mtime（新增/修改章节都会使字数/花费变化）。
    """
    paths = (f"books/{bid}/book.json", f"books/{bid}/storyline.json",
             f"books/{bid}/outline/outline.json", f"books/{bid}/cost.json")
    base = tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in paths)
    cdir = f"books/{bid}/chapters"
    ctime = 0
    if os.path.isdir(cdir):
        times = [os.path.getmtime(os.path.join(cdir, f))
                 for f in os.listdir(cdir) if f.endswith(".json")]
        if times:
            ctime = max(times)
    return base + (ctime,)


def _book_word_count(bid: str) -> int:
    """该书已写章节总字数：遍历 chapters/*.json 累加 word_count，缺字段时按正文算。"""
    cdir = os.path.join("books", bid, "chapters")
    if not os.path.isdir(cdir):
        return 0
    from core.text_utils import count_prose_units
    total = 0
    for f in os.listdir(cdir):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(cdir, f), encoding="utf-8") as fp:
                ch = json.load(fp)
            wc = int(ch.get("word_count") or 0)
            if not wc:
                wc = count_prose_units(ch.get("content") or "")
            total += wc
        except Exception:
            continue
    return total


def _book_cost_spent(bid: str):
    """该书已花费（元，CostTracker 持久化 books/<id>/cost.json）；无记录返回 None。"""
    cost_path = os.path.join("books", bid, "cost.json")
    if not os.path.exists(cost_path):
        return None
    try:
        from libraries.cost_tracker import CostTracker
        return CostTracker.load(cost_path).spent or 0.0
    except Exception:
        return None


def _book_rows():
    """书库共用：每本书附带故事线/大纲元数据（故事线已并入书目录，无独立草稿）。
    行级缓存 key = 三份文件 mtime；后续导航只做 stat，不再 parse 大 JSON。"""
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
        sl = book_mgr.load_storyline(b.book_id)
        outline = book_mgr.get_outline(b.book_id)
        row = {
            "book": b,
            "has_storyline": sl is not None,
            "storyline_outlines": len(sl.outlines) if sl else 0,
            "storyline_plots": len(sl.plots) if sl else 0,
            "outline_count": len((outline or {}).get("stages", [])) if outline else 0,
            "word_count": _book_word_count(b.book_id),
            "cost_spent": _book_cost_spent(b.book_id),
        }
        _book_rows_cache[b.book_id] = (sig, row)
        rows.append(row)

    for key in list(_book_rows_cache):
        if key not in valid_ids:
            del _book_rows_cache[key]
    return rows


def _basic_info_from_outline(outline, book):
    """无 timeline 的书（旧引擎路径）：从结构大纲尽力还原 basic_info 供详情页展示。"""
    bi = {"characters": [], "world_building": {},
          "tone": "", "target_audience": "", "synopsis": ""}
    if not outline:
        return bi
    bi["synopsis"] = outline.get("synopsis", "") or ""
    chars = outline.get("characters", []) or []
    if isinstance(chars, list):
        first = True
        for c in chars:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            item = dict(c)
            item.setdefault("role", "主角" if first else "配角")
            item.setdefault("relations", [])
            bi["characters"].append(item)
            first = False
    return bi


@bp.route("/books/<book_id>")
def book_detail(book_id):
    book_mgr.list_all()   # mtime 感知重扫：外部进程（MCP）写入 book.json 后读到新值
    book = book_mgr.get(book_id)
    if not book: return "Not found", 404
    outline = book_mgr.get_outline(book_id)
    storyline = book_mgr.load_storyline(book_id)
    if storyline:
        basic_info = dict(storyline.basic_info or {})  # 拷贝再打标，避免就地污染缓存对象
        basic_info["has_storyline"] = True
    else:
        basic_info = _basic_info_from_outline(outline, book)
        basic_info["has_storyline"] = False
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
    # 「从已有书借鉴」已挪到启动新书向导②（dashboard GET 提供 borrow_books），详情页不再传
    return render_template("book_detail.html", book=book,
        outline=outline, chapters=chapters,
        storyline=storyline,
        basic_info=basic_info,
        draft=draft)


@bp.route("/api/book/<book_id>/generate-meta", methods=["POST"])
def api_book_generate_meta(book_id):
    """书库详情页：手动生成书名+简介（基于第 1 章内容）。

    从"第 1 章写完自动触发"改为详情页独立动作。
    生成结果写 book.json（title）/ storyline.json（book_title）/ outline.json（synopsis）。
    """
    if not book_mgr.get(book_id):
        return jsonify({"ok": False, "error": "not found"}), 404
    ch1 = book_mgr.load_chapter(book_id, 1)
    if not ch1 or not ch1.get("content"):
        return jsonify({"ok": False, "error": "尚无第 1 章正文，请先写作再生成书名/简介"}), 400
    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置"}), 500

    from plugins import task_manager
    task_manager.ensure_single("生成书名/简介")
    tid = f"meta_{book_id}_{int(time.time())}"
    _book_meta = book_mgr.get(book_id)
    task_manager.start(tid, name="生成书名/简介",
                       title=getattr(_book_meta, "title", "") or "",
                       url=f"/books/{book_id}")
    try:
        from libraries.engine import NovelEngine
        engine = NovelEngine(llm_client=llm)
        engine.continue_book(book_id)   # 恢复 book/storyline（无 storyline 会报错）
        result = engine._generate_book_meta(ch1["content"])
        task_manager.log(tid, f"生成书名「{result.get('title', '')}」", "success")
        task_manager.done(tid, message="书名/简介生成完成")
        # 使 web_ui 的 book 缓存失效，下次详情页加载读到磁盘新值
        book_mgr._cache.pop(book_id, None)
        _engines.pop(f"cont_{book_id}", None)
        return jsonify({"ok": True, **result})
    except Exception as e:
        task_manager.fail(tid, str(e))
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/books/<book_id>/delete", methods=["POST"])
def delete_book(book_id):
    book_mgr.delete(book_id)
    return redirect(url_for("books.books"))


