#!/usr/bin/env python3
"""建书流程状态机（libraries/build_flow.py）单测。

纯函数部分覆盖判定表每一行 + 新鲜度窗口；存储部分跑一次真实 round-trip（自清）。
不 spawn dsh、不调 LLM。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from libraries import build_flow as BF  # noqa: E402


def _ts(offset_seconds: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + offset_seconds))


def main():
    failed = []

    def check(name, cond, detail=""):
        print(f"[{'OK' if cond else 'FAIL'}] {name}{(' ' + detail) if not cond else ''}")
        if not cond:
            failed.append(name)

    # ── 1) 判定表（快照 → 下一动作）──
    fresh = _ts(0)
    stale = _ts(-BF.FRESH_WINDOW_SECONDS - 60)
    cases = [
        ({}, "STALE", "无快照"),
        ({"cur": 3, "updated_at": stale}, "STALE", "快照过期"),
        ({"cur": 3, "updated_at": ""}, "STALE", "空时效戳"),
        ({"cur": 3, "updated_at": "不是时间"}, "STALE", "时效戳不可解析"),
        ({"cur": 1, "updated_at": fresh}, "CANDIDATES", "步 1"),
        ({"cur": 2, "updated_at": fresh}, "CANDIDATES", "步 2 未挑选"),
        ({"cur": 2, "_picked": True, "updated_at": fresh}, "CANDIDATES", "步 2 已挑选（仍是步 2）"),
        ({"cur": 3, "updated_at": fresh}, "BUILD", "步 3"),
        ({"cur": 3, "_picked": False, "updated_at": fresh}, "BUILD", "步 3 但未挑选（「跳过，手动设定」路径）"),
        ({"cur": 3, "has_world": True, "has_outline": True, "updated_at": fresh}, "BUILD", "步 3 已填（仍要 BUILD）"),
        ({"cur": 3, "book_id": "book_009", "updated_at": fresh}, "SUBMITTED", "已建书"),
        ({"cur": 3, "book_id": "book_009", "submit_error": "重名", "updated_at": fresh},
         "SUBMITTED", "成功终态优先于失败"),
        ({"cur": 3, "submit_error": "重名", "updated_at": fresh}, "FAILED", "建书失败"),
    ]
    for snap, want, label in cases:
        got = BF.next_action(BF.build_stage(snap))
        check(f"next_action {label} → {want}", got == want, f"(got {got})")

    # ── 2) 阶段推导 ──
    check("phase 步 3 = BUILDING", BF.build_stage({"cur": 3, "updated_at": fresh})["phase"] == "BUILDING")
    check("phase 已建书 = SUBMITTED",
          BF.build_stage({"cur": 3, "book_id": "book_009", "updated_at": fresh})["phase"] == "SUBMITTED")
    check("phase 失败 = FAILED",
          BF.build_stage({"cur": 3, "submit_error": "x", "updated_at": fresh})["phase"] == "FAILED")
    check("next_action 不返回 AWAIT_USER（它是记录态而非动作）",
          "AWAIT_USER" not in {BF.next_action(BF.build_stage(s, now=time.time()))
                               for s in ({}, {"cur": 3, "updated_at": fresh}, {"cur": 1, "updated_at": fresh})})

    # ── 3) 存储 round-trip（自清）──
    sid = "selftest-build-flow"
    path = BF.flow_path(sid)
    try:
        flow = BF.start_flow(sid, mode="selftest")
        check("start_flow 落 STAGING", flow["phase"] == "STAGING" and path.exists())
        flow = BF.append_child_run(sid, "BUILDING", kind="build", ok=True)
        check("append_child_run 记审计 + 转 BUILDING",
              flow["phase"] == "BUILDING" and len(flow["child_runs"]) == 1)
        BF.transition(sid, "AWAIT_USER", attempts=2)
        reloaded = BF.load_flow(sid)
        check("transition 持久化 phase/resume_point/updates",
              reloaded["phase"] == "AWAIT_USER" and reloaded["resume_point"] == "AWAIT_USER"
              and reloaded["attempts"] == 2)
        try:
            BF.save_flow(sid, {"phase": "NOPE"})
            check("非法 phase 被拒", False)
        except ValueError:
            check("非法 phase 被拒", True)
        try:
            BF.flow_path("")
            check("空 session_id 被拒", False)
        except ValueError:
            check("空 session_id 被拒", True)
    finally:
        if path.exists():
            path.unlink()
    check("测试记录已清理", not path.exists())

    if failed:
        raise AssertionError(failed)
    print("\n全部通过")


if __name__ == "__main__":
    main()
