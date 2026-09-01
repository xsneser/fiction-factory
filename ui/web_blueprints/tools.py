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
# 🔍 侦察 · 提取（合并页 /scout：侦察抓取 + 外部书库；提取工作台 /extract 承接 set_review 与入库）
# ═══════════════════════════════════════

@bp.route("/scout")
def scout_page():
    """合并页·侦察书库：热榜侦察 + 按书名/book_id 下载章节到本地书库 + 实时进度 + 已下载书库
    （书卡点击 → /extract 提取）"""
    return render_template("scout.html", profiles=profiles.list_all())


@bp.route("/extract")
def extract_page():
    """提取工作台：从 /scout 书库点书卡跳入（?platform=&folder= 自动选中或页内下拉），
    显示该书 + 提取按钮，agent 提炼五类候选经 set_review 呈现 → 勾选确认入库五库"""
    return render_template("extract.html", profiles=profiles.list_all())


@bp.route("/novels")
def novels_page():
    """外部书库已并入 /scout 合并页；保留路由作兼容别名（阅读器返回链接/书签），302 跳转"""
    return redirect(url_for("tools.scout_page"))


@bp.route("/novels/read")
def novel_reader_page():
    """外部书库阅读器：只注入章节列表（index/title/字数），正文按章懒加载。"""
    from plugins.novel_storage import load_novel
    folder = request.args.get("folder", "").strip()
    platform = (request.args.get("platform", "") or "fanqie").strip()
    if not folder:
        return redirect(url_for("tools.novels_page"))
    data = load_novel(platform, folder)
    if not data:
        return redirect(url_for("tools.novels_page"))
    info = data["info"]
    chapter_list = [{"index": c.get("index"), "title": c.get("title", ""),
                     "word_count": c.get("word_count", 0)} for c in data["chapters"]]
    meta = {"platform": platform, "folder": folder, "title": info.get("title", folder)}
    return render_template("novel_reader.html", meta=meta, chapter_list=chapter_list)


@bp.route("/api/scout/run", methods=["POST"])
def scout_run():
    """启动侦察任务"""
    from plugins.fanqie_scout import FanqieScoutAgent

    data = request.json or {}
    title = data.get("title", "").strip()
    chapters = int(data.get("chapters", 30))
    start_chapter = int(data.get("start_chapter", 1) or 1)
    end_chapter = int(data.get("end_chapter", 0) or 0)
    direct_id = data.get("book_id", "").strip()

    if not title and not direct_id:
        return jsonify({"error": "请输入书名或 book_id"}), 400

    # 下载是纯规则操作（FanqieScoutAgent.fetch_novel 仅下载不分析），不强制 LLM 配置
    llm = get_llm() or None

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
                novel_info, dl_info = scout.fetch_novel(
                    novel.title, chapters, start_chapter=start_chapter,
                    end_chapter=end_chapter, on_progress=on_progress)
                # 如果没有被取消才标记完成
                if not task_manager.is_cancelled(task_id):
                    task_manager.done(task_id, f"下载完成 {dl_info['chapters']}章")
                    write_crawl_progress("done", "download", dl_info["chapters"], dl_info["chapters"],
                                         f"下载完成 {dl_info['chapters']}章",
                                         extra={"folder": dl_info.get("folder", ""), "platform": "fanqie"})
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


# ─── 热榜侦察：多平台注册表分发 + 后台线程拉取 + storage/hot_cache.json 缓存 ───
_HOT_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "storage", "hot_cache.json")
_HOT_TTL = 600          # 热榜缓存有效秒数（10 分钟）
_HOT_SCHEMA = 2         # 条目模型版本（1=无 cover，2=含 cover）；旧 schema 缓存视为未命中重拉
_hot_fetching = {}      # cache_key(platform:gender:key) -> Thread，防同键并发重复拉取


def _read_hot_cache() -> dict:
    return read_json(_HOT_CACHE_PATH, {}) or {}


def _write_hot_cache(cache: dict) -> None:
    write_json_atomic(_HOT_CACHE_PATH, cache)


def _spawn_hot_fetch(cache_key: str, platform: str, key: str, gender: str, count: int) -> None:
    """后台拉取热榜写入缓存；同键已有在跑线程则跳过（单线程 Flask 下不阻塞请求）。"""
    if _hot_fetching.get(cache_key) and _hot_fetching[cache_key].is_alive():
        return

    def fetch():
        try:
            from plugins.hot_ranks import discover as hot_discover
            items = hot_discover(platform, key=key, count=count, gender=gender) or []
            cache = _read_hot_cache()
            cache[cache_key] = {"ts": time.time(), "schema": _HOT_SCHEMA, "novels": items}
            _write_hot_cache(cache)
        except Exception as e:
            logging.getLogger("tools").warning(
                "热榜拉取失败(platform=%s key=%s): %s", platform, key, e)
        finally:
            _hot_fetching.pop(cache_key, None)

    t = threading.Thread(target=fetch, daemon=True, name=f"hot-{cache_key[:24]}")
    _hot_fetching[cache_key] = t
    t.start()


