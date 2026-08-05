"""故事线编辑（新书启动 v2） — 蓝图（自 ui/web_ui.py 按域拆分）。"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, Response, stream_with_context
from .ctx import *

bp = Blueprint("storyline", __name__)

# ═══════════════════════════════════════════
# ⏱️ 故事线编辑（新书启动 v2）
@bp.route("/storyline/<storyline_id>/edit")
# ═══════════════════════════════════════════

def storyline_edit(storyline_id):
    """故事线规划已并入统一写作台（storyline_write_flow 三栏页），此入口重定向。"""
    if storyline_id.startswith("book_"):
        return redirect(url_for("desk.continue_book_page", book_id=storyline_id), 302)
    return redirect(url_for("desk._compat_storyline_start_writing", timeline_id=storyline_id), 302)


@bp.route("/storyline/<storyline_id>/detail")
def storyline_detail(storyline_id):
    """故事线详情一律收敛到书详情页（书详情已含完整大纲/设定/章节）。"""
    return redirect(url_for("books.book_detail", book_id=storyline_id))


def _build_next_arc(builder, tl, mode="rule"):
    """在故事线末尾追加下一段大纲弧。rule=确定性模板循环；ai=单弧 LLM 再锚定。"""
    if mode == "ai":
        seq = builder.build_outline_sequence(
            genre=tl.genre, sub_genre=tl.sub_genre,
            custom_context=tl.basic_info.get("world_building", {}).get("description", ""),
            max_outlines=1, mode="ai")
        if not seq:
            return None
        arc = seq[0]
        max_end = max((o.end_chapter for o in tl.outlines), default=0)
        span = max(arc.end_chapter - arc.start_chapter + 1, 20)
        arc.start_chapter = max_end + 1
        arc.end_chapter = arc.start_chapter + span - 1
        if tl.outlines:
            arc.predecessor = tl.outlines[-1].id
            tl.outlines[-1].successor = arc.id
        return arc

    # rule：按流派模板循环取下一个
    structs = struct_lib.search(genre=tl.genre) or struct_lib.templates
    if not structs:
        return None
    idx = len(tl.outlines) % len(structs)
    tmpl = structs[idx]
    max_end = max((o.end_chapter for o in tl.outlines), default=0)
    start = max_end + 1
    span = min(tmpl.total_chapters, 60)
    from libraries.storyline import OutlineSlot
    arc = OutlineSlot(
        id=builder._next_id("outline"),
        template_id=tmpl.id,
        name=f"{tmpl.name}(第{len(tl.outlines) + 1}部分)",
        start_chapter=start,
        end_chapter=start + span - 1,
        stages=[
            {"name": s.name, "min_ch": s.min_chapters, "max_ch": s.max_chapters,
             "events": s.key_events[:5]}
            for s in tmpl.stages
        ],
        predecessor=tl.outlines[-1].id if tl.outlines else "",
        transition_type="sequential",
    )
    if tl.outlines:
        tl.outlines[-1].successor = arc.id
    return arc


@bp.route("/api/storyline/<storyline_id>/extend-outline", methods=["POST"])
def extend_outline(storyline_id):
    """续写时扩展故事线：末尾追加新大纲弧 + 填充桥段 + 加料（book_* 与 tl_* 通用）。"""
    tl = _resolve_storyline(storyline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    if not tl.outlines:
        return jsonify({"ok": False,
                        "error": "尚无故事线大纲，请先生成完整大纲后再扩展"}), 400

    mode = request.args.get("mode", "rule")
    llm = get_llm() if mode == "ai" else None
    builder = StorylineBuilder(
        structure_lib=struct_lib, plot_lib=plot_lib,
        gag_lib=gag_lib, theme_lib=theme_lib, llm_client=llm,
    )
    _seed_builder_counter(builder,
                          [o.id for o in tl.outlines] + [p.id for p in tl.plots])

    new_arc = _build_next_arc(builder, tl, mode)
    if new_arc is None:
        return jsonify({"ok": False, "error": "无可用大纲模板"}), 400

    tl.outlines.append(new_arc)
    new_plots = builder.fill_plots_for_outline(new_arc, tl)
    existing_ids = {p.id for p in tl.plots}
    added = [p for p in new_plots if p.id not in existing_ids]
    tl.plots.extend(added)
    builder.fill_themes_and_hooks(added, tl)
    from libraries.storyline import annotate_plot_roles
    annotate_plot_roles(tl)
    tl.phase = "ready"
    _save_storyline(tl, storyline_id)

    # 正式书：同步 bump 章节总数，并使续写引擎缓存失效（下一章从磁盘重建）
    new_total = 0
    if storyline_id.startswith("book_"):
        book = book_mgr.get(storyline_id)
        if book:
            new_total = max(book.chapter_count, new_arc.end_chapter)
            if new_total > book.chapter_count:
                book.chapter_count = new_total
                book_mgr.update(book)
            _engines.pop(f"cont_{storyline_id}", None)

    # 日志入右侧栏
    from plugins import task_manager
    task_manager.ensure_single("扩展故事线")
    tid = f"extend_{storyline_id}_{int(time.time())}"
    task_manager.start(tid, name="扩展故事线", title=tl.book_title or tl.pen_name or "",
                       total=1, phase="完成", url=f"/storyline/{storyline_id}/edit")
    task_manager.log(tid, f"扩展故事线：新弧「{new_arc.name}」第{new_arc.start_chapter}-{new_arc.end_chapter}章 +{len(added)}桥段", "success")
    task_manager.done(tid, message="扩展完成")

    return jsonify({
        "ok": True,
        "outline": {"id": new_arc.id, "name": new_arc.name,
                    "start_chapter": new_arc.start_chapter,
                    "end_chapter": new_arc.end_chapter},
        "total_chapters": new_total,
        "plots_added": len(added),
    })


@bp.route("/api/storyline/<storyline_id>/generate-title", methods=["POST"])
def generate_title(storyline_id):
    """AI 生成书名：从主角/世界观/基调产出候选，选一个写入 tl.book_title。"""
    tl = _resolve_storyline(storyline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置"}), 500

    bi = tl.basic_info or {}
    protag = bi.get("protagonist") or {}
    world = bi.get("world_building") or {}
    ctx = f"流派：{tl.genre}{'/' + tl.sub_genre if tl.sub_genre else ''}"
    if protag.get("name"):
        ctx += f"；主角：{protag.get('name')}（{protag.get('identity','')}）"
    if world.get("description"):
        ctx += f"；世界观：{world['description']}"
    if bi.get("tone"):
        ctx += f"；基调：{bi['tone']}"

    prompt = f"""为下面这本网络小说起书名（3-5 个，2-10 字，朗朗上口、有网文味）。

