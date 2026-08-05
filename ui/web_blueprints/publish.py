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
        timeline = book_mgr.load_timeline(book_id)
    except Exception:
        timeline = None
    report = _publisher().build_report(book, timeline=timeline, outline=outline)
    return render_template("publish.html", book=book, report=report,
                           timeline=timeline, outline=outline)


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
        timeline = book_mgr.load_timeline(book_id)
    except Exception:
        timeline = None
    report = _publisher().build_report(book, timeline=timeline, outline=outline)
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