@bp.route("/api/scout/hot")
def scout_hot():
    """多平台热榜。platform 默认 fanqie；key 为榜单分类 id 或题材中文名（空/'全部'=聚合）；
    gender male/female；count 默认 10。

    返回 {ok, platform, key, novels, ts, refreshing|loading}：缓存 TTL 内直接回缓存；
    过期/缺失立即返回当前状态并起 daemon 线程后台刷新（页面轮询直到 novels 出现）。
    novels 每项 {platform, rank, book_id, title, author, category, word_count,
    chapter_count, hot_score, intro, url}（统一 HotRankItem，见 plugins/hot_ranks.py）。
    """
    platform = (request.args.get("platform") or "fanqie").strip()
    genre = (request.args.get("genre") or "").strip()      # 兼容旧前端：题材中文名
    key = (request.args.get("key") or genre).strip()       # key 优先，回退 genre
    gender = (request.args.get("gender") or "male").strip()
    try:
        count = min(int(request.args.get("count", 10) or 10), 50)
    except (TypeError, ValueError):
        count = 10
    cache = _read_hot_cache()
    cache_key = f"{platform}:{gender}:{key or '__all__'}"
    entry = cache.get(cache_key) or {}
    now = time.time()
    # schema 不匹配视为未命中：条目模型升级（如加 cover）后旧缓存强制重拉
    if (entry and now - entry.get("ts", 0) < _HOT_TTL
            and entry.get("schema") == _HOT_SCHEMA):
        return jsonify({"ok": True, "platform": platform, "key": key,
                        "novels": entry.get("novels", []), "ts": entry.get("ts")})
    payload = {"ok": True, "platform": platform, "key": key}
    if entry:
        payload.update({"novels": entry.get("novels", []), "ts": entry.get("ts"),
                        "refreshing": True})
    else:
        payload.update({"novels": [], "loading": True})
    _spawn_hot_fetch(cache_key, platform, key, gender, count)
    return jsonify(payload)


@bp.route("/api/scout/hot/rankings")
def scout_hot_rankings():
    """榜单/分类清单（供 UI 下拉 / 未来动态 chips）。platform 默认 fanqie；gender male/female。
    返回 {ok, platform, gender, rankings: [{id, name}]}（缓存 10min）。"""
    platform = (request.args.get("platform") or "fanqie").strip()
    gender = (request.args.get("gender") or "male").strip()
    cache = _read_hot_cache()
    cache_key = f"rankings:{platform}:{gender}"
    entry = cache.get(cache_key) or {}
    now = time.time()
    if (entry and now - entry.get("ts", 0) < _HOT_TTL
            and entry.get("schema") == _HOT_SCHEMA):
        return jsonify({"ok": True, "platform": platform, "gender": gender,
                        "rankings": entry.get("rankings", [])})
    from plugins.hot_ranks import list_rankings as hr_list_rankings
    rankings = hr_list_rankings(platform, gender=gender) or []
    cache[cache_key] = {"ts": time.time(), "schema": _HOT_SCHEMA, "rankings": rankings}
    _write_hot_cache(cache)
    return jsonify({"ok": True, "platform": platform, "gender": gender, "rankings": rankings})


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


# ─── 已下载小说列表 ───

@bp.route("/api/scout/novels")
def scout_novels():
    """列出已下载的小说（/novels 列表 + /extract 书下拉共用）"""
    from plugins.novel_storage import list_novels
    platform = request.args.get("platform", "")
    novels = list_novels(platform)
    # 标记是否已分析
    for n in novels:
        from pathlib import Path
        analyzed_file = Path(n["path"]) / ".analyzed"
        n["analyzed"] = analyzed_file.exists()
    return jsonify(novels)


# ─── 候选审查快照（agent set_review 持久化，/extract 轮询恢复） ───

@bp.route("/api/scout/novels/delete", methods=["POST"])
def scout_novels_delete():
    """删除已下载小说（storage/novels/<platform>/<folder>/）。"""
    from plugins.novel_storage import delete_novel
    data = request.json or {}
    platform = data.get("platform") or "fanqie"
    folder = data.get("folder", "")
    if not folder:
        return jsonify({"ok": False, "error": "缺少 folder"}), 400
    ok = delete_novel(platform, folder)
    if not ok:
        return jsonify({"ok": False, "error": "未找到该小说"}), 404
    return jsonify({"ok": True, "deleted": folder})


@bp.route("/api/scout/novels/chapter")
def scout_novel_chapter():
    """读已下载小说单章正文（按真实章号 index，供阅读器懒加载）。"""
    from plugins.novel_storage import read_chapter
    folder = request.args.get("folder", "").strip()
    platform = (request.args.get("platform", "") or "fanqie").strip()
    try:
        chapter = int(request.args.get("chapter", 0))
    except (TypeError, ValueError):
        chapter = 0
    if not folder or chapter <= 0:
        return jsonify({"ok": False, "error": "缺 folder 或 chapter"}), 400
    ch = read_chapter(platform, folder, chapter)
    if not ch:
        return jsonify({"ok": False, "error": f"第 {chapter} 章不存在"}), 404
    return jsonify({"ok": True, "chapter": ch})

