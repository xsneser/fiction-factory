#!/usr/bin/env python3
"""建书服务端 FSM 派发单测（libraries/dsh_bridge._build_fsm）。

全部走假 run_dsh_task + 假向导快照：不起 dsh 子进程、不调 LLM、不动真实 build_status.json
（只 monkeypatch 读函数），Flow 记录用一次性 session id 并自清。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import libraries.build_status as BS  # noqa: E402
import libraries.dsh_bridge as B  # noqa: E402
from libraries import build_flow as BF  # noqa: E402

SID = "selftest-build-fsm"
OTHER_SID = "selftest-build-fsm-other"
STEP3_TASK = ("请继续建这本新书（步 2 已选定候选「2050：星港从零建起」），自主生成填写步 3 "
              "内容表单及故事线：挑选弧 → 挑选情节段 → 补全其余表单。")


def _fresh() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _snap(**over) -> dict:
    base = {"cur": 3, "book_id": "", "build_session_id": SID, "creating": False,
            "created": False, "_picked": True, "pen_selected": True, "has_world": False,
            "has_picks": False, "has_outline": False, "updated_at": _fresh(),
            "submit_error": ""}
    base.update(over)
    return base


def main():
    failed = []
    calls = []
    real_run, real_status = B.run_dsh_task, BS.get_build_status

    def fake_run(task, history=None, debug=False, flow_id="", child_run_id="", book_id="",
                 mcp_profile=""):
        calls.append({"task": task, "mcp_profile": mcp_profile, "flow_id": flow_id,
                      "child_run_id": child_run_id, "history": history})
        yield {"type": "tool_call", "name": "drive_ui"}
        yield {"type": "reply", "content": "（子 run 回复）"}
        yield {"type": "done"}          # 子 done 必须被吞掉，不能透传给前端

    def check(name, cond, detail=""):
        print(f"[{'OK' if cond else 'FAIL'}] {name}{(' ' + detail) if not cond else ''}")
        if not cond:
            failed.append(name)

    def run(task, snapshot):
        calls.clear()
        BS.get_build_status = lambda: snapshot
        return list(B._build_fsm(task, None, False))

    B.run_dsh_task = fake_run
    try:
        # ── 1) 步 3（cur=3）→ build profile ──
        evs = run(STEP3_TASK, _snap(cur=3))
        check("步 3 起 build profile（本次修复的核心）",
              len(calls) == 1 and calls[0]["mcp_profile"] == "build",
              f"calls={[c['mcp_profile'] for c in calls]}")
        check("步 3 走标记里的 session id",
              calls[0]["flow_id"] == SID, f"flow_id={calls[0]['flow_id']}")
        check("步 3 薄任务不枚举工具名",
              "mcp__novelengine" not in calls[0]["task"] and "drive_ui" not in calls[0]["task"])
        check("步 3 尾部只有一个 done", sum(e.get("type") == "done" for e in evs) == 1)
        check("子 run 的中间事件（工具卡/回复）照常透传",
              any(e.get("type") == "tool_call" for e in evs)
              and any("（子 run 回复）" in (e.get("content") or "") for e in evs))
        check("步 3 reply 提醒用户自己点提交",
              any("自己点" in (e.get("content") or "") for e in evs if e.get("type") == "reply"))
        check("Flow 落到 BUILDING", BF.load_flow(SID)["phase"] == "BUILDING")

        # ── 2) 步 1-2（cur=2）→ build-candidates profile ──
        evs = run('请继续建这本新书（步 2 已选定候选「星环工兵」）', _snap(cur=2, _picked=False))
        check("步 2 起 build-candidates profile",
              len(calls) == 1 and calls[0]["mcp_profile"] == "build-candidates",
              f"calls={[c['mcp_profile'] for c in calls]}")
        check("步 2 Flow 落 STAGING", BF.load_flow(SID)["phase"] == "STAGING")

        # ── 3) 已建书 → SUBMITTED，不起 run ──
        evs = run("继续建书", _snap(book_id="book_009"))
        check("已建书不起 run", not calls)
        check("已建书 reply 带着 book_id",
              any("book_009" in (e.get("content") or "") for e in evs))

        # ── 4) submit_error → FAILED，原文透出 ──
        evs = run("继续建书", _snap(submit_error="书名重复"))
        check("建书失败不起 run", not calls)
        check("失败原因是 error 事件且带原文",
              any(e.get("type") == "error" and "书名重复" in (e.get("message") or "") for e in evs))

        # ── 5) 无快照 → STALE，退回关键词判到的 profile ──
        evs = run("生成故事线", {})
        check("STALE 退回关键词 profile（build）",
              len(calls) == 1 and calls[0]["mcp_profile"] == "build",
              f"calls={[c['mcp_profile'] for c in calls]}")
        check("STALE 且无 session → 不记 Flow（flow_id 空）",
              calls[0]["flow_id"] == "", f"flow_id={calls[0]['flow_id']!r}")

        # ── 6) 快照属于别的向导页（任务带标记且与之不一致）→ 不采信快照，退回关键词 ──
        evs = run(f"生成故事线 build_session={SID}", _snap(build_session_id=OTHER_SID))
        check("session 错配不采信快照（退回关键词 build，且仍用任务标记的 sid）",
              len(calls) == 1 and calls[0]["mcp_profile"] == "build"
              and calls[0]["flow_id"] == SID,
              f"calls={[(c['mcp_profile'], c['flow_id']) for c in calls]}")

        # ── 7) 连败上限：同一阶段重跑超过 MAX_BUILD_ATTEMPTS → 显式失败 ──
        path = BF.flow_path(SID)
        if path.exists():
            path.unlink()
        for i in range(B.MAX_BUILD_ATTEMPTS):
            evs = run(STEP3_TASK, _snap(cur=3))
            check(f"连败第 {i + 1} 轮仍起 run", len(calls) == 1)
        evs = run(STEP3_TASK, _snap(cur=3))
        check("超过上限不再起 run", not calls, f"calls={[c['mcp_profile'] for c in calls]}")
        check("超过上限显式报错（不空转烧会话）",
              any(e.get("type") == "error" and "没换来阶段推进" in (e.get("message") or "")
                  for e in evs))
        check("超限后 Flow 落 FAILED", BF.load_flow(SID)["phase"] == "FAILED")

        # ── 8) 换阶段即归零（连败计数不该跨阶段污染）──
        path = BF.flow_path(SID)
        if path.exists():
            path.unlink()
        run(STEP3_TASK, _snap(cur=3))       # BUILDING, attempts=1
        run("开新书", _snap(cur=2, _picked=False))   # 换 STAGING
        run(STEP3_TASK, _snap(cur=3))       # 回 BUILDING → 应重新计 1
        check("换阶段后计数归零", BF.load_flow(SID)["attempts"] == 1,
              f"attempts={BF.load_flow(SID)['attempts']}")

        # ── 9) UNROUTABLE 显式指引（run_dsh_flow 层）──
        B.run_dsh_task = real_run
        evs = list(B.run_dsh_flow("今天天气不错"))
        check("未分类不静默回落只读面",
              any(e.get("type") == "error" and "没看出要做哪个阶段" in (e.get("message") or "")
                  for e in evs))
        check("未分类指引列出阶段入口",
              any("写下一章" in (e.get("message") or "") and "续规划" in (e.get("message") or "")
                  for e in evs))
        check("未分类只发一个 done", sum(e.get("type") == "done" for e in evs) == 1)
    finally:
        B.run_dsh_task = real_run
        BS.get_build_status = real_status
        for sid in (SID, OTHER_SID):
            p = BF.flow_path(sid)
            if p.exists():
                p.unlink()

    if failed:
        raise AssertionError(failed)
    print("\n全部通过")


if __name__ == "__main__":
    main()
