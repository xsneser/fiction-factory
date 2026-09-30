#!/usr/bin/env python3
"""建书步 3 薄工具单测（get_build_context / validate_build / save_build_draft）。

不起 dsh、不调 LLM、不碰浏览器：直接对着 canonical 记录（storage/build_drafts/<sid>.json）
断言「先验证后落表」的事务语义。用一次性 session id 并在 finally 自清。

覆盖的回归点：
  · 上下文只来自服务端记录（不再靠转发整段聊天）
  · 校验不过**什么都不写**（revision 不动）
  · revision CAS（中途被改过 → 拒收，不覆盖）
  · 人物载荷的四类结构错误（role/importance/relations/relation 指向不存在的人）
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent_tools as AT  # noqa: E402
from libraries import build_draft as BD  # noqa: E402

SID = "selftest-step3-tools"

WORLD = {
    "world_building": {
        "era": "铁陨历 317 年",
        "core_conflict": "用不断折损的记忆，把母国随时准备回收的流放地建成谁也送不走的要塞",
        "world_summary": "源流魔力与 2050 工程学在断脊隘口相撞。",
        "rules": ["源流三律：力不出符文、符不承死铁、脉不越隘口"],
        "factions": [{"name": "断脊流放营", "stance": "主角阵营", "desc": "被母国判死的流放地"},
                     {"name": "岚庭联邦", "stance": "压迫者", "desc": "掌控星门与补给的母国"}],
    },
    "tone": "硬核冷峻",
}
CHARACTERS = [
    {"name": "陆铮", "role": "主角", "importance": 1, "faction": "断脊流放营",
     "identity": "2050 结构工程师穿越成营长",
     "relations": [{"name": "阿苔", "relation": "最初的伙伴"}]},
    {"name": "阿苔", "role": "配角", "importance": 2, "faction": "断脊流放营",
     "relations": [{"name": "陆铮", "relation": "合作者"}]},
    {"name": "裴崇", "role": "反派", "importance": 2, "faction": "岚庭联邦", "relations": []},
]
STORYLINE = {
    "outlines": [
        {"id": "A", "name": "断脊立足", "start_word": 0, "end_word": 3000, "notes": "目标：立起第一段防线"},
        {"id": "B", "name": "骨誓与铁", "start_word": 3000, "end_word": 6000, "notes": "目标：把部族变成盟友"},
    ],
    "plots": [
        # 新情节段粒度：primary_turn 必填、字数硬上限 1200。
        # 注：本夹具此前没有这两项却"校验通过"——那是 validate_build 取
        # storyline_report["issues"]（该键根本不存在）导致结构错误被静默吞掉；
        # 修掉吞错误后按文档规则如实拒收，故这里补成真正合法的情节段。
        {"id": "p1", "name": "醒于刑台", "outline_id": "A", "order": 1, "words": 1000,
         "primary_turn": "从刑台上活下来，并接掌流放营"},
        {"id": "p2", "name": "一纸弃令", "outline_id": "A", "order": 2, "words": 1000,
         "primary_turn": "确认母国不会接回他们，只能自建"},
        {"id": "p3", "name": "雪夜合围", "outline_id": "B", "order": 1, "words": 1100,
         "primary_turn": "第一次守住围城，部族开始相信他"},
        {"id": "p4", "name": "骨誓为证", "outline_id": "B", "order": 2, "words": 1000,
         "primary_turn": "与部族结成正式同盟"},
    ],
    "threads": [{"id": "t1", "name": "记忆的贴现", "type": "foreshadow"}],
    "planning": {"future_intents": [{"id": "fi1", "kind": "arc_intent", "desc": "星门清算"}]},
}


def main():
    failed = []
    real_status = AT._current_build_session
    # 会话固定：不许退回浏览器快照（全局单份，测试里没有向导页）
    AT._current_build_session = lambda explicit="": str(explicit or SID)

    def check(name, cond, detail=""):
        print(f"[{'OK' if cond else 'FAIL'}] {name}{(' ' + detail) if not cond else ''}")
        if not cond:
            failed.append(name)

    try:
        if os.path.exists(BD.path_for(SID)):
            os.remove(BD.path_for(SID))
        BD.transition(SID, step=3, idea="2050 军工工程师穿越流放营",
                      tags=["穿越", "星际", "基建"],
                      pen_name="星烬",
                      selected_candidate={"title": "2050：铁陨要塞",
                                          "one_liner": "用钢筋混凝土造一座要塞",
                                          "world_brief": "略"})

        # ── 1) 上下文只来自服务端记录 ──
        ctx = AT.get_build_context(SID)
        check("get_build_context ok", ctx.get("ok") is True, str(ctx)[:120])
        check("上下文带 step/revision", ctx["session"]["step"] == 3 and ctx["session"]["revision"] >= 1)
        check("上下文带 idea/tags/pen",
              ctx["idea"].startswith("2050") and ctx["tags"] == ["穿越", "星际", "基建"]
              and ctx["pen"]["name"] == "星烬")
        check("上下文带选中候选", (ctx["selected_candidate"] or {}).get("title") == "2050：铁陨要塞")
        check("首次无草稿", ctx["existing_draft"] is None)

        # ── 2) 校验不过 → 什么都不写 ──
        bad_rev = BD.load(SID)["revision"]
        r = AT.validate_build(world={"world_building": {"factions": [{"name": "孤势力"}]}},
                              storyline={"outlines": [{"id": "A", "name": "缺跨度"}], "plots": []},
                              characters=[{"name": "无名", "role": "女主", "relations": "陆铮"}],
                              build_session_id=SID)
        check("校验不过时 passed=False", r.get("passed") is False)
        check("校验点名 role 非法", any("role" in i for i in r["issues"]), str(r["issues"])[:160])
        check("校验点名 relations 形状", any("relations" in i for i in r["issues"]))
        check("校验点名跨度缺失/情节段为空",
              any("outlines" in i or "plots" in i for i in r["issues"]))
        save = AT.save_build_draft(world={"world_building": {"factions": []}},
                                   storyline={"outlines": [], "plots": []},
                                   characters=[], build_session_id=SID)
        check("校验不过时 save 拒收", save.get("saved") is False and save.get("ok") is False)
        check("拒收时 revision 不动（什么都没写）", BD.load(SID)["revision"] == bad_rev)
        check("拒收时草稿仍为空", BD.load(SID)["draft"] is None)

        # ── 3) 合法草稿 → 先校验后落表 ──
        ok = AT.validate_build(world=WORLD, storyline=STORYLINE, characters=CHARACTERS,
                               build_session_id=SID)
        check("合法草稿校验通过", ok.get("passed") is True, f"issues={ok.get('issues')}")
        save = AT.save_build_draft(world=WORLD, storyline=STORYLINE, characters=CHARACTERS,
                                   build_session_id=SID)
        check("合法草稿落盘成功", save.get("saved") is True, str(save)[:160])
        rec = BD.load(SID)
        check("落盘后 revision +1", rec["revision"] == bad_rev + 1)
        check("落盘内容与提交一致",
              (rec["draft"] or {}).get("storyline", {}).get("outlines", [{}])[0].get("id") == "A"
              and len((rec["draft"] or {}).get("characters") or []) == 3)
        check("落盘不吞 future_intents",
              (rec["draft"]["storyline"].get("planning") or {}).get("future_intents"))
        ctx2 = AT.get_build_context(SID)
        check("回读能看到已落草稿", isinstance(ctx2["existing_draft"], dict))

        # ── 4) 零参数校验：对着已落盘草稿跑 ──
        r2 = AT.validate_build(build_session_id=SID)
        check("零参数 validate_build 读服务端草稿", r2.get("passed") is True, f"issues={r2.get('issues')}")

        # ── 5) revision CAS ──
        stale = AT.save_build_draft(world=WORLD, storyline=STORYLINE, characters=CHARACTERS,
                                    expected_revision=1,   # 早已不是 1
                                    build_session_id=SID)
        check("revision 不匹配 → 拒收", stale.get("error") == "revision_conflict", str(stale)[:120])
        check("CAS 拒收不动草稿", BD.load(SID)["revision"] == bad_rev + 1)

        # ── 6) 无会话 → 明确报错（不静默） ──
        AT._current_build_session = lambda explicit="": ""
        nc = AT.get_build_context()
        check("无会话 id 时 get_build_context 明确报错", nc.get("ok") is False
              and nc.get("error") == "no_build_session")
        AT._current_build_session = lambda explicit="": str(explicit or SID)

        # ── 7) 缺主角/半组跨度等结构性错误逐条点名 ──
        r3 = AT.validate_build(world=WORLD, characters=[{"name": "甲", "role": "配角",
                                                         "importance": 2, "faction": "断脊流放营"}],
                               storyline=STORYLINE, build_session_id=SID)
        check("缺主角被点名", any("主角" in i for i in r3["issues"]), str(r3["issues"])[:160])
    finally:
        AT._current_build_session = real_status
        try:
            if os.path.exists(BD.path_for(SID)):
                os.remove(BD.path_for(SID))
        except Exception:
            pass

    if failed:
        raise AssertionError(failed)


if __name__ == "__main__":
    main()
