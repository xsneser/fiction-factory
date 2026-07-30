"""
NovelEngine — 完整 Web UI v2.0 (Flask + Jinja2)
引擎集成版：新书启动 / 续写 / 管理面板
"""
from flask import Flask, render_template, request, jsonify, redirect, url_for, Response, stream_with_context
import sys, os, json, threading, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libraries.plot import PlotLibrary
from libraries.structure import StructureLibrary
from libraries.gag import GagLibrary
from libraries.theme import ThemeLibrary
from libraries.profiles import ProfileManager
from libraries.book_manager import BookManager
from libraries.cost_tracker import CostTracker
from libraries.de_ai import DeAIEngine
from libraries.writing_pipeline import WritingPipeline, WritingContext
from libraries.character_state import CharacterStateMachine
from libraries.reviewer import ContentReviewer
from libraries.engine import NovelEngine, BookMode, Phase, Op
from libraries.new_book import NewBookConfig, NewBookPipeline, recommend_opening
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
        )
        _llm_client = LLMClient(api_cfg)
        return _llm_client
    return None

# ─── 引擎实例缓存 ───
_engines: dict[str, NovelEngine] = {}

def create_engine() -> NovelEngine:
    llm = get_llm()
    return NovelEngine(llm_client=llm)


# ═══════════════════════════════════════════
# 首页 Dashboard
# ═══════════════════════════════════════════
@app.route("/")
def dashboard():
    books = book_mgr.list_all()
    pen_names = profiles.list_all()
    return render_template("dashboard.html",
        books=books, pen_names=pen_names,
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
    """新书启动页面"""
    if request.method == "POST":
        config = NewBookConfig(
            title=request.form.get("title", ""),
            pen_name=request.form.get("pen_name", ""),
            genre=request.form.get("genre", ""),
            sub_genre=request.form.get("sub_genre", ""),
            platform=request.form.get("platform", "fanqie"),
            chapter_count=int(request.form.get("chapter_count", 500)),
            opening_template_id=request.form.get("opening_template_id", ""),
            golden_finger_template_id=request.form.get("golden_finger_template_id", ""),
            structure_template_id=request.form.get("structure_template_id", ""),
            style_profile_id=request.form.get("style_profile_id", ""),
        )

        llm = get_llm()
        if not llm:
            return jsonify({"error": "LLM 未配置，请先设置 api.json"}), 500

        engine = NovelEngine(llm_client=llm)
        engine.start_new_book(config)

        # 缓存引擎
        temp_id = f"new_{engine.state.pen_name}"
        _engines[temp_id] = engine

        return redirect(url_for("new_book_workflow", engine_id=temp_id))

    return render_template("start_book.html",
        pen_names=profiles.list_all(),
        structures=struct_lib.templates,
        openings=plot_lib.search(category="开篇"),
        golden_fingers=plot_lib.search(category="成长") + plot_lib.search(category="爽文"),
    )


@app.route("/books/start/<engine_id>")
def new_book_workflow(engine_id):
    """新书启动工作流页面"""
    engine = _engines.get(engine_id)
    if not engine:
        return "引擎会话已过期", 404
    return render_template("new_book_flow.html",
        engine_id=engine_id,
        state=engine.state,
    )


@app.route("/api/engine/<engine_id>/step", methods=["POST"])
def engine_step(engine_id):
    """执行引擎一步"""
    engine = _engines.get(engine_id)
    if not engine:
        return jsonify({"error": "not found"}), 404

    inst = engine.route()
    if inst.op in (Op.COMPLETE, Op.PAUSE, Op.CONFIRM_CHAPTER):
        return jsonify({"status": inst.op.value, "reason": inst.reason, "done": True})

    result = engine.execute(inst)

    # 检查是否转入续写
    if inst.op == Op.GENERATE_TITLE and result.get("status") == "title_generated":
        # 新书启动完成，创建正式图书记录
        book = engine.finalize_new_book()
        return jsonify({
            **result,
            "flow_complete": True,
            "book_id": book.book_id,
            "next_phase": "continue",
        })

    return jsonify({
        "op": inst.op.value,
        "chapter_num": inst.chapter_num,
        "reason": inst.reason,
        **result,
    })


@app.route("/api/engine/<engine_id>/status")
def engine_status_api(engine_id):
    """获取引擎状态"""
    engine = _engines.get(engine_id)
    if not engine:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "book_mode": engine.state.book_mode.value,
        "phase": engine.state.phase.value,
        "current_chapter": engine.state.current_chapter,
        "total_chapters": engine.state.total_chapters,
        "title": engine.state.title,
        "pen_name": engine.state.pen_name,
        "genre": engine.state.genre,
        "new_book": {
            "chapters_written": sum(1 for ch in [
                engine.state.new_book.chapter1,
                engine.state.new_book.chapter2,
                engine.state.new_book.chapter3,
            ] if ch),
            "title_options": engine.state.new_book.title_options,
            "best_title": engine.state.new_book.best_title,
            "characters": len(engine.state.new_book.characters_created),
            "foreshadows": len(engine.state.new_book.foreshadows_planned),
        } if engine.state.book_mode == BookMode.NEW else None,
    })


