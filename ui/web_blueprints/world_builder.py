"""世界观设定卡（启动新书前置 v3）— 蓝图（自 ui/web_ui.py 按域拆分）。

启动新书改为「设定先行」：一句话设定 → 世界观设定卡（生成/示例候选/从书借鉴/逐项编辑）
→ 确认 → 进现有大纲生成（OutlineGenerator 此时 skip Phase 1 LLM 分析）。
"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, request, jsonify, redirect, url_for
from .ctx import *

bp = Blueprint("world_builder", __name__)


def _profile_for(tl):
    """按笔名取风格档案（没有则 None，生成时降级为无风格约束）。"""
    if tl and tl.pen_name:
        try:
            return profiles.get_by_name(tl.pen_name)
        except Exception:
            return None
    return None


# ═══════════════════════════════════════════
# 世界观设定卡审查/编辑页
# ═══════════════════════════════════════════

@bp.route("/books/<book_id>/world")
def world_card(book_id):
    """世界观设定已并入书详情页（book_detail 内嵌可编辑表单），此入口 302 重定向。

    保留路由与全部 /api/world-builder/* API，兼容旧入口/书签/仪表盘下一步。
    """
    return redirect(url_for("books.book_detail", book_id=book_id))


# ═══════════════════════════════════════════
# API：一句话/借鉴 → 生成世界观（SSE 流式）
# ═══════════════════════════════════════════

@bp.route("/api/world-builder/<book_id>/generate", methods=["POST"])
def api_world_generate(book_id):
    """生成世界观。body: {mode: "one"|"borrow", idea?, source_book_id?, tweak?}"""
    tl = _resolve_storyline(book_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置，请先在设置页配置 API"}), 500

    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "one")
    idea = (body.get("idea") or "").strip()
    source_book_id = (body.get("source_book_id") or "").strip()
    tweak = (body.get("tweak") or "").strip()

    seed = None
    if mode == "borrow" and source_book_id:
        src = _resolve_storyline(source_book_id)
        if src:
            from libraries.world_builder import WorldBuildingGenerator
            seed = WorldBuildingGenerator.extract_seed(src.basic_info)
    if mode == "borrow":
        idea = tweak  # 借鉴模式：微调句即主输入
    if not idea:
        idea = str((tl.basic_info or {}).get("world_building", {}).get("description", "") or "").strip()

    profile = _profile_for(tl)
    from libraries.world_builder import WorldBuildingGenerator
    from libraries.prompt_harness import PromptHarness
    harness = PromptHarness(storyline=tl, profile=profile)
    gen = WorldBuildingGenerator(llm_client=llm, profile=profile, harness=harness)

    def generate():
        import json as _json
        from plugins import task_manager
        task_manager.ensure_single("世界观生成")
        task_id = f"world_{book_id}_{int(time.time())}"
        task_manager.start(task_id, name="世界观生成",
                           title=tl.book_title or tl.pen_name or "",
                           agent="world", book_id=book_id,
                           book_title=tl.book_title or "",
                           step=("借鉴生成世界观" if mode == "borrow" else "生成世界观"),
                           total=2, phase="构思设定...",
                           url=f"/books/{book_id}")
        try:
            for event_type, message, data_dict in gen.generate(
                genre=tl.genre, sub_genre=tl.sub_genre, idea=idea,
                pen_name=tl.pen_name, platform=tl.platform,
                seed_basic_info=seed, storyline=tl,
                on_save=lambda _tl: _save_storyline(_tl, book_id),
            ):
                if event_type == "thinking":
                    continue  # 思考流不在设定卡页逐字展示
                payload = {"event": event_type, "message": message}
                if data_dict:
                    payload.update(data_dict)
                if event_type == "phase_done":
                    task_manager.progress(task_id,
                                          current=data_dict.get("phase", 0),
                                          phase=message or "")
                    task_manager.llm_call(task_id)
                elif event_type == "done":
                    task_manager.done(task_id, message="世界观生成完成")
                elif event_type == "error":
                    task_manager.fail(task_id, message or "世界观生成失败")
                if event_type in ("world_summary", "phase_done", "done", "error"):
                    payload["basic_info"] = tl.basic_info
                yield "data: " + _json.dumps(payload, ensure_ascii=False) + "\n\n"
        except Exception as e:
            import traceback
            task_manager.fail(task_id, str(e))
            err_payload = {"event": "error", "message": str(e),
                           "traceback": traceback.format_exc()}
            yield "data: " + _json.dumps(err_payload, ensure_ascii=False) + "\n\n"

    return sse_stream_response(generate())


# ═══════════════════════════════════════════
# API：示例候选（JSON，非流式）
# ═══════════════════════════════════════════

@bp.route("/api/world-builder/<book_id>/candidates", methods=["POST"])
def api_world_candidates(book_id):
    """示例候选：一次产出 2-3 个差异化世界观方向供点选。"""
    tl = _resolve_storyline(book_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置，请先在设置页配置 API"}), 500
    body = request.get_json(silent=True) or {}
    idea = (body.get("idea") or "").strip()

    profile = _profile_for(tl)
    from libraries.world_builder import WorldBuildingGenerator
    from libraries.prompt_harness import PromptHarness
    harness = PromptHarness(storyline=tl, profile=profile)
    gen = WorldBuildingGenerator(llm_client=llm, profile=profile, harness=harness)

    from plugins import task_manager
    task_manager.ensure_single("世界观候选")
    tid = f"worldcand_{book_id}_{int(time.time())}"
    task_manager.start(tid, name="世界观候选",
                       title=tl.book_title or tl.pen_name or "",
                       agent="world", book_id=book_id,
                       book_title=tl.book_title or "",
                       step="产出差异化候选", total=1, phase="生成中...",
                       url=f"/books/{book_id}")
    try:
        candidates = gen.generate_candidates(genre=tl.genre, sub_genre=tl.sub_genre, idea=idea)
        task_manager.llm_call(tid)
    except Exception as e:
        task_manager.fail(tid, str(e))
        raise
    if not candidates:
        task_manager.fail(tid, "示例候选生成失败")
        return jsonify({"ok": False, "error": "示例候选生成失败，请重试"}), 500
    task_manager.log(tid, f"产出 {len(candidates)} 个世界观方向", "success")
    task_manager.done(tid, message="候选生成完成")
    return jsonify({"ok": True, "candidates": candidates})


# 无 book_id 别名：新书启动向导②在建书前生成 AI 候选（generate_candidates 本就不读目标书）
@bp.route("/api/world-builder/candidates", methods=["POST"])
def api_world_candidates_nobook():
    """示例候选（无目标书版本，供启动向导②）：body {idea, genre?, sub_genre?}。"""
    body = request.get_json(silent=True) or {}
    idea = (body.get("idea") or "").strip()
    genre = (body.get("genre") or "").strip()
    sub_genre = (body.get("sub_genre") or "").strip()
    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置，请先在设置页配置 API"}), 500
    from libraries.world_builder import WorldBuildingGenerator
    from libraries.prompt_harness import PromptHarness
    harness = PromptHarness()   # 无书：storyline=None，_tags_block 空
    gen = WorldBuildingGenerator(llm_client=llm, harness=harness)
    candidates = gen.generate_candidates(genre=genre, sub_genre=sub_genre, idea=idea)
    if not candidates:
        return jsonify({"ok": False, "error": "示例候选生成失败，请重试"}), 500
    return jsonify({"ok": True, "candidates": candidates})


# 无 book_id 别名：向导③根据世界观生成书名候选 + 主角设定候选（建书前）
@bp.route("/api/world-builder/title-protag", methods=["POST"])
def api_world_title_protag_nobook():
    """根据世界观生成书名候选 + 主角设定候选。body {idea, genre?, tags?}。"""
    body = request.get_json(silent=True) or {}
    idea = (body.get("idea") or "").strip()
    genre = (body.get("genre") or "").strip()
    tags = body.get("tags") or []
    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置，请先在设置页配置 API"}), 500
    from libraries.world_builder import WorldBuildingGenerator
    from libraries.prompt_harness import PromptHarness
    harness = PromptHarness()   # 无书：storyline=None
    gen = WorldBuildingGenerator(llm_client=llm, harness=harness)
    result = gen.generate_title_protag(idea=idea, genre=genre, tags=tags)
    if not result:
        return jsonify({"ok": False, "error": "书名/主角候选生成失败，请重试"}), 500
    return jsonify({"ok": True, **result})


# ═══════════════════════════════════════════
# API：从已有书借鉴预览
# ═══════════════════════════════════════════

def _borrow_preview():
    """从源书抽取借鉴种子（extract_seed）。"""
    body = request.get_json(silent=True) or {}
    source_book_id = (body.get("source_book_id") or "").strip()
    src = _resolve_storyline(source_book_id) if source_book_id else None
    if not src:
        return jsonify({"ok": False, "error": "源书不存在"}), 404
    from libraries.world_builder import WorldBuildingGenerator
    seed = WorldBuildingGenerator.extract_seed(src.basic_info)
    if not seed:
        return jsonify({"ok": False, "error": "源书没有可借鉴的设定"}), 404
    return jsonify({"ok": True, "seed": seed,
                    "source_title": src.book_title or src.pen_name or source_book_id,
                    "source_genre": src.genre})


@bp.route("/api/world-builder/<book_id>/borrow-preview", methods=["POST"])
def api_world_borrow_preview(book_id):
    """预览将借鉴源书的哪些设定（extract_seed 结果）。兼容旧路由。"""
    return _borrow_preview()


# 无 book_id 别名：启动向导②在建书前预览借鉴设定（该端点本就不读目标书）
@bp.route("/api/world-builder/borrow-preview", methods=["POST"])
def api_world_borrow_preview_nobook():
    """预览借鉴设定（无目标书版本，供新书启动向导②）。"""
    return _borrow_preview()


# ═══════════════════════════════════════════
# API：确认世界观设定，进入大纲生成
# ═══════════════════════════════════════════

@bp.route("/api/world-builder/<book_id>/confirm", methods=["POST"])
def api_world_confirm(book_id):
    """确认设定并落盘，返回下一步跳转。

    防误标：仅当 basic_info 够充实（≥4 维度+主角名，或已打标）才置 _world_generated，
    否则不置——让「一键生成完整大纲」的 Phase 1 正常跑 LLM 故事分析，避免薄设定跳过。
    """
    tl = _resolve_storyline(book_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    tl.basic_info = tl.basic_info or {}
    from libraries.outline_generator import basic_info_is_rich
    if basic_info_is_rich(tl.basic_info):
        tl.basic_info["_world_generated"] = True
    else:
        tl.basic_info.pop("_world_generated", None)
    tl.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_storyline(tl, book_id)
    return jsonify({"ok": True, "redirect": f"/storyline/{book_id}/edit"})
