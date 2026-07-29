"""
NovelEngine — 完整 Web UI v2.0 (Flask + Jinja2)
引擎集成版：新书启动 / 续写 / 管理面板
"""
from flask import Flask, render_template, request, jsonify, redirect, url_for, Response, stream_with_context
import sys, os, json, threading
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

        def send_event(event, d):
            return f"data: {_json.dumps({'event': event, **d}, ensure_ascii=False)}\n\n"

        try:
            yield send_event("start", {"title": title or direct_id, "chapters": chapters})

            # 搜索或直接用ID
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

            yield send_event("found", {
                "title": novel.title, "author": novel.author,
                "genre": novel.genre, "chapters": novel.chapter_count,
                "words": novel.word_count,
            })

            result = scout.scout_single_book(novel.title, chapters)

            yield send_event("progress", {
                "phase": "download", "current": result.downloaded_chapters,
                "total": result.downloaded_chapters,
                "message": f"downloaded {result.downloaded_chapters} chapters",
            })
            yield send_event("progress", {
                "phase": "analyze", "current": 1, "total": 1, "message": "analyzing...",
            })
            yield send_event("progress", {
                "phase": "ingest", "current": 1, "total": 1, "message": "ingesting...",
            })

            yield send_event("done", {
                "title": novel.title,
                "downloaded_chapters": result.downloaded_chapters,
                "plots": len(result.new_plots),
                "structures": len(result.new_structures),
                "gags": len(result.new_gags),
                "themes": len(result.new_themes),
                "plot_details": [{"name": p.get("name",""), "category": p.get("category",""),
                    "description": p.get("description",""), "structure": p.get("structure","")}
                    for p in result.new_plots],
                "gag_details": [{"name": g.get("name",""), "category": g.get("category",""),
                    "description": g.get("pattern_description",""), "examples": g.get("examples",[])}
                    for g in result.new_gags],
                "structure_details": [{"name": s.get("name",""), "description": s.get("description",""),
                    "stages": [st.get("name","") for st in s.get("stages",[])]}
                    for s in result.new_structures],
                "theme_details": [{"name": t.get("name",""), "description": t.get("description",""),
                    "techniques": t.get("techniques",t.get("writing_tips",[]))}
                    for t in result.new_themes],
            })
        except Exception as e:
            yield send_event("error", {"message": str(e)})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ═══════════════════════════════════════
# ⚙️ 设置页
# ═══════════════════════════════════════

@app.route("/settings")
def settings_page():
    """设置页面"""
    api_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api.json")
    cfg = {}
    if os.path.exists(api_path):
        with open(api_path, encoding="utf-8") as f:
            cfg = json.load(f)

    # 检查 LLM 连接状态
    from core.llm_client import LLMClient
    from core.models import APIConfig
    llm_ok = False
    try:
        llm = get_llm()
        if llm:
            # 简单 ping
            models = llm.client.models.list() if hasattr(llm.client, 'models') else None
            llm_ok = True
    except Exception:
        llm_ok = False

    return render_template("settings.html",
        config={
            "api_key": cfg.get("api_key", ""),
            "base_url": cfg.get("base_url", "https://api.deepseek.com"),
            "model": cfg.get("model", "deepseek-chat"),
            "http_timeout_seconds": cfg.get("http_timeout_seconds", 300),
            "context_budget_tokens": cfg.get("context_budget_tokens", 300000),
            "url_strict": cfg.get("url_strict", False),
        },
        llm_ok=llm_ok,
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
        # 发一条简单的请求确认连接
        result = client.chat_completion([
            {"role": "user", "content": "回复 'ok'"}
        ], max_tokens=10, temperature=0)
        return jsonify({"ok": True, "model": api_cfg.model,
                        "response": result.get("content", "")[:50]})
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


if __name__ == "__main__":
    os.makedirs("ui/templates", exist_ok=True)
    os.makedirs("ui/static", exist_ok=True)
    print("NovelEngine Web UI v2.0: http://localhost:58080")
    app.run(host="0.0.0.0", port=58080, debug=True)
