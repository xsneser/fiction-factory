#!/usr/bin/env python3
"""主 Agent 编排协议回归：状态 / Plot 体检 / 评审接受 / 改稿 / 写作期人物修正。

锁定五件事：
  1. Plot 提交后**必然**产生规则体检报告（不依赖 Agent 主动调门禁），且报告随草稿持久化。
  2. **判决来源不可伪造（I1）**：编排路径的 accept 必须凭 `record_plot_review` 签发的
     `review_receipt`；主 Agent 自带的 verdict 无效，凭据一次性、digest 一变即 stale。
  3. 授权真源与真实守卫同源（I2）：`decision_options` 说 false 的动作，调用必被拒。
  4. 改稿走独立 revision token：只能改当前章最后一段、旧 digest/复用/非末段都拒，
     必须带 rewrite_brief，改后重跑体检。
  5. `phase=ready` 的 `save_basic_info` 是受限人物修正：必须带 revision、只准 characters、
     不得删人物；且未评审接受的草稿会挡住改人物。

用法：python tools/test_orchestrator_protocol.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _seed(bm, agent_tools, title, created):
    """建一本 phase=ready 的书：1 弧 + 1 情节段，并提交一段正文（走公开 commit_token 路径）。"""
    from libraries.storyline import BookStoryline
    from ui.web_blueprints.ctx import book_mgr
    cfg = bm.create(title=title, pen_name="测试", chapter_count=4)
    bid = cfg.book_id
    created.append(bid)
    tl = BookStoryline(book_title=title, pen_name="测试", phase="ready", words_per_chapter=3000)
    tl.basic_info = {"characters": [
        {"name": "顾衡", "role": "主角", "importance": 1, "identity": "调查员", "speech_profile": {}},
        {"name": "老赵", "role": "配角", "importance": 2, "identity": "线人", "speech_profile": {}},
    ]}
    book_mgr.save_storyline(bid, tl)
    res = agent_tools.save_outlines(
        bid, outlines=[{"id": "a1", "name": "第一弧", "start_word": 0, "end_word": 3000}],
        plots=[{"id": "p1", "name": "段1", "outline_id": "a1", "words": 1000, "roles": ["顾衡"],
                "primary_turn": "顾衡在旧工区找到转移装置仍在运转的证据"}],
        mode="replace", expected_revision=0, planning_patch={"committed_until_word": 3000})
    assert res.get("ok"), res
    return bid


def _write_plot(agent_tools, bid, text=None):
    run = agent_tools.prepare_plot_run(bid)
    body = text if text is not None else ("顾衡继续追查真相。" * 120)
    return agent_tools.save_plot_draft(
        run["run"]["commit_token"], body,
        plot_summary="顾衡在旧工区追查回声的来处，确认了转移装置仍在运转，并决定连夜返回取证、把老赵提供的线索与现场记录逐条对齐。",
        outcome={"information_revealed": ["回声来自旧工区"]})


def _expect_raise(fn, needle, label):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        assert needle in str(exc), f"{label}: 报错不含「{needle}」→ {exc}"
        return
    raise AssertionError(f"{label}: 应被拒绝但没有")


def main():
    from libraries.book_manager import BookManager
    import agent_tools
    from ui.web_blueprints.ctx import book_mgr

    bm = BookManager(os.path.join(_ROOT, "books"))
    created = []
    prev_gate = os.environ.get("NOVEL_REVIEW_GATE")
    os.environ["NOVEL_REVIEW_GATE"] = "1"     # 模拟编排路径注入的门禁
    try:
        bid = _seed(bm, agent_tools, "编排协议验收", created)

        # ── 1) 状态读取 ──
        state = agent_tools.get_orchestration_state(bid)
        assert state["current_plot"]["id"] == "p1", state
        assert state["next_action"], state
        assert state["limits"]["max_plots_per_run"] == 1, state
        assert state["schema_version"] == 2, state
        assert state["draft_digest"], state
        # 授权真源必须随状态下发：此时没草稿，写下一段是允许的
        assert state["advisory"]["decision_options"]["write_next_plot"]["allowed"] is True, state

        # ── 2) Plot 提交必然产生体检报告（无需主动调门禁） ──
        result = _write_plot(agent_tools, bid)
        assert result.get("ok"), result
        gate = result.get("quality_gate") or {}
        assert gate.get("gate_digest") and gate.get("plot_id") == "p1", gate
        assert "checks" in gate and gate.get("issues") is not None, gate
        assert gate["actual_words"] > 0, gate
        draft = agent_tools._draft_read(bid)
        assert (draft.get("plot_gates") or {}).get("p1", {}).get("gate_digest") == gate["gate_digest"], \
            "体检报告必须持久化到草稿"
        # 门禁开启时：未接受的段不许续写、不许收章（主 Agent 忘了 accept 的检测点）
        _expect_raise(lambda: agent_tools.prepare_plot_run(bid), "PLOT_REVIEW_PENDING", "未接受就续写")
        _expect_raise(lambda: agent_tools.finalize_draft_chapter(bid), "尚未评审接受", "未接受就收章")
        # 授权真源与真实守卫同源：此时 decision_options 必须显式说「不能收章、不能写下一段」
        state_pending = agent_tools.get_orchestration_state(bid)
        opts = state_pending["advisory"]["decision_options"]
        assert opts["write_next_plot"]["allowed"] is False, opts
        assert "PLOT_REVIEW_PENDING" in opts["write_next_plot"]["reasons"], opts
        assert opts["finalize_chapter"]["allowed"] is False, opts
        assert "PLOT_REVIEW_PENDING" in opts["finalize_chapter"]["reasons"], opts
        assert opts["accept_plot"]["allowed"] is False, opts
        assert "REVIEW_RECEIPT_REQUIRED" in opts["accept_plot"]["reasons"], opts

        # ── 3) 未评审接受的草稿挡住写作期人物修正 ──
        _expect_raise(
            lambda: agent_tools.save_basic_info(
                bid, {"characters": [{"name": "顾衡", "role": "主角", "importance": 1},
                                     {"name": "老赵", "role": "配角", "importance": 2}]},
                expected_revision=state["storyline_revision"]),
            "尚未评审接受", "未评审草稿存在时改人物")

        # ── 4) 评审上下文 + digest 过期即拒 ──
        ctx = agent_tools.get_plot_review_context(bid, "p1", gate_digest=gate["gate_digest"])
        assert ctx["text"].strip(), ctx
        assert ctx["quality_gate"]["gate_digest"] == gate["gate_digest"], ctx
        _expect_raise(lambda: agent_tools.get_plot_review_context(bid, "p1", gate_digest="deadbeef"),
                      "已过期", "陈旧 gate_digest")

        # ── 5) 判决必须由服务端签发（I1）：root 自带 verdict 不能通过 ──
        _expect_raise(lambda: agent_tools.accept_plot_draft(
            bid, "p1", gate_digest=gate["gate_digest"],
            critic_verdict={"verdict": "accept", "confidence": "high"}),
            "必须凭 review_receipt", "无凭据的 accept")
        # 判决写回：digest 对不上 / accept 带 error / revise 缺 brief 都被拒
        _expect_raise(lambda: agent_tools.record_plot_review(bid, "p1", "deadbeef", "accept"),
                      "不一致", "陈旧 digest 记录判决")
        _expect_raise(lambda: agent_tools.record_plot_review(
            bid, "p1", gate["gate_digest"], "accept",
            issues=[{"code": "HARD", "severity": "error", "summary": "硬伤"}]),
            "与 error 级 issue 冲突", "accept 与 error issue 冲突")
        _expect_raise(lambda: agent_tools.record_plot_review(bid, "p1", gate["gate_digest"], "revise_text"),
                      "必须给出 rewrite_brief", "revise_text 缺 brief")
        _expect_raise(lambda: agent_tools.record_plot_review(bid, "p1", gate["gate_digest"], "banana"),
                      "未知 verdict", "非法 verdict")
        receipt = agent_tools.record_plot_review(
            bid, "p1", gate["gate_digest"], "accept",
            issues=[{"code": "PACE", "severity": "warning", "summary": "节奏略慢"}])
        assert receipt["receipt_id"].startswith("rr_"), receipt
        # 状态读取必须能把凭据交给主 Agent（root 只认服务端凭据，不认 Critic 的口头结论）
        state_recv = agent_tools.get_orchestration_state(bid)
        assert state_recv["latest_draft_plot"]["review_receipt"] == receipt["receipt_id"], state_recv
        assert state_recv["advisory"]["decision_options"]["accept_plot"]["allowed"] is True, state_recv
        accepted = agent_tools.accept_plot_draft(
            bid, "p1", gate_digest=gate["gate_digest"], review_receipt=receipt["receipt_id"])
        assert accepted["review"]["state"] == "accepted", accepted
        assert accepted["review"]["review_receipt"] == receipt["receipt_id"], accepted
        # 凭据一次性
        _expect_raise(lambda: agent_tools.accept_plot_draft(
            bid, "p1", gate_digest=gate["gate_digest"], review_receipt=receipt["receipt_id"]),
            "已使用", "凭据复用")
        _expect_raise(lambda: agent_tools.accept_plot_draft(bid, "p1", gate_digest="deadbeef"),
                      "已过期", "accept 用陈旧 digest")

        # ── 6) 改稿协议：独立令牌、只改末段、改后重跑体检 ──
        _expect_raise(lambda: agent_tools.prepare_plot_revision(
            bid, "p1", expected_text_digest=gate["text_digest"]),
            "必须带 rewrite_brief", "无 brief 的改稿")
        rev = agent_tools.prepare_plot_revision(bid, "p1", expected_text_digest=gate["text_digest"],
                                                rewrite_brief={"goals": ["加快节奏"]})
        token = rev["revision_token"]
        assert token.startswith("pr_"), rev
        _expect_raise(lambda: agent_tools.prepare_plot_revision(bid, "p1",
                                                                expected_text_digest="deadbeef"),
                      "已变化", "改稿票据用陈旧 digest")
        saved = agent_tools.save_plot_revision(
            token, "顾衡重新梳理线索，发现回声的时间戳指向同一天。" * 40,
            plot_summary="顾衡重查线索后发现回声的时间戳异常，把怀疑对象进一步缩小到当夜的值班表，并决定先不惊动老赵。")
        assert saved["ok"], saved
        assert saved["quality_gate"]["text_digest"] != gate["text_digest"], "改稿后体检必须换 digest"
        _expect_raise(lambda: agent_tools.save_plot_revision(
            token, "再改一次。" * 80, plot_summary="试图复用同一张改稿票据继续改写正文内容。"),
            "已使用", "改稿票据复用")

        # 改稿后评审状态回到未接受 → 仍挡住改人物，接受后才放行
        _expect_raise(
            lambda: agent_tools.save_basic_info(
                bid, {"characters": [{"name": "顾衡", "role": "主角", "importance": 1},
                                     {"name": "老赵", "role": "配角", "importance": 2}]},
                expected_revision=agent_tools.get_orchestration_state(bid)["storyline_revision"]),
            "尚未评审接受", "改稿后未复评就改人物")
        rev_gate = saved["quality_gate"]
        rev_receipt = agent_tools.record_plot_review(bid, "p1", rev_gate["gate_digest"], "accept")
        agent_tools.accept_plot_draft(bid, "p1", gate_digest=rev_gate["gate_digest"],
                                      review_receipt=rev_receipt["receipt_id"])

        # ── 7) 写作期人物修正：越界一律拒，合规放行并 bump 版本 ──
        rev_now = agent_tools.get_orchestration_state(bid)["storyline_revision"]
        _expect_raise(lambda: agent_tools.save_basic_info(
            bid, {"world_building": {"era": "架空"}}, expected_revision=rev_now),
            "只允许提交 characters", "写作期改世界观")
        _expect_raise(lambda: agent_tools.save_basic_info(
            bid, {"characters": [{"name": "顾衡", "role": "主角", "importance": 1}]},
            expected_revision=rev_now),
            "不允许删除人物", "写作期删人物")
        _expect_raise(lambda: agent_tools.save_basic_info(
            bid, {"characters": [{"name": "顾衡", "role": "主角", "importance": 1},
                                 {"name": "老赵", "role": "配角", "importance": 2}]}),
            "必须带 expected_revision", "写作期不带版本")

        patched = agent_tools.save_basic_info(
            bid, {"characters": [
                {"name": "顾衡", "role": "主角", "importance": 1, "identity": "调查员",
                 "speech_profile": {}, "development_plan": "从独行者成为愿意求助的人"},
                {"name": "老赵", "role": "配角", "importance": 2, "identity": "线人",
                 "speech_profile": {}}]},
            expected_revision=rev_now)
        assert patched.get("ok"), patched
        assert patched["storyline_revision"] > rev_now, patched
        assert patched.get("scope") == "ready_character_patch", patched
        assert "world_check" in patched, patched

        print("[OK] 编排状态：schema 2 / next_action / 当前 Plot / 预算上限")
        print("[OK] 授权真源：decision_options 的 allowed/reasons 与真实守卫一致")
        print("[OK] 评审门禁：未接受的段不许续写、不许收章（legacy 不注入该开关）")
        print("[OK] Plot 提交必然产出并持久化规则体检报告")
        print("[OK] 判决来源：自带 verdict 无效、凭据一次性、digest 变化即 stale（I1）")
        print("[OK] 改稿协议：独立单次令牌、只改末段、必带 brief、改后重跑体检")
        print("[OK] 写作期人物修正：带版本、只准 characters、不得删人物、bump 版本")
    finally:
        import shutil
        for bid in created:
            shutil.rmtree(os.path.join(_ROOT, "books", bid), ignore_errors=True)
        if prev_gate is None:
            os.environ.pop("NOVEL_REVIEW_GATE", None)
        else:
            os.environ["NOVEL_REVIEW_GATE"] = prev_gate


if __name__ == "__main__":
    main()
