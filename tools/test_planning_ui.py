"""增量规划 UI HTTP 契约：聚合、确认提交、陈旧版本拒绝。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_tools import save_outlines
from libraries.planning_state import load_replan_preview, save_replan_preview
from libraries.storyline import BookStoryline
from ui.web_blueprints.ctx import book_mgr
from ui.web_ui import app


book = book_mgr.create(title="规划 UI 测试", pen_name="test", genre="玄幻")
bid = book.book_id
try:
    book_mgr.save_storyline(bid, BookStoryline(book_title=book.title, pen_name=book.pen_name, phase="ready"))
    first = save_outlines(
        bid, outlines=[{"id": "a1", "name": "第一弧", "start_word": 0, "end_word": 3000}],
        plots=[{"id": f"p{i}", "name": f"段{i}", "outline_id": "a1", "words": 1000,
                "primary_turn": f"第{i}个主要戏剧变化"} for i in range(1, 4)],
        mode="replace", expected_revision=0,
        planning_patch={"committed_until_word": 3000, "future_intents": ["远方威胁"]},
    )
    assert first["storyline_revision"] == 1
    preview = save_replan_preview(bid, {
        "expected_revision": 1,
        "diagnosis": {"current_pressure": "承诺区将尽"},
        "directions": [{"id": "d1", "title": "迎战"}, {"id": "d2", "title": "撤退"}],
        "selected_direction_id": "d1",
        "outlines": [{"id": "a2", "name": "第二弧", "start_word": 3000, "end_word": 6000}],
        "plots": [{"id": f"p{i}", "name": f"段{i}", "outline_id": "a2", "words": 1000,
                   "primary_turn": f"第{i}个主要戏剧变化"} for i in range(4, 7)],
        "planning_patch": {"committed_until_word": 6000,
                           "horizon": {"h1": [{"title": "下一段方向", "arc_intent": "继续推进当前矛盾"}]},
                           "future_intents": [{"title": "更远威胁", "intent": "更远威胁"}]},
        "validation": {"passed": True, "problems": []},
    })
    client = app.test_client()
    # 写作台章级入口只对 flow_mode=chapter_to_completion 强制 auto，不改变普通任务 policy。
    import ui.web_blueprints.agent as agent_routes
    _old_flow = agent_routes.run_dsh_flow
    _old_status = agent_routes.get_current_task_status
    _seen_flow = []
    def _fake_flow(task, history=None, debug=False, policy=None, **kwargs):
        _seen_flow.append({"task": task, "policy": policy, **kwargs})
        yield {"type": "done"}
    agent_routes.run_dsh_flow = _fake_flow
    try:
        auto_resp = client.post("/api/agent/chat", json={
            "messages": [{"role": "user", "content": "完成第1章 book_test"}],
            "flow_mode": "chapter_to_completion", "book_id": bid,
        })
        auto_data = auto_resp.get_data()
        normal_resp = client.post("/api/agent/chat", json={
            "messages": [{"role": "user", "content": "普通任务"}],
        })
        normal_data = normal_resp.get_data()
        assert auto_resp.status_code == 200 and normal_resp.status_code == 200
        assert [x["policy"] for x in _seen_flow] == ["auto", None]
        assert _seen_flow[0]["flow_mode"] == "chapter_to_completion" and _seen_flow[0]["explicit_book_id"] == bid
        agent_routes.get_current_task_status = lambda: {"running": True}
        busy_resp = client.post("/api/agent/chat", json={
            "messages": [{"role": "user", "content": "再次续写"}],
            "flow_mode": "chapter_to_completion", "book_id": bid, "busy_policy": "reject",
        })
        busy_data = busy_resp.get_data()
        assert busy_resp.status_code == 200 and b"agent_busy" in busy_data
    finally:
        agent_routes.run_dsh_flow = _old_flow
        agent_routes.get_current_task_status = _old_status
    invalid_mode = client.post("/api/agent/chat", json={
        "messages": [{"role": "user", "content": "非法模式"}], "flow_mode": "other",
    })
    assert invalid_mode.status_code == 400 and invalid_mode.json["error"] == "invalid_flow_mode"
    state = client.get(f"/api/storyline/{bid}/planning-state")
    assert state.status_code == 200
    # 公开 GET 只给正式规划事实：preview 草稿（含校验问题/owner）不进浏览器。
    assert "replan_preview" not in state.json and "replan_preview_status" not in state.json
    assert load_replan_preview(bid)["preview_id"] == preview["preview_id"]
    committed = client.post(f"/api/storyline/{bid}/commit-plan", json={
        "preview_id": preview["preview_id"], "expected_revision": 1,
    })
    assert committed.status_code == 200 and committed.json["storyline_snapshot"]["revision"] == 2
    latest_storyline = client.get(f"/api/storyline/{bid}")
    latest_data = latest_storyline.json.get("storyline", {})
    assert latest_storyline.status_code == 200 and latest_data.get("storyline_revision") == 2
    assert any(x.get("id") == "a2" for x in latest_data.get("outlines", []))
    assert any(x.get("id") == "p4" for x in latest_data.get("plots", []))
    # 旧文件即使伪造 validation.passed=true，提交端也必须动态拒绝 malformed H1。
    bad_preview = {**preview, "preview_id": "bad-shape", "expected_revision": 2,
                   "planning_patch": {"horizon": {"h1": "错误"},
                                      "future_intents": [{"title": "远期", "intent": "远期"}]},
                   "validation": {"passed": True}}
    save_replan_preview(bid, bad_preview)
    bad_commit = client.post(f"/api/storyline/{bid}/commit-plan", json={
        "preview_id": "bad-shape", "expected_revision": 2,
    })
    assert bad_commit.status_code == 400 and bad_commit.json["error"] == "preview_invalid"
    assert book_mgr.load_storyline(bid).storyline_revision == 2
    stale = save_replan_preview(bid, {**preview, "preview_id": "stale-preview", "expected_revision": 1})
    rejected = client.post(f"/api/storyline/{bid}/commit-plan", json={
        "preview_id": stale["preview_id"], "expected_revision": 1,
    })
    assert rejected.status_code == 409 and rejected.json["error"] == "stale_storyline"
    assert book_mgr.load_storyline(bid).storyline_revision == 2
    # 校验失败必须拦截：validation.passed=False 的预览禁止确认，正式 storyline/planning 均不变
    bad = save_replan_preview(bid, {**preview, "preview_id": "bad-preview", "expected_revision": 2,
                                    "validation": {"passed": False, "problems": ["弧 r2 跨度与现有弧重叠"]}})
    blocked = client.post(f"/api/storyline/{bid}/commit-plan", json={
        "preview_id": bad["preview_id"], "expected_revision": 2,
    })
    assert blocked.status_code == 400 and blocked.json["error"] == "preview_invalid"
    assert blocked.json["problems"] == ["弧 r2 跨度与现有弧重叠"]
    assert book_mgr.load_storyline(bid).storyline_revision == 2
    print("[OK] planning UI aggregate / commit / stale revision / invalid preview blocked")

    # 对照区口径：desk 的 cast_pack 必须与 writer 同源（含本章 staged 投影 + state_source 标记），
    # 否则 UI 显示的人物状态会比 Agent 实际拿到的旧 —— 这是「UI 说什么、Agent 就写什么」的前提。
    from agent_tools import get_orchestration_state, save_basic_info
    from libraries.plot_run_state import make_plot_delta, stage_delta
    # 写作期（phase=ready）改人物是**受限修正**：必须带最新 storyline_revision。
    # 这是产品侧的既定硬边界（见 `_guard_ready_character_patch`），夹具照它走。
    save_basic_info(bid, {"characters": [{"name": "陆凌舟", "role": "主角", "importance": 1,
                                          "identity": "工程师", "speech_profile": {}}]},
                    expected_revision=get_orchestration_state(bid)["storyline_revision"])
    stage_delta(bid, make_plot_delta(
        plot_id="p1", plot_name="段1", chapter_num=1,
        facts={"character_events": [{"name": "陆凌舟",
                "events": [{"type": "location_shift", "from": "云海矿岛", "to": "重力井底"}]}]},
        text="……", run_id="p1@2", reconcile={}))
    desk = client.get(f"/api/desk/chapters/{bid}").json
    cards = {c["name"]: c for c in (desk["plot_run"]["cast_pack"]["protagonists"]
                                    + desk["plot_run"]["cast_pack"]["active"])}
    assert cards["陆凌舟"]["dyn"]["location"] == "重力井底", cards.get("陆凌舟")
    assert cards["陆凌舟"]["state_source"] == "staged_fact", cards.get("陆凌舟")
    # 顶部两栏的契约：只吃这几个字段（旧的三列 projection 已删，不得回流）
    assert all(k in desk for k in ("current_chapter", "writing_chapter", "plot_run",
                                   "recent_plot_outcome", "cast_events", "planning"))
    assert all(k not in desk for k in ("comparison", "past", "current", "future",
                                       "selection", "audit", "state_revision"))
    # «本段»卡要的写作重点 = primary_turn（desk 侧注入，未改 _build_plot_run 的指纹）
    assert desk["plot_run"]["plot"].get("primary_turn"), desk["plot_run"]["plot"]
    # «动作»行的补充来源：章内上报的 character_changes（每人最近一条）
    assert desk["cast_events"]["陆凌舟"]["to"] == "重力井底", desk["cast_events"]
    ev = desk["cast_events"]["陆凌舟"]
    assert ev.get("to") == "重力井底" and ev.get("type") == "location_shift", ev
    assert ev.get("chapter") == 1, ev          # 事件带着它来自哪一章
    print("[OK] desk 顶部两栏契约：writing_chapter / primary_turn / staged cast_events，旧 projection 已删")

    # 旧书降级：无 planning_state.json 的书 → 聚合接口返回默认空态，不落盘、不报错
    legacy = book_mgr.create(title="规划 UI 旧书", pen_name="test", genre="都市")
    lbid = legacy.book_id
    try:
        book_mgr.save_storyline(lbid, BookStoryline(book_title=legacy.title, pen_name=legacy.pen_name, phase="ready"))
        from libraries.planning_state import planning_path as _ppath
        assert not _ppath(lbid).exists()
        state = client.get(f"/api/storyline/{lbid}/planning-state")
        assert state.status_code == 200 and state.json["ok"]
        ps = state.json["planning_state"]
        assert ps["written_until_word"] == 0 and ps["committed_until_word"] == 0
        assert state.json["storyline_snapshot"]["revision"] == 0
        assert not _ppath(lbid).exists()  # persist=False 只读聚合，不建文件
        print("[OK] legacy book planning-state empty state / no file created")

    finally:
        book_mgr.delete(lbid)
finally:
    book_mgr.delete(bid)

    # ── 故事线 UI 契约：远期通道 + 清单徽标 + 台账"规划中"组（源码级，防接线回退）──
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sl_js = open(os.path.join(root, "ui", "static", "js", "story_line.js"), encoding="utf-8").read()
    sl_css = open(os.path.join(root, "ui", "static", "css", "story_line.css"), encoding="utf-8").read()
    # 规划提示条（横幅）连同它的模板与 JS 已整体删除——写作台的规划叠层改由页内两行内联赋值
    # + desk 轮询喂给 Gantt。这里断言文件确实不在了，并用空串让下面的「不得回流」断言继续表达。
    pu_js_path = os.path.join(root, "ui", "static", "js", "planning_ui.js")
    planning_tpl_path = os.path.join(root, "ui", "templates", "_planning_ui.html")
    assert not os.path.exists(pu_js_path) and not os.path.exists(planning_tpl_path), (
        "规划 UI 已删除：planning_ui.js / _planning_ui.html 不得回流")
    pu_js = ""
    panels = open(os.path.join(root, "ui", "templates", "_book_runtime_panels.html"), encoding="utf-8").read()
    start_tpl = open(os.path.join(root, "ui", "templates", "start_book.html"), encoding="utf-8").read()
    write_tpl = open(os.path.join(root, "ui", "templates", "storyline_write_flow.html"), encoding="utf-8").read()
    book_tpl = open(os.path.join(root, "ui", "templates", "book_detail.html"), encoding="utf-8").read()
    planning_tpl = ""
    agent_js = open(os.path.join(root, "ui", "static", "js", "agent_panel.js"), encoding="utf-8").read()
    agent_py = open(os.path.join(root, "ui", "web_blueprints", "agent.py"), encoding="utf-8").read()
    desk_py = open(os.path.join(root, "ui", "web_blueprints", "desk.py"), encoding="utf-8").read()
    # 三列对照投影（上一段 | 对照字段 | 本段）连同它的整套行构造器已删除：
    # 写作台顶部改两栏（本段最新 + 角色状态），agent 对账视图不再进人看的页面。
    assert all(x not in desk_py for x in ("_build_compare_projection", "_COMPARE_FACT_FIELD",
                                          "arc_thread_rows", "pair_records", "promise_rows",
                                          "question_rows", "location_rows", "character_rows"))
    # 顶部两栏要的新字段必须在响应里（否则前端拿不到待写章号与章内人物动作）
    assert '"writing_chapter": writing_chapter' in desk_py and '"cast_events": cast_events' in desk_py
    # 「动作」的来源 = 章内已上报的 character_changes（每人最近一条）；不是从人设推断
    assert "character_changes" in desk_py and "load_staged" in desk_py
    # primary_turn 在 desk 侧补进 plot 投影（改 _build_plot_run 会动 commit_token 指纹）
    assert 'plot_run["plot"]["primary_turn"]' in desk_py

    # 预测与正式三泳道共享同一个纵向画布；它不是右侧第四泳道，也不是 sibling。
    assert "sl-lane-forecast" not in sl_js and "sl-lane-forecast" not in sl_css
    assert all(x in sl_js for x in ("sl-forecast-strip", "sl-forecast-row", "sl-forecast-h1", "sl-forecast-h2", "renderForecast"))
    main_pos = sl_js.index("'<div class=\"sl-main\">'")
    formal_pos = sl_js.index("'<div class=\"sl-formal-row\">'")
    axis_pos = sl_js.index("'<div class=\"sl-axis-panel\"")
    content_pos = sl_js.index("'<div class=\"sl-content-area\"")
    forecast_pos = sl_js.index("'<div class=\"sl-forecast-strip\"")
    legend_pos = sl_js.index("'<div class=\"sl-legend\">")
    assert main_pos < formal_pos < axis_pos < content_pos < forecast_pos < legend_pos
    assert "-h1b" in sl_js and "-h2b" in sl_js
    assert "🧭 近期方向 H1" in sl_js and "🔭 远期方向 H2" in sl_js
    assert "尚未形成近期方向" in sl_js and "远期保持开放" in sl_js
    assert "_panels = [axisPanel, contentArea]" in sl_js

    main_css = sl_css[sl_css.index(".sl-main {"):sl_css.index("/* ─── 轴线")]
    forecast_css = sl_css[sl_css.index(".sl-forecast-strip {"):sl_css.index("@container", sl_css.index(".sl-forecast-strip {"))]
    assert "flex-direction: column" in main_css
    assert "max-height" not in forecast_css and "overflow-y: auto" not in forecast_css
    assert "overflow: visible" in forecast_css
    assert "sl-forecast-card" in sl_css and "repeating-linear-gradient" in sl_css
    assert "white-space: normal" in sl_css and "flex-wrap: wrap" in sl_css and "overflow-wrap: anywhere" in sl_css

    # 预测数据只认 horizon.h1 与 future_intents（horizon.h2 不另存一份）。
    assert "planning.horizon && planning.horizon.h1" in sl_js
    assert "planning.future_intents" in sl_js
    assert "horizon.h2" not in sl_js
    assert "contentArea.appendChild(cursor)" in sl_js and "contentArea.appendChild(line)" in sl_js

    # 续规划通过 updateProgress 增量刷新条带，而不是只刷新红/黄线；更新时维护滚动锚点。
    progress_block = sl_js[sl_js.index("updateProgress: function"):sl_js.index("highlight: function")]
    assert "renderForecast" in progress_block and "captureScrollState" in progress_block and "restoreScrollState" in progress_block
    assert "scrollToDirections" in sl_js

    # 规划横幅（下一段 H0 / 🟡 边界告警 / ⛔ 规划门禁）已从写作台整体删除，
    # 连同 _planning_ui.html 与 planning_ui.js 两个文件（上一段开头已断言它们不存在）。
    # 用**精确引入标记**断言（而非文件名本身）：注释里提到名字不算回流，真回流一定需要 include/script 标签
    assert '{% include "_planning_ui.html" %}' not in write_tpl
    assert "/static/js/planning_ui.js" not in write_tpl
    assert 'id="planning-state-panel"' not in write_tpl
    assert all(x not in write_tpl for x in ("NEPlanning", "pm-boundary", "pm-alert",
                                            "planning-minibar", "nextHint" + "："))
    # 但**规划叠层的接线必须还在**（否则 Gantt 丢掉已写红线/承诺黄线/H1-H2 方向条）：
    # 页面用 render 上下文里的 planning_state / planning_boundary 内联赋值，
    # 之后由 desk 轮询持续合并刷新；故事线 revision 前进时内联的 syncStoryline 拉新故事线。
    assert "window.__PLANNING_STATE__ = {{ planning_state|tojson }}" in write_tpl
    assert "window.__PLANNING_BOUNDARY__ = {{ planning_boundary|tojson }}" in write_tpl
    assert "StoryLine.updateProgress" in write_tpl and "function syncStoryline(" in write_tpl
    assert "function _mergeStorylineRuntimeFields(" in write_tpl
    # 书详情同样不加载规划 UI，改用只取数不渲染的极简 loader
    assert "_planning_ui.html" not in book_tpl
    assert "__PLANNING_STATE__" in book_tpl and "ne:planning-updated" in book_tpl
    # 书详情四个 agent 面板已删；角色状态留下并默认展开
    assert all(x not in panels for x in ("运行时决策中心", "读者承诺台账", "质量诊断", "历史快照",
                                        "loadDecisionCenter", "runDiagnose", "loadSnapshots",
                                        "runChapterReview", "runChapterDeai", "runPunchPoints"))
    assert "🎭 角色状态" in panels and 'class="accordion-body show"' in panels

    # replan 提交后的正式合同可重新拉取；三页都接收统一故事线刷新事件。
    # 故事线同步改为写作台内联（原 planning_ui.syncStoryline），仍用 fetch 不硬编码 HTTP 文本
    assert "/api/storyline/' + encodeURIComponent(window.__STORYLINE_ID__" in write_tpl
    assert "ne:storyline-updated" in write_tpl and "ne:storyline-updated" in book_tpl

    # 步 3 的 Gantt 读写 planning 都接通；写作台轮询把最新 planning 合并进全局并刷新 Gantt。
    # （原来还有一条「比较 H1/H2 变化 → 刷新横幅」的断言，随横幅删除而失效：那次比较只为横幅服务。）
    assert "planning: (d.planning" in start_tpl
    assert "planning: (args.planning" in start_tpl
    assert "WZ.renderStoryline()" in start_tpl
    assert "Object.assign({}, oldPlanning, d.planning)" in write_tpl
    assert "StoryLine.updateProgress" in write_tpl

    # 规划已改由章级 FSM 在续写父任务中自主完成，页面不再有手工抽屉。
    # 手工 replan 抽屉早已移除：相关控件名一律不得回流
    assert all(x not in write_tpl for x in ("replan-drawer", "replan-backdrop", "revision-modal",
                                            "boundary-banner", "requestReplan", "openDrawer",
                                            "commitPreview", "set_replan_preview"))
    assert "flowMode" in write_tpl and "busyPolicy" in write_tpl and "taskKind" in write_tpl
    assert "needs_replan" not in write_tpl[write_tpl.index("function _resolveNextChapterAndSend"):write_tpl.index("function stopWritingTask")]
    assert "flow_mode" in agent_js and "busy_policy" in agent_js and "task_kind" in agent_js
    # 顶部两栏：上一段（已完成）+ 角色状态。HTML 挂载点与 JS 渲染入口双向命中。
    assert all(x in write_tpl for x in ('id="wf-latest-context"', 'id="wf-last-plot"',
                                        'id="wf-current-cast"', '上一段（已完成）', '角色状态'))
    assert "renderLatestContext(d)" in write_tpl and "function renderLatestContext(" in write_tpl
    assert "function renderLastPlot(" in write_tpl
    # 左栏 = 上一段的结构化事实（recent_plot_outcome.facts）+ 规划上下文折叠组；不再展示待写段
    assert "recent_plot_outcome" in write_tpl and "还没有已完成的段落" in write_tpl
    assert all(x in write_tpl for x in ('已作选择', '已知信息', '关系变化', '资源变化',
                                        '承诺更新', '新问题'))
    # 折叠组只留「待解问题」：H1 方向条（planner 视图）与弧/线程（Gantt 已有）已删
    assert "wf-last-plan" in write_tpl and "待解问题" in write_tpl
    assert "story_questions" in write_tpl and "规划上下文" not in write_tpl
    assert all(x not in write_tpl for x in ('H1 近期方向', '弧与线程', 'horizon.h1', 'arc_goal'))
    # 「人物意图」按人归属 → 放右栏角色状态底部（只列本段未出场的人），**不进左栏规划上下文组**：
    # 它的 observations 与本段出场角色卡上的「位置/状态/动作」同源，列左栏是重复。
    plan_fn = write_tpl[write_tpl.index("function _renderPlanContext"):]
    plan_fn = plan_fn[:plan_fn.index("\n}\n")]
    assert "character_intents" not in plan_fn
    assert all(x in write_tpl for x in ('_renderOtherIntents', '其他人物意图', '_INTENT_EVENT_ZH'))
    cast_fn = write_tpl[write_tpl.index("function renderCurrentCast"):]
    assert "_renderOtherIntents(data, seen)" in cast_fn
    # 分工红线：角色的 位置/目标/实力/关系 由右栏覆盖，左栏不重复；页头已占用的章号/字数也不重复
    assert "function renderCurrentCast(" in write_tpl
    assert all(x not in write_tpl for x in ('id="wf-current-plot"', '写作重点', '待写 · 第',
                                            'wf-last-progress', '全书 '))
    # 旧三列对照区（含它那个五组折叠的审计区）整块删除，且不得回流
    assert all(x not in write_tpl for x in ('id="wf-compare"', 'wf-cmp-prev', 'wf-cmp-axis',
                                            'wf-cmp-next', 'wf-cmp-grid', 'renderCompareTable',
                                            'renderCompare(', '_legacyComparison', '_renderSide',
                                            '_renderCompareGroup', '完整状态审计',
                                            '⬅ 上一段', '对照字段', 'wfc-single', 'wfc-audit'))
    # 对账视图（reconcile / expected_facts）不再进人看的页面
    assert all(x not in write_tpl for x in ('_RECONCILE_ZH', '_reconcileLabel', '_FACT_FIELD',
                                            'expected_to', '变动前值未记录', '实际变化'))
    assert "renderPlotRun(" not in write_tpl and "renderRecentOutcome(" not in write_tpl
    # 旧堆叠面板不得回流
    assert all(x not in write_tpl for x in ('id="wf-plot-run"', 'id="wf-plot-outcome"',
                                            'id="wpr-body"', 'id="wf-left-panels"'))
    # 口径红线：不许假地点、不把「未记录」画成「空值」、没数据就如实说没有
    assert "目标地点" not in write_tpl
    assert "'未记录'" in write_tpl and "'暂无明确行动'" in write_tpl and "_ctxMissing" in write_tpl
    assert "state_source" in write_tpl and "本章已上报" in write_tpl
    assert "还没有已完成的段落" in write_tpl and "本段未指定出场角色" in write_tpl
    # 跨模块信号已拆：顶部不再展示待写段 → 横幅的「下一段：…」恒按 H0 显示，不再需要同步标志
    assert "__NE_LATEST_HAS_CURRENT__" not in write_tpl and "ne:current-context-updated" not in write_tpl
    assert all(x not in write_tpl for x in ("__NE_LATEST_HAS_CURRENT__", "ne:current-context-updated",
                                            "__NE_COMPARE_HAS_CURRENT__", "ne:compare-updated"))
    # 左栏新样式类（跟着 id 一起改名，避免读起来像"当前段"）
    assert ".wf-last-title" in sl_css and ".wf-last-stamp" in sl_css and "wf-current-title" not in sl_css
    # 孤儿不得回流（曾查询一个从未存在的 #wpr-state-label）
    assert "wpr-state-label" not in write_tpl and "_runningPid" not in write_tpl
    # CSS：两栏 + 角色卡；旧三列矩阵规则已清；.wpr-dim 仍被页头用，必须留
    assert all(x in sl_css for x in (".wf-latest-context", ".wf-context-card", ".wf-context-head",
                                     ".character-state-card", ".character-state-lines",
                                     ".character-state-chip", ".character-other-group",
                                     ".character-staged-mark"))
    assert "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)" in sl_css
    assert all(x not in sl_css for x in (".wf-compare", ".wfc-table-head", ".wfc-grid",
                                         ".wfc-audit", ".wfc-reconcile", "min-width: 560px"))
    assert "#wf-left-panels" not in sl_css and ".wf-plot-run" not in sl_css
    assert ".wpr-dim" in sl_css
    # 三卡与方向入口已删
    assert 'id="wf-past-meta"' not in write_tpl and 'id="wf-current-meta"' not in write_tpl
    assert "wf-context-strip" not in write_tpl and "wf-dir-btn" not in write_tpl
    assert "wfScrollToDirections" not in write_tpl
    # 已删的空转/降级控件不得回流：跟随写作（无消费方）、Agent 检查器（内容已被面板取代）、审计模式
    assert "wf-follow-writing" not in write_tpl and "跟随写作" not in write_tpl
    assert "wf-inspector" not in write_tpl and "toggleInspector" not in write_tpl
    assert "wf-audit-mode" not in write_tpl and "toggleAuditMode" not in write_tpl
    # 用户侧不再展示 preview 校验明细；错误只走 MCP tool result / 后台日志。
    assert "planning-preview-status" not in write_tpl and "renderPreviewStatus" not in write_tpl
    assert "planning-preview-status" not in sl_css
    assert "evt.internal" in agent_js
    assert "busyPolicy === 'reject'" in agent_js
    assert "chapter_to_completion" in agent_py and "policy = \"auto\"" in agent_py
    print("[OK] 故事线 UI：单一纵向画布 / 底部双期方向 / 红黄线边界 / 正式合同刷新")