# ═══════════════════════════════════════════
# ♻️ 续写
# ═══════════════════════════════════════════

@app.route("/books/<book_id>/continue")
def continue_book_page(book_id):
    """续写页面"""
    book = book_mgr.get(book_id)
    if not book:
        return "图书不存在", 404

    engine_id = f"cont_{book_id}"
    if engine_id not in _engines:
        llm = get_llm()
        if not llm:
            return jsonify({"error": "LLM 未配置"}), 500
        engine = NovelEngine(llm_client=llm)
        engine.continue_book(book_id)
        _engines[engine_id] = engine

    engine = _engines[engine_id]
    return render_template("continue_flow.html",
        engine_id=engine_id,
        book=book,
        state=engine.state,
    )


@app.route("/api/engine/<engine_id>/chapter/preview", methods=["POST"])
def chapter_preview(engine_id):
    """预览写作 prompt（不生成）"""
    engine = _engines.get(engine_id)
    if not engine:
        return jsonify({"error": "not found"}), 404

    data = request.json or {}
    chapter_num = data.get("chapter_num", engine.state.current_chapter + 1)
    chapter_outline = data.get("chapter_outline", "")

    ctx = WritingContext(
        chapter_num=chapter_num,
        chapter_outline=chapter_outline,
        style_profile=engine.profile,
        target_words=engine.book.words_per_chapter if engine.book else 3000,
    )
    ctx = engine.writing.match_templates(chapter_outline, engine.state.genre,
                                         chapter_outline, existing_ctx=ctx)
    system, user = engine.writing.build_prompt(ctx)

    return jsonify({
        "system": system,
        "user": user,
        "plot_name": ctx.plot_template.name if ctx.plot_template else "auto",
        "gags": [g.name for g in ctx.gags],
        "themes": [t.name for t in ctx.themes],
    })


@app.route("/api/engine/<engine_id>/chapter/generate", methods=["POST"])
def chapter_generate(engine_id):
    """生成一章（流式）"""
    engine = _engines.get(engine_id)
    if not engine:
        # Auto-create: cont_book_XXX -> load book
        if engine_id.startswith("cont_"):
            book_id = engine_id[5:]
            llm = get_llm()
            if not llm:
                return jsonify({"error": "LLM not configured"}), 500
            engine = NovelEngine(llm_client=llm)
            try:
                engine.continue_book(book_id)
            except ValueError:
                return jsonify({"error": f"book {book_id} not found"}), 404
            _engines[engine_id] = engine
        else:
            return jsonify({"error": "not found"}), 404

    if engine.state.book_mode != BookMode.CONTINUE:
        return jsonify({"error": "仅续写模式支持单章生成"}), 400

    inst = engine.route()
    if inst.op == Op.COMPLETE:
        return jsonify({"status": "complete"})
    if inst.op == Op.PAUSE:
        return jsonify({"status": "paused", "reason": inst.reason})

    result = engine.execute(inst)

    # 如果写完了，继续审+去AI
    if result.get("status") == "chapter_written":
        review_inst = engine.route()
        review_result = engine.execute(review_inst)
        if review_result.get("status") == "review_passed":
            deai_inst = InstructionAdapter(Op.DE_AI_PASS, engine.state.current_chapter)
            deai_result = engine.execute(deai_inst)
            return jsonify({
                **result,
                "review": review_result,
                "de_ai": deai_result,
                "cycle_complete": True,
            })
        else:
            return jsonify({**result, "review": review_result})

    return jsonify(result)


