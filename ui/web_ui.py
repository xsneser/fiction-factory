"""
NovelEngine — 完整 Web UI v2.0 (Flask + Jinja2)
引擎集成版：新书启动 / 续写 / 管理面板
"""
from flask import Flask, render_template, request, jsonify, redirect, url_for, Response, stream_with_context
import sys, os, json, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libraries.plot import PlotLibrary
from libraries.structure import StructureLibrary
from libraries.gag import GagLibrary
from libraries.theme import ThemeLibrary
from libraries.profiles import ProfileManager
from libraries.book_manager import BookManager
from libraries.cost_tracker import CostTracker
from libraries.de_ai import DeAIEngine
from libraries.character_state import CharacterStateMachine
from libraries.reviewer import ContentReviewer
from libraries.engine import NovelEngine, BookMode, Op, Instruction
from core.llm_client import LLMClient
from core.models import APIConfig

# 设置日志级别以便调试搜索
for name in ["fanqie-scout", "__main__"]:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        logger.addHandler(handler)

app = Flask(__name__, template_folder="templates", static_folder="static")

# ─── 全局服务 ───
plot_lib = PlotLibrary()
struct_lib = StructureLibrary()
gag_lib = GagLibrary()
theme_lib = ThemeLibrary()
profiles = ProfileManager("profiles")
book_mgr = BookManager("books")

# ─── LLM 客户端 ───
_llm_client = None

def get_llm():
    global _llm_client
    if _llm_client is not None:
        return _llm_client
    api_path = "api.json"
    if os.path.exists(api_path):
        cfg = json.loads(open(api_path, encoding="utf-8").read())
        api_cfg = APIConfig(
            api_key=cfg.get("api_key",""),
            base_url=cfg.get("base_url","https://api.deepseek.com"),
            model=cfg.get("model","deepseek-chat"),
            http_timeout_seconds=cfg.get("http_timeout_seconds",300),
            # verify_ssl 跟随 api.json：默认开启；旧证书环境可显式设为 false
            verify_ssl=cfg.get("verify_ssl", True),
        )
        _llm_client = LLMClient(api_cfg)
        return _llm_client
    return None


