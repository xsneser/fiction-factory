"""写作台引擎（按情节段撰写/续写） — 蓝图（自 ui/web_ui.py 按域拆分）。"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, render_template, render_template_string, request, jsonify, redirect, url_for, Response, stream_with_context
from .ctx import *
bp = Blueprint("desk", __name__)

# ═══════════════════════════════════════════
# ✍️ 写作台（按书列出，进入写作/续写）
@bp.route("/desk")
# ═══════════════════════════════════════════

def desk_list():
    """写作台已按书进入（书库每本书的「开始写作/继续写作」入口），
    /desk 无书上下文时直接引导回书库，避免空页面死胡同。"""
    return redirect(url_for("books.books"), 302)


@bp.route("/books/start/timeline/<timeline_id>/write")
def _compat_storyline_start_writing(timeline_id):
    """旧「从故事线启动写作」URL 兼容：规划书即正式书，「开始写作」统一走 /books/<id>/continue。"""
    return redirect(url_for("desk.continue_book_page", book_id=timeline_id), 302)


# ─── 兼容：旧 /books/timeline/write 与 /api/timeline-engine 前缀 ───

@bp.route("/books/timeline/write/<engine_id>")
def _compat_storyline_write_flow(engine_id):
    return redirect(url_for("desk.storyline_write_flow", engine_id=engine_id), 302)


@bp.route("/api/timeline-engine/<path:rest>", methods=["POST"])
def _compat_api_storyline_engine(rest):
    return redirect("/api/storyline-engine/" + rest, 307)


def _chapters_from_disk(book_id: str, current_chapter: int):
    """从磁盘构造「已写章节 + 进行中草稿」列表（跨进程 stale 免疫：MCP/dsh 子进程写盘后可见）。

    MCP 是独立进程，save_chapter_text/save_plot_draft 只写磁盘 book.json/chapters/、draft_chapter.json；
    Web 进程缓存的 engine.book.current_chapter 可能滞后。这里全部从磁盘现读。"""
    from core.text_utils import count_prose_units
    chapters = []
    for n in range(1, current_chapter + 1):
        ch = book_mgr.load_chapter(book_id, n)
        if ch and ch.get("content"):
            metrics = {"actual_prose_units": int(ch.get("actual_prose_units") or count_prose_units(ch.get("content") or "")),
                       "raw_codepoints": int(ch.get("raw_codepoints") or len(ch.get("content") or ""))}
            chapters.append({
                "num": n,
                    "title": ch.get("title") or f"第{n}章",
                    "content": ch.get("content") or "",
                    "bridges": ch.get("bridges") or [],
                    "word_count": metrics["actual_prose_units"],
                    **metrics,
            })
    # 进行中草稿（draft_chapter.json）：bridges 逐情节段 span.m-bridge，刚写完的情节段即时可见
    dp = os.path.join(str(book_mgr.dir), book_id, "draft_chapter.json")
    if os.path.exists(dp):
        try:
            with open(dp, encoding="utf-8") as f:
                draft = json.load(f)
            bridges = draft.get("bridges") or []
            buffer = draft.get("buffer") or []
            dn = int(draft.get("chapter_num") or 0)
            if dn > current_chapter and (bridges or buffer):
                chapters.append({
                    "num": dn,
                    "title": "（写作中）",
                    "content": "\n\n".join((b.get("text") or "") for b in bridges) if bridges
                               else "\n\n".join(buffer),
                    "bridges": bridges,
                    "word_count": count_prose_units("\n\n".join((b.get("text") or "") for b in bridges) if bridges else "\n\n".join(buffer)),
                    "actual_prose_units": count_prose_units("\n\n".join((b.get("text") or "") for b in bridges) if bridges else "\n\n".join(buffer)),
                    "raw_codepoints": len("\n\n".join((b.get("text") or "") for b in bridges) if bridges else "\n\n".join(buffer)),
                    "draft": True,
                })
        except Exception as e:
            logging.getLogger(__name__).warning("加载进行中草稿失败: %s", e)
    return chapters


def _plot_context_projection(tl, plot):
    """从正式故事线提取某个 Plot 的弧/线程只读摘要，供对照投影使用。

    这里只读取已落盘的结构，不调用模型，也不把当前 Plot 的规划状态倒推成历史事实。
    """
    if tl is None or plot is None:
        return {}, {}
    arc = next((o for o in (tl.outlines or []) if o.id == getattr(plot, "outline_id", "")), None)
    arc_goal = {}
    if arc:
        arc_goal = {
            "arc_id": getattr(arc, "id", "") or "",
            "name": getattr(arc, "name", "") or "",
            "stage_index": int(getattr(plot, "stage_index", 0) or 0),
            "stage_name": "",
            "stage_desc": "",
        }
        stages = list(getattr(arc, "stages", None) or [])
        idx = arc_goal["stage_index"]
        if 0 <= idx < len(stages) and isinstance(stages[idx], dict):
            arc_goal["stage_name"] = stages[idx].get("name", "") or ""
            arc_goal["stage_desc"] = stages[idx].get("description", "") or ""
    tid = getattr(plot, "thread_id", "") or ""
    thread_def = next((t for t in (tl.threads or [])
                       if (t.get("id") if isinstance(t, dict) else getattr(t, "id", "")) == tid), None)
    if isinstance(thread_def, dict):
        thread = {"id": tid, "name": thread_def.get("name", "") or "",
                  "desc": thread_def.get("desc", "") or ""}
    else:
        thread = {"id": tid, "name": "", "desc": ""}
    if tid:
        same = [q for q in (tl.plots or []) if (getattr(q, "thread_id", "") or "") == tid]
        thread["written_count"] = sum(1 for q in same if getattr(q, "written_chapter", 0) or 0)
        thread["unwritten_count"] = sum(1 for q in same if not (getattr(q, "written_chapter", 0) or 0))
    return arc_goal, thread


def _accepted_staged_deltas(staged, draft):
    """只返回明确通过 Plot 评审的暂存 delta，缺证据时 fail closed。"""
    staged = staged if isinstance(staged, dict) else {}
    draft = draft if isinstance(draft, dict) else {}
    bridges = {}
    for bridge in list(staged.get("bridges") or []) + list(draft.get("bridges") or []):
        if not isinstance(bridge, dict) or not bridge.get("plot_id"):
            continue
        bridges.setdefault(str(bridge.get("plot_id")), []).append(bridge)

    accepted = []
    for delta in staged.get("plot_deltas") or []:
        if not isinstance(delta, dict):
            continue
        pid = str(delta.get("plot_id") or "")
        review = delta.get("review") if isinstance(delta.get("review"), dict) else {}
        state = str(delta.get("review_state") or delta.get("state") or review.get("state") or "").strip().lower()
        candidates = bridges.get(pid, [])
        if state != "accepted":
            candidates = [b for b in candidates
                          if str((b.get("review") or {}).get("state") or
                                 b.get("review_state") or b.get("state") or "").strip().lower() == "accepted"]
        if not candidates:
            continue
        run_id = str(delta.get("run_id") or "")
        if run_id:
            candidates = [b for b in candidates if not str(b.get("run_id") or "")
                          or str(b.get("run_id") or "") == run_id]
        if not candidates:
            continue
        accepted.append(delta)
    return accepted


def _character_roster_for_desk(book_id, tl, draft, writing_chapter):
    """构造写作台全员角色卡；不触碰 plot_run.cast_pack。"""
    if tl is None:
        return []
    try:
        from agent_tools import _load_char_states
        from libraries.character_state import project_character_roster
        from libraries.storyline import get_characters
        from libraries.plot_run_state import load_staged
        staged = load_staged(book_id)
        deltas = _accepted_staged_deltas(staged, draft)
        events = []
        for delta in deltas:
            events.extend([x for x in (delta.get("character_changes") or []) if isinstance(x, dict)])
        return project_character_roster(get_characters(tl.basic_info), _load_char_states(book_id),
                                        events, writing_chapter=writing_chapter)
    except Exception as exc:  # noqa: BLE001 — 状态面板失败不应阻断正文刷新
        log.warning("组装 character_roster 失败 book=%s: %s", book_id, exc)
        return []


def _plot_outcome(bridge, tl, chapter_num=0):
    """把一条已完成 plot 的 bridge 转成 Prediction→Fact 对照视图（WS2）。

    predictions = PlotSlot.character_impact（自然语言，供人读）；expected_facts = 可机器比较预测；
    facts = structured facts（agent 上报，绝不从正文推断）；reconcile = reconcile_run 对照结果。
    """
    if not bridge:
        return None
    from libraries.reconcile import reconcile_run
    pid = bridge.get("plot_id")
    plot = next((x for x in ((tl.plots) or []) if x.id == pid), None) if tl is not None else None
    facts = bridge.get("facts") or {}
    if not facts and bridge.get("character_events"):
        facts = {"character_events": list(bridge.get("character_events") or [])}
    base = int(bridge.get("based_on_storyline_revision") or 0)
    cur = int(getattr(tl, "storyline_revision", 0) or 0) if tl is not None else 0
    run = reconcile_run(plot=plot, bridge=bridge,
                        based_on_storyline_revision=base, current_revision=cur,
                        chapter_num=int(chapter_num or 0))
    arc_goal, thread = _plot_context_projection(tl, plot)
    return {
        "plot_id": pid,
        "plot_name": getattr(plot, "name", "") if plot else bridge.get("plot_name", ""),
        "roles": list(getattr(plot, "roles", None) or []) if plot else list(bridge.get("roles") or []),
        # 对照区左栏要写「已提交 · 第 N 章」基准戳：章节号此前只喂给 reconcile_run，没回传。
        "chapter_num": int(chapter_num or 0),
        "run_id": bridge.get("run_id") or run.get("run_id") or "",
        "predictions": list(getattr(plot, "character_impact", None) or []) if plot else [],
        "expected_facts": (bridge.get("expected_facts")
                           or (list(getattr(plot, "expected_facts", None) or []) if plot else [])),
        "facts": facts,
        # 顶部「本段」卡的「承接」一行用 agent 自己写的 plot_summary（展示/检索用途，50~120 字），
        # 比 reconcile.summary（对账口径的机器文案）更适合人读。
        "plot_summary": str(bridge.get("plot_summary") or ""),
        "reconcile": run,
        # 历史弧/线程只来自该 plot 在正式故事线中的归属；不从当前规划反推。
        "arc_goal": arc_goal,
        "thread": thread,
    }


@bp.route("/api/desk/chapters/<book_id>")
def desk_chapters_api(book_id):
    """写作台正文 JSON：从磁盘现读已写章节+草稿（供前端轮询刷新，修「agent 写完不显示」）。

    附带 plot_run：当前 Plot Run 上下文（下一个未写情节段 + 弧目标 + 线程状态 + 承诺 +
    字数余量 + **章内 staged 覆盖后的 cast_pack**）。组装逻辑与 Agent 侧共用
    agent_tools._build_plot_run/_next_plot，保证 UI 显示的就是 agent 实际会写的那一段。

    顶部两栏只吃这几个字段：`plot_run`（本段：情节段名/状态/primary_turn/roles/words）、
    `recent_plot_outcome.plot_summary`（承接）、`writing_chapter`（待写章号）、
    `cast_events` + `plot_run.cast_pack`（角色状态）。
    旧的 comparison / past / current / future / selection / audit / state_revision 已删：
    它们只服务那张已移除的三列对照矩阵，其中 audit 每 3 秒要读令牌账本与任务事件，
    在单线程 Flask 上是纯浪费。
    """
    cur = 0
    try:
        d = json.load(open(os.path.join(str(book_mgr.dir), book_id, "book.json"), encoding="utf-8"))
        cur = int(d.get("current_chapter") or 0)
    except Exception:
        pass
    plot_run = None
    recent_plot_outcome = None
    planning = {}
    # 待写章节号：`current_chapter` 是**已完成**章号，正在进行/即将写的章是它 +1
    # （草稿带着章号时以草稿为准）。顶部左栏的进度行用它（「… · 待写第 N 章」）。
    draft_chapter_num = 0
    try:
        _dp = os.path.join(str(book_mgr.dir), book_id, "draft_chapter.json")
        if os.path.exists(_dp):
            with open(_dp, encoding="utf-8") as _fh:
                draft_chapter_num = int((json.load(_fh) or {}).get("chapter_num") or 0)
    except Exception:
        pass
    writing_chapter = draft_chapter_num if draft_chapter_num > cur else cur + 1
    # 章内已上报的人物变化（每人最近一条）：写作台右栏「动作」一行的数据来源。
    # 刻意在 desk 侧单独组装而不去改 agent_tools._staged_cast_projection——那个函数同时喂
    # Writer 上下文，它的 _dyn_of 只给 5 个动态字段「防噪音」，塞事件会污染写入提示词。
    cast_events = {}
    try:
        from libraries.plot_run_state import load_staged
        for _delta in (load_staged(book_id).get("plot_deltas") or []):
            _ch = int((_delta or {}).get("chapter_num") or 0)
            for _row in ((_delta or {}).get("character_changes") or []):
                _name = str((_row or {}).get("name") or "").strip()
                _evs = [e for e in ((_row or {}).get("events") or []) if isinstance(e, dict)]
                if _name and _evs:
                    cast_events[_name] = {**_evs[-1], "chapter": _ch}
    except Exception as _exc:  # noqa: BLE001 — 事件只是「动作」行的补充，读不到不影响其它
        log.warning("组装 cast_events 失败 book=%s: %s", book_id, _exc)
    try:
        from agent_tools import _build_plot_run, _draft_plot_ids, _next_plot, _runtime_projection
        from libraries.storyline import load_storyline
        tl = load_storyline(_storyline_filepath(book_id))
        draft = None
        dp = os.path.join(str(book_mgr.dir), book_id, "draft_chapter.json")
        if os.path.exists(dp):
            with open(dp, encoding="utf-8") as fh:
                draft = json.load(fh)
        runtime = _runtime_projection(book_id, tl, book_mgr.get(book_id), draft)
        written_now = runtime["display_written_words"]
        if tl is not None:
            p = _next_plot(tl, draft)
            if p is not None:
                from agent_tools import _load_char_states, _staged_cast_projection
                plot_run = _build_plot_run(tl, p, _load_char_states(book_id), draft=draft)
                # 与 writer 同源：character_states 只在收章落账，章内已上报未收章的变化必须在这里
                # 覆盖上去（并打 state_source="staged_fact"），否则 UI 显示的人物状态比 Agent 看到的旧。
                from libraries.plot_run_state import load_staged
                plot_run["cast_pack"] = _staged_cast_projection(
                    plot_run.get("cast_pack") or {}, load_staged(book_id))
                # 顶部「本段」卡要显示「写作重点」= 这一段唯一的主要戏剧变化（primary_turn，
                # 新情节段的硬要求，也是人一眼能读懂的那句）。_build_plot_run 的 plot 投影里
                # 没有它（prepare_plot_run 是在 execution 上另加的），这里**在 desk 侧补**而不是
                # 改 _build_plot_run——那个 dict 会进 commit_token 的 context_fingerprint，
                # 加字段会让在途已签发的令牌全部失效，代价远大于收益。
                plot_run["plot"]["primary_turn"] = str(getattr(p, "primary_turn", "") or "")
                plot_run["plot"]["hook_points"] = list(getattr(p, "hook_points", None) or [])
            event_bridge = next((b for b in reversed((draft or {}).get("bridges") or [])
                                 if isinstance(b, dict) and b.get("plot_id")), None)
            if event_bridge:
                recent_plot_outcome = _plot_outcome(event_bridge, tl,
                                                    int((draft or {}).get("chapter_num") or 0))
            from libraries.planning_state import load_planning_state, detect_story_boundary
            disk_book = book_mgr.get(book_id)
            ps = load_planning_state(book_id, tl, disk_book, persist=False)
            draft_ids = _draft_plot_ids(draft or {})
            remaining = sum(1 for item in (tl.plots or [])
                            if not (getattr(item, "written_chapter", 0) or 0) and item.id not in draft_ids)
            planning = {
                "storyline_revision": int(getattr(tl, "storyline_revision", 0) or 0),
                "target_word_budget": int(ps.get("target_word_budget") or 0),
                "committed_until_word": int(ps.get("committed_until_word") or 0),
                "written_until_word": written_now,
                "committed_words": runtime["committed_words"],
                "draft_words": runtime["draft_words"],
                "raw_codepoints": runtime["committed_raw_codepoints"] + runtime["draft_raw_codepoints"],
                "current_plot": runtime["current_plot"],
                # 写作台轮询与规划面板使用同一份导航数据，避免一个接口显示
                # “未规划”、另一个接口已有 H1/H2 的短暂分裂。
                "horizon": ps.get("horizon") or {},
                "future_intents": ps.get("future_intents") or [],
                "story_questions": ps.get("story_questions") or [],
                "character_intents": ps.get("character_intents") or [],
                "last_replan": ps.get("last_replan") or {},
                "boundary": detect_story_boundary(
                    written_until_word=written_now,
                    committed_until_word=int(ps.get("committed_until_word") or 0),
                    remaining_plots=remaining,
                    words_per_batch=int(tl.words_per_chapter or 3000),
                    storyline_revision=int(getattr(tl, "storyline_revision", 0) or 0),
                    last_replan=ps.get("last_replan") or {},
                ),
            }
    except Exception as e:
        logging.getLogger(__name__).warning("组装 plot_run 失败: %s", e)
    chapters = _chapters_from_disk(book_id, cur)
    if recent_plot_outcome is None:
        # 承接来源放宽为「最近一个带 plot_id 的 bridge」：旧条件要求它有 facts/character_events，
        # 于是「有正文有摘要、但本段没有人物事件」的 Plot 当不了承接来源，顶部承接会空掉。
        last_ch, last_bridge = None, None
        for ch in reversed(chapters):
            for b in reversed(ch.get("bridges") or []):
                if isinstance(b, dict) and b.get("plot_id"):
                    last_ch, last_bridge = ch, b
                    break
            if last_bridge:
                break
        if last_bridge:
            _tlc = tl if 'tl' in locals() else None
            recent_plot_outcome = _plot_outcome(last_bridge, _tlc, (last_ch or {}).get("num") or 0)
    fact_ledger = {}
    try:
        from libraries.fact_ledger import project as project_fact_ledger
        fact_ledger = project_fact_ledger(book_id)
    except Exception as exc:
        logging.getLogger(__name__).warning("组装 fact_ledger 失败 book=%s: %s", book_id, exc)
    character_roster = _character_roster_for_desk(book_id, tl if 'tl' in locals() else None,
                                                   draft if 'draft' in locals() else None,
                                                   writing_chapter)
    return jsonify({"book_id": book_id, "current_chapter": cur,
                    "writing_chapter": writing_chapter,
                    "chapters": chapters,
                    "plot_run": plot_run, "recent_plot_outcome": recent_plot_outcome,
                    "fact_ledger": fact_ledger,
                    "character_roster": character_roster,
                    # 兼容旧的 Agent 面板投影；写作台角色区使用 character_roster。
                    "cast_events": cast_events,
                    "planning": planning})


@bp.route("/api/desk/facts/<book_id>")
def desk_facts_history_api(book_id):
    """写作台历史事实分页接口：展开“更早历史”时按需读取，保持主轮询有界。"""
    cursor = request.args.get("cursor") or None
    raw_limit = request.args.get("limit")
    try:
        limit = int(raw_limit) if raw_limit is not None else 30
    except (TypeError, ValueError):
        limit = 30
    try:
        from libraries.fact_ledger import page as page_fact_ledger
        data = page_fact_ledger(book_id, cursor=cursor, limit=limit)
        return jsonify({"ok": True, "book_id": book_id,
                        "revision": data.get("revision", ""),
                        "items": data.get("recent") or [],
                        "next_cursor": data.get("history_cursor"),
                        "archived_count": int(data.get("archived_count") or 0),
                        "truncated": bool(data.get("truncated")),
                        "provenance": data.get("provenance") or {}})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except Exception as exc:
        logging.getLogger(__name__).warning("读取事实历史失败 book=%s: %s", book_id, exc)
        return jsonify({"ok": False, "error": "读取事实历史失败"}), 500


@bp.route("/books/storyline/write/<engine_id>")
def storyline_write_flow(engine_id):
    """蓝图式写作流程页（新核心）"""
    engine = _engines.get(engine_id)
    if not engine:
        return "引擎会话已过期", 404
    # 已写章节（供中栏「章节正文」预载，作为书目内容连续展示）
    chapters = []
    initial_written_words = 0
    initial_raw_codepoints = 0
    book = getattr(engine, "book", None)
    if book and book.book_id:
        # 跨进程 stale：MCP/dsh 子进程写盘后，用磁盘 book.json 的最新 current_chapter 修正缓存
        try:
            d = json.load(open(os.path.join(str(book_mgr.dir), book.book_id, "book.json"), encoding="utf-8"))
            cur = int(d.get("current_chapter") or 0)
            if cur > 0:
                book.current_chapter = cur
        except Exception:
            pass
    if book and (book.current_chapter or 0) >= 1:
        try:
            for n in range(1, book.current_chapter + 1):
                ch = book_mgr.load_chapter(book.book_id, n)
                if ch and ch.get("content"):
                    from core.text_utils import count_prose_units
                    initial_written_words += count_prose_units(ch.get("content") or "")
                    initial_raw_codepoints += len(ch.get("content") or "")
                    chapters.append({
                        "num": n,
                        "title": ch.get("title") or f"第{n}章",
                        "content": ch.get("content") or "",
                        "bridges": ch.get("bridges") or [],
                        "word_count": int(ch.get("word_count") or count_prose_units(ch.get("content") or "")),
                    })
        except Exception as e:
            logger.warning("加载已写章节失败: %s", e)
    # 进行中的章节草稿：与已固化章节同格式渲染（bridges 逐情节段 span.m-bridge），
    # 让刚写完的情节段在写作台上即时可见、可点击高亮；切章固化（_clear_draft）后自动消失。
    try:
        draft = engine._load_draft() if hasattr(engine, "_load_draft") else None
    except Exception as e:
        draft = None
        logger.warning("加载进行中草稿失败: %s", e)
    if book and draft:
        bridges = draft.get("bridges") or []
        buffer = draft.get("buffer") or []
        dn = int(draft.get("chapter_num") or 0)
        # 已固化章节跳过（防陈旧草稿重复渲染）；旧草稿无 bridges 时回退 buffer 纯文本展示
        if dn > (book.current_chapter or 0) and (bridges or buffer):
            chapters.append({
                "num": dn,
                "title": "（写作中）",
                "content": "\n\n".join((b.get("text") or "") for b in bridges) if bridges
                           else "\n\n".join(buffer),
                "bridges": bridges,
                "draft": True,
            })
            from core.text_utils import count_prose_units
            initial_written_words += count_prose_units("\n\n".join((b.get("text") or "") for b in bridges) if bridges else "\n\n".join(buffer))
            initial_raw_codepoints += len("\n\n".join((b.get("text") or "") for b in bridges) if bridges else "\n\n".join(buffer))
    sl = getattr(engine, "storyline", None)
    # 将正文桥接段按统一口径回填到故事线视图；规划对象本身不落盘修改。
    storyline_data = sl.to_dict() if sl else None
    if storyline_data:
        from core.text_utils import count_prose_units
        actual_plot_words = {}
        for chapter in chapters:
            for bridge in chapter.get("bridges") or []:
                pid = str(bridge.get("plot_id") or "")
                if pid:
                    actual_plot_words[pid] = actual_plot_words.get(pid, 0) + count_prose_units(bridge.get("text") or "")
        for plot in storyline_data.get("plots") or []:
            pid = str(plot.get("id") or "")
            plot["actual_words"] = int(actual_plot_words.get(pid, 0))
    # 字数轴：总章数优先用引擎已字数化的 state.total_chapters，否则由情节段 planned_words 推导
    total_ch = getattr(getattr(engine, "state", None), "total_chapters", 0) or 0
    if not total_ch and sl:
        try:
            from libraries.storyline_writer import planned_words
            _w = sum(planned_words(p) for p in sl.plots) if sl.plots else 0
            _wpc = (sl.words_per_chapter or 3000)
            total_ch = max(1, (_w + _wpc - 1) // _wpc)
        except Exception:
            total_ch = 0
    from libraries.storyline import basic_info_world_done
    world_done = basic_info_world_done((sl.basic_info if sl else None))
    planning_state = {}
    planning_boundary = {}
    if sl and book:
        try:
            from libraries.planning_state import load_planning_state, detect_story_boundary
            planning_state = load_planning_state(book.book_id, sl, book, persist=False)
            # 页面首屏必须使用磁盘章节的实时字数；Web 进程中的 book 对象可能是 Agent
            # 写作前加载的旧快照。故事线和轮询接口仍以领域状态为准。
            planning_state = dict(planning_state or {})
            planning_state["written_until_word"] = int(initial_written_words)
            remaining_plots = sum(1 for p in (sl.plots or []) if not (getattr(p, "written_chapter", 0) or 0))
            planning_boundary = detect_story_boundary(
                written_until_word=int(initial_written_words),
                committed_until_word=int(planning_state.get("committed_until_word") or 0),
                remaining_plots=remaining_plots,
                words_per_batch=int(sl.words_per_chapter or 3000),
                storyline_revision=int(getattr(sl, "storyline_revision", 0) or 0),
                last_replan=planning_state.get("last_replan") or {},
            )
        except Exception:
            planning_state, planning_boundary = {}, {}
    return render_template("storyline_write_flow.html",
        engine_id=engine_id,
        state=engine.state,
        storyline=sl,
        storyline_data=storyline_data,
        book=book,
        chapters=chapters,
        total_ch=total_ch,
        world_done=world_done,
        planning_state=planning_state,
        planning_boundary=planning_boundary,
        initial_written_words=initial_written_words,
        initial_raw_codepoints=initial_raw_codepoints,
    )


# ⚠️ 已废弃：旧 NovelEngine 逐步写作入口（当前写作走侧栏 dsh + 服务端 FSM）。（无前端引用；删除属 API 面变更，待单独确认）
@bp.route("/api/storyline-engine/<engine_id>/step", methods=["POST"])
def storyline_engine_step(engine_id):
    """蓝图引擎：按故事线写下一章（新书前三章 / 续写任意章节通用）"""
    from plugins import task_manager

    engine = _engines.get(engine_id)
    if not engine:
        return jsonify({"error": "not found"}), 404

    is_continue = engine.state.book_mode == BookMode.CONTINUE
    task_id = f"engine_{engine_id}"
    next_ch = engine.state.current_chapter + 1
    total_ch = engine.state.total_chapters or next_ch
    flow_url = url_for("desk.storyline_write_flow", engine_id=engine_id)
    book = getattr(engine, "book", None)
    book_id = getattr(book, "book_id", "") or ""
    book_title = getattr(book, "title", "") or ""

    # 注册/更新任务（新书生成 / 续写写作）
    task_name = "续写写作" if is_continue else "新书生成"
    if next_ch == 1:
        task_manager.ensure_single(task_name)
        task_manager.start(task_id, name=task_name,
                          title=engine.state.pen_name or "",
                          phase=f"第{next_ch}章...", url=flow_url)
    else:
        task_manager.progress(task_id, current=min(next_ch, total_ch), phase=f"第{next_ch}章...")
    task_manager.log(task_id, f"蓝图写作：第{next_ch}章", "info")

    # 全书完成（章节数到顶）
    if next_ch > total_ch:
        task_manager.done(task_id, message="全书完成")
        return jsonify({"status": "done", "flow_complete": True, "reason": "已写完全部章节"})

    inst = Instruction(Op.WRITE_STORYLINE_CHAPTER, chapter_num=next_ch)
    result = engine.execute(inst)
    if result.get("error"):
        task_manager.fail(task_id, str(result["error"]))
        return jsonify({"error": result["error"]}), 500
    task_manager.log(task_id, f"第{next_ch}章完成 {result.get('word_count', 0)}字", "success")

    return jsonify({
        "op": "write_storyline_chapter",
        "chapter_num": next_ch,
        "status": result.get("status"),
        "word_count": result.get("word_count", 0),
        "beats": result.get("beats", 0),
        "blueprint": result.get("blueprint", {}),
        "cost": result.get("cost", 0),
        "flow_complete": next_ch >= total_ch,
    })


# ⚠️ 已废弃：旧整章 SSE 写作入口（当前写作走侧栏 dsh + 服务端 FSM）。（无前端引用；删除属 API 面变更，待单独确认）
@bp.route("/api/storyline-engine/<engine_id>/write-chapter", methods=["POST"])
def storyline_engine_write_chapter_sse(engine_id):
    """蓝图引擎：流式写一章（SSE）。逐情节段下发 plot_start / plot_done / chapter_done。

    前端据此在右侧逐情节段展示步骤与正文，并高亮左侧故事线对应的大纲/情节段。
    """
    import json as _json
    from plugins import task_manager
    engine = _engines.get(engine_id)
    if not engine:
        return jsonify({"error": "not found"}), 404
    chapter_num = engine.state.current_chapter + 1
    flow_url = url_for("desk.storyline_write_flow", engine_id=engine_id)
    _book = getattr(engine, "book", None)
    _book_id = getattr(_book, "book_id", "") or ""
    _book_title = getattr(_book, "title", "") or ""

    def generate():
        task_manager.ensure_single("整章写作")
        task_id = f"writechap_{engine_id}_{int(time.time())}"
        task_manager.start(task_id, name="整章写作",
                           title=_book_title or "",
                           url=flow_url)
        try:
            for evt in engine._write_storyline_chapter_stream(chapter_num):
                if isinstance(evt, dict):
                    t = evt.get("type", "")
                    if t == "plot_chunk":
                        pass
                    elif t == "chapter_done":
                        task_manager.done(task_id,
                                          message=f"第{evt.get('chapter', chapter_num)}章完成")
                    elif t == "error":
                        task_manager.fail(task_id, str(evt.get("message", "写入失败")))
                yield "data: " + _json.dumps(evt, ensure_ascii=False) + "\n\n"
        except Exception as e:
            import traceback
            task_manager.fail(task_id, str(e))
            err = {"type": "error", "message": str(e),
                   "traceback": traceback.format_exc()}
            yield "data: " + _json.dumps(err, ensure_ascii=False) + "\n\n"

    return sse_stream_response(generate())


# ⚠️ 已废弃：旧单情节段 SSE 写作入口（当前写作走侧栏 dsh + 服务端 FSM）。（无前端引用；删除属 API 面变更，待单独确认）
@bp.route("/api/storyline-engine/<engine_id>/write-bridge", methods=["POST"])
def storyline_engine_write_bridge_sse(engine_id):
    """蓝图引擎：流式写「一个」情节段（SSE，新核心·按情节段撰写）。

    事件：bridge_start / group_chunk / bridge_done / chapter_done / complete。
    写一个情节段即返回；连续点击则继续写下一个未写情节段，本章满字数自动切章。
    """
    import json as _json
    from plugins import task_manager
    engine = _engines.get(engine_id)
    if not engine:
        return jsonify({"error": "not found"}), 404
    flow_url = url_for("desk.storyline_write_flow", engine_id=engine_id)
    _book = getattr(engine, "book", None)
    _book_id = getattr(_book, "book_id", "") or ""
    _book_title = getattr(_book, "title", "") or ""

    def generate():
        task_manager.ensure_single("情节段写作")
        task_id = f"writebrg_{engine_id}_{int(time.time())}"
        task_manager.start(task_id, name="情节段写作",
                           title=_book_title or "",
                           url=flow_url)
        try:
            for evt in engine._write_next_plot_stream():
                if isinstance(evt, dict):
                    t = evt.get("type", "")
                    if t == "group_chunk":
                        pass
                    elif t == "bridge_done":
                        pass
                    elif t in ("chapter_done", "complete"):
                        task_manager.done(task_id,
                                          message=f"第{evt.get('chapter', '')}章完成" if t == "chapter_done"
                                                  else evt.get("message", "全书完成"))
                    elif t == "error":
                        task_manager.fail(task_id, str(evt.get("message", "写入失败")))
                yield "data: " + _json.dumps(evt, ensure_ascii=False) + "\n\n"
        except Exception as e:
            import traceback
            task_manager.fail(task_id, str(e))
            err = {"type": "error", "message": str(e),
                   "traceback": traceback.format_exc()}
            yield "data: " + _json.dumps(err, ensure_ascii=False) + "\n\n"

    return sse_stream_response(generate())





# ═══════════════════════════════════════════
# ♻️ 续写
@bp.route("/books/<book_id>/continue")
# ═══════════════════════════════════════════

def continue_book_page(book_id):
    """书续写 — 统一走故事线蓝图写作流程（新核心）"""
    book = book_mgr.get(book_id)
    if not book:
        return "图书不存在", 404
    llm = get_llm()
    if not llm:
        return jsonify({"error": "LLM 未配置"}), 500
    engine_id = f"cont_{book_id}"
    if engine_id not in _engines:
        try:
            engine = NovelEngine(llm_client=llm)
            engine.continue_book(book_id)
            _engines[engine_id] = engine
        except (ValueError, RuntimeError) as e:
            # 无故事线（旧书/未生成 storyline）：给出指引而非 500，
            # 避免「续写/进入写作台」在残缺书上直接崩溃。
            return render_template_string(
                '<div class="tle-layout"><h2>⚠️ 无法进入写作</h2>'
                '<p style="color:#8b949e">{{ msg }}</p>'
                '<p><a class="btn" style="background:#1f6feb;color:#fff;text-decoration:none" '
                'href="/books/{{ bid }}">📖 返回书详情</a> '
                '<a class="btn" style="background:#30363d;color:#c9d1d9;text-decoration:none" '
                'href="/books">📚 去书库</a></p></div>',
                msg=str(e), bid=book_id), 200
    return redirect(url_for("desk.storyline_write_flow", engine_id=engine_id))
