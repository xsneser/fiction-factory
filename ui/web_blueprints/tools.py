"""工具（内容提取/小说抓取/去AI/审查测试） — 蓝图（自 ui/web_ui.py 按域拆分）。"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, Response, stream_with_context
from .ctx import *

bp = Blueprint("tools", __name__)

@bp.route("/write")
def write():
    """旧写作台 — 已废弃，重定向到书库"""
    return redirect(url_for("books.books"))


@bp.route("/deai", methods=["GET","POST"])
def deai_test():
    result = None
    if request.method == "POST":
        engine = DeAIEngine()
        text = request.form["text"]
        result = engine.process_rule_based(text)
    return render_template("deai.html", result=result)


@bp.route("/review-test", methods=["GET","POST"])
def review_test():
    result = None
    if request.method == "POST":
        reviewer = ContentReviewer()
        text = request.form["text"]
        result = reviewer.review(text, chapter_num=1)
    return render_template("review_test.html", result=result)


# ═══════════════════════════════════════
# 🔍 番茄侦察兵
@bp.route("/extract")
# ═══════════════════════════════════════

def extract_page():
    """内容提取页"""
    return render_template("extract.html")


@bp.route("/scout")
def scout_page():
    """侦察兵页面"""
    return render_template("scout.html")


@bp.route("/api/scout/run", methods=["POST"])
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

    scout = FanqieScoutAgent(llm, plot_lib, struct_lib, gag_lib)

    def generate():
        import json as _json
        import queue as _queue
        import threading as _threading
        from libraries.crawl_progress import write_crawl_progress

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
            # 共享进度文件（/scout 页轮询 /api/crawl/progress，与 MCP fetch_novel 工具同源）
            write_crawl_progress("running", phase, current, total, message)
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
                    write_crawl_progress("done", "download", dl_info["chapters"], dl_info["chapters"],
                                         f"下载完成 {dl_info['chapters']}章")
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
                write_crawl_progress("error", "", 0, 0, err_msg)
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


@bp.route("/api/crawl/progress", methods=["GET"])
def crawl_progress():
    """抓取实时进度（/scout 页轮询）：{state: running|done|error, phase, current,
    total, message, ts}。web 表单与 MCP fetch_novel 工具共用 crawl_progress.json。"""
    from libraries.crawl_progress import read_crawl_progress
    return jsonify({"ok": True, **read_crawl_progress()})


# ─── 入库（人工筛选后） ───

@bp.route("/api/scout/ingest", methods=["POST"])
def scout_ingest():
    """入库选中的分析结果（五库：桥段/弧/笑点/角色 + 风格规则按笔名）。

    纯规则落盘、不强制 LLM；agent 驱动链路上由 dsh 分析后经 set_review 呈现、
    用户在本页确认后 POST 到此端点落库。
    """
    from plugins.fanqie_scout import FanqieScoutAgent
    from plugins import task_manager
    from plugins.novel_storage import NOVELS_DIR
    from libraries.style_rules import StyleRule, StyleRuleLibrary
    data = request.json or {}
    title = data.get("title", "")
    plots = data.get("plots", [])
    structures = data.get("structures", [])
    gags = data.get("gags", [])
    characters = data.get("characters", [])
    style_rules_in = data.get("style_rules", [])
    profile_id = data.get("profile_id", "")   # 风格规则归属笔名（空则落默认笔名）
    platform = data.get("platform") or "fanqie"
    folder = data.get("folder", "")           # 落盘成功后可标记该小说 .analyzed

    if not any([plots, structures, gags, characters, style_rules_in]):
        return jsonify({"ok": False, "error": "参数为空"}), 400

    llm = get_llm() or None   # 入库是纯规则副作用，不强制 LLM 配置

    # 单任务互斥：资产入库同一时间只允许一个
    task_manager.ensure_single("资产入库")
    task_id = f"ingest_{title}_{int(time.time())}"
    task_manager.start(task_id, name="资产入库", title=title,
                       total=1, phase="入库中...", url="/scout")

    stats = {"plots": 0, "structures": 0, "gags": 0, "characters": 0, "style_rules": 0}

    # 四库：经 FanqieScoutAgent.ingest_selected（纯规则，含 char_lib）
    if any([plots, structures, gags, characters]):
        scout = FanqieScoutAgent(llm_client=llm, plot_lib=plot_lib, struct_lib=struct_lib,
                                 gag_lib=gag_lib, char_lib=char_lib)
        four = scout.ingest_selected(plots=plots, structures=structures, gags=gags,
                                     characters=characters, source="fanqie")
        stats.update(four)

    # 风格规则：按笔名直写 StyleRuleLibrary（复用 add_style_rule 范式，按 profile_id+kind+pattern 去重）
    if style_rules_in:
        srl = StyleRuleLibrary()
        existing = {r.id for r in srl.rules}
        pairs = {(r.profile_id, r.kind, r.pattern) for r in srl.rules}
        n = 1
        for sr in style_rules_in:
            kind = str(sr.get("kind", "")).strip()
            pattern = str(sr.get("pattern", "")).strip()
            if kind not in ("ban", "prefer") or not pattern:
                continue
            pid = profile_id or sr.get("profile_id") or ""
            if (pid, kind, pattern) in pairs:
                continue
            while f"{kind}_{n}" in existing:
                n += 1
            rule = StyleRule(id=f"{kind}_{n}", kind=kind, profile_id=pid,
                             pattern=pattern, desc=str(sr.get("desc", "")),
                             severity=str(sr.get("severity", "warning")),
                             replacements=[str(x) for x in (sr.get("replacements") or []) if str(x).strip()])
            srl.rules.append(rule)
            existing.add(rule.id)
            pairs.add((pid, kind, pattern))
            stats["style_rules"] += 1
        srl._save()

    # 落盘成功后标记该小说已提取（folder 对应 storage/novels/fanqie/<folder>/）
    if folder and any(stats.values()):
        novel_dir = NOVELS_DIR / platform / folder
        if novel_dir.is_dir():
            (novel_dir / ".analyzed").touch()

    task_manager.done(task_id, message=f"入库完成: +{stats['plots']}桥段 +{stats['structures']}大纲")
    task_manager.log(task_id, f"✅ 入库完成: +{stats['plots']}桥段 +{stats['structures']}大纲 "
                              f"+{stats['gags']}笑点 +{stats['characters']}角色 "
                              f"+{stats['style_rules']}风格规则", "success")

    return jsonify({
        "ok": True,
        "stats": stats,
        "message": (f"入库完成: +{stats['plots']}桥段 +{stats['structures']}大纲 "
                    f"+{stats['gags']}笑点 +{stats['characters']}角色 "
                    f"+{stats['style_rules']}风格规则"),
    })


# ═══════════════════════════════════════
# ⚙️ 设置页
@bp.route("/api/scout/novels")
# ═══════════════════════════════════════

# ─── 已下载小说列表 ───

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

@bp.route("/api/scout/analyze", methods=["POST"])
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
    task_manager.start(task_id, name="内容分析", title=title,
                       total=50, phase="准备中", url="/extract")
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
                        f"笑点: {len(analysis.get('gags',[]))}个", "success")
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


