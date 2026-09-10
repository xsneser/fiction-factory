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
    """run_dsh_flow 分支验收（R7 FSM 模型）：

    - write 意图 → _writer_fsm：带书号时首个子 run 收「归一化薄续写任务」（无聊天历史、无任何工具名，
      杜绝任务里出现 write profile 之外的工具名）；子 run 若未提交任何 Plot → 恰 1 error + 1 done。
    - 非 write（闲聊）→ 单 spawn、1 done、原样转发回复。
    """
    import libraries.dsh_bridge as B

    real = B.run_dsh_task
    calls = []

    def fake_run(task, history=None, debug=False, **kw):
        calls.append((task or "", history))
        for e in [{"type": "reply", "content": "写完了。"}, {"type": "done"}]:
            yield dict(e)

    B.run_dsh_task = fake_run
    try:
        # write：带书号 → 首个子 run 收归一化任务；fake 无 plot_run_changed → 子 run 未提交 = 真实失败路径
        calls.clear()
        evs = list(B.run_dsh_flow("继续写 book_042", policy="auto"))
        if sum(1 for e in evs if e.get("type") == "done") != 1:
            return f"write→FSM done 数≠1: {[e.get('type') for e in evs]}"
        if sum(1 for e in evs if e.get("type") == "error") != 1:
            return "write 子 run 未提交应恰 1 error"
        if len(calls) != 1:
            return f"writer spawn 数≠1: {len(calls)}"
        t, h = calls[0]
        if "book_042" not in t or "当前 Plot" not in t or h is not None:
            return f"首个子 run 未归一化薄任务: task={t!r} hist={h}"
        for tool in ("prepare_plot_run", "save_plot_draft", "get_pen_style",
                     "save_chapter_text", "chapter_quality_gate"):
            if tool in t:
                return f"子 run 任务不应含工具名 {tool}: {t!r}"
        # 无标记（非 write）：1 次 spawn、1 done、原样转发回复
        calls.clear()
        evs3 = list(B.run_dsh_flow("闲聊一句", policy="auto"))
        if sum(1 for e in evs3 if e.get("type") == "done") != 1 or len(calls) != 1:
            return f"无标记分支不对 calls={len(calls)} types={[e.get('type') for e in evs3]}"
        return ""
    finally:
        B.run_dsh_task = real


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
    os.environ.pop("ALLOW_UNSCOPED_AGENT_TOOLS", None)
    check("legacy 未显式授权", B._unscoped_tools_allowed() is False)
    blocked = list(B.run_dsh_task("闲聊"))
    check("legacy 未授权不启动", any("ALLOW_UNSCOPED_AGENT_TOOLS" in (e.get("message") or "")
                                     for e in blocked))
    os.environ["ALLOW_UNSCOPED_AGENT_TOOLS"] = "1"
    check("legacy 需双开关授权", B._unscoped_tools_allowed() is True)
    os.environ.pop("ALLOW_UNSCOPED_AGENT_TOOLS", None)
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