# 适配器：engine 内部使用 Instruction dataclass
from dataclasses import dataclass

@dataclass
class InstructionAdapter:
    op: Op
    chapter_num: int = 0
    chapter_title: str = ""
    reason: str = ""


# ═══════════════════════════════════════════
# 原有路由（保留兼容）
# ═══════════════════════════════════════════

@app.route("/books")
def books():
    return render_template("books.html", books=book_mgr.list_all())


@app.route("/books/<book_id>")
def book_detail(book_id):
    book = book_mgr.get(book_id)
    if not book: return "Not found", 404
    outline = book_mgr.get_outline(book_id)
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
        cost=cost.summary(), characters=csm.characters)


@app.route("/books/<book_id>/delete", methods=["POST"])
def delete_book(book_id):
    book_mgr.delete(book_id)
    return redirect(url_for("books"))


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


# ─── 库启用/禁用/删除 ───

@app.route("/api/plots/<plot_id>/toggle", methods=["POST"])
def plot_toggle(plot_id):
    t = plot_lib.get_by_id(plot_id)
    if not t: return jsonify({"ok": False, "error": "not found"}), 404
    t.enabled = not t.enabled
    plot_lib._save()
    return jsonify({"ok": True, "enabled": t.enabled})


@app.route("/api/plots/<plot_id>/delete", methods=["POST"])
def plot_delete(plot_id):
    t = plot_lib.get_by_id(plot_id)
    if not t: return jsonify({"ok": False, "error": "not found"}), 404
    plot_lib.templates = [x for x in plot_lib.templates if x.id != plot_id]
    plot_lib._save()
    return jsonify({"ok": True})


@app.route("/api/structures/<struct_id>/toggle", methods=["POST"])
def struct_toggle(struct_id):
    t = struct_lib.get_by_id(struct_id)
    if not t: return jsonify({"ok": False, "error": "not found"}), 404
    t.enabled = not t.enabled
    struct_lib._save()
    return jsonify({"ok": True, "enabled": t.enabled})


@app.route("/api/structures/<struct_id>/delete", methods=["POST"])
def struct_delete(struct_id):
    t = struct_lib.get_by_id(struct_id)
    if not t: return jsonify({"ok": False, "error": "not found"}), 404
    struct_lib.templates = [x for x in struct_lib.templates if x.id != struct_id]
    struct_lib._save()
    return jsonify({"ok": True})


@app.route("/api/gags/<gag_id>/toggle", methods=["POST"])
def gag_toggle(gag_id):
    p = gag_lib.get_by_id(gag_id)
    if not p: return jsonify({"ok": False, "error": "not found"}), 404
    p.enabled = not p.enabled
    gag_lib._save()
    return jsonify({"ok": True, "enabled": p.enabled})


@app.route("/api/gags/<gag_id>/delete", methods=["POST"])
def gag_delete(gag_id):
    p = gag_lib.get_by_id(gag_id)
    if not p: return jsonify({"ok": False, "error": "not found"}), 404
    gag_lib.patterns = [x for x in gag_lib.patterns if x.id != gag_id]
    gag_lib._save()
    return jsonify({"ok": True})


@app.route("/api/themes/<theme_id>/toggle", methods=["POST"])
def theme_toggle(theme_id):
    e = theme_lib.get_by_id(theme_id)
    if not e: return jsonify({"ok": False, "error": "not found"}), 404
    e.enabled = not e.enabled
    theme_lib._save()
    return jsonify({"ok": True, "enabled": e.enabled})


