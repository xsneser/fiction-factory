"""
NovelEngine — 完整 Web UI (Flask + Jinja2)
管理面板：书库 / 桥段库 / 写作台 / 笔名 / 审查 / 设置
"""
from flask import Flask, render_template, request, jsonify, redirect, url_for
import sys, os
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

app = Flask(__name__, template_folder="templates", static_folder="static")

# ─── 全局服务 ───
plot_lib = PlotLibrary()
struct_lib = StructureLibrary()
gag_lib = GagLibrary()
theme_lib = ThemeLibrary()
profiles = ProfileManager("profiles")
book_mgr = BookManager("books")

# ─── 首页 Dashboard ───
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
    )

# ─── 书库 ───
@app.route("/books")
def books():
    return render_template("books.html", books=book_mgr.list_all())

@app.route("/books/new", methods=["GET","POST"])
def new_book():
    if request.method == "POST":
        cfg = book_mgr.create(
            title=request.form["title"],
            pen_name=request.form["pen_name"],
            genre=request.form["genre"],
            sub_genre=request.form.get("sub_genre",""),
            platform=request.form.get("platform","fanqie"),
            chapter_count=int(request.form.get("chapter_count",500)),
            structure_template_id=request.form.get("structure_id",""),
            style_profile_id=request.form.get("profile_id",""),
        )
        return redirect(url_for("book_detail", book_id=cfg.book_id))
    return render_template("new_book.html",
        pen_names=profiles.list_all(),
        structures=struct_lib.templates,
    )

@app.route("/books/<book_id>")
def book_detail(book_id):
    book = book_mgr.get(book_id)
    if not book: return "Not found", 404
    outline = book_mgr.get_outline(book_id)
    chapters = []
    for n in range(1, book.current_chapter + 2):
        ch = book_mgr.load_chapter(book_id, n)
        if ch: chapters.append(ch)
    cost = CostTracker.load(f"books/{book_id}/cost.json")
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

# ─── 桥段库 ───
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

# ─── 大纲库 ───
@app.route("/structures")
def structures():
    return render_template("structures.html",
        templates=struct_lib.templates)

# ─── 笑点库 ───
@app.route("/gags")
def gags():
    return render_template("gags.html", patterns=gag_lib.patterns)

# ─── 内涵库 ───
@app.route("/themes")
def themes():
    return render_template("themes.html", entries=theme_lib.entries)

# ─── 笔名档案 ───
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

# ─── 写作台 ───
@app.route("/write")
def write():
    books = book_mgr.list_all()
    return render_template("write.html", books=books,
        plots=plot_lib.templates, profiles=profiles.list_all())

@app.route("/api/write/preview", methods=["POST"])
def write_preview():
    """预览写作 prompt（不调用 LLM）"""
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

# ─── 去AI味测试 ───
@app.route("/deai", methods=["GET","POST"])
def deai_test():
    result = None
    if request.method == "POST":
        engine = DeAIEngine()
        text = request.form["text"]
        result = engine.process_rule_based(text)
    return render_template("deai.html", result=result)

# ─── 审查测试 ───
@app.route("/review-test", methods=["GET","POST"])
def review_test():
    result = None
    if request.method == "POST":
        reviewer = ContentReviewer()
        text = request.form["text"]
        result = reviewer.review(text, chapter_num=1)
    return render_template("review_test.html", result=result)

# ─── 引擎状态 ───
@app.route("/api/engine/status")
def engine_status():
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
    print("🚀 NovelEngine Web UI: http://localhost:58080")
    app.run(host="0.0.0.0", port=58080, debug=True)
