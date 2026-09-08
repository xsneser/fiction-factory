#!/usr/bin/env python3
"""R5/R7 单元测试 —— 纯 Python，不需 mcp/LLM/服务器。

覆盖（对应修订验收）：
  1. env 未设置 → MCP profile 默认开；AGENT_TOOL_PROFILES=0/off → legacy 关
  2. 未分类任务 → inspect（只读小面，绝不回退全量 45）
  3. 意图 → profile 分类（replan/continue 优先于 write）
  4. [NEED_REPLAN] 机器交接解析
  5. story_questions 语义合并（去重/状态机/终态不复开）
  6. character_intents 按人物 upsert

用法：python tools/test_replan_units.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}  {detail}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def test_flow_orchestration() -> str:
    """run_dsh_flow 分支验收：自动/confirm/无标记 都只产 1 个 done、链数正确。"""
    import libraries.dsh_bridge as B

    real = B.run_dsh_task
    calls = []

    def fake_run(task, history=None, debug=False):
        calls.append(task or "")
        t = task or ""
        if "[系统自动触发续规划]" in t:
            evs = [{"type": "tool_result", "name": "drive_ui", "ok": True},
                   {"type": "reply", "content": "已暂存预览 pv_x。"}]
        elif "[系统自动续写]" in t:
            evs = [{"type": "reply", "content": "继续写作完成。"}]
        elif "写下一章" in t:
            evs = [{"type": "reply", "content": "写完了。\n[NEED_REPLAN] book_id=book_007 reason=PLOTS_LOW"}]
        else:
            evs = [{"type": "reply", "content": "普通回复。"}]
        evs = evs + [{"type": "done"}]
        for e in evs:
            yield dict(e)

    B.run_dsh_task = fake_run
    import libraries.replan_service as RS
    import libraries.planning_state as PS
    _cp, _lp = RS.commit_replan_preview, PS.load_replan_preview
    committed = []
    RS.commit_replan_preview = lambda *a, **k: (committed.append(a) or {"ok": True})
    PS.load_replan_preview = lambda bid: {"preview_id": "pv_x", "expected_revision": 1}
    try:
        # auto：写(marker)→replan→commit→resume，共 3 次 spawn，恰好 1 done
        calls.clear(); committed.clear()
        evs = list(B.run_dsh_flow("写下一章", policy="auto"))
        if sum(1 for e in evs if e.get("type") == "done") != 1:
            return f"auto done 数≠1: {[e.get('type') for e in evs]}"
        if len(calls) != 3 or not committed:
            return f"auto 链数不对 calls={len(calls)} committed={bool(committed)}"
        if not any(e.get("type") == "reply" and "续规划已自动提交" in (e.get("content") or "")
                   for e in evs):
            return "auto 缺 orchestrator 状态回复"
        # confirm：写(marker)→replan 停，共 2 次 spawn、不 commit、1 done
        calls.clear(); committed.clear()
        evs2 = list(B.run_dsh_flow("写下一章", policy="confirm"))
        if sum(1 for e in evs2 if e.get("type") == "done") != 1 or committed or len(calls) != 2:
            return f"confirm 分支不对 done={sum(1 for e in evs2 if e.get('type')=='done')} " \
                   f"committed={bool(committed)} calls={len(calls)}"
        # 无标记：1 次 spawn、1 done、原样转发回复
        calls.clear()
        evs3 = list(B.run_dsh_flow("闲聊一句", policy="auto"))
        if sum(1 for e in evs3 if e.get("type") == "done") != 1 or len(calls) != 1:
            return f"无标记分支不对 calls={len(calls)} types={[e.get('type') for e in evs3]}"
        return ""
    finally:
        B.run_dsh_task = real
        RS.commit_replan_preview, PS.load_replan_preview = _cp, _lp


def main():
    import libraries.dsh_bridge as B
    from libraries.agent_tool_router import PROFILE_TOOLS

    # 1) profile 默认开 / env 关闭
    os.environ.pop("AGENT_TOOL_PROFILES", None)
    check("profiles 默认开启（env 未设）", B._profiles_enabled() is True)
    os.environ["AGENT_TOOL_PROFILES"] = "0"
    check("AGENT_TOOL_PROFILES=0 → legacy 关闭", B._profiles_enabled() is False)
    os.environ["AGENT_TOOL_PROFILES"] = "off"
    check("AGENT_TOOL_PROFILES=off → 关闭", B._profiles_enabled() is False)
    os.environ.pop("AGENT_TOOL_PROFILES", None)

    # 2) 分类（含未分类→inspect，绝不回退空/全量）
    cases = {
        "写下一章": "write", "继续": "write", "继续写第5章": "write", "续写": "write",
        "帮我往下想一段": "replan", "续规划": "replan", "扩弧": "replan",
        "给枫落加风格规则": "style", "查样文池": "style",
        "开新书": "build-candidates", "生成候选": "build-candidates",
        "建书": "build", "补全世界观": "build",
        "上架这本书": "publish", "完本": "publish",
        "抓取番茄小说": "scout", "侦察热榜": "scout",
        "你好，看看进度": "inspect", "这本书写到哪了": "inspect",
    }
    for text, want in cases.items():
        got = B._task_tool_profile(text)
        check(f"classify {text!r} → {want}", got == want, f"(got {got})")
    insp = PROFILE_TOOLS["inspect"]
    check("inspect 是小只读面（非全量45）",
          len(insp) < 20 and "save_chapter_text" not in insp
          and "fetch_novel" not in insp and "publish_book" not in insp,
          f"inspect={sorted(insp)}")

    # 3) NEED_REPLAN 解析
    parsed = B.parse_need_replan("正文写完……\n[NEED_REPLAN] book_id=book_009 reason=PLOTS_LOW;WORDS_LOW")
    check("parse_need_replan 命中 book/reason", parsed and parsed["book_id"] == "book_009"
          and parsed["reason"] == "PLOTS_LOW;WORDS_LOW", f"{parsed}")
    check("parse_need_replan 普通回复 None", B.parse_need_replan("写得不错") is None)

    # 4) story_questions 语义合并
    from libraries.planning_state import upsert_story_questions as uq, upsert_character_intents as uc
    r = uq([{"id": "q1", "question": "谁是内鬼？", "status": "open"}],
           [{"id": "q1", "question": "谁是内鬼？", "status": "progressed",
             "last_touched_plot_id": "p1"}])
    check("story_questions 同 id 更新状态", len(r) == 1 and r[0]["status"] == "progressed",
          f"{r}")
    r2 = uq([{"id": "q1", "question": "谁是内鬼？", "status": "open"}],
            [{"question": "谁是内鬼？", "status": "progressed"}])
    check("story_questions 同文去重（不重复 append）", len(r2) == 1 and r2[0]["id"] == "q1",
          f"{r2}")
    r3 = uq([{"id": "q9", "question": "旧谜", "status": "answered"}],
           [{"question": "旧谜", "last_touched_plot_id": "p9"}])   # 无显式 status 的轻触
    check("answered 不带显式 status 不复开",
          len(r3) == 1 and r3[0]["status"] == "answered" and r3[0]["id"] == "q9", f"{r3}")
    r4 = uq([{"id": "q9", "question": "旧谜", "status": "answered"}],
           [{"question": "旧谜", "status": "open"}])
    check("answered 显式重开允许", r4 and r4[0]["status"] == "open", f"{r4}")
    r5 = uq([], [{"question": "新问题", "priority": 0.8, "source_plot_id": "p2"}])
    check("新问题自动生成稳定 id 且默认 open",
          len(r5) == 1 and r5[0]["id"] and r5[0]["status"] == "open"
          and r5[0]["source_plot_id"] == "p2", f"{r5}")

    # 5) character_intents upsert
    c = uc([{"name": "顾衡", "intent": "证明自己", "status": "active"}],
           [{"name": "顾衡", "intent": "承担系统责任"}, {"name": "林晚", "intent": "活下去"}])
    by = {x["name"]: x for x in c}
    check("character_intents 同人 upsert + 新人追加",
          len(c) == 2 and by["顾衡"]["intent"] == "承担系统责任"
          and by["林晚"]["intent"] == "活下去", f"{c}")

    ferr = test_flow_orchestration()
    check("orchestrator auto/confirm/无标记（各 1 done、链数正确）", not ferr, ferr or "")

    print("\n" + "=" * 50)
    print(f"  Replan 单元验收: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("  失败:", "、".join(FAIL))
        sys.exit(1)
    print("  ✅ 全部通过")
    print("=" * 50)


if __name__ == "__main__":
    main()
