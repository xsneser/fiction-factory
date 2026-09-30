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
from libraries import build_draft as BD  # noqa: E402
from libraries import build_flow as BF  # noqa: E402

SID = "selftest-build-fsm"
OTHER_SID = "selftest-build-fsm-other"

# 向导真实任务文本（ui/templates/start_book.html：步 1 约 432-436、步 2→3 约 350-351）。
# 这两段文本是**表单数据的唯一载体**（服务端没有副本），下面据此断言子 run 必须收到原文。
STEP1_TASK = ('请为这本新书生成世界观候选（已完成步 1 填表、已自动进步 2）：一句话设定「2050」'
              '题材标签「穿越、星际、异界、基建、军事」笔名「星烬」。'
              '笔名未选就 query_profiles 查一个最匹配的并 drive_ui(set_field pen) 补填。')
STEP3_TASK = ("请继续建这本新书（步 2 已选定候选「2050：星港从零建起」），自主生成填写步 3 "
              "内容表单及故事线：挑选弧 → 挑选情节段 → 补全其余表单。")
STEP1_DATA = ("2050", "穿越、星际、异界、基建、军事")     # 步 1 必须透传的 idea / 标签
STEP3_DATA = ("2050：星港从零建起",)                      # 步 3 必须透传的用户选定候选
HISTORY = [{"role": "user", "content": "我想要偏军事硬核的方向"},
           {"role": "assistant", "content": "好的"}]


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
        # 模拟真实 build 子 run 的**副作用**：把草稿落 canonical。收尾文案与"本轮是否产出
        # 草稿"的护栏都以 canonical 为准（不再无条件宣称"已发起填写"），夹具不写就会被
        # 判成空转/无产出。
        if mcp_profile == "build" and flow_id:
            BD.update(flow_id, draft={"world": {"world_building": {"core_conflict": "测试"}},
                                      "storyline": {"outlines": [{"id": "o1"}], "plots": [{"id": "p1"}]},
                                      "characters": [{"name": "甲", "role": "主角", "importance": 1}]})
        yield {"type": "tool_call", "name": "drive_ui"}
        yield {"type": "reply", "content": "（子 run 回复）"}
        yield {"type": "done"}          # 子 done 必须被吞掉，不能透传给前端

    def check(name, cond, detail=""):
        print(f"[{'OK' if cond else 'FAIL'}] {name}{(' ' + detail) if not cond else ''}")
        if not cond:
            failed.append(name)

    def drop_rec(sid=SID):
        """清 canonical 记录：各用例必须独立（步 3 的恢复链会写记录，残留会串台）。"""
        try:
            p = BD.path_for(sid)
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    def run(task, snapshot, keep_record=False):
        """跑一轮 FSM。默认**清掉 canonical 记录**让各用例独立——步 3 的上下文恢复链
        会写记录，残留会让下一条用例走「记录权威」分支而串台（实测踩过）。
        `keep_record=True` 供显式铺记录用例（10b~10g）使用。"""
        calls.clear()
        if not keep_record:
            drop_rec()
        BS.get_build_status = lambda: snapshot
        return list(B._build_fsm(task, HISTORY, False))

    B.run_dsh_task = fake_run
    try:
        # ── 1) 步 3（cur=3）→ build profile ──
        evs = run(STEP3_TASK, _snap(cur=3))
        check("步 3 起 build profile（本次修复的核心）",
              len(calls) == 1 and calls[0]["mcp_profile"] == "build",
              f"calls={[c['mcp_profile'] for c in calls]}")
        check("步 3 走标记里的 session id",
              calls[0]["flow_id"] == SID, f"flow_id={calls[0]['flow_id']}")
        check("步 3 尾部只有一个 done", sum(e.get("type") == "done" for e in evs) == 1)
        check("子 run 的中间事件（工具卡/回复）照常透传",
              any(e.get("type") == "tool_call" for e in evs)
              and any("（子 run 回复）" in (e.get("content") or "") for e in evs))
        check("步 3 reply 提醒用户自己点提交",
              any("自己点" in (e.get("content") or "") for e in evs if e.get("type") == "reply"))
        check("Flow 落到 BUILDING", BF.load_flow(SID)["phase"] == "BUILDING")
        # 步 3 改为**薄任务 + 不转发历史**（2026-09-10 收口）：数据走 get_build_context
        # 读 canonical 记录。此前转发整段聊天 → 单次 8.3 万字符 / ~4.2 万 token，且内容
        # 与服务端判定互相矛盾（模型被迫仲裁）。
        check("步 3 子 run 收薄任务（不再转发整段原文）",
              calls[0]["task"].startswith("完成建书步 3（build session=")
              and len(calls[0]["task"]) < 600, f"task={calls[0]['task'][:80]!r}")
        check("步 3 薄任务点名 get_build_context（本 profile 内工具）",
              "get_build_context" in calls[0]["task"])
        check("步 3 子 run 不继承对话历史（数据在 canonical 记录里）",
              calls[0]["history"] == [], f"history={calls[0]['history']!r}")
        note = B._build_stage_note("BUILDING")
        check("服务端追加文字里没有工具名",
              "mcp__novelengine" not in note and "drive_ui" not in note
              and "save_outlines" not in note)

        # ── 2) 步 1-2（cur=2）→ build-candidates profile ──
        evs = run(STEP1_TASK, _snap(cur=2, _picked=False))
        check("步 2 起 build-candidates profile",
              len(calls) == 1 and calls[0]["mcp_profile"] == "build-candidates",
              f"calls={[c['mcp_profile'] for c in calls]}")
        check("步 2 Flow 落 STAGING", BF.load_flow(SID)["phase"] == "STAGING")
        # 回归（本次用户报的：步 2 生成候选时没拿到步 1 选的标签）
        check("步 2 子 run 收到步 1 的 idea 与题材标签",
              all(d in calls[0]["task"] for d in STEP1_DATA),
              f"task={calls[0]['task'][:90]!r}")
        check("步 2 子 run 收到对话历史", calls[0]["history"] == HISTORY)
        # 向导原文自带的工具名必须落在这个 profile 内（否则子 run 会 unknown-tool 停摆）
        from libraries.agent_tool_router import PROFILE_TOOLS
        mentioned = [n for n in ("query_profiles", "drive_ui", "save_outlines",
                                 "mcp__novelengine") if n in STEP1_TASK]
        check("向导原文提到的工具都在 build-candidates 面内",
              all(n in PROFILE_TOOLS["build-candidates"] for n in mentioned),
              f"越界={[n for n in mentioned if n not in PROFILE_TOOLS['build-candidates']]}")

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

        # ── 9) 合法迭代不该被判死：同阶段重复请求但快照显示已出产物 → 计数归零、照常起 run ──
        # （回归：原先只看 resume_point，用户反复「再改改」会在第 3 次被误判空转而 FAILED）
        path = BF.flow_path(SID)
        if path.exists():
            path.unlink()
        for i in range(B.MAX_BUILD_ATTEMPTS + 1):
            evs = run(STEP3_TASK, _snap(cur=3, has_world=True, has_outline=True))
            check(f"迭代第 {i + 1} 轮仍起 run（已出产物→归零）", len(calls) == 1)
            check(f"迭代第 {i + 1} 轮不误报失败",
                  not any(e.get("type") == "error" for e in evs),
                  f"types={[e.get('type') for e in evs]}")

        # ── 10) 快照过期（看不到产物）→ 不记账、不误报失败 ──
        path = BF.flow_path(SID)
        if path.exists():
            path.unlink()
        stale = _snap(cur=3)
        stale["updated_at"] = "2026-01-01 00:00:00"
        for i in range(B.MAX_BUILD_ATTEMPTS + 1):
            evs = run(STEP3_TASK, stale)
            check(f"过期快照第 {i + 1} 轮仍起 run（不记账）", len(calls) == 1)
            check(f"过期快照第 {i + 1} 轮不误报失败",
                  not any(e.get("type") == "error" for e in evs),
                  f"types={[e.get('type') for e in evs]}")
        check("过期快照不建 Flow 记录（无进展可观测）", not BF.flow_path(SID).exists())

        # ── 10b) 2026-09-10 事故防复发：canonical 记录 step=3 + 快照仍是 cur=2 ──
        # 现场：用户点「已挑选完毕」时前端**先**发 agent 任务、**后**才上报 cur=3，于是服务端
        # 读到 cur=2 → 起了 build-candidates profile（工具面没有 set_world/set_outline），
        # 模型整轮只能做平台错误恢复。修法=阶段权威改看服务端 canonical 记录。
        path = BF.flow_path(SID)
        if path.exists():
            path.unlink()
        BD.transition(SID, step=3, selected_candidate={"title": "2050：星港从零建起"})
        evs = run(STEP3_TASK, _snap(cur=2, _picked=False), keep_record=True)
        check("记录 step=3 + 滞后快照(cur=2) → 起 build（事故防复发）",
              len(calls) == 1 and calls[0]["mcp_profile"] == "build",
              f"calls={[c['mcp_profile'] for c in calls]}")

        # ── 10c) 反向：记录 step=2 + 快照 cur=3 → 记录权威，起 build-candidates ──
        BD.transition(SID, step=2)
        evs = run(STEP1_TASK, _snap(cur=3), keep_record=True)
        check("记录 step=2 + 快照 cur=3 → 起 build-candidates（记录权威）",
              len(calls) == 1 and calls[0]["mcp_profile"] == "build-candidates",
              f"calls={[c['mcp_profile'] for c in calls]}")

        # ── 10d) 记录带回 book_id → SUBMITTED，不起 run ──
        BD.mark_submitted(SID, book_id="book_010")
        evs = run("继续建书", _snap(cur=3), keep_record=True)
        check("canonical 记录的 book_id → SUBMITTED 不起 run", not calls)
        check("canonical SUBMITTED 的 reply 带 book_id",
              any("book_010" in (e.get("content") or "") for e in evs))

        # ── 10e) 记录存在 + 快照带 submit_error → FAILED（提交失败仍由浏览器侧原文透出）──
        path = BF.flow_path(SID)
        if path.exists():
            path.unlink()
        # 记录也要清：上一条 10d 写进了 book_id，留着会让本用例被判成 SUBMITTED
        if os.path.exists(BD.path_for(SID)):
            os.remove(BD.path_for(SID))
        BD.transition(SID, step=3)
        evs = run("继续建书", _snap(cur=3, submit_error="书名重复"), keep_record=True)
        check("记录 step=3 + submit_error → 不起 run", not calls)
        check("提交失败原文透出",
              any(e.get("type") == "error" and "书名重复" in (e.get("message") or "") for e in evs))

        # ── 10f) spawn 硬不变量：skill 与 profile 不互指 → 拒绝启动 LLM ──
        import libraries.skill_profile as SP
        path = BF.flow_path(SID)
        if path.exists():
            path.unlink()
        BD.transition(SID, step=3)
        _orig_map = dict(SP.SKILL_PROFILE_MAP)
        SP.SKILL_PROFILE_MAP["novel-build"] = "write"   # 人为把 build 的 skill 指到别处
        try:
            evs = run(STEP3_TASK, _snap(cur=3), keep_record=True)
        finally:
            SP.SKILL_PROFILE_MAP.clear()
            SP.SKILL_PROFILE_MAP.update(_orig_map)
        check("skill/profile 不互指 → 不启动 LLM（硬不变量）", not calls,
              f"calls={[c['mcp_profile'] for c in calls]}")
        check("不变量失败给出明确原因",
              any(e.get("type") == "error" and "不变量不成立" in (e.get("message") or "")
                  for e in evs), f"evs={[e.get('type') for e in evs]}")

        # ── 10g) profile→skill 映射与 skill_profile.SKILL_PROFILE_MAP 互指（静态）──
        _mismatch = [(p, s) for p, s in B._SKILL_FOR_PROFILE.items()
                     if SP.SKILL_PROFILE_MAP.get(s) != p]
        check("profile→skill 与 skill→profile 双向一致", not _mismatch, f"{_mismatch}")

        # ── 10h) 步 3 上下文补齐：记录缺选中候选 → 服务端有界提取并写回 ──
        drop_rec()
        BD.transition(SID, step=3)          # 记录有 step、没有候选（旧页面/被打断）
        evs = run(STEP3_TASK, _snap(cur=3), keep_record=True)
        check("缺选中候选 → 从向导原文有界提取并派发",
              len(calls) == 1 and calls[0]["mcp_profile"] == "build",
              f"calls={[c['mcp_profile'] for c in calls]}")
        check("提取结果写回 canonical 记录",
              (BD.load(SID).get("selected_candidate") or {}).get("title") == "2050：星港从零建起",
              str(BD.load(SID).get("selected_candidate")))
        check("补齐时带上 step（否则默认 step=1 会把下一条判成步 1-2）",
              int(BD.load(SID).get("step") or 0) == 3)
        check("补齐后仍不转发历史（数据在记录里）", calls[0]["history"] == [])

        # ── 10i) 上下文恢复不出来 → **不派发**（宁可让用户重选，不让 agent 编设定）──
        drop_rec()
        BD.transition(SID, step=3)
        evs = run("继续建书", _snap(cur=3), keep_record=True)   # 无「已选定候选「…」」字样
        check("上下文不全时不派发", not calls, f"calls={[c['mcp_profile'] for c in calls]}")
        check("并给出可行动提示",
              any(e.get("type") == "error" and "上下文不全" in (e.get("message") or "")
                  for e in evs), f"evs={[e.get('type') for e in evs]}")

        # ── 11) UNROUTABLE 显式指引（run_dsh_flow 层）──
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
            # canonical 记录也要清（本测试写的是真文件 storage/build_drafts/<sid>.json）
            try:
                dp = BD.path_for(sid)
                if os.path.exists(dp):
                    os.remove(dp)
            except Exception:
                pass

    if failed:
        raise AssertionError(failed)
    print("\n全部通过")


if __name__ == "__main__":
    main()