def sse_stream_response(gen):
    """包装 SSE 流式响应（统一 headers，避免各端点重复）"""
    return Response(
        stream_with_context(gen),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


# ─── 引擎实例缓存 ───
_engines: dict[str, NovelEngine] = {}
_timelines: dict[str, dict] = {}  # 时间线配置缓存
from libraries.timeline import BookTimeline, save_timeline, load_timeline, TimelineBuilder


# ─── 时间线统一存取：草稿(tl_*) 与正式书(book_*) 两套 id 分派 ───

def _timeline_filepath(timeline_id: str) -> str:
    """按 id 前缀把时间线分派到磁盘路径：正式书在书目录内，草稿在 books/timelines/。"""
    if timeline_id.startswith("book_"):
        return f"books/{timeline_id}/timeline.json"
    return f"books/timelines/{timeline_id}.json"


def _resolve_timeline(timeline_id):
    """从内存缓存或磁盘加载 BookTimeline，兼容 tl_* 与 book_*。"""
    tl = _timelines.get(timeline_id)
    if tl is None:
        tl = load_timeline(_timeline_filepath(timeline_id))
        if tl:
            _timelines[timeline_id] = tl
    return tl


def _save_timeline(tl, timeline_id):
    """写入内存缓存并落盘。"""
    _timelines[timeline_id] = tl
    save_timeline(tl, _timeline_filepath(timeline_id))


def _max_id_suffix(ids) -> int:
    """取 id 列表中最大数字后缀，用于 seed TimelineBuilder 计数器防碰撞。"""
    import re as _re
    max_n = 0
    for i in ids:
        m = _re.search(r"_(\d+)$", i or "")
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n


def _seed_builder_counter(builder, ids) -> None:
    """把 TimelineBuilder._counter 抬到现有 id 最大后缀之上，避免 outline_0001/plot_0001 碰撞。"""
    builder._counter = _max_id_suffix(ids)


# ═══════════════════════════════════════════
# 首页 Dashboard
# ═══════════════════════════════════════════
@app.route("/")
def dashboard():
    rows = _book_rows()
    pen_names = profiles.list_all()
    return render_template("dashboard.html",
        books=rows, pen_names=pen_names,
        plot_count=len(plot_lib.templates),
        struct_count=len(struct_lib.templates),
        gag_count=len(gag_lib.patterns),
        theme_count=len(theme_lib.entries),
        engine_count=len(_engines),
    )


# ═══════════════════════════════════════════
# 🔰 新书启动（两步走）
# ═══════════════════════════════════════════

@app.route("/books/start", methods=["GET", "POST"])
def start_new_book():
    """新书启动 — v2: 先创建时间线配置，再跳转编辑器"""
    if request.method == "POST":
        llm = get_llm()
        if not llm:
            return jsonify({"error": "LLM 未配置"}), 500

        pen_name = request.form.get("pen_name", "")
        genre = request.form.get("genre", "")
        sub_genre = request.form.get("sub_genre", "")

        # 创建时间线配置
        timeline = BookTimeline(
            book_title=request.form.get("title", ""),
            genre=genre,
            sub_genre=sub_genre,
            words_per_chapter=int(request.form.get("words_per_chapter", 3000)),
            pen_name=pen_name,
            basic_info={
                "protagonist": {
                    "name": request.form.get("protag_name", ""),
                    "identity": request.form.get("protag_identity", ""),
                    "personality": request.form.get("protag_personality", ""),
                    "golden_finger": request.form.get("protag_golden_finger", ""),
                },
                "world_building": {
                    "description": request.form.get("world_desc", ""),
                },
            },
            phase="config",
        )

        # 如果用户给了时间线描述，立即用 AI 生成大纲序列
        timeline_hint = request.form.get("timeline_hint", "")
        if timeline_hint:
            builder = TimelineBuilder(
                structure_lib=struct_lib,
                plot_lib=plot_lib,
                gag_lib=gag_lib,
                theme_lib=theme_lib,
                llm_client=llm,
            )
            timeline.outlines = builder.build_outline_sequence(
                genre=genre, sub_genre=sub_genre, custom_context=timeline_hint)
            timeline.phase = "outlines"

        # 保存并跳转
        timeline_id = f"tl_{pen_name}_{int(time.time())}"
        _timelines[timeline_id] = timeline
        save_timeline(timeline, f"books/timelines/{timeline_id}.json")

        return redirect(url_for("timeline_edit", timeline_id=timeline_id))

    return render_template("start_book.html",
        pen_names=profiles.list_all(),
        structures=struct_lib.templates,
        openings=plot_lib.search(category="开篇"),
        golden_fingers=plot_lib.search(category="成长") + plot_lib.search(category="爽文"),
    )


# ═══════════════════════════════════════════
# ⏱️ 时间线编辑（新书启动 v2）
# ═══════════════════════════════════════════

@app.route("/timeline/<timeline_id>/edit")
def timeline_edit(timeline_id):
    """时间线编辑器页面"""
    tl_data = _resolve_timeline(timeline_id)
    if not tl_data:
        return "故事线配置不存在或已过期", 404
    return render_template("timeline_editor.html",
        timeline_id=timeline_id,
        timeline=tl_data,
        timeline_json=tl_data.to_dict(),
    )


@app.route("/timeline/<timeline_id>/detail")
def timeline_detail(timeline_id):
    """故事线草稿详情页（世界观/主角/配角/故事线/桥段）"""
    tl_data = _resolve_timeline(timeline_id)
    if not tl_data:
        return "故事线配置不存在或已过期", 404
    # 若该草稿已建正式书，直接跳书详情
    linked = next((b for b in book_mgr.list_all()
                   if b.source_timeline_id == timeline_id), None)
    if linked is not None:
        return redirect(url_for("book_detail", book_id=linked.book_id))
    return render_template("timeline_detail.html",
        timeline_id=timeline_id,
        timeline=tl_data,
    )


def _build_next_arc(builder, tl, mode="rule"):
    """在时间线末尾追加下一段大纲弧。rule=确定性模板循环；ai=单弧 LLM 再锚定。"""
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
    from libraries.timeline import OutlineSlot
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


@app.route("/api/timeline/<timeline_id>/extend-outline", methods=["POST"])
def extend_outline(timeline_id):
    """续写时扩展故事线：末尾追加新大纲弧 + 填充桥段 + 加料（book_* 与 tl_* 通用）。"""
    tl = _resolve_timeline(timeline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    if not tl.outlines:
        return jsonify({"ok": False,
                        "error": "尚无故事线大纲，请先生成完整大纲后再扩展"}), 400

    mode = request.args.get("mode", "rule")
    llm = get_llm() if mode == "ai" else None
    builder = TimelineBuilder(
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
    builder.fill_gags_and_hooks(added, tl)
    tl.phase = "ready"
    _save_timeline(tl, timeline_id)

    # 正式书：同步 bump 章节总数，并使续写引擎缓存失效（下一章从磁盘重建）
    new_total = 0
    if timeline_id.startswith("book_"):
        book = book_mgr.get(timeline_id)
        if book:
            new_total = max(book.chapter_count, new_arc.end_chapter)
            if new_total > book.chapter_count:
                book.chapter_count = new_total
                book_mgr.update(book)
            _engines.pop(f"cont_{timeline_id}", None)

    # 日志入右侧栏
    from plugins import task_manager
    task_manager.ensure_single("扩展故事线")
    tid = f"extend_{timeline_id}_{int(time.time())}"
    task_manager.start(tid, name="扩展故事线", title=tl.book_title or tl.pen_name or "",
                       total=1, phase="完成", url=f"/timeline/{timeline_id}/edit")
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


@app.route("/api/timeline/<timeline_id>/generate-title", methods=["POST"])
def generate_title(timeline_id):
    """AI 生成书名：从主角/世界观/基调产出候选，选一个写入 tl.book_title。"""
    tl = _resolve_timeline(timeline_id)
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
    _save_timeline(tl, timeline_id)
    # 正式书：同步更新 book.json 的书名
    if timeline_id.startswith("book_"):
        book = book_mgr.get(timeline_id)
        if book:
            book.title = titles[0]
            book_mgr.update(book)
    return jsonify({"ok": True, "titles": titles, "chosen": titles[0]})


@app.route("/api/timeline/<timeline_id>/generate-outlines", methods=["POST"])
def api_generate_outlines(timeline_id):
    """AI 或规则生成大纲序列"""
    tl = _resolve_timeline(timeline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404

    llm = get_llm()
    builder = TimelineBuilder(
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
    _save_timeline(tl, timeline_id)
    return jsonify({"ok": True, "count": len(tl.outlines)})


@app.route("/api/timeline/<timeline_id>/confirm-outlines", methods=["POST"])
def api_confirm_outlines(timeline_id):
    """确认大纲配置，进入桥段编排阶段"""
    tl = _resolve_timeline(timeline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    tl.phase = "plots"
    _save_timeline(tl, timeline_id)
    return jsonify({"ok": True, "phase": "plots"})


@app.route("/api/timeline/<timeline_id>/fill-plots", methods=["POST"])
def api_fill_plots(timeline_id):
    """给每个大纲填充桥段"""
    tl = _resolve_timeline(timeline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    if not tl.outlines:
        return jsonify({"ok": False, "error": "请先生成大纲序列"}), 400

    llm = get_llm()
    builder = TimelineBuilder(
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

    _save_timeline(tl, timeline_id)
    return jsonify({"ok": True, "plots_added": len(new_plots),
                    "total_plots": len(tl.plots)})


@app.route("/api/timeline/<timeline_id>/fill-gags", methods=["POST"])
def api_fill_gags(timeline_id):
    """注入笑点和吸睛点"""
    tl = _resolve_timeline(timeline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404

    builder = TimelineBuilder(
        structure_lib=struct_lib, plot_lib=plot_lib,
        gag_lib=gag_lib, theme_lib=theme_lib,
    )
    builder.fill_gags_and_hooks(tl.plots, tl)
    tl.phase = "ready" if tl.plots else "gags"
    _save_timeline(tl, timeline_id)
    return jsonify({"ok": True, "phase": tl.phase})


@app.route("/api/timeline/<timeline_id>/plot-confirm", methods=["POST"])
def api_plot_confirm(timeline_id):
    """切换单个桥段的确认状态"""
    tl = _resolve_timeline(timeline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    data = request.json or {}
    plot_id = data.get("plot_id", "")
    confirmed = data.get("confirmed", False)
    for p in tl.plots:
        if p.id == plot_id:
            p.confirmed = confirmed
            break
    _save_timeline(tl, timeline_id)
    return jsonify({"ok": True})


@app.route("/api/timeline/<timeline_id>/update-outline", methods=["POST"])
def api_update_outline(timeline_id):
    """更新大纲的章节范围"""
    tl = _resolve_timeline(timeline_id)
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
    _save_timeline(tl, timeline_id)
    return jsonify({"ok": True})


@app.route("/api/timeline/<timeline_id>/set-narrative", methods=["POST"])
def api_set_narrative(timeline_id):
    """设置大纲的叙事手法（顺叙/倒叙/插叙）+ 叙事目标"""
    tl = _resolve_timeline(timeline_id)
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
    _save_timeline(tl, timeline_id)
    return jsonify({"ok": True})


@app.route("/api/timeline/<timeline_id>/move-outline", methods=["POST"])
def api_move_outline(timeline_id):
    """上移/下移大纲"""
    tl = _resolve_timeline(timeline_id)
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
    _save_timeline(tl, timeline_id)
    return jsonify({"ok": True})


@app.route("/api/timeline/<timeline_id>/delete-outline", methods=["POST"])
def api_delete_outline(timeline_id):
    """删除一个大纲（同时删除其下的桥段）"""
    tl = _resolve_timeline(timeline_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    data = request.json or {}
    oid = data.get("id", "")
    tl.outlines = [o for o in tl.outlines if o.id != oid]
    tl.plots = [p for p in tl.plots if p.outline_id != oid]
    _save_timeline(tl, timeline_id)
    return jsonify({"ok": True})


# ═══════════════════════════════════════════
# 🎯 基础设定 + 一键完整大纲（启动新书/续写共用）
# ═══════════════════════════════════════════

@app.route("/api/timeline/<timeline_id>/save-basic-info", methods=["POST"])
def api_save_basic_info(timeline_id):
    """保存基础设定（主角/世界观/配角/基调/目标读者）。

    兼容草稿(tl_*)与正式书(book_*)；主角/世界观逐 key 深合并，保留用户已填值。
    """
    tl = _resolve_timeline(timeline_id)
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
    for field in ("supporting_cast", "tone", "target_audience"):
        if data.get(field) not in (None, ""):
            bi[field] = data[field]

    tl.basic_info = bi
    # 书名（与基础设定一起保存，正式书同步更新 book.json）
    if data.get("book_title") not in (None, ""):
        tl.book_title = data["book_title"]
        if timeline_id.startswith("book_"):
            book = book_mgr.get(timeline_id)
            if book:
                book.title = data["book_title"]
                book_mgr.update(book)
    tl.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_timeline(tl, timeline_id)
    return jsonify({"ok": True})


def _merge_basic_info(existing, generated):
    """以 generated 为基础，但保留 existing 里用户已填的非空字段。"""
    existing = existing or {}
    generated = generated or {}
    merged = {}
    for key, gv in generated.items():
        ev = existing.get(key)
        if isinstance(gv, dict) and isinstance(ev, dict):
            sub = dict(gv)
            for sk, sv in ev.items():
                if sv not in (None, "", [], {}):
                    sub[sk] = sv
            merged[key] = sub
        elif ev not in (None, "", [], {}):
            merged[key] = ev
        else:
            merged[key] = gv
    for key, ev in existing.items():  # generated 没覆盖的字段也保留
        if key not in merged:
            merged[key] = ev
    return merged


def _merge_generated_timeline(tl, generated):
    """把 5 阶段生成结果合并进现有时间线，不覆盖用户已填的基础信息。"""
    tl.basic_info = _merge_basic_info(tl.basic_info, generated.basic_info)
    if generated.outlines:
        tl.outlines = generated.outlines
    if generated.plots:
        tl.plots = generated.plots
    if generated.themes:
        tl.themes = generated.themes
    if generated.global_gags:
        tl.global_gags = generated.global_gags
    tl.phase = "ready"
    tl.generated_at = generated.generated_at or time.strftime("%Y-%m-%d %H:%M:%S")
    tl.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")


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
    if kind == "gag_review":
        gags = "、".join((chosen.get("gags") or [])[:5]) or "无"
        themes = "、".join((chosen.get("themes") or [])[:3]) or "无"
        return f"🎭 笑点/内涵[{step}]：笑点 {gags}｜内涵 {themes}"
    if kind == "validate":
        issues = (chosen.get("issues") or [])
        return f"✅ 一致性验证[{step}]：{len(issues)} 个建议｜{data.get('reason','')}"
    return f"🤖 {step}：{data.get('reason','')}"


@app.route("/api/timeline/<timeline_id>/generate-full", methods=["POST"])
def api_generate_full(timeline_id):
    """一键生成完整大纲（5 阶段 OutlineGenerator，SSE 流式），原地累加并逐步落盘。

    - 生成器直接操作当前 timeline 对象（timeline=tl），每阶段结束 on_save 落盘，
      实现"大纲→桥段→笑点/内涵挨个步骤写进配置文件"。
    - 新增 SSE 事件：thinking（AI 流式思考 token）、decision（候选→选中→理由），
      前端右侧"AI 思考过程"面板展示；decision 同时写入右侧栏任务日志。
    - phase_done 附带 timeline 快照，前端据此实时刷新左侧故事线视图。
    """
    tl = _resolve_timeline(timeline_id)
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
    gen = OutlineGenerator(
        llm_client=llm,
        structure_lib=struct_lib,
        plot_lib=plot_lib,
        gag_lib=gag_lib,
        theme_lib=theme_lib,
        profile=profile,
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
    task_id = f"genfull_{timeline_id}_{int(time.time())}"
    task_manager.start(task_id, name="完整大纲生成",
                       title=tl.pen_name or "", total=5,
                       phase="故事分析...", url=f"/timeline/{timeline_id}/edit")

    def generate():
        import json as _json

        try:
            for event_type, message, data_dict in gen.generate(
                genre=tl.genre, sub_genre=tl.sub_genre,
                custom_context=custom_context, pen_name=tl.pen_name,
                words_per_chapter=tl.words_per_chapter,
                timeline=tl,                       # 原地累加，可逐步落盘
                on_save=lambda _tl: _save_timeline(_tl, timeline_id),
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
                # 快照：内容已变化的 SSE 事件附带 timeline，前端据此逐条实时刷新左侧故事线
                if event_type in ("outline_added", "outline_plots", "plot_added",
                                  "gag_injected", "phase_done", "done"):
                    payload["timeline"] = tl.to_dict()

                # 每个决策写进右侧栏日志（用户能看到"确定了哪个大纲/桥段/笑点"）
                if event_type == "decision" and data_dict:
                    task_manager.log(task_id,
                                     _decision_log_message(data_dict.get("kind", "decision"), data_dict),
                                     "success")
                    # 决策后也落一次盘（桥段/加料已变化）
                    _save_timeline(tl, timeline_id)

                if event_type == "done":
                    _save_timeline(tl, timeline_id)
                    payload["timeline"] = tl.to_dict()
                    task_manager.done(task_id, message="完整大纲生成完成")

                yield "data: " + _json.dumps(payload, ensure_ascii=False) + "\n\n"
        except Exception as e:
            import traceback
            task_manager.fail(task_id, str(e))
            err_payload = {"event": "error", "message": str(e),
                           "traceback": traceback.format_exc()}
            yield "data: " + _json.dumps(err_payload, ensure_ascii=False) + "\n\n"

    return sse_stream_response(generate())


@app.route("/api/timeline/<timeline_id>/agent", methods=["POST"])
def api_timeline_agent(timeline_id):
    """大纲助手：用自然语言调整故事线配置（改桥段/加笑点/改大纲/增删桥段等）。

    由前端右侧「大纲助手」聊天面板调用；改动直接落盘，返回最新 timeline 供前端重绘。
    """
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "error": "消息为空"}), 400

    tl = _resolve_timeline(timeline_id)
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

    _save_timeline(tl, timeline_id)

    # 写入右侧栏任务日志，方便追溯
    task_id = f"agent_{timeline_id}"
    try:
        task_manager.start(task_id, name="大纲助手", title=tl.pen_name or "",
                           total=1, phase="调整故事线",
                           url=f"/timeline/{timeline_id}/edit")
        task_manager.log(task_id, f"🎙 {message}", "info")
        for line in result.get("summary", []):
            task_manager.log(task_id, line, "success")
        task_manager.done(task_id, message=result.get("reply", ""))
    except Exception:
        pass

    return jsonify(result)


# ═══════════════════════════════════════════
# ✍️ 写作台（按书列出，进入写作/续写）
# ═══════════════════════════════════════════

@app.route("/desk")
def desk_list():
    """写作台 — 列出正式书籍，每本进入写作/续写（不再列游离时间线草稿）"""
    rows = _book_rows()
    return render_template("desk_list.html", books=rows)


@app.route("/books/start/timeline/<timeline_id>/write")
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
        return redirect(url_for("timeline_write_flow", engine_id=engine_id))

    engine = NovelEngine(llm_client=llm)
    engine.start_new_book_timeline(tl, source_timeline_id=timeline_id)

    temp_id = f"tlw_{tl.pen_name}_{int(time.time())}"
    _engines[temp_id] = engine
    return redirect(url_for("timeline_write_flow", engine_id=temp_id))


@app.route("/books/timeline/write/<engine_id>")
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


@app.route("/api/timeline-engine/<engine_id>/step", methods=["POST"])
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
    flow_url = url_for("timeline_write_flow", engine_id=engine_id)

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


@app.route("/api/timeline-engine/<engine_id>/write-chapter", methods=["POST"])
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


@app.route("/api/timeline-engine/<engine_id>/write-bridge", methods=["POST"])
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
# ═══════════════════════════════════════════

@app.route("/books/<book_id>/continue")
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
    return redirect(url_for("timeline_write_flow", engine_id=engine_id))




# ═══════════════════════════════════════════
# 原有路由（保留兼容）
# ═══════════════════════════════════════════

@app.route("/books")
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


@app.route("/books/<book_id>")
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
    chapters = []
    for n in range(1, book.current_chapter + 2):
        ch = book_mgr.load_chapter(book_id, n)
        if ch: chapters.append(ch)
    cost_path = f"books/{book_id}/cost.json"
    cost = CostTracker.load(cost_path) if os.path.exists(cost_path) else CostTracker()
    csm = CharacterStateMachine()
    char_path = f"books/{book_id}/character_states.json"
    if os.path.exists(char_path): csm.load(char_path)
    return render_template("book_detail.html", book=book,
        outline=outline, chapters=chapters,
        timeline=timeline,
        basic_info=basic_info,
        cost=cost.summary(), characters=csm.characters)


@app.route("/books/<book_id>/delete", methods=["POST"])
def delete_book(book_id):
    book_mgr.delete(book_id)
    return redirect(url_for("books"))


@app.route("/timeline/<timeline_id>/delete", methods=["POST"])
def timeline_delete(timeline_id):
    """删除故事线草稿（tl_*/gen_*）。正式书 book_* 请走 /books/<id>/delete。"""
    if timeline_id.startswith("book_"):
        return jsonify({"ok": False, "error": "正式书请从书库删除"}), 400
    _timelines.pop(timeline_id, None)
    path = _timeline_filepath(timeline_id)
    try:
        if os.path.exists(path):
            os.remove(path)
        return jsonify({"ok": True})
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/plots")
def plots():
    cat = request.args.get("category","")
    templates = plot_lib.search(category=cat) if cat else plot_lib.templates
    return render_template("plots.html",
        templates=templates, categories=plot_lib.categories(),
        current_cat=cat)


@app.route("/api/plots/<plot_id>")
def plot_detail_api(plot_id):
    t = plot_lib.get_by_id(plot_id)
    if not t: return jsonify({"error":"not found"}), 404
    return jsonify(t.to_dict())


# ─── 库启用/禁用/删除（四大库共用一套逻辑） ───

# kind → (库实例, 条目列表属性名)
_LIB_TABLE = {
    "plots": (plot_lib, "templates"),
    "structures": (struct_lib, "templates"),
    "gags": (gag_lib, "patterns"),
    "themes": (theme_lib, "entries"),
}


def _lib_toggle(kind: str, item_id: str):
    """按 kind 切换某库条目的 enabled 状态"""
    lib, attr = _LIB_TABLE[kind]
    item = next((x for x in getattr(lib, attr) if x.id == item_id), None)
    if not item:
        return jsonify({"ok": False, "error": "not found"}), 404
    item.enabled = not item.enabled
    lib._save()
    return jsonify({"ok": True, "enabled": item.enabled})


def _lib_delete(kind: str, item_id: str):
    """按 kind 删除某库条目"""
    lib, attr = _LIB_TABLE[kind]
    items = getattr(lib, attr)
    if not any(x.id == item_id for x in items):
        return jsonify({"ok": False, "error": "not found"}), 404
    setattr(lib, attr, [x for x in items if x.id != item_id])
    lib._save()
    return jsonify({"ok": True})


@app.route("/api/plots/<plot_id>/toggle", methods=["POST"])
def plot_toggle(plot_id): return _lib_toggle("plots", plot_id)


@app.route("/api/plots/<plot_id>/delete", methods=["POST"])
def plot_delete(plot_id): return _lib_delete("plots", plot_id)


@app.route("/api/structures/<struct_id>/toggle", methods=["POST"])
def struct_toggle(struct_id): return _lib_toggle("structures", struct_id)


@app.route("/api/structures/<struct_id>/delete", methods=["POST"])
def struct_delete(struct_id): return _lib_delete("structures", struct_id)


@app.route("/api/gags/<gag_id>/toggle", methods=["POST"])
def gag_toggle(gag_id): return _lib_toggle("gags", gag_id)


@app.route("/api/gags/<gag_id>/delete", methods=["POST"])
def gag_delete(gag_id): return _lib_delete("gags", gag_id)


@app.route("/api/themes/<theme_id>/toggle", methods=["POST"])
def theme_toggle(theme_id): return _lib_toggle("themes", theme_id)


@app.route("/api/themes/<theme_id>/delete", methods=["POST"])
def theme_delete(theme_id): return _lib_delete("themes", theme_id)


@app.route("/structures")
def structures():
    return render_template("structures.html", templates=struct_lib.templates)


@app.route("/gags")
def gags():
    return render_template("gags.html", patterns=gag_lib.patterns)


@app.route("/themes")
def themes():
    return render_template("themes.html", entries=theme_lib.entries)


@app.route("/profiles")
def profile_list():
    return render_template("profiles.html", profiles=profiles.list_all())


@app.route("/profiles/new", methods=["GET","POST"])
def new_profile():
    if request.method == "POST":
        wp = {}
        if request.form.get("common_words"): wp["common_words"] = [w.strip() for w in request.form["common_words"].split(",")]
        if request.form.get("avoid_words"): wp["avoid_words"] = [w.strip() for w in request.form["avoid_words"].split(",")]
        profiles.create(
            pen_name=request.form["pen_name"],
            description=request.form["description"],
            style_fingerprint={
                "humor_style": request.form.get("humor_style",""),
                "action_style": request.form.get("action_style",""),
                "sentence_length": request.form.get("sentence_length","medium"),
            },
            word_print=wp,
        )
        return redirect(url_for("profile_list"))
    return render_template("new_profile.html")


@app.route("/write")
def write():
    """旧写作台 — 已废弃，重定向到书库"""
    return redirect(url_for("books"))


@app.route("/deai", methods=["GET","POST"])
def deai_test():
    result = None
    if request.method == "POST":
        engine = DeAIEngine()
        text = request.form["text"]
        result = engine.process_rule_based(text)
    return render_template("deai.html", result=result)


@app.route("/review-test", methods=["GET","POST"])
def review_test():
    result = None
    if request.method == "POST":
        reviewer = ContentReviewer()
        text = request.form["text"]
        result = reviewer.review(text, chapter_num=1)
    return render_template("review_test.html", result=result)


# ═══════════════════════════════════════
# 🔍 番茄侦察兵
# ═══════════════════════════════════════

@app.route("/extract")
def extract_page():
    """内容提取页"""
    return render_template("extract.html")


@app.route("/scout")
def scout_page():
    """侦察兵页面"""
    return render_template("scout.html")


@app.route("/api/scout/debug", methods=["GET"])
def scout_debug():
    """调试：测试搜索"""
    from plugins.fanqie_scout import FanqieCrawler
    title = request.args.get("title", "十日终焉")

    crawler = FanqieCrawler()
    results = []

    # 方案A: 番茄移动端API (novel.snssdk.com)
    try:
        import urllib.parse
        mobile_url = f"https://novel.snssdk.com/api/novel/channel/homepage/search/search_book/v1/?word={urllib.parse.quote(title)}&offset=0&count=5"
        r = crawler.session.get(mobile_url, timeout=15,
            headers={"Referer": "https://novel.snssdk.com/"})
        results.append({"method": "mobile_api", "status": r.status_code,
                        "body": r.text[:300]})
    except Exception as e:
        results.append({"method": "mobile_api", "error": str(e)})

    # 方案B: 番茄主站抓取书籍ID (模拟搜索页)
    try:
        import urllib.parse
        # 先访问首页拿数据
        r = crawler.session.get("https://fanqienovel.com/rank/hot", timeout=15)
        # 从页面中提取 book_id 列表
        ids = re.findall(r'"book_id"\s*:\s*"?(\d+)"?', r.text)
        results.append({"method": "rank_page_ids", "status": r.status_code,
                        "found_ids": ids[:10]})
    except Exception as e:
        results.append({"method": "rank_page", "error": str(e)})

    # 方案C: Bing搜索番茄
    try:
        import urllib.parse
        bing_query = f"{title} site:fanqienovel.com"
        bing_url = f"https://www.bing.com/search?q={urllib.parse.quote(bing_query)}"
        r = crawler.session.get(bing_url, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        ids = re.findall(r'fanqienovel\.com/page/(\d+)', r.text)
        results.append({"method": "bing_search", "status": r.status_code,
                        "found_ids": ids[:5]})
    except Exception as e:
        results.append({"method": "bing_search", "error": str(e)})

    # 方案D: 番茄分类页
    try:
        r = crawler.session.get("https://fanqienovel.com/category/1", timeout=15)
        ids = re.findall(r'book_id["\']?\s*[:=]\s*["\']?(\d+)', r.text)
        results.append({"method": "category_page", "status": r.status_code,
                        "found_ids": ids[:10]})
    except Exception as e:
        results.append({"method": "category_page", "error": str(e)})

    # 方案F：扫描API
    scan_api = request.args.get("scan_api")
    scan_bid = request.args.get("book_id", "7143038691944959011")
    if scan_api:
        try:
            r = crawler.session.get(
                f"https://fanqienovel.com/page/{scan_bid}",
                timeout=15)
            text = r.text
            import re as _re
            apis = _re.findall(r'["(][^"]*?(?:reader|chapter|directory|content)[^"]*?[")]', text)
            results.append({"method": "api_urls", "found": apis[:20]})
            api_paths = _re.findall(r'["(](/api/[^"]+?)[")]', text)
            results.append({"method": "api_paths", "found": api_paths[:20]})
        except Exception as e:
            results.append({"method": "api_scan", "error": str(e)})
    bid = request.args.get("book_id", "")
    if bid:
        try:
            r = crawler.session.get(
                f"https://fanqienovel.com/page/{scan_bid}",
                timeout=15,
                headers={"Accept": "text/html,application/xhtml+xml"})
            text = r.text
            # 尝试多种数据提取方式
            results.append({"method": "page_html", "status": r.status_code,
                            "length": len(text)})
            
            # 方式1: __NEXT_DATA__ / __NUXT__ / __INITIAL_STATE__
            import re as _re
            import json as _json
            for pattern_name, pattern in [
                ("initial_state", r'window\.__INITIAL_STATE__\s*=\s*({.+?});'),
                ("next_data", r'__NEXT_DATA__\s*=\s*({.+?});'),
                ("nuxt_data", r'window\.__NUXT__\s*=\s*({.+?});'),
            ]:
                m = _re.search(pattern, text, _re.DOTALL)
                if m:
                    try:
                        ssr = _json.loads(m.group(1))
                        # 尝试提取书籍信息
                        page_data = ssr.get("page", {})
                        if isinstance(page_data, dict):
                            page_keys = list(page_data.keys())[:20]
                            results.append({"method": f"ssr_{pattern_name}",
                                            "page_keys": page_keys})
                            # 提取书名
                            for key in ["bookName", "book_name", "title", "name"]:
                                if key in page_data:
                                    results.append({"method": "found_book",
                                                    "field": key,
                                                    "value": str(page_data[key])[:100]})
                                # 嵌套查找
                                for sub_key in page_data:
                                    sv = page_data[sub_key]
                                    if isinstance(sv, dict) and key in sv:
                                        results.append({"method": "found_book_nested",
                                                        "path": f"page.{sub_key}.{key}",
                                                        "value": str(sv[key])[:100]})
                            # 列出所有含 "book" 的字段
                            ssr_str = _json.dumps(page_data, ensure_ascii=False)
                            book_fields = _re.findall(r'"(book[^"]*)"\s*:\s*"([^"]{1,80})"', ssr_str)
                            if book_fields:
                                results.append({"method": "book_fields",
                                                "fields": [{"k": k, "v": v} for k, v in book_fields[:10]]})
                    except Exception as e:
                        results.append({"method": f"ssr_{pattern_name}",
                                        "error": str(e), "preview": m.group(1)[:300]})

            # 方式2: meta标签
            import re as _re
            title_match = _re.search(r'<meta[^>]*property="og:title"[^>]*content="([^"]+)"', text)
            if title_match:
                results.append({"method": "og_title", "title": title_match.group(1)})
            
            # 方式3: title标签
            title_match = _re.search(r'<title>([^<]+)</title>', text)
            if title_match:
                results.append({"method": "html_title", "title": title_match.group(1)})
                
        except Exception as e:
            results.append({"method": "page_html", "error": str(e)})

    # 方案G：提取章节列表
    if request.args.get("scan_chapters"):
        from plugins.fanqie_scout import FanqieCrawler
        c = FanqieCrawler()
        bid = request.args.get("book_id", "7143038691944959011")
        try:
            # 1. 书籍页SSR
            r = c.session.get(f"https://fanqienovel.com/page/{bid}", timeout=15,
                headers={"Accept": "text/html,application/xhtml+xml"})
            m = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.+?});', r.text, re.DOTALL)
            if m:
                ssr = json.loads(m.group(1))
                # 深度扫描找章节相关数据
                def find_chapters(obj, path="", depth=0):
                    if depth > 5: return
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if any(w in str(k).lower() for w in ['chapter','volume','directory','catalog']):
                                if isinstance(v, list) and len(v) > 0:
                                    results.append({"method": f"found_chapters",
                                        "path": f"{path}.{k}",
                                        "count": len(v),
                                        "first": str(v[0])[:200] if v else "empty"})
                                elif isinstance(v, dict):
                                    results.append({"method": f"found_chapter_data",
                                        "path": f"{path}.{k}",
                                        "keys": list(v.keys())[:10]})
                            find_chapters(v, f"{path}.{k}", depth+1)
                    elif isinstance(obj, list) and len(obj) > 0:
                        find_chapters(obj[0], f"{path}[0]", depth+1)
                find_chapters(ssr)

            # 2. 尝试API获取目录
            for api_url in [
                f"https://fanqienovel.com/api/reader/directory/detail?bookId={bid}",
                f"https://fanqienovel.com/api/reader/directory/v2?book_id={bid}",
                f"https://novel.snssdk.com/api/novel/book/directory/list/v1/?book_id={bid}&offset=0&count=10",
            ]:
                try:
                    r2 = c.session.get(api_url, timeout=15)
                    results.append({"method": "api_dir", "url": api_url[:60],
                        "status": r2.status_code,
                        "body": r2.text[:300]})
                except Exception as e:
                    results.append({"method": "api_dir", "url": api_url[:60], "error": str(e)})
        except Exception as e:
            results.append({"method": "scan_chapters", "error": str(e)})

    return jsonify(results)


@app.route("/api/scout/run", methods=["POST"])
def scout_run():
    """启动侦察任务"""
    from plugins.fanqie_scout import FanqieScoutAgent

    data = request.json or {}
    title = data.get("title", "").strip()
    chapters = int(data.get("chapters", 30))
    direct_id = data.get("book_id", "").strip()

    if not title and not direct_id:
        return jsonify({"error": "请输入书名或 book_id"}), 400

    llm = get_llm()
    if not llm:
        return jsonify({"error": "LLM 未配置"}), 500

    scout = FanqieScoutAgent(llm, plot_lib, struct_lib, gag_lib, theme_lib)

    def generate():
        import json as _json
        import queue as _queue
        import threading as _threading

        def send_event(event, d):
            return f"data: {_json.dumps({'event': event, **d}, ensure_ascii=False)}\n\n"

        yield send_event("start", {"title": title or direct_id, "chapters": chapters})

        # 搜索阶段：阻塞执行，但速度很快
        try:
            if direct_id:
                novel = scout.crawler._get_novel_from_page(direct_id)
                if not novel:
                    yield send_event("error", {"message": f"book_id={direct_id} not found"})
                    return
            else:
                novel = scout.crawler.search_novel(title)
                if not novel:
                    yield send_event("error", {"message": f"not found: {title}"})
                    return
        except Exception as e:
            yield send_event("error", {"message": f"搜索失败: {e}"})
            return

        yield send_event("found", {
            "title": novel.title, "author": novel.author,
            "genre": novel.genre, "chapters": novel.chapter_count,
            "words": novel.word_count,
        })

        # 注册到全局任务管理器（跨页面可见）
        # 单任务互斥：同一工具（小说抓取）同时只允许一个任务，新任务替代旧任务
        from plugins import task_manager
        task_manager.ensure_single("小说抓取")
        task_id = f"fetch_{novel.title}"
        task_manager.start(task_id, name="小说抓取", title=novel.title,
                          total=chapters, phase="搜索", url="/scout")
        task_manager.register_cancel(task_id)
        task_manager.log(task_id, f"找到: {novel.title}", "success")

        evt_queue = _queue.Queue()
        _cancel_exception = Exception("__CANCELLED__")

        def on_progress(phase, current, total, message):
            # 检查取消：如果被取消了就抛异常，让 worker catch 住
            if task_manager.is_cancelled(task_id):
                raise _cancel_exception
            evt_queue.put(("progress", phase, current, total, message))
            # 同步更新全局任务管理器
            if phase == "search":
                task_manager.progress(task_id, current, total, "搜索", message)
                task_manager.log(task_id, message, "info")
            elif phase == "download":
                task_manager.progress(task_id, current, total, "下载", message)
                task_manager.log(task_id, message, "info")
            elif phase == "analysis_done":
                task_manager.progress(task_id, 0, 1, "完成", "分析完成")
                task_manager.log(task_id, "分析完成", "success")

        def worker():
            try:
                novel_info, dl_info = scout.fetch_novel(novel.title, chapters, on_progress=on_progress)
                # 如果没有被取消才标记完成
                if not task_manager.is_cancelled(task_id):
                    task_manager.done(task_id, f"下载完成 {dl_info['chapters']}章")
                    evt_queue.put(("fetch_done", {"novel_info": novel_info, "dl_info": dl_info}))
            except Exception as e:
                import traceback
                err_msg = str(e)
                # 如果是取消导致的，不报错
                if str(e) == "__CANCELLED__":
                    return
                # 翻译常见异常为用户友好提示
                if "NoneType" in err_msg and "subscriptable" in err_msg:
                    err_msg = "页面数据解析失败，番茄页面结构可能已变更，请等待插件更新"
                elif "timeout" in err_msg.lower() or "timed out" in err_msg.lower():
                    err_msg = "网络请求超时，请检查网络连接或稍后重试"
                elif "Connection" in err_msg:
                    err_msg = "网络连接失败，请检查网络"
                task_manager.fail(task_id, err_msg)
                evt_queue.put(("error", err_msg))

        t = _threading.Thread(target=worker, daemon=True, name="scout-fetch")
        t.start()

        # 从队列读取进度事件，实时 yield
        while t.is_alive() or not evt_queue.empty():
            # SSE 循环中也检查取消，如果已被取消则提前结束 SSE 流
            if task_manager.is_cancelled(task_id):
                yield send_event("cancelled", {"message": "任务已取消"})
                break
            try:
                item = evt_queue.get(timeout=0.3)
                kind = item[0]
                if kind == "progress":
                    _, phase, current, total, message = item
                    yield send_event("progress", {
                        "phase": phase, "current": current,
                        "total": total, "message": message,
                    })
                elif kind == "fetch_done":
                    ni = item[1]["novel_info"]
                    di = item[1]["dl_info"]
                    yield send_event("fetch_done", {
                        "title": ni.title,
                        "author": ni.author,
                        "saved_chapters": di["chapters"],
                        "folder": di["folder"],
                        "platform": "fanqie",
                    })
                elif kind == "error":
                    yield send_event("error", {"message": item[1]})
            except _queue.Empty:
                pass

    resp = sse_stream_response(generate())
    return resp


# ─── 入库（人工筛选后） ───

@app.route("/api/scout/ingest", methods=["POST"])
def scout_ingest():
    """入库选中的分析结果"""
    from plugins.fanqie_scout import FanqieScoutAgent
    from plugins import task_manager
    data = request.json or {}
    title = data.get("title", "")
    plots = data.get("plots", [])
    structures = data.get("structures", [])
    gags = data.get("gags", [])
    themes = data.get("themes", [])

    if not title and not any([plots, structures, gags, themes]):
        return jsonify({"ok": False, "error": "参数为空"}), 400

    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置"}), 500

    # 单任务互斥：资产入库同一时间只允许一个
    task_manager.ensure_single("资产入库")
    task_id = f"ingest_{title}_{int(time.time())}"
    task_manager.start(task_id, name="资产入库", title=title, total=1, phase="入库中...", url="/extract")
    task_manager.log(task_id, f"入库: {len(plots)}桥段 {len(structures)}大纲 {len(gags)}笑点 {len(themes)}内涵", "info")

    scout = FanqieScoutAgent(llm, plot_lib, struct_lib, gag_lib, theme_lib)
    stats = scout.ingest_selected(
        plots=plots, structures=structures,
        gags=gags, themes=themes, source="fanqie",
    )

    task_manager.done(task_id, message=f"入库完成: +{stats['plots']}桥段 +{stats['structures']}大纲")
    task_manager.log(task_id, f"✅ 入库完成: +{stats['plots']}桥段 +{stats['structures']}大纲 +{stats['gags']}笑点 +{stats['themes']}内涵", "success")

    return jsonify({
        "ok": True,
        "stats": stats,
        "message": f"入库完成: +{stats['plots']}桥段 +{stats['structures']}大纲 "
                   f"+{stats['gags']}笑点 +{stats['themes']}内涵",
    })


# ═══════════════════════════════════════
# ⚙️ 设置页
# ═══════════════════════════════════════

# ─── 已下载小说列表 ───

@app.route("/api/scout/novels")
def scout_novels():
    """列出已下载的小说"""
    from plugins.novel_storage import list_novels
    platform = request.args.get("platform", "")
    novels = list_novels(platform)
    # 标记是否已分析
    for n in novels:
        from pathlib import Path
        analyzed_file = Path(n["path"]) / ".analyzed"
        n["analyzed"] = analyzed_file.exists()
    return jsonify(novels)


# ─── 分析已下载的小说（提取库条目+写作风格） ───

@app.route("/api/scout/analyze", methods=["POST"])
def scout_analyze():
    """分析已下载的小说（后台线程 + SSE 流式，支持单任务互斥/取消）"""
    from plugins.novel_storage import load_novel
    from plugins.style_analyzer import extract_writing_style
    from plugins.fanqie_scout import NovelAnalyzer
    from plugins import task_manager

    data = request.json or {}
    platform = data.get("platform", "")
    folder = data.get("folder", "")
    profile_id = data.get("profile_id", "")
    mode = data.get("mode", "library")  # library | style

    if not platform or not folder:
        return jsonify({"ok": False, "error": "参数缺失"}), 400

    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置"}), 500

    novel_data = load_novel(platform, folder)
    if not novel_data:
        return jsonify({"ok": False, "error": "小说不存在"}), 404

    info = novel_data["info"]
    chapters = novel_data["chapters"]
    title = info.get("title", folder)

    # 单任务互斥：同一工具（内容分析）同时只允许一个任务，新任务替代旧任务
    task_manager.ensure_single("内容分析")
    task_id = f"analyze_{title}_{int(time.time())}"
    task_manager.start(task_id, name="内容分析", title=title, total=50, phase="准备中", url="/extract")
    task_manager.register_cancel(task_id)
    task_manager.log(task_id, f"开始分析: {title} ({len(chapters)}章)", "info")

    from pathlib import Path
    _cancel_exception = Exception("__CANCELLED__")

    def on_progress(phase, current, total, message):
        if task_manager.is_cancelled(task_id):
            raise _cancel_exception
        task_manager.progress(task_id, current=current, total=total,
                              phase=message, message=message)
        task_manager.log(task_id, f"LLM 分析：{message}", "info")

    def generate():
        import json as _json
        import queue as _queue
        import threading as _threading

        def send_event(event, d):
            return f"data: {_json.dumps({'event': event, **d}, ensure_ascii=False)}\n\n"

        yield send_event("start", {"title": title})

        evt_queue = _queue.Queue()

        def worker():
            try:
                analysis = {}
                if mode == "library":
                    # Step 1: 分析四大库
                    analyzer = NovelAnalyzer(llm)
                    analysis = analyzer.analyze_book(
                        type("obj", (object,), {
                            "title": title,
                            "genre": info.get("genre", ""),
                            "sub_genre": "",
                            "chapter_count": len(chapters),
                        })(),
                        chapters, on_progress=on_progress,
                    )
                    if task_manager.is_cancelled(task_id):
                        return
                    task_manager.progress(task_id, current=45, phase="分析完成", message="四大库提取完毕")
                    task_manager.log(task_id,
                        f"桥段: {len(analysis.get('plots',[]))}个 大纲: {len(analysis.get('structures',[]))}个 "
                        f"笑点: {len(analysis.get('gags',[]))}个 内涵: {len(analysis.get('themes',[]))}个", "success")
                else:
                    # Step 2: 分析写作风格
                    task_manager.progress(task_id, current=30, phase="LLM 分析写作风格...", message="正在分析写作风格")
                    task_manager.log(task_id, "LLM 分析：写作风格...", "info")
                    style = extract_writing_style(llm, title, chapters)
                    analysis = {"writing_style": style}
                    if task_manager.is_cancelled(task_id):
                        return

                # 如果指定了笔名，自动生成风格档案
                profile_ready = False
                if profile_id and profiles.get(profile_id):
                    profile = profiles.get(profile_id)
                    style_words = (analysis.get("writing_style") or {}).get("common_words", [])
                    avoid_words = (analysis.get("writing_style") or {}).get("avoid_words", [])
                    if style_words or avoid_words:
                        wp = profile.word_print or {}
                        wp["common_words"] = list(set(wp.get("common_words", []) + style_words))
                        wp["avoid_words"] = list(set(wp.get("avoid_words", []) + avoid_words))
                        profile.word_print = wp
                        profiles.update(profile)
                        profile_ready = True

                if task_manager.is_cancelled(task_id):
                    return

                # 标记已分析
                novel_path = Path(__file__).parent.parent / "storage" / "novels" / platform / folder
                (novel_path / ".analyzed").touch()

                task_manager.done(task_id, message=f"分析完成: {title}")
                task_manager.log(task_id, f"✅ 分析完成: {title}", "success")
                evt_queue.put(("done", {
                    "title": info.get("title", ""),
                    "mode": mode,
                    "plot_details": analysis.get("plots", []),
                    "structure_details": analysis.get("structures", []),
                    "gag_details": analysis.get("gags", []),
                    "theme_details": analysis.get("themes", []),
                    "writing_style": analysis.get("writing_style"),
                    "profile_ready": profile_ready,
                }))
            except Exception as e:
                err_msg = str(e)
                if str(e) == "__CANCELLED__":
                    return
                if "NoneType" in err_msg and "subscriptable" in err_msg:
                    err_msg = "LLM 返回格式异常，请重试"
                elif "timeout" in err_msg.lower() or "timed out" in err_msg.lower():
                    err_msg = "LLM 请求超时，请稍后重试"
                elif "Connection" in err_msg:
                    err_msg = "网络连接失败，请检查网络"
                task_manager.fail(task_id, err_msg)
                evt_queue.put(("error", err_msg))

        t = _threading.Thread(target=worker, daemon=True, name="scout-analyze")
        t.start()

        # 从队列读取事件，实时 yield
        while t.is_alive() or not evt_queue.empty():
            if task_manager.is_cancelled(task_id):
                yield send_event("cancelled", {"message": "任务已被新任务替代"})
                break
            try:
                item = evt_queue.get(timeout=0.3)
                kind = item[0]
                if kind == "done":
                    yield send_event("done", item[1])
                elif kind == "error":
                    yield send_event("error", {"message": item[1]})
            except _queue.Empty:
                pass

    resp = sse_stream_response(generate())
    return resp


@app.route("/api/status/tasks")
def status_tasks():
    """返回当前运行中的任务列表（供右侧状态栏轮询）"""
    from plugins import task_manager
    tasks = task_manager.get_tasks()
    return jsonify(tasks)


@app.route("/api/status/tasks/close", methods=["POST"])
def status_tasks_close():
    """关闭/删除指定任务（运行中的任务自动取消，已完成/失败的直接移除）"""
    from plugins import task_manager
    data = request.get_json()
    task_id = data.get("id", "")
    if task_id:
        # 如果还在运行则先取消
        task_manager.cancel(task_id)
        # 从列表移除
        task_manager.remove(task_id)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "missing id"})


def _load_api_config() -> dict:
    """读取 api.json（不存在返回空 dict）"""
    api_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api.json")
    if os.path.exists(api_path):
        with open(api_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _mask_api_key(key: str) -> str:
    """掩码 API key：保留前 8 位，其余用 **** 代替（含 **** 即视为"未修改"）"""
    if not key:
        return ""
    return key[:8] + "****"


@app.route("/settings")
def settings_page():
    """设置页面 — 纯静态渲染，不发起 API 请求"""
    cfg = _load_api_config()

    # 不调用 LLM API，只检查本地配置是否存在（瞬间完成）
    api_configured = bool(cfg.get("api_key") and cfg.get("base_url"))

    return render_template("settings.html",
        config={
            # 只回传掩码，明文 key 不进入 HTML（防止源码泄露）
            "api_key_masked": _mask_api_key(cfg.get("api_key", "")),
            "base_url": cfg.get("base_url", "https://api.deepseek.com"),
            "model": cfg.get("model", "deepseek-chat"),
            "http_timeout_seconds": cfg.get("http_timeout_seconds", 300),
            "context_budget_tokens": cfg.get("context_budget_tokens", 300000),
            "url_strict": cfg.get("url_strict", False),
        },
        llm_ok=api_configured,
    )


@app.route("/api/settings/save", methods=["POST"])
def settings_save():
    """保存设置"""
    data = request.json or {}
    api_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api.json")

    # 读取当前配置，只覆盖传入的字段
    cfg = _load_api_config()

    # API Key 掩码值（含 ****）表示未修改，保留已保存的原 key
    new_key = data.get("api_key", "")
    if new_key and "****" in new_key:
        data.pop("api_key", None)

    for key in ("api_key", "base_url", "model", "url_strict",
                "http_timeout_seconds", "context_budget_tokens"):
        if key in data:
            cfg[key] = data[key]

    # 校验
    if not cfg.get("api_key"):
        return jsonify({"ok": False, "error": "API Key 不能为空"}), 400
    if not cfg.get("base_url"):
        return jsonify({"ok": False, "error": "API 地址不能为空"}), 400

    try:
        with open(api_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return jsonify({"ok": False, "error": f"写入失败: {e}"}), 500

    # 清除缓存的 LLM 客户端
    global _llm_client
    _llm_client = None

    return jsonify({"ok": True, "message": "设置已保存"})


@app.route("/api/settings/test", methods=["POST"])
def settings_test():
    """测试 LLM 连接"""
    data = request.json or {}

    from core.llm_client import LLMClient
    from core.models import APIConfig

    # 前端传回掩码/空值 → 用当前已保存的 key 测试（避免 key 进入浏览器后回传）
    api_key = data.get("api_key", "")
    if not api_key or "****" in api_key:
        api_key = _load_api_config().get("api_key", "")

    api_cfg = APIConfig(
        api_key=api_key,
        base_url=data.get("base_url", "https://api.deepseek.com"),
        model=data.get("model", "deepseek-chat"),
        http_timeout_seconds=10,
    )

    try:
        client = LLMClient(api_cfg)
        result = client.test_connection()
        if result.get("success"):
            return jsonify({"ok": True, "model": api_cfg.model,
                            "response": result.get("sample", "")[:50]})
        else:
            return jsonify({"ok": False, "error": result.get("error", "连接失败")}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/debug/search-test")
def debug_search_test():
    """调试：测试搜索"""
    from plugins.fanqie_scout import FanqieCrawler
    title = request.args.get("q", "这个游戏不对劲，我挖矿成神！")
    try:
        c = FanqieCrawler()
        # 检查文件修改时间
        import os
        mtime = os.path.getmtime(os.path.join(os.path.dirname(__file__), "..", "plugins", "fanqie_scout.py"))
        from datetime import datetime
        mtime_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        
        novel = c.search_novel(title)
        if novel:
            return jsonify({
                "ok": True,
                "title": novel.title,
                "author": novel.author,
                "book_id": novel.book_id,
                "chapters": novel.chapter_count,
                "file_time": mtime_str,
            })
        return jsonify({"ok": False, "message": "not found", "file_time": mtime_str})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "traceback": traceback.format_exc()})



# ═══════════════════════════════════════════
# 大纲生成（已内嵌到启动新书 / 续写流程）
# ═══════════════════════════════════════════

@app.route("/books/generator")
def outline_generator_page():
    """大纲生成器已内嵌到「启动新书」流程（时间线编辑器：一键生成完整大纲）"""
    return redirect(url_for("start_new_book"))


if __name__ == "__main__":
    os.makedirs("ui/templates", exist_ok=True)
    os.makedirs("ui/static", exist_ok=True)
    # debug 由环境变量控制：开发用 NOVEL_DEBUG=1，默认关闭（避免 reloader 干扰自动化）
    debug = os.environ.get("NOVEL_DEBUG") == "1"
    host = os.environ.get("NOVEL_HOST", "127.0.0.1")
    print(f"NovelEngine Web UI v2.0: http://localhost:58080")
    app.run(host=host, port=58080, debug=debug, use_reloader=debug)