@app.route("/api/themes/<theme_id>/delete", methods=["POST"])
def theme_delete(theme_id):
    e = theme_lib.get_by_id(theme_id)
    if not e: return jsonify({"ok": False, "error": "not found"}), 404
    theme_lib.entries = [x for x in theme_lib.entries if x.id != theme_id]
    theme_lib._save()
    return jsonify({"ok": True})


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


@app.route("/api/write/preview", methods=["POST"])
def write_preview():
    data = request.json
    plot_id = data.get("plot_id")
    profile_id = data.get("profile_id")
    variables = data.get("variables", {})
    chapter_num = data.get("chapter_num", 1)
    chapter_outline = data.get("chapter_outline", "")
    target_words = data.get("target_words", 3000)

    plot = plot_lib.get_by_id(plot_id)
    profile = profiles.get(profile_id) if profile_id else None

    wp = WritingPipeline(profile_manager=profiles)
    ctx = WritingContext(
        plot_template=plot, plot_variables=variables,
        chapter_num=chapter_num, chapter_outline=chapter_outline,
        style_profile=profile, target_words=target_words,
        de_ai=True,
    )
    ctx = wp.match_templates(chapter_outline, existing_ctx=ctx)
    system, user = wp.build_prompt(ctx)

    return jsonify({
        "system": system, "user": user,
        "plot_name": plot.name if plot else "",
        "gags": [g.name for g in ctx.gags],
        "themes": [t.name for t in ctx.themes],
    })


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
        ids = __import__('re').findall(r'"book_id"\s*:\s*"?(\d+)"?', r.text)
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
        ids = __import__('re').findall(r'fanqienovel\.com/page/(\d+)', r.text)
        results.append({"method": "bing_search", "status": r.status_code,
                        "found_ids": ids[:5]})
    except Exception as e:
        results.append({"method": "bing_search", "error": str(e)})

    # 方案D: 番茄分类页
    try:
        r = crawler.session.get("https://fanqienovel.com/category/1", timeout=15)
        ids = __import__('re').findall(r'book_id["\']?\s*[:=]\s*["\']?(\d+)', r.text)
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
            m = __import__('re').search(r'window\.__INITIAL_STATE__\s*=\s*({.+?});', r.text, __import__('re').DOTALL)
            if m:
                ssr = __import__('json').loads(m.group(1))
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
        from plugins import task_manager
        task_id = f"fetch_{novel.title}"
        task_manager.start(task_id, name="小说抓取", title=novel.title,
                          total=chapters, phase="搜索")
        task_manager.log(task_id, f"找到: {novel.title}", "success")

        evt_queue = _queue.Queue()

        def on_progress(phase, current, total, message):
            evt_queue.put(("progress", phase, current, total, message))
            # 同步更新全局任务管理器（日志由前端SSE事件处理，此处避免重复）
            if phase == "search":
                task_manager.progress(task_id, current, total, "搜索", message)
            elif phase == "download":
                task_manager.progress(task_id, current, total, "下载", message)
            elif phase == "analysis_done":
                task_manager.progress(task_id, 0, 1, "完成", "分析完成")

        def worker():
            try:
                novel_info, dl_info = scout.fetch_novel(novel.title, chapters, on_progress=on_progress)
                task_manager.done(task_id, f"下载完成 {dl_info['chapters']}章")
                evt_queue.put(("fetch_done", {"novel_info": novel_info, "dl_info": dl_info}))
            except Exception as e:
                import traceback
                err_msg = str(e)
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

    resp = Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )
    return resp


# ─── 入库（人工筛选后） ───

