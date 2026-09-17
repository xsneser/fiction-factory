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
    from agent_tools import save_basic_info
    from libraries.plot_run_state import make_plot_delta, stage_delta
    save_basic_info(bid, {"characters": [{"name": "陆凌舟", "role": "主角", "importance": 1,
                                          "identity": "工程师", "speech_profile": {}}]})
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
    assert "current_chapter" in desk and "comparison" in desk
    assert isinstance(desk["comparison"].get("sections"), list)
    from ui.web_blueprints.desk import _build_compare_projection
    projection = _build_compare_projection(
        {"plot_id": "p0", "plot_name": "上一段", "chapter_num": 1, "roles": ["甲", "丁"],
         "facts": {"character_events": [{"name": "甲", "events": [
             {"type": "location_shift", "to": "旧港"}]}]}},
        {"plot": {"id": "p2", "name": "当前段", "roles": ["乙", "戊"], "expected_facts": [
             {"subject": "乙", "type": "goal_shift", "expected_to": "夺回钥匙"}]},
         "cast_pack": {"protagonists": [{"name": "乙", "dyn": {"goal": "寻找线索"}}],
                       "active": [{"name": "丙", "dyn": {"location": "新城"}}]},
         "execution_brief": {"dramatic_goal": "迫使双方摊牌"},
         "arc_goal": {"arc_id": "a2", "name": "第二弧", "stage_name": "逼近真相"},
         "thread": {"id": "t1", "name": "钥匙线", "chain_note": "已写 1 条 / 未写 2 条"}}, 2)
    chars = next(s for s in projection["sections"] if s["id"] == "characters")
    assert [r["entity"] for r in chars["rows"] if r["label"] == "人物"] == ["乙", "丙", "戊", "甲", "丁"]
    assert next(r for r in chars["rows"] if r["entity"] == "丁" and r["label"] == "人物")["current"] == {}
    # 每角色合并为一行：不再按 目标/实力/关系/弧阶段 拆多行，字段内联进 fields 数组
    assert all(r["label"] == "人物" for r in chars["rows"])
    yifld = next(r for r in chars["rows"] if r["entity"] == "乙")["current"].get("fields") or []
    assert any(f["label"] == "目标" and f["value"] == "寻找线索" and f["expected_to"] == "夺回钥匙"
               for f in yifld)
    locs = next(s for s in projection["sections"] if s["id"] == "locations")
    assert any(r["label"] == "地点" and r["current"].get("value") == "新城" for r in locs["rows"])
    # 地点不绑定人物：行标签统一为「地点」，不再按「位置 · 人名」分行
    assert all(r["label"] == "地点" for r in locs["rows"])
    arcs = next(s for s in projection["sections"] if s["id"] == "arc_thread")
    assert {r["label"] for r in arcs["rows"]} == {"弧阶段", "叙事线程"}
    # 对照 projection：弧线程独立、stable key 配对、计数不依赖 entity 是否存在。
    stable = _build_compare_projection(
        {"facts": {"promise_updates": [{"id": "pr-a", "desc": "钥匙必须兑现"}],
                    "new_story_questions": [{"id": "q-a", "question": "谁持有钥匙？"}]}},
        {"plot": {"id": "p3", "name": "稳定配对测试"},
         "promise_state": [{"id": "pr-b", "desc": "警报会响起"},
                            {"id": "pr-a", "desc": "钥匙必须兑现"}],
         "planning": {"story_questions": [{"id": "q-a", "question": "谁持有钥匙？"},
                                             {"id": "q-b", "question": "谁在监视？"}]}}, 2)
    stable_promises = next(s for s in stable["sections"] if s["id"] == "promises")
    stable_questions = next(s for s in stable["sections"] if s["id"] == "questions")
    assert [r["key"] for r in stable_promises["rows"]] == ["promise:id:pr-a", "promise:id:pr-b"]
    assert [r["key"] for r in stable_questions["rows"]] == ["question:id:q-a", "question:id:q-b"]
    print("[OK] compare 口径：desk cast_pack 与 writer 同源（staged 投影 + state_source）")

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
    pu_js = open(os.path.join(root, "ui", "static", "js", "planning_ui.js"), encoding="utf-8").read()
    panels = open(os.path.join(root, "ui", "templates", "_book_runtime_panels.html"), encoding="utf-8").read()
    start_tpl = open(os.path.join(root, "ui", "templates", "start_book.html"), encoding="utf-8").read()
    write_tpl = open(os.path.join(root, "ui", "templates", "storyline_write_flow.html"), encoding="utf-8").read()
    book_tpl = open(os.path.join(root, "ui", "templates", "book_detail.html"), encoding="utf-8").read()
    planning_tpl = open(os.path.join(root, "ui", "templates", "_planning_ui.html"), encoding="utf-8").read()
    agent_js = open(os.path.join(root, "ui", "static", "js", "agent_panel.js"), encoding="utf-8").read()
    agent_py = open(os.path.join(root, "ui", "web_blueprints", "agent.py"), encoding="utf-8").read()
    desk_py = open(os.path.join(root, "ui", "web_blueprints", "desk.py"), encoding="utf-8").read()
    assert "arc_thread_rows" in desk_py and "pair_records" in desk_py
    assert '"missing_from": event.get("from") in' in desk_py

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

    # 写作台只保留必要提示；完整规划矩阵仍由书详情的非 compact 分支提供。
    assert "<h4>下一段方向</h4>" not in pu_js and "<h4>远期方向</h4>" not in pu_js
    assert "pm-h1" not in pu_js and "pm-h2" not in pu_js
    assert "<h4>现在执行</h4>" in pu_js
    assert "renderChecklist" in pu_js and "planning-checklist" in pu_js
    assert "openStoryQuestions" in pu_js and "compareHasCurrentPlot" in pu_js
    assert "lastReplanLabel" in pu_js and "reasonLabel(value.reason_codes)" in pu_js
    assert "展开规划" not in pu_js and "收起 ▴" not in pu_js
    assert "detailOpen" not in pu_js and "planning-cards" not in pu_js
    assert "当前没有额外规划提示" not in pu_js
    assert "pm-alert-danger" in pu_js and "pm-boundary" in pu_js
    assert "planning-grid" in pu_js and "最近续规划" in pu_js
    assert "🧭 规划中（未落笔）" in panels and "d.planned" in panels

    # replan 提交后的正式合同可重新拉取；三页都接收统一故事线刷新事件。
    assert "GET /api/storyline" not in pu_js  # 使用 fetch，避免把 HTTP 文本硬编码进实现
    assert "/api/storyline/' + encodeURIComponent(bookId)" in pu_js
    assert "ne:storyline-updated" in write_tpl and "ne:storyline-updated" in book_tpl
    assert "syncStoryline" in pu_js and "mergeStorylineRuntimeFields" in pu_js

    # 步 3 的 Gantt 读写 planning 都接通，且写作台轮询比较 H1/H2 并合并状态。
    assert "planning: (d.planning" in start_tpl
    assert "planning: (args.planning" in start_tpl
    assert "WZ.renderStoryline()" in start_tpl
    assert "JSON.stringify(newHorizon.h1" in write_tpl
    assert "JSON.stringify(d.planning.future_intents" in write_tpl
    assert "Object.assign({}, oldPlanning, d.planning)" in write_tpl

    # 规划已改由章级 FSM 在续写父任务中自主完成，页面不再有手工抽屉。
    assert all(x not in planning_tpl for x in ("replan-drawer", "replan-backdrop", "revision-modal", "boundary-banner"))
    assert all(x not in pu_js for x in ("requestReplan", "openDrawer", "commitPreview"))
    assert "set_replan_preview" in pu_js and "refresh();" in pu_js
    assert "flowMode" in write_tpl and "busyPolicy" in write_tpl and "taskKind" in write_tpl
    assert "needs_replan" not in write_tpl[write_tpl.index("function _resolveNextChapterAndSend"):write_tpl.index("function stopWritingTask")]
    assert "flow_mode" in agent_js and "busy_policy" in agent_js and "task_kind" in agent_js
    # 全宽三列对照区：HTML 挂载点与 JS 查询点双向命中，且只有一个共享表格渲染入口
    assert all(x in write_tpl for x in ('id="wf-compare"', 'id="wf-cmp-prev"',
                                        'id="wf-cmp-axis"', 'id="wf-cmp-next"',
                                        'id="wf-cmp-grid"'))
    assert "getElementById('wf-cmp-grid')" in write_tpl
    assert "renderCompare(d.recent_plot_outcome, d.plot_run, d.current_chapter, d.comparison)" in write_tpl
    assert "ne:compare-updated" in write_tpl and "__NE_COMPARE_HAS_CURRENT__" in pu_js
    assert "renderCompareTable(" in write_tpl
    assert "renderPlotRun(" not in write_tpl and "renderRecentOutcome(" not in write_tpl
    assert "上一情节段" in write_tpl and "对照字段" in write_tpl and "当前情节段" in write_tpl
    assert "上一段" in write_tpl and "本段" in write_tpl and "写作重点" in write_tpl
    assert "上一情节段（已发生）" in write_tpl and "当前情节段（本段待写）" in write_tpl
    assert "完整状态审计" in write_tpl and "wfc-single" in write_tpl
    assert "上一情节造成的变化" not in write_tpl and "身后变化" not in write_tpl
    assert "实际变化" not in write_tpl
    # 旧堆叠面板不得回流
    assert all(x not in write_tpl for x in ('id="wf-plot-run"', 'id="wf-plot-outcome"',
                                            'id="wpr-body"', 'id="wf-left-panels"'))
    # 口径红线：不许假地点、不把「未记录」画成「空值」、from 缺失要有说明
    assert "目标地点" not in write_tpl
    assert "状态未记录" in write_tpl and "变动前值未记录" in write_tpl
    assert "state_source" in write_tpl and "本章已上报" in write_tpl
    assert "_FACT_FIELD" in write_tpl and "expected_to" in write_tpl
    # 孤儿不得回流（曾查询一个从未存在的 #wpr-state-label）
    assert "wpr-state-label" not in write_tpl and "_runningPid" not in write_tpl
    # CSS：三列矩阵/折叠区/响应式规则在，旧面板规则已清，.wpr-dim 必须留
    assert all(x in sl_css for x in (".wf-compare", ".wfc-table-head", ".wfc-grid",
                                     ".wfc-label-cell", ".wfc-group", ".wfc-collapsed-preview"))
    assert "grid-template-columns: minmax(0, 1fr) minmax(82px, 112px) minmax(0, 1fr)" in sl_css
    assert "overflow-x: auto" in sl_css and "min-width: 560px" in sl_css
    assert "#wf-left-panels" not in sl_css and ".wf-plot-run" not in sl_css
    assert ".wpr-dim" in sl_css
    assert ".wfc-reconcile" in sl_css and ".wfc-audit" in sl_css
    assert ".wfc-col[hidden] { display: block; visibility: hidden; min-width: 0" in sl_css
    assert ".wfc-cell.wfc-empty-side { min-height: 0" in sl_css
    assert ".wfc-no-previous" in sl_css and ".wfc-no-current" in sl_css
    # 三卡与方向入口已删（运行上下文由全宽对照区承担）
    assert 'id="wf-past-meta"' not in write_tpl and 'id="wf-current-meta"' not in write_tpl
    assert "wf-context-strip" not in write_tpl and "wf-dir-btn" not in write_tpl
    assert "wfScrollToDirections" not in write_tpl
    # 已删的空转/降级控件不得回流：跟随写作（无消费方）、Agent 检查器（内容已被面板取代）、审计模式
    assert "wf-follow-writing" not in write_tpl and "跟随写作" not in write_tpl
    assert "wf-inspector" not in write_tpl and "toggleInspector" not in write_tpl
    assert "wf-audit-mode" not in write_tpl and "toggleAuditMode" not in write_tpl
    # 预测 → 实际 → 结果：predictions 入面板，对账中文标签共用同一映射
    assert "outcome.predictions" in write_tpl and "_reconcileLabel(" in write_tpl and "_RECONCILE_ZH" in write_tpl
    # 用户侧不再展示 preview 校验明细；错误只走 MCP tool result / 后台日志。
    assert "planning-preview-status" not in planning_tpl
    assert "renderPreviewStatus" not in pu_js
    assert "planning-preview-status" not in sl_css
    assert "evt.internal" in agent_js
    assert "busyPolicy === 'reject'" in agent_js
    assert "chapter_to_completion" in agent_py and "policy = \"auto\"" in agent_py
    print("[OK] 故事线 UI：单一纵向画布 / 底部双期方向 / 红黄线边界 / 正式合同刷新")
