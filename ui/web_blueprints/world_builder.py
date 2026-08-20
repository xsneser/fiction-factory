"""世界观设定卡（启动新书前置 v3）— 蓝图（自 ui/web_ui.py 按域拆分）。

启动新书「设定先行」三条无书路径（建书向导内触发）：
- 候选：`POST /api/world-builder/candidates`（步 2 挑世界观方向，tags 硬约束）
- 世界观补全：`POST /api/world-builder/world-complete`（步 3 自动补全 12 维 + 基调）
- 从书借鉴：`POST /api/world-builder/borrow-preview`（无书别名预览 seed）
另有带书路径：生成（SSE）/ 确认 / 逐项编辑（书详情页设定卡）。
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
                       url=f"/books/{book_id}")
    try:
        candidates = gen.generate_candidates(genre=tl.genre, sub_genre=tl.sub_genre, idea=idea)
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
    """示例候选（无目标书版本，供启动向导②）：body {idea, genre?, sub_genre?, tags?}。

    tags 为题材标签（硬约束，向导步 1 已选）；genre 为空时由 derive_genre(tags) 推导。
    """
    body = request.get_json(silent=True) or {}
    idea = (body.get("idea") or "").strip()
    genre = (body.get("genre") or "").strip()
    sub_genre = (body.get("sub_genre") or "").strip()
    raw_tags = body.get("tags") or []
    tags = [str(t).strip() for t in raw_tags if isinstance(t, str) and t.strip()]
    if not genre and tags:
        from libraries.world_tags import derive_genre
        genre = derive_genre(tags)
    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置，请先在设置页配置 API"}), 500
    from libraries.world_builder import WorldBuildingGenerator
    from libraries.prompt_harness import PromptHarness
    harness = PromptHarness()   # 无书：storyline=None，_tags_block 空；tags 由 generate_candidates 透传
    gen = WorldBuildingGenerator(llm_client=llm, harness=harness)
    candidates = gen.generate_candidates(genre=genre, sub_genre=sub_genre, idea=idea, tags=tags)
    if not candidates:
        return jsonify({"ok": False, "error": "示例候选生成失败，请重试"}), 500
    return jsonify({"ok": True, "candidates": candidates})


# 无 book_id 别名：新书启动向导③世界观补全（进入步 3 自动触发；generate() 本就不读目标书）
@bp.route("/api/world-builder/world-complete", methods=["POST"])
def api_world_complete_nobook():
    """世界观补全（无目标书版本，供启动向导③）：AI 从 idea+题材标签+候选方向
    补全 world_building 12 维 + 基调（tone/target_audience/pov/era_language）。

    body {idea, world_brief?, tags?, title?, genre?, sub_genre?, pen_name?}；
    种子优先 world_brief（候选 120-200 字简述），空则用 idea；genre 空时由 derive_genre(tags) 推导。
    返回 basic_info（world_building 12 键 + 基调；characters 由 LLM 推导，向导忽略——角色仍由 Agent set_characters 推送）。
    """
    body = request.get_json(silent=True) or {}
    idea = (body.get("idea") or "").strip()
    world_brief = (body.get("world_brief") or "").strip()
    title = (body.get("title") or "").strip()
    pen_name = (body.get("pen_name") or "").strip()
    genre = (body.get("genre") or "").strip()
    sub_genre = (body.get("sub_genre") or "").strip()
    raw_tags = body.get("tags") or []
    tags = [str(t).strip() for t in raw_tags if isinstance(t, str) and t.strip()]
    seed = world_brief or idea
    if not seed:
        return jsonify({"ok": False, "error": "缺少一句话设定或世界观简述"}), 400
    if not genre and tags:
        from libraries.world_tags import derive_genre
        genre = derive_genre(tags)
    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置，请先在设置页配置 API"}), 500

    from libraries.world_builder import WorldBuildingGenerator
    from libraries.prompt_harness import PromptHarness
    from libraries.storyline import BookStoryline
    tl = BookStoryline(genre=genre, sub_genre=sub_genre, pen_name=pen_name,
                       platform="fanqie",
                       basic_info={"characters": [],
                                   "world_building": {"description": seed, "tags": tags},
                                   "tone": "", "target_audience": "",
                                   "pov": "第三人称", "era_language": ""})
    profile = _profile_for(tl)   # 笔名风格档案（无则 None，生成降级为无风格约束）
    harness = PromptHarness(storyline=tl, profile=profile)   # 无书：storyline=新鲜种子 tl，_tags_block 注入标签硬约束
    gen = WorldBuildingGenerator(llm_client=llm, profile=profile, harness=harness)
    done_basic_info = None
    try:
        for event_type, message, data_dict in gen.generate(
                genre=genre, sub_genre=sub_genre, idea=seed,
                pen_name=pen_name, platform="fanqie", storyline=tl):
            if event_type == "done":
                done_basic_info = data_dict.get("basic_info") or tl.basic_info
            elif event_type == "error":
                return jsonify({"ok": False, "error": message or "世界观生成失败"}), 500
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": f"世界观生成失败：{e}",
                        "traceback": traceback.format_exc()}), 500
    if not done_basic_info:
        return jsonify({"ok": False, "error": "世界观生成失败"}), 500
    return jsonify({"ok": True, "basic_info": done_basic_info})


# ═══════════════════════════════════════════
# 分阶段内容构建端点（无书，供内部 agent / skill 逐步填充向导步 3）
# ═══════════════════════════════════════════

def _stage_gen(llm, pen_name=""):
    """构造无书 WorldBuildingGenerator（带笔名风格档案，无则 None）。"""
    from libraries.world_builder import WorldBuildingGenerator
    from libraries.prompt_harness import PromptHarness
    from libraries.storyline import BookStoryline
    profile = None
    if pen_name:
        profile = _profile_for(BookStoryline(pen_name=pen_name))
    harness = PromptHarness(profile=profile)
    return WorldBuildingGenerator(llm_client=llm, profile=profile, harness=harness)


@bp.route("/api/world-builder/stage/core-conflict", methods=["POST"])
def api_stage_core_conflict():
    """分阶段构建①：从一句话设定+题材标签推导主线核心矛盾。

    body {idea, world_brief?, tags?, genre?, sub_genre?, pen_name?} → {core_conflict, genre}。
    """
    body = request.get_json(silent=True) or {}
    idea = (body.get("idea") or "").strip()
    world_brief = (body.get("world_brief") or "").strip()
    pen_name = (body.get("pen_name") or "").strip()
    genre = (body.get("genre") or "").strip()
    sub_genre = (body.get("sub_genre") or "").strip()
    tags = [str(t).strip() for t in (body.get("tags") or []) if isinstance(t, str) and t.strip()]
    if not genre and tags:
        from libraries.world_tags import derive_genre
        genre = derive_genre(tags)
    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置"}), 500
    gen = _stage_gen(llm, pen_name)
    conflict = gen.generate_core_conflict(
        genre=genre, sub_genre=sub_genre, idea=world_brief or idea,
        tags=tags, pen_name=pen_name)
    if not conflict:
        return jsonify({"ok": False, "error": "核心矛盾生成失败，请重试"}), 500
    return jsonify({"ok": True, "core_conflict": conflict, "genre": genre})


@bp.route("/api/world-builder/stage/factions", methods=["POST"])
def api_stage_factions():
    """分阶段构建③：从一句话+核心矛盾发散世界里的势力派系。

    body {idea, world_brief?, core_conflict?, tags?, genre?, sub_genre?} → {factions}。
    """
    body = request.get_json(silent=True) or {}
    idea = (body.get("idea") or "").strip()
    world_brief = (body.get("world_brief") or "").strip()
    core_conflict = (body.get("core_conflict") or "").strip()
    genre = (body.get("genre") or "").strip()
    sub_genre = (body.get("sub_genre") or "").strip()
    tags = [str(t).strip() for t in (body.get("tags") or []) if isinstance(t, str) and t.strip()]
    if not genre and tags:
        from libraries.world_tags import derive_genre
        genre = derive_genre(tags)
    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置"}), 500
    gen = _stage_gen(llm)
    factions = gen.generate_factions(
        genre=genre, sub_genre=sub_genre, idea=world_brief or idea,
        core_conflict=core_conflict, tags=tags)
    if not factions:
        return jsonify({"ok": False, "error": "势力生成失败，请重试"}), 500
    return jsonify({"ok": True, "factions": factions})


@bp.route("/api/world-builder/stage/rest-world", methods=["POST"])
def api_stage_rest_world():
    """分阶段构建⑤：大纲确定后补全其余世界观维度 + 基调（保留 core_conflict/factions）。

    body {idea, world_brief?, core_conflict?, factions?, outline_preview?, tags?,
          genre?, sub_genre?, pen_name?} → {world_building:{...8维}, tone, target_audience, pov, era_language}。
    """
    body = request.get_json(silent=True) or {}
    idea = (body.get("idea") or "").strip()
    world_brief = (body.get("world_brief") or "").strip()
    core_conflict = (body.get("core_conflict") or "").strip()
    outline_preview = (body.get("outline_preview") or "").strip()
    pen_name = (body.get("pen_name") or "").strip()
    genre = (body.get("genre") or "").strip()
    sub_genre = (body.get("sub_genre") or "").strip()
    raw_factions = body.get("factions") or []
    factions = [f for f in raw_factions if isinstance(f, dict)] or []
    tags = [str(t).strip() for t in (body.get("tags") or []) if isinstance(t, str) and t.strip()]
    if not genre and tags:
        from libraries.world_tags import derive_genre
        genre = derive_genre(tags)
    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置"}), 500
    gen = _stage_gen(llm, pen_name)
    result = gen.generate_rest_world(
        genre=genre, sub_genre=sub_genre, idea=idea, world_brief=world_brief,
        core_conflict=core_conflict, factions=factions,
        outline_preview=outline_preview, tags=tags, pen_name=pen_name)
    if not result.get("world_building"):
        return jsonify({"ok": False, "error": "世界观维度补全失败，请重试"}), 500
    return jsonify({"ok": True, **result})


@bp.route("/api/world-builder/characters", methods=["POST"])
def api_stage_characters():
    """分阶段构建④：从一句话+已定核心矛盾/势力/开篇大纲桥段生成角色候选。

    body {idea, world_brief?, core_conflict?, factions?, outline_preview?, tags?, title?,
          genre?, sub_genre?} → {protagonists, supporting_cast}。
    """
    body = request.get_json(silent=True) or {}
    idea = (body.get("idea") or "").strip()
    world_brief = (body.get("world_brief") or "").strip()
    core_conflict = (body.get("core_conflict") or "").strip()
    outline_preview = (body.get("outline_preview") or "").strip()
    title = (body.get("title") or "").strip()
    genre = (body.get("genre") or "").strip()
    sub_genre = (body.get("sub_genre") or "").strip()
    raw_factions = body.get("factions") or []
    factions = [f for f in raw_factions if isinstance(f, dict)] or []
    tags = [str(t).strip() for t in (body.get("tags") or []) if isinstance(t, str) and t.strip()]
    if not genre and tags:
        from libraries.world_tags import derive_genre
        genre = derive_genre(tags)
    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置"}), 500
    gen = _stage_gen(llm)
    result = gen.generate_characters(
        idea=world_brief or idea, genre=genre, sub_genre=sub_genre, tags=tags,
        title=title, core_conflict=core_conflict, factions=factions,
        outline_preview=outline_preview)
    if not result:
        return jsonify({"ok": False, "error": "角色候选生成失败，请重试"}), 500
    return jsonify({"ok": True, "protagonists": result.get("protagonists", []),
                    "supporting_cast": result.get("supporting_cast", [])})


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