@app.route("/api/scout/ingest", methods=["POST"])
def scout_ingest():
    """入库选中的分析结果"""
    from plugins.fanqie_scout import FanqieScoutAgent
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

    scout = FanqieScoutAgent(llm, plot_lib, struct_lib, gag_lib, theme_lib)
    stats = scout.ingest_selected(
        plots=plots, structures=structures,
        gags=gags, themes=themes, source="fanqie",
    )

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
    """分析已下载的小说"""
    from plugins.novel_storage import load_novel
    from plugins.style_analyzer import extract_writing_style
    from plugins.fanqie_scout import FanqieScoutAgent, NovelAnalyzer

    data = request.json or {}
    platform = data.get("platform", "")
    folder = data.get("folder", "")
    profile_id = data.get("profile_id", "")

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

    from libraries.plot import PlotTemplate
    from pathlib import Path

    # Step 1: 分析四大库
    analyzer = NovelAnalyzer(llm)
    analysis = analyzer.analyze_book(
        type("obj", (object,), {
            "title": info.get("title", ""),
            "genre": info.get("genre", ""),
            "sub_genre": "",
            "chapter_count": len(chapters),
        })(),
        chapters
    )

    # Step 2: 分析写作风格
    style = extract_writing_style(llm, info.get("title", ""), chapters)

    # 如果指定了笔名，自动生成风格档案
    profile_ready = False
    if profile_id and profiles.get(profile_id):
        profile = profiles.get(profile_id)
        style_words = style.get("common_words", [])
        avoid_words = style.get("avoid_words", [])
        if style_words or avoid_words:
            wp = profile.word_print or {}
            wp["common_words"] = list(set(wp.get("common_words", []) + style_words))
            wp["avoid_words"] = list(set(wp.get("avoid_words", []) + avoid_words))
            profile.word_print = wp
            profile.save()
            profile_ready = True

    # 标记已分析
    novel_path = Path(__file__).parent.parent / "storage" / "novels" / platform / folder
    (novel_path / ".analyzed").touch()

    return jsonify({
        "ok": True,
        "title": info.get("title", ""),
        "plot_details": analysis.get("plots", []),
        "structure_details": analysis.get("structures", []),
        "gag_details": analysis.get("gags", []),
        "theme_details": analysis.get("themes", []),
        "writing_style": style,
        "profile_updated": profile_ready,
    })


@app.route("/api/status/tasks")
def status_tasks():
    """返回当前运行中的任务列表（供右侧状态栏轮询）"""
    from plugins import task_manager
    tasks = task_manager.get_tasks()
    return jsonify(tasks)


@app.route("/api/status/tasks/close", methods=["POST"])
def status_tasks_close():
    """关闭/删除指定任务"""
    from plugins import task_manager
    data = request.get_json()
    task_id = data.get("id", "")
    if task_id:
        task_manager.remove(task_id)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "missing id"})


@app.route("/settings")
def settings_page():
    """设置页面 — 纯静态渲染，不发起 API 请求"""
    api_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api.json")
    cfg = {}
    if os.path.exists(api_path):
        with open(api_path, encoding="utf-8") as f:
            cfg = json.load(f)

    # 不调用 LLM API，只检查本地配置是否存在（瞬间完成）
    api_configured = bool(cfg.get("api_key") and cfg.get("base_url"))

    return render_template("settings.html",
        config={
            "api_key": cfg.get("api_key", ""),
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
    cfg = {}
    if os.path.exists(api_path):
        with open(api_path, encoding="utf-8") as f:
            cfg = json.load(f)

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

    api_cfg = APIConfig(
        api_key=data.get("api_key", ""),
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


@app.route("/api/engine/status")
def engine_status():
    """全局引擎状态"""
    books = book_mgr.list_all()
    status = []
    for b in books:
        cost_path = f"books/{b.book_id}/cost.json"
        cost = CostTracker.load(cost_path) if os.path.exists(cost_path) else CostTracker()
        status.append({
            "book_id": b.book_id, "title": b.title,
            "pen_name": b.pen_name, "genre": b.genre,
            "current_chapter": b.current_chapter,
            "total_chapters": b.chapter_count,
            "status": b.status,
            "cost": round(cost.spent, 2),
        })
    return jsonify(status)
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



if __name__ == "__main__":
    os.makedirs("ui/templates", exist_ok=True)
    os.makedirs("ui/static", exist_ok=True)
    print("NovelEngine Web UI v2.0: http://localhost:58080")
    app.run(host="0.0.0.0", port=58080, debug=True)
