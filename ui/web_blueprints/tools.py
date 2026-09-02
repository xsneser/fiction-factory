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
    """提取工作台：从 /scout 书库点书卡跳入（?platform=&folder= 直达书目），
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
    """启动侦察任务（后台线程执行，立即返回；进度/状态走 crawl_progress.json 轮询，
    暂停/继续/停止走 /api/scout/fetch/control）。"""
    from plugins import task_manager
    from libraries.crawl_progress import write_crawl_progress

    data = request.json or {}
    title = data.get("title", "").strip()
    # chapters<=0（默认）= 全文下载；书已下载则只补新章节（增量）。仅传正数时才按章号区间下载。
    chapters = int(data.get("chapters", 0) or 0)
    start_chapter = int(data.get("start_chapter", 1) or 1)
    end_chapter = int(data.get("end_chapter", 0) or 0)
    direct_id = data.get("book_id", "").strip()
    # 综合抓取（默认）：番茄元数据+权威目录 + 镜像站全文，统一书库一本；
    # 显式 platform=fanqie / web 走单源（高级用）
    platform = (data.get("platform", "") or "merged").strip() or "merged"
    raw_site = (data.get("site", "") or "").strip()   # 用户显式下载源；空/auto=服务端自动择优
    if raw_site == "auto":
        raw_site = ""
    url = data.get("url", "").strip()
    download_delay = float(data.get("download_delay", 0.5) or 0.5)
    # mirrors：勾选的下载源（仅综合抓取时限制探测集合；空 = 由服务端自动全部探测）
    mirrors = data.get("mirrors") or []
    if not isinstance(mirrors, list):
        mirrors = []
    mirrors = [str(m).strip() for m in mirrors if str(m).strip()]

    # 区间归一化：前端三模式统一传 start/end 真实章号；番茄路径 chapters<=0 会走全文/增量而非区间，
    # 故当给定 end>=1 且未显式给 chapters 时折算 chapters（merged/web 以 end_chapter 为准，互不影响）。
    if end_chapter >= 1 and chapters <= 0:
        if end_chapter < start_chapter:
            return jsonify({"error": f"结束章 {end_chapter} 小于起始章 {start_chapter}"}), 400
        chapters = end_chapter - start_chapter + 1

    if platform == "web":
        if not url and not direct_id:
            return jsonify({"error": "请输入网页书籍 URL 或 book_id"}), 400
    elif platform == "fanqie":
        if not title and not direct_id:
            return jsonify({"error": "请输入书名或 book_id"}), 400
    else:
        platform = "merged"
        if not title and not url and not direct_id:
            return jsonify({"error": "请输入书名 / 番茄 book_id / 镜像站 URL"}), 400

    # site：web 需要具体镜像站（默认 wodushu）；merged/fanqie 留空 → merged 内部自动择优
    site = raw_site or ("wodushu" if platform == "web" else "")

    # 单任务互斥 + 任务前置注册：搜索阶段即可暂停/停止
    task_manager.ensure_single("小说抓取")
    if platform == "web":
        task_id = f"web_{site}_{direct_id or url}"
    elif platform == "merged":
        task_id = f"merge_{direct_id or url or title}"
    else:
        task_id = f"fetch_{title or direct_id}"
    task_manager.start(task_id, name="小说抓取", title=title or direct_id,
                       total=1, phase="搜索", url="/scout")
    task_manager.register_cancel(task_id)
    _task_title = (title or direct_id or url) or "抓取任务"
    # 顶部显示标题：提交的是 book_id/URL 时，解析番茄成功后由 on_step 换成书名
    _tname = {"title": _task_title}
    write_crawl_progress("running", "搜索", 0, 1, "开始搜索...",
                         task_id=task_id, title=_tname["title"], extra={"pausable": True})

    def worker():
        from plugins.fanqie_scout import FanqieScoutAgent
        llm = get_llm() or None   # 下载是纯规则操作，不强制 LLM 配置
        scout = FanqieScoutAgent(llm, plot_lib, struct_lib, gag_lib)
        _cancel_exception = Exception("__CANCELLED__")
        _extra = {"pausable": True}

        _state = {"phase": "", "cur": 0, "total": 0}   # 供 on_step 复用当前进度条位置（防跳 0）

        def on_progress(phase, current, total, message):
            _state.update(phase=phase, cur=current, total=total)
            # 检查取消：如果被取消了就抛异常，让 worker catch 住
            if task_manager.is_cancelled(task_id):
                raise _cancel_exception
            # 暂停：在章节边界等待直到恢复或取消
            while task_manager.is_paused(task_id):
                if task_manager.is_cancelled(task_id):
                    raise _cancel_exception
                time.sleep(0.5)
            write_crawl_progress("running", phase, current, total, message,
                                 task_id=task_id, title=_tname["title"], extra=_extra)
            if phase == "search":
                task_manager.progress(task_id, current, total, "搜索", message)
                task_manager.log(task_id, message, "info")
            elif phase == "download":
                task_manager.progress(task_id, current, total, "下载", message)
                task_manager.log(task_id, message, "info")

        def on_step(label, status="running", detail=""):
            # 分步清单：写 crawl_progress steps + 侧栏 log（取消/暂停与 on_progress 同检查）
            if task_manager.is_cancelled(task_id):
                raise _cancel_exception
            while task_manager.is_paused(task_id):
                if task_manager.is_cancelled(task_id):
                    raise _cancel_exception
                time.sleep(0.5)
            # 解析番茄成功后：顶部标题换成书名（detail 形如「冒姓琅琊 · 封面/简介/目录 N 章」）
            if label == "解析番茄" and detail:
                _nm = detail.split(" · ")[0].strip()
                if _nm:
                    _tname["title"] = _nm
            write_crawl_progress("running", _state["phase"], _state["cur"], _state["total"],
                                 detail or label, task_id=task_id, title=_tname["title"],
                                 extra=_extra,
                                 step={"label": label, "status": status, "detail": detail})
            task_manager.log(task_id, f"{label} {detail}".strip(), "info")

        try:
            if platform == "merged":
                # 综合抓取（默认）：番茄元数据+权威目录 + 镜像站全文 → 统一书库一本
                from plugins.book_fetch import download_book_merged
                # mirrors：勾选的下载源 → 只探测这些源（缺章在勾选源间补缺）；空列表=全部自动
                info, dl = download_book_merged(
                    title=title, url=url, book_id=direct_id, site=site,
                    mirrors=mirrors,
                    chapters=chapters, start_chapter=start_chapter, end_chapter=end_chapter,
                    download_delay=download_delay, on_progress=on_progress,
                    on_step=on_step)
                if not task_manager.is_cancelled(task_id):
                    n = dl["chapters"]
                    msg = (f"已是最新（{dl.get('skipped', 0)} 章）" if dl.get("already")
                           else f"下载完成 {n}章")
                    task_manager.done(task_id, msg)
                    # 顶部标题以解析出的书名为准（兜底：未走 on_step 的路径也用 info title）
                    _tname["title"] = (info.get("title") if info else "") or _tname["title"]
                    # site 用实际服务源（用户选源可能解析失败回退自动），非请求时写死的默认源
                    _actual_site = (info.get("site") if info else None) or site or "wodushu"
                    write_crawl_progress("done", "download", n, n, msg,
                                         task_id=task_id, title=_tname["title"],
                                         extra={**_extra, "folder": dl.get("folder", ""),
                                                "platform": "merged", "site": _actual_site})
                return
            if platform == "web":
                # 网页镜像站（如 wodushu）：download_webnovel 内部已按站点适配器
                # 解析书→章表→逐章下载落盘；on_progress 在章边界检查暂停/取消
                from plugins.webnovel_scraper import download_webnovel
                info, dl = download_webnovel(
                    site=site, url=url or direct_id, chapters=chapters,
                    start_chapter=start_chapter, end_chapter=end_chapter,
                    download_delay=download_delay, on_progress=on_progress, platform="web")
                if not task_manager.is_cancelled(task_id):
                    n = dl["chapters"]
                    msg = (f"已是最新（{dl.get('skipped', 0)} 章）" if dl.get("already")
                           else f"下载完成 {n}章")
                    task_manager.done(task_id, msg)
                    write_crawl_progress("done", "download", n, n, msg,
                                         task_id=task_id, title=_tname["title"],
                                         extra={**_extra, "folder": dl.get("folder", ""),
                                                "platform": "web", "site": site})
                return
            # 搜索阶段（番茄）
            if direct_id:
                novel = scout.crawler._get_novel_from_page(direct_id)
            else:
                novel = scout.crawler.search_novel(title)
            if not novel:
                task_manager.fail(task_id, "未找到该书")
                write_crawl_progress("error", "", 0, 0, f"not found: {title or direct_id}",
                                     task_id=task_id, title=_tname["title"], extra=_extra)
                return
            task_manager.log(task_id, f"找到: {novel.title}", "success")

            _, dl_info = scout.fetch_novel(
                novel.title, chapters, start_chapter=start_chapter,
                end_chapter=end_chapter, on_progress=on_progress)
            # 如果没有被取消才标记完成
            if not task_manager.is_cancelled(task_id):
                task_manager.done(task_id, f"下载完成 {dl_info['chapters']}章")
                write_crawl_progress("done", "download", dl_info["chapters"], dl_info["chapters"],
                                     f"下载完成 {dl_info['chapters']}章",
                                     task_id=task_id, title=_tname["title"],
                                     extra={**_extra, "folder": dl_info.get("folder", ""),
                                            "platform": "fanqie"})
        except Exception as e:
            err_msg = str(e)
            # 取消导致的：不报错，写终态后结束（防止 worker 在途 on_progress 覆盖成 running）
            if err_msg == "__CANCELLED__":
                task_manager.cancel(task_id)
                write_crawl_progress("cancelled", "", 0, 0, "已停止",
                                     task_id=task_id, title=_tname["title"], extra=_extra)
                return
            # 翻译常见异常为用户友好提示
            if "NoneType" in err_msg and "subscriptable" in err_msg:
                err_msg = "页面数据解析失败，番茄页面结构可能已变更，请等待插件更新"
            elif "timeout" in err_msg.lower() or "timed out" in err_msg.lower():
                err_msg = "网络请求超时，请检查网络连接或稍后重试"
            elif "Connection" in err_msg:
                err_msg = "网络连接失败，请检查网络"
            task_manager.fail(task_id, err_msg)
            write_crawl_progress("error", "", 0, 0, err_msg,
                                 task_id=task_id, title=_tname["title"], extra=_extra)

    threading.Thread(target=worker, daemon=True, name="scout-fetch").start()
    return jsonify({"ok": True, "task_id": task_id})


@bp.route("/api/crawl/progress", methods=["GET"])
def crawl_progress():
    """抓取实时进度（/scout 页轮询）：{state: running|done|error, phase, current,
    total, message, ts}。web 表单与 MCP fetch_novel 工具共用 crawl_progress.json。"""
    from libraries.crawl_progress import read_crawl_progress
    return jsonify({"ok": True, **read_crawl_progress()})


@bp.route("/api/scout/fetch/control", methods=["POST"])
def scout_fetch_control():
    """控制「小说抓取」任务（多任务并行）：pause 暂停 / resume 继续 / cancel 停止。

    请求体可带 task_id（前端按进度行传，指定控制哪个任务）；缺省回退「running 的『小说抓取』」。
    /scout 页每个任务行「⏸ / ▶ / ⏹」按钮调此端点，UI 仍由 crawl-progress 轮询接棒。"""
    from plugins import task_manager
    from libraries.crawl_progress import write_crawl_progress
    data = request.json or {}
    action = data.get("action", "")
    if action not in ("pause", "resume", "cancel"):
        return jsonify({"ok": False, "error": "未知操作"}), 400
    tid = (data.get("task_id") or "").strip()
    if not tid:
        for t in task_manager.get_tasks():
            if t.get("name") == "小说抓取" and t.get("status") == "running":
                tid = t.get("id")
                break
    if not tid:
        return jsonify({"ok": False, "error": "没有进行中的抓取任务"}), 404
    t = task_manager.get(tid) or {}
    cur = t.get("current", 0) or 0
    total = t.get("total", 0) or 0
    phase = t.get("phase", "") or ""
    _extra = {"pausable": True}
    _title = t.get("title", "") or tid
    if action == "pause":
        task_manager.pause(tid)
        write_crawl_progress("paused", phase, cur, total, "已暂停",
                             task_id=tid, title=_title, extra=_extra)
    elif action == "resume":
        task_manager.resume(tid)
        write_crawl_progress("running", phase, cur, total, "已恢复下载",
                             task_id=tid, title=_title, extra=_extra)
    else:  # cancel
        task_manager.cancel(tid)
        write_crawl_progress("cancelled", phase, cur, total, "已停止",
                             task_id=tid, title=_title, extra=_extra)
    return jsonify({"ok": True, "action": action})


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


@bp.route("/api/scout/sources")
def scout_sources():
    """可用镜像下载源清单（静态，不探测）：{ok, sources: [{key, name, js_render}]}。
    供 /scout 抓取表单「下载源」下拉填充；源来自 MIRROR_SOURCES（多源注册表）+ SITES（名称）。
    js_render=True 表示正文由 JS 填充，需无头浏览器渲染（如 hushuge/piaofeige），耗时较长。"""
    try:
        from plugins.webnovel_scraper import MIRROR_SOURCES, SITES
        return jsonify({"ok": True, "sources": [
            {"key": k, "name": (SITES.get(k, {}) or {}).get("name") or k,
             "js_render": bool((SITES.get(k, {}) or {}).get("content_render"))}
            for k in MIRROR_SOURCES]})
    except Exception:
        return jsonify({"ok": False, "sources": []})


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
    from libraries.style_rules import StyleRule, StyleRuleLibrary
    data = request.json or {}
    title = data.get("title", "")
    plots = data.get("plots", [])
    structures = data.get("structures", [])
    gags = data.get("gags", [])
    characters = data.get("characters", [])
    style_rules_in = data.get("style_rules", [])
    profile_id = data.get("profile_id", "")   # 风格规则归属笔名（空则落默认笔名）

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
    return jsonify(novels)


@bp.route("/api/scout/novels/cover")
def scout_novel_cover():
    """已下载小说本地封面（统一书库 storage/novels/<folder>/cover.jpg）。"""
    from flask import send_file
    from plugins.novel_storage import NOVELS_DIR
    folder = request.args.get("folder", "").strip()
    if not folder:
        return ("", 404)
    cover_file = NOVELS_DIR / folder / "cover.jpg"
    if not cover_file.is_file():
        return ("", 404)
    return send_file(str(cover_file), mimetype="image/jpeg")


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

