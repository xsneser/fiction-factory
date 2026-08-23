"""上架 / 发布 — 蓝图（状态机 + 检查 + 导出，纯规则无 LLM）。"""
import sys, os, glob, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, render_template, request, jsonify, send_file
from .ctx import *
from core.safe_paths import is_safe_book_id
from libraries.publisher import Publisher

bp = Blueprint("publish", __name__)
logger = logging.getLogger("novel-engine.web")


def _publisher() -> Publisher:
    return Publisher(book_mgr)


def _latest_export(book_id: str) -> str:
    """返回最近一次导出的 zip 路径（无则空串）。"""
    export_root = os.path.join(str(book_mgr.dir), book_id, "export")
    if not os.path.isdir(export_root):
        return ""
    dirs = sorted(glob.glob(os.path.join(export_root, "*")),
                  key=os.path.getmtime, reverse=True)
    if not dirs:
        return ""
    zips = glob.glob(os.path.join(dirs[0], "*_投稿包_*.zip"))
    return zips[0] if zips else ""


@bp.route("/publish")
def publish_index():
    """上架管理中心：聚合展示所有书的上架信息（状态/检查/字数/上架时间/导出）。"""
    from .books import _book_rows
    rows = _book_rows()
    pub = _publisher()
    books_info = []
    for r in rows:
        b = r["book"]
        if b.status == "planning" or (b.current_chapter or 0) == 0:
            continue  # 规划书（无章节）不可上架
        try:
            outline = book_mgr.get_outline(b.book_id)
            try:
                storyline = book_mgr.load_storyline(b.book_id)
            except Exception:
                storyline = None
            report = pub.build_report(b, storyline=storyline, outline=outline)
        except Exception as e:
            logger.warning("上架中心：构建 %s 报告失败: %s", b.book_id, e)
            continue
        books_info.append({
            "book": b,
            "report": report,
            "can_export": (b.current_chapter or 0) >= 1,
            "export_zip": _latest_export(b.book_id),
        })
    # 排序：未上架在前（待处理优先），已上架靠后
    books_info.sort(key=lambda x: (x["book"].status == "published", x["book"].status == "finished"))
    return render_template("publish_index.html", books_info=books_info)


@bp.route("/books/<book_id>/publish")
def publish_page(book_id):
    """上架页：检查报告 + 状态机按钮 + 导出。"""
    if not is_safe_book_id(book_id):
        return "非法图书 ID", 404
    book = book_mgr.get(book_id)
    if not book:
        return "图书不存在", 404
    outline = book_mgr.get_outline(book_id)
    try:
        storyline = book_mgr.load_storyline(book_id)
    except Exception:
        storyline = None
    report = _publisher().build_report(book, storyline=storyline, outline=outline)
    # 题材标签（建书向导选定，存于 world_building.tags）；无标签的老书回退书级 genre
    tags = []
    if storyline and (storyline.basic_info or {}):
        tags = ((storyline.basic_info.get("world_building") or {}).get("tags") or [])
    tags_str = "、".join(tags) or book.genre or ""
    return render_template("publish.html", book=book, report=report,
                           storyline=storyline, outline=outline, tags=tags_str)


@bp.route("/api/books/<book_id>/publish-check", methods=["POST"])
def api_publish_check(book_id):
    """上架前检查（免费规则）→ {ok, report}。"""
    if not is_safe_book_id(book_id):
        return jsonify({"ok": False, "error": "非法图书 ID"}), 400
    book = book_mgr.get(book_id)
    if not book:
        return jsonify({"ok": False, "error": "图书不存在"}), 404
    outline = book_mgr.get_outline(book_id)
    try:
        storyline = book_mgr.load_storyline(book_id)
    except Exception:
        storyline = None
    report = _publisher().build_report(book, storyline=storyline, outline=outline)
    return jsonify({"ok": True, "report": report.to_dict()})


@bp.route("/api/books/<book_id>/mark-finished", methods=["POST"])
def api_mark_finished(book_id):
    """标记完本（status=finished + finished_at）。"""
    if not is_safe_book_id(book_id):
        return jsonify({"ok": False, "error": "非法图书 ID"}), 400
    result = _publisher().mark_finished(book_id)
    code = 200 if result.get("ok") else (404 if result.get("error") == "图书不存在" else 400)
    return jsonify(result), code


@bp.route("/api/books/<book_id>/publish", methods=["POST"])
def api_publish(book_id):
    """标记已上架（status=published + published_at）。未过检查时需 force。"""
    if not is_safe_book_id(book_id):
        return jsonify({"ok": False, "error": "非法图书 ID"}), 400
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force")) or request.form.get("force") == "1"
    result = _publisher().publish(book_id, force=force)
    code = 200 if result.get("ok") else 400
    return jsonify(result), code


@bp.route("/api/books/<book_id>/export", methods=["POST"])
def api_export(book_id):
    """导出投稿包 → {ok, manifest}。"""
    if not is_safe_book_id(book_id):
        return jsonify({"ok": False, "error": "非法图书 ID"}), 400
    result = _publisher().export_book(book_id)
    code = 200 if result.get("ok") else 400
    return jsonify(result), code


@bp.route("/api/books/<book_id>/export/download")
def api_export_download(book_id):
    """下载最近一次导出的 zip。"""
    if not is_safe_book_id(book_id):
        return jsonify({"ok": False, "error": "非法图书 ID"}), 400
    # 以 book_mgr.dir（绝对路径，锚定项目根 books/）为基准，避免依赖进程 cwd
    export_root = os.path.join(str(book_mgr.dir), book_id, "export")
    if not os.path.isdir(export_root):
        return jsonify({"ok": False, "error": "尚无导出"}), 404
    dirs = sorted(glob.glob(os.path.join(export_root, "*")),
                  key=os.path.getmtime, reverse=True)
    if not dirs:
        return jsonify({"ok": False, "error": "尚无导出"}), 404
    zips = glob.glob(os.path.join(dirs[0], "*_投稿包_*.zip"))
    if not zips:
        return jsonify({"ok": False, "error": "导出目录中没有 zip"}), 404
    logger.debug("download export zip: %s", zips[0])
    return send_file(zips[0], as_attachment=True, download_name=os.path.basename(zips[0]))
