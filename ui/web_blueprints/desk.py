"""写作台引擎（按桥段撰写/续写） — 蓝图（自 ui/web_ui.py 按域拆分）。"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, Response, stream_with_context
from .ctx import *
from .books import _book_rows
bp = Blueprint("desk", __name__)

# ═══════════════════════════════════════════
# ✍️ 写作台（按书列出，进入写作/续写）
@bp.route("/desk")
# ═══════════════════════════════════════════

def desk_list():
    """写作台 — 列出正式书籍，每本进入写作/续写（不再列游离时间线草稿）"""
    rows = _book_rows()
    return render_template("desk_list.html", books=rows)


@bp.route("/books/start/timeline/<timeline_id>/write")
def timeline_start_writing(timeline_id):
    """从时间线配置启动蓝图式写作引擎（新核心）。

    同一故事线草稿只建一本正式书：再次「开始写作」复用已有 book_*（避免书名/笔名重复建书）。
    """
    tl = _resolve_timeline(timeline_id)
    if not tl:
        return "故事线配置不存在或已过期", 404

    llm = get_llm()
    if not llm:
        return jsonify({"error": "LLM 未配置"}), 500

    # 复用已由该草稿创建的正式书
    existing = next((b for b in book_mgr.list_all()
                     if b.source_timeline_id == timeline_id), None)
    if existing is not None:
        engine_id = f"cont_{existing.book_id}"
        if engine_id not in _engines:
            engine = NovelEngine(llm_client=llm)
            engine.continue_book(existing.book_id)
            _engines[engine_id] = engine
        return redirect(url_for("desk.timeline_write_flow", engine_id=engine_id))

    engine = NovelEngine(llm_client=llm)
    engine.start_new_book_timeline(tl, source_timeline_id=timeline_id)

    temp_id = f"tlw_{tl.pen_name}_{int(time.time())}"
    _engines[temp_id] = engine
    return redirect(url_for("desk.timeline_write_flow", engine_id=temp_id))


@bp.route("/books/timeline/write/<engine_id>")
def timeline_write_flow(engine_id):
    """蓝图式写作流程页（新核心）"""
    engine = _engines.get(engine_id)
    if not engine:
        return "引擎会话已过期", 404
    return render_template("timeline_write_flow.html",
        engine_id=engine_id,
        state=engine.state,
        timeline=engine.timeline,
    )


@bp.route("/api/timeline-engine/<engine_id>/step", methods=["POST"])
def timeline_engine_step(engine_id):
    """蓝图引擎：按故事线写下一章（新书前三章 / 续写任意章节通用）"""
    from plugins import task_manager

    engine = _engines.get(engine_id)
    if not engine:
        return jsonify({"error": "not found"}), 404

    is_continue = engine.state.book_mode == BookMode.CONTINUE
    task_id = f"engine_{engine_id}"
    next_ch = engine.state.current_chapter + 1
    total_ch = engine.state.total_chapters or next_ch
    flow_url = url_for("desk.timeline_write_flow", engine_id=engine_id)

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

    inst = Instruction(Op.WRITE_TIMELINE_CHAPTER, chapter_num=next_ch)
    result = engine.execute(inst)
    if result.get("error"):
        task_manager.fail(task_id, str(result["error"]))
        return jsonify({"error": result["error"]}), 500
    task_manager.log(task_id, f"第{next_ch}章完成 {result.get('word_count', 0)}字", "success")

    return jsonify({
        "op": "write_timeline_chapter",
        "chapter_num": next_ch,
        "status": result.get("status"),
        "word_count": result.get("word_count", 0),
        "beats": result.get("beats", 0),
        "blueprint": result.get("blueprint", {}),
        "cost": result.get("cost", 0),
        "flow_complete": next_ch >= total_ch,
    })


@bp.route("/api/timeline-engine/<engine_id>/write-chapter", methods=["POST"])
def timeline_engine_write_chapter_sse(engine_id):
    """蓝图引擎：流式写一章（SSE）。逐桥段下发 plot_start / plot_done / chapter_done。

    前端据此在右侧逐桥段展示步骤与正文，并高亮左侧故事线对应的大纲/桥段。
    """
    import json as _json
    engine = _engines.get(engine_id)
    if not engine:
        return jsonify({"error": "not found"}), 404

    def generate():
        try:
            for evt in engine._write_timeline_chapter_stream(
                    engine.state.current_chapter + 1):
                yield "data: " + _json.dumps(evt, ensure_ascii=False) + "\n\n"
        except Exception as e:
            import traceback
            err = {"type": "error", "message": str(e),
                   "traceback": traceback.format_exc()}
            yield "data: " + _json.dumps(err, ensure_ascii=False) + "\n\n"

    return sse_stream_response(generate())


@bp.route("/api/timeline-engine/<engine_id>/write-bridge", methods=["POST"])
def timeline_engine_write_bridge_sse(engine_id):
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
    """书续写 — 统一走时间线蓝图写作流程（新核心）"""
    book = book_mgr.get(book_id)
    if not book:
        return "图书不存在", 404
    llm = get_llm()
    if not llm:
        return jsonify({"error": "LLM 未配置"}), 500
    engine_id = f"cont_{book_id}"
    if engine_id not in _engines:
        engine = NovelEngine(llm_client=llm)
        engine.continue_book(book_id)
        _engines[engine_id] = engine
    return redirect(url_for("desk.timeline_write_flow", engine_id=engine_id))




