"""写作台引擎（按桥段撰写/续写） — 蓝图（自 ui/web_ui.py 按域拆分）。"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, render_template, render_template_string, request, jsonify, redirect, url_for, Response, stream_with_context
from .ctx import *
bp = Blueprint("desk", __name__)

# ═══════════════════════════════════════════
# ✍️ 写作台（按书列出，进入写作/续写）
@bp.route("/desk")
# ═══════════════════════════════════════════

def desk_list():
    """写作台 — 故事线编辑器（从书库带书进入）。

    写作台按书进入：书库每本书的「✍️ 写作台」入口打开 /storyline/<id>/edit；
    直接访问 /desk（无书上下文）显示空界面，引导回书库选书。
    """
    return render_template("desk_empty.html")


@bp.route("/books/start/timeline/<timeline_id>/write")
def _compat_storyline_start_writing(timeline_id):
    """旧「从故事线启动写作」URL 兼容：规划书即正式书，「开始写作」统一走 /books/<id>/continue。"""
    return redirect(url_for("desk.continue_book_page", book_id=timeline_id), 302)


# ─── 兼容：旧 /books/timeline/write 与 /api/timeline-engine 前缀 ───

@bp.route("/books/timeline/write/<engine_id>")
def _compat_storyline_write_flow(engine_id):
    return redirect(url_for("desk.storyline_write_flow", engine_id=engine_id), 302)


@bp.route("/api/timeline-engine/<path:rest>", methods=["POST"])
def _compat_api_storyline_engine(rest):
    return redirect("/api/storyline-engine/" + rest, 307)


@bp.route("/books/storyline/write/<engine_id>")
def storyline_write_flow(engine_id):
    """蓝图式写作流程页（新核心）"""
    engine = _engines.get(engine_id)
    if not engine:
        return "引擎会话已过期", 404
    # 已写章节（供中栏「章节正文」预载，作为书目内容连续展示）
    chapters = []
    book = getattr(engine, "book", None)
    if book and (book.current_chapter or 0) >= 1:
        try:
            for n in range(1, book.current_chapter + 1):
                ch = book_mgr.load_chapter(book.book_id, n)
                if ch and ch.get("content"):
                    chapters.append({
                        "num": n,
                        "title": ch.get("title") or f"第{n}章",
                        "content": ch.get("content") or "",
                    })
        except Exception as e:
            logger.warning("加载已写章节失败: %s", e)
    sl = getattr(engine, "storyline", None)
    total_ch = 0
    if sl:
        try:
            total_ch = max((o.end_chapter for o in sl.outlines), default=0)
        except Exception:
            total_ch = 0
    return render_template("storyline_write_flow.html",
        engine_id=engine_id,
        state=engine.state,
        storyline=sl,
        book=book,
        chapters=chapters,
        total_ch=total_ch,
    )


@bp.route("/api/storyline-engine/<engine_id>/step", methods=["POST"])
def storyline_engine_step(engine_id):
    """蓝图引擎：按故事线写下一章（新书前三章 / 续写任意章节通用）"""
    from plugins import task_manager

    engine = _engines.get(engine_id)
    if not engine:
        return jsonify({"error": "not found"}), 404

    is_continue = engine.state.book_mode == BookMode.CONTINUE
    task_id = f"engine_{engine_id}"
    next_ch = engine.state.current_chapter + 1
    total_ch = engine.state.total_chapters or next_ch
    flow_url = url_for("desk.storyline_write_flow", engine_id=engine_id)

    # 注册/更新任务（新书生成 / 续写写作）
    task_name = "续写写作" if is_continue else "新书生成"
    if next_ch == 1:
        task_manager.ensure_single(task_name)
        task_manager.start(task_id, name=task_name,
                          title=engine.state.pen_name or "",
                          total=max(total_ch, 1), phase=f"第{next_ch}章...", url=flow_url)
    else:
        task_manager.progress(task_id, current=min(next_ch, total_ch), phase=f"第{next_ch}章...")
    task_manager.log(task_id, f"蓝图写作：第{next_ch}章", "info")

    # 全书完成（章节数到顶）
    if next_ch > total_ch:
        task_manager.done(task_id, message="全书完成")
        return jsonify({"status": "done", "flow_complete": True, "reason": "已写完全部章节"})

    inst = Instruction(Op.WRITE_STORYLINE_CHAPTER, chapter_num=next_ch)
    result = engine.execute(inst)
    if result.get("error"):
        task_manager.fail(task_id, str(result["error"]))
        return jsonify({"error": result["error"]}), 500
    task_manager.log(task_id, f"第{next_ch}章完成 {result.get('word_count', 0)}字", "success")

    return jsonify({
        "op": "write_storyline_chapter",
        "chapter_num": next_ch,
        "status": result.get("status"),
        "word_count": result.get("word_count", 0),
        "beats": result.get("beats", 0),
        "blueprint": result.get("blueprint", {}),
        "cost": result.get("cost", 0),
        "flow_complete": next_ch >= total_ch,
    })


@bp.route("/api/storyline-engine/<engine_id>/write-chapter", methods=["POST"])
def storyline_engine_write_chapter_sse(engine_id):
    """蓝图引擎：流式写一章（SSE）。逐桥段下发 plot_start / plot_done / chapter_done。

    前端据此在右侧逐桥段展示步骤与正文，并高亮左侧故事线对应的大纲/桥段。
    """
    import json as _json
    engine = _engines.get(engine_id)
    if not engine:
        return jsonify({"error": "not found"}), 404

    def generate():
        try:
            for evt in engine._write_storyline_chapter_stream(
                    engine.state.current_chapter + 1):
                yield "data: " + _json.dumps(evt, ensure_ascii=False) + "\n\n"
        except Exception as e:
            import traceback
            err = {"type": "error", "message": str(e),
                   "traceback": traceback.format_exc()}
            yield "data: " + _json.dumps(err, ensure_ascii=False) + "\n\n"

    return sse_stream_response(generate())


@bp.route("/api/storyline-engine/<engine_id>/write-bridge", methods=["POST"])
def storyline_engine_write_bridge_sse(engine_id):
    """蓝图引擎：流式写「一个」桥段（SSE，新核心·按桥段撰写）。

    事件：bridge_start / group_chunk / bridge_done / chapter_done / complete。
    写一个桥段即返回；连续点击则继续写下一个未写桥段，本章满字数自动切章。
    """
    import json as _json
    engine = _engines.get(engine_id)
    if not engine:
        return jsonify({"error": "not found"}), 404

    def generate():
        try:
            for evt in engine._write_next_bridge_stream():
                yield "data: " + _json.dumps(evt, ensure_ascii=False) + "\n\n"
        except Exception as e:
            import traceback
            err = {"type": "error", "message": str(e),
                   "traceback": traceback.format_exc()}
            yield "data: " + _json.dumps(err, ensure_ascii=False) + "\n\n"

    return sse_stream_response(generate())





# ═══════════════════════════════════════════
# ♻️ 续写
@bp.route("/books/<book_id>/continue")
# ═══════════════════════════════════════════

def continue_book_page(book_id):
    """书续写 — 统一走故事线蓝图写作流程（新核心）"""
    book = book_mgr.get(book_id)
    if not book:
        return "图书不存在", 404
    llm = get_llm()
    if not llm:
        return jsonify({"error": "LLM 未配置"}), 500
    engine_id = f"cont_{book_id}"
    if engine_id not in _engines:
        try:
            engine = NovelEngine(llm_client=llm)
            engine.continue_book(book_id)
            _engines[engine_id] = engine
        except (ValueError, RuntimeError) as e:
            # 无故事线（旧书/未生成 storyline）：给出指引而非 500，
            # 避免「续写/进入写作台」在残缺书上直接崩溃。
            return render_template_string(
                '<div class="tle-layout"><h2>⚠️ 无法进入写作</h2>'
                '<p style="color:#8b949e">{{ msg }}</p>'
                '<p><a class="btn" style="background:#1f6feb;color:#fff;text-decoration:none" '
                'href="/books/{{ bid }}">📖 返回书详情</a> '
                '<a class="btn" style="background:#30363d;color:#c9d1d9;text-decoration:none" '
                'href="/books">📚 去书库</a></p></div>',
                msg=str(e), bid=book_id), 200
    return redirect(url_for("desk.storyline_write_flow", engine_id=engine_id))