{ctx}

返回 JSON：{{"titles": ["书名1", "书名2", "书名3"]}}"""
    try:
        from core.llm_client import extract_json
        raw = llm.call("你是网文书名策划。只返回JSON。", prompt,
                       temperature=0.8, max_tokens=1024)
        data = json.loads(extract_json(raw))
        titles = [t for t in (data.get("titles") or [])
                  if isinstance(t, str) and t.strip()]
    except Exception:
        titles = []
    if not titles:
        return jsonify({"ok": False, "error": "书名生成失败"}), 500

    tl.book_title = titles[0]
    _save_storyline(tl, storyline_id)
    # 正式书：同步更新 book.json 的书名
    if storyline_id.startswith("book_"):
        book = book_mgr.get(storyline_id)
        if book:
            book.title = titles[0]
            book_mgr.update(book)
    return jsonify({"ok": True, "titles": titles, "chosen": titles[0]})


@bp.route("/api/storyline/<storyline_id>/generate-outlines", methods=["POST"])
def api_generate_outlines(storyline_id):
    """AI 或规则生成大纲序列"""
    tl = _resolve_storyline(storyline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404

    llm = get_llm()
    builder = StorylineBuilder(
        structure_lib=struct_lib, plot_lib=plot_lib,
        gag_lib=gag_lib, theme_lib=theme_lib, llm_client=llm,
    )

    mode = request.args.get("mode", "ai")
    if mode == "rule":
        tl.outlines = builder.build_outline_sequence(genre=tl.genre, mode="rule")
    else:
        tl.outlines = builder.build_outline_sequence(
            genre=tl.genre, sub_genre=tl.sub_genre,
            custom_context=tl.basic_info.get("world_building", {}).get("description", ""),
            mode="ai",
        )
    tl.phase = "outlines"
    _save_storyline(tl, storyline_id)
    return jsonify({"ok": True, "count": len(tl.outlines)})


@bp.route("/api/storyline/<storyline_id>/confirm-outlines", methods=["POST"])
def api_confirm_outlines(storyline_id):
    """确认大纲配置，进入桥段编排阶段"""
    tl = _resolve_storyline(storyline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    tl.phase = "plots"
    _save_storyline(tl, storyline_id)
    return jsonify({"ok": True, "phase": "plots"})


@bp.route("/api/storyline/<storyline_id>/fill-plots", methods=["POST"])
def api_fill_plots(storyline_id):
    """给每个大纲填充桥段"""
    tl = _resolve_storyline(storyline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    if not tl.outlines:
        return jsonify({"ok": False, "error": "请先生成大纲序列"}), 400

    llm = get_llm()
    builder = StorylineBuilder(
        structure_lib=struct_lib, plot_lib=plot_lib,
        gag_lib=gag_lib, theme_lib=theme_lib, llm_client=llm,
    )
    # seed 计数器，避免新桥段 id 与已有桥段撞号（否则去重会静默丢弃）
    _seed_builder_counter(builder, [p.id for p in tl.plots])

    new_plots = []
    for o in tl.outlines:
        new_plots.extend(builder.fill_plots_for_outline(o, tl))

    # 去重：按 id 合并
    existing_ids = {p.id for p in tl.plots}
    for p in new_plots:
        if p.id not in existing_ids:
            tl.plots.append(p)

    from libraries.storyline import annotate_plot_roles
    annotate_plot_roles(tl)
    _save_storyline(tl, storyline_id)
    return jsonify({"ok": True, "plots_added": len(new_plots),
                    "total_plots": len(tl.plots)})


@bp.route("/api/storyline/<storyline_id>/fill-gags", methods=["POST"])
def api_fill_gags(storyline_id):
    """注入笑点和吸睛点"""
    tl = _resolve_storyline(storyline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404

    builder = StorylineBuilder(
        structure_lib=struct_lib, plot_lib=plot_lib,
        gag_lib=gag_lib, theme_lib=theme_lib,
    )
    builder.fill_themes_and_hooks(tl.plots, tl)
    from libraries.storyline import annotate_plot_roles
    annotate_plot_roles(tl)
    tl.phase = "ready" if tl.plots else "gags"
    _save_storyline(tl, storyline_id)
    return jsonify({"ok": True, "phase": tl.phase})


@bp.route("/api/storyline/<storyline_id>/plot-confirm", methods=["POST"])
def api_plot_confirm(storyline_id):
    """切换单个桥段的确认状态"""
    tl = _resolve_storyline(storyline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    data = request.json or {}
    plot_id = data.get("plot_id", "")
    confirmed = data.get("confirmed", False)
    for p in tl.plots:
        if p.id == plot_id:
            p.confirmed = confirmed
            break
    _save_storyline(tl, storyline_id)
    return jsonify({"ok": True})


@bp.route("/api/storyline/<storyline_id>/update-outline", methods=["POST"])
def api_update_outline(storyline_id):
    """更新大纲的章节范围"""
    tl = _resolve_storyline(storyline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    data = request.json or {}
    oid = data.get("id", "")
    field = data.get("field", "")
    val = data.get("value", 0)
    # 字段白名单：只允许改章节范围，避免任意字段被客户端 setattr
    if field not in ("start_chapter", "end_chapter"):
        return jsonify({"ok": False, "error": "非法字段"}), 400
    try:
        val = int(val)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "章节号必须是整数"}), 400
    if val < 1:
        return jsonify({"ok": False, "error": "章节号必须 ≥ 1"}), 400
    for o in tl.outlines:
        if o.id == oid:
            if field == "end_chapter" and val < o.start_chapter:
                return jsonify({"ok": False, "error": "结束章节不能小于起始章节"}), 400
            if field == "start_chapter" and o.end_chapter and val > o.end_chapter:
                return jsonify({"ok": False, "error": "起始章节不能大于结束章节"}), 400
            setattr(o, field, val)
            break
    _save_storyline(tl, storyline_id)
    return jsonify({"ok": True})


@bp.route("/api/storyline/<storyline_id>/set-narrative", methods=["POST"])
def api_set_narrative(storyline_id):
    """设置大纲的叙事手法（顺叙/倒叙/插叙）+ 叙事目标"""
    tl = _resolve_storyline(storyline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    data = request.json or {}
    oid = data.get("id", "")
    narrative = data.get("narrative", "chronological")
    target = data.get("narrative_target", "")
    if narrative not in ("chronological", "flashback", "interleaved"):
        return jsonify({"ok": False, "error": "非法叙事手法"}), 400
    for o in tl.outlines:
        if o.id == oid:
            o.narrative = narrative
            o.narrative_target = target
            break
    _save_storyline(tl, storyline_id)
    return jsonify({"ok": True})


@bp.route("/api/storyline/<storyline_id>/move-outline", methods=["POST"])
def api_move_outline(storyline_id):
    """上移/下移大纲"""
    tl = _resolve_storyline(storyline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    data = request.json or {}
    oid = data.get("id", "")
    direction = data.get("direction", "up")
    idx = next((i for i, o in enumerate(tl.outlines) if o.id == oid), -1)
    if idx < 0:
        return jsonify({"ok": False, "error": "not found"}), 404
    new_idx = idx - 1 if direction == "up" else idx + 1
    if 0 <= new_idx < len(tl.outlines):
        tl.outlines[idx], tl.outlines[new_idx] = tl.outlines[new_idx], tl.outlines[idx]
        # 换序后重建前后驱链，保持 predecessor/successor 一致
        for i, o in enumerate(tl.outlines):
            o.predecessor = tl.outlines[i - 1].id if i > 0 else ""
            o.successor = tl.outlines[i + 1].id if i + 1 < len(tl.outlines) else ""
    _save_storyline(tl, storyline_id)
    return jsonify({"ok": True})


@bp.route("/api/storyline/<storyline_id>/delete-outline", methods=["POST"])
def api_delete_outline(storyline_id):
    """删除一个大纲（同时删除其下的桥段）"""
    tl = _resolve_storyline(storyline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    data = request.json or {}
    oid = data.get("id", "")
    tl.outlines = [o for o in tl.outlines if o.id != oid]
    tl.plots = [p for p in tl.plots if p.outline_id != oid]
    _save_storyline(tl, storyline_id)
    return jsonify({"ok": True})


# ═══════════════════════════════════════════
# 🎯 基础设定 + 一键完整大纲（启动新书/续写共用）
@bp.route("/api/storyline/<storyline_id>/save-basic-info", methods=["POST"])
# ═══════════════════════════════════════════

def api_save_basic_info(storyline_id):
    """保存基础设定（主角/世界观/配角/基调/目标读者）。

    兼容草稿(tl_*)与正式书(book_*)；主角/世界观逐 key 深合并，保留用户已填值。
    """
    tl = _resolve_storyline(storyline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    data = request.json or {}
    bi = tl.basic_info or {}

    for section in ("protagonist", "world_building"):
        incoming = data.get(section)
        if isinstance(incoming, dict):
            base = bi.get(section, {}) or {}
            for k, v in incoming.items():
                if v not in (None, ""):
                    base[k] = v
            bi[section] = base
    for field in ("supporting_cast", "tone", "target_audience",
                  "pov", "era_language"):
        if data.get(field) not in (None, ""):
            bi[field] = data[field]

    tl.basic_info = bi
    # 书名（与基础设定一起保存，正式书同步更新 book.json）
    if data.get("book_title") not in (None, ""):
        tl.book_title = data["book_title"]
        if storyline_id.startswith("book_"):
            book = book_mgr.get(storyline_id)
            if book:
                book.title = data["book_title"]
                book_mgr.update(book)
    tl.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_storyline(tl, storyline_id)
    return jsonify({"ok": True})


def _decision_log_message(kind: str, data: dict) -> str:
    """把一条 decision 事件渲染成右侧栏日志的一句话（候选→选中→理由）。"""
    step = data.get("step", "")
    cands = "、".join(c.get("name", "") for c in (data.get("candidates") or [])[:6]) or "（无候选）"
    chosen = data.get("chosen") or {}
    if kind == "outline_choice":
        if isinstance(chosen, dict):
            name = chosen.get("name", "")
        elif isinstance(chosen, list) and chosen:
            name = chosen[0].get("name", "")
        else:
            name = ""
        if name:
            return f"📋 大纲选择[{step}]：候选 {cands} → 选中「{name}」"
        return f"📋 大纲选择[{step}]：候选 {cands}"
    if kind == "plot_choice":
        if isinstance(chosen, list):
            names = [c.get("name", "") for c in chosen if isinstance(c, dict)]
        elif isinstance(chosen, dict):
            names = [chosen.get("name", "")]
        else:
            names = []
        if names:
            return f"🧩 桥段选择[{step}]：候选 {cands} → 选中「{'、'.join(names)}」"
        return f"🧩 桥段选择[{step}]：候选 {cands}"
    if kind == "theme_review":
        themes = "、".join((chosen.get("themes") or [])[:3]) or "无"
        return f"🎭 内涵挂载[{step}]：母题 {themes}"
    if kind == "thread_split":
        threads = "、".join((chosen.get("threads") or [])[:3]) or "—"
        splits = chosen.get("splits", 0)
        return f"🧵 线程与呼应[{step}]：线程 {threads}｜拆分 {splits} 处"
    if kind == "validate":
        issues = (chosen.get("issues") or [])
        return f"✅ 一致性验证[{step}]：{len(issues)} 个建议｜{data.get('reason','')}"
    return f"🤖 {step}：{data.get('reason','')}"


@bp.route("/api/storyline/<storyline_id>/generate-full", methods=["POST"])
def api_generate_full(storyline_id):
    """一键生成完整大纲（5 阶段 OutlineGenerator，SSE 流式），原地累加并逐步落盘。

    - 生成器直接操作当前 storyline 对象（storyline=tl），每阶段结束 on_save 落盘，
      实现"大纲→桥段→笑点/内涵挨个步骤写进配置文件"。
    - 新增 SSE 事件：thinking（AI 流式思考 token）、decision（候选→选中→理由），
      前端右侧"AI 思考过程"面板展示；decision 同时写入右侧栏任务日志。
    - phase_done 附带 storyline 快照，前端据此实时刷新左侧故事线视图。
    """
    tl = _resolve_storyline(storyline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404

    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置，请先在设置页配置 API"}), 500

    profile = None
    if tl.pen_name:
        try:
            profile = profiles.get_by_name(tl.pen_name)
        except Exception:
            pass

    from libraries.outline_generator import OutlineGenerator
    from libraries.prompt_harness import PromptHarness
    harness = PromptHarness(storyline=tl, profile=profile,
                            gag_lib=gag_lib, theme_lib=theme_lib, plot_lib=plot_lib)
    gen = OutlineGenerator(
        llm_client=llm,
        structure_lib=struct_lib,
        plot_lib=plot_lib,
        gag_lib=gag_lib,
        theme_lib=theme_lib,
        profile=profile,
        harness=harness,
    )

    # 用草稿已填的基础信息做上下文（保留用户输入）
    bi = tl.basic_info or {}
    world = bi.get("world_building", {}) or {}
    protag = bi.get("protagonist", {}) or {}
    ctx_parts = []
    if world.get("description"):
        ctx_parts.append(f"世界观：{world['description']}")
    if protag.get("name") or protag.get("identity"):
        ctx_parts.append(f"主角：{protag.get('name','')}（{protag.get('identity','')}）")
    custom_context = "；".join(ctx_parts) or (tl.book_title or "")

    from plugins import task_manager
    task_manager.ensure_single("完整大纲生成")
    task_id = f"genfull_{storyline_id}_{int(time.time())}"
    task_manager.start(task_id, name="完整大纲生成",
                       title=tl.pen_name or "", total=6,
                       phase="故事分析...", url=f"/storyline/{storyline_id}/edit")

    def generate():
        import json as _json

        try:
            for event_type, message, data_dict in gen.generate(
                genre=tl.genre, sub_genre=tl.sub_genre,
                custom_context=custom_context, pen_name=tl.pen_name,
                words_per_chapter=tl.words_per_chapter,
                storyline=tl,                       # 原地累加，可逐步落盘
                on_save=lambda _tl: _save_storyline(_tl, storyline_id),
            ):
                # 原始思考流（thinking token）不再下发，前端只展示决策/动作
                if event_type == "thinking":
                    continue

                payload = {"event": event_type, "message": message}
                if data_dict:
                    payload.update(data_dict)

                # 运行状态（右侧栏任务卡片）随阶段推进
                if event_type == "phase" and data_dict:
                    task_manager.progress(task_id, current=data_dict.get("phase", 0),
                                          phase=message or "")
                if event_type == "phase_done" and data_dict:
                    task_manager.progress(task_id, current=data_dict.get("phase", 0),
                                          phase="完成", message=message)
                    task_manager.log(task_id, message, "success")
                # 快照：内容已变化的 SSE 事件附带 storyline，前端据此逐条实时刷新左侧故事线
                if event_type in ("outline_added", "outline_plots", "plot_added",
                                  "theme_injected", "phase_done", "done"):
                    payload["storyline"] = tl.to_dict()

                # 每个决策写进右侧栏日志（用户能看到"确定了哪个大纲/桥段/笑点"）
                if event_type == "decision" and data_dict:
                    task_manager.log(task_id,
                                     _decision_log_message(data_dict.get("kind", "decision"), data_dict),
                                     "success")
                    # 决策后也落一次盘（桥段/加料已变化）
                    _save_storyline(tl, storyline_id)

                if event_type == "done":
                    _save_storyline(tl, storyline_id)
                    payload["storyline"] = tl.to_dict()
                    task_manager.done(task_id, message="完整大纲生成完成")

                yield "data: " + _json.dumps(payload, ensure_ascii=False) + "\n\n"
        except Exception as e:
            import traceback
            task_manager.fail(task_id, str(e))
            err_payload = {"event": "error", "message": str(e),
                           "traceback": traceback.format_exc()}
            yield "data: " + _json.dumps(err_payload, ensure_ascii=False) + "\n\n"

    return sse_stream_response(generate())


@bp.route("/api/storyline/<storyline_id>/agent", methods=["POST"])
def api_storyline_agent(storyline_id):
    """大纲助手：用自然语言调整故事线配置（改桥段/加笑点/改大纲/增删桥段等）。

    由前端右侧「大纲助手」聊天面板调用；改动直接落盘，返回最新 storyline 供前端重绘。
    """
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "error": "消息为空"}), 400

    tl = _resolve_storyline(storyline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404

    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置，请先在设置页配置 API"}), 500

    from libraries.outline_agent import OutlineAgent
    agent = OutlineAgent(llm=llm, structure_lib=struct_lib, plot_lib=plot_lib,
                         gag_lib=gag_lib, theme_lib=theme_lib)
    try:
        result = agent.handle(tl, message)
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e),
                        "traceback": traceback.format_exc()}), 500

    _save_storyline(tl, storyline_id)

    # 写入右侧栏任务日志，方便追溯
    task_id = f"agent_{storyline_id}"
    try:
        task_manager.start(task_id, name="大纲助手", title=tl.pen_name or "",
                           total=1, phase="调整故事线",
                           url=f"/storyline/{storyline_id}/edit")
        task_manager.log(task_id, f"🎙 {message}", "info")
        for line in result.get("summary", []):
            task_manager.log(task_id, line, "success")
        task_manager.done(task_id, message=result.get("reply", ""))
    except Exception:
        pass

    return jsonify(result)


# ═══════════════════════════════════════════
# 🔗 兼容重定向（timeline → storyline 旧 URL）
# 页面 GET 用 302；POST API 用 307 保留方法/body（避免 fetch 下 POST→GET 降级）。
# ═══════════════════════════════════════════

@bp.route("/timeline/<timeline_id>/edit")
def _compat_storyline_edit(timeline_id):
    return redirect(url_for("storyline.storyline_edit", storyline_id=timeline_id), 302)


@bp.route("/timeline/<timeline_id>/detail")
def _compat_storyline_detail(timeline_id):
    return redirect(url_for("storyline.storyline_detail", storyline_id=timeline_id), 302)


@bp.route("/api/timeline/<path:rest>", methods=["GET", "POST"])
def _compat_api_storyline(rest):
    return redirect("/api/storyline/" + rest, 307)
