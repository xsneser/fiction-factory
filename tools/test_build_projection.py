#!/usr/bin/env python3
"""建书步 3「草稿落盘 → 进到页面」链路的回归测试（2026-09-13 事故）。

事故现象：agent 说写完了，世界观的报【缺少必填参数：world_building】，人物/故事线
一条都没进页面；点提交后建出的书三者皆空。三处根因，这个文件各钉一条：

  1. **形状**：canonical 里存的是 world_building 裸本体，而投影与提交都要求外面包一层
     → 归一后三端一致（含**老记录**的读时兼容）。
  2. **投递**：save_build_draft 曾把三条命令写进 nav_intent 队列，而该队列在 agent 忙时
     会被浏览器取走清空 → 现在不再走队列，改由页面从 /api/build/draft 拉取。
     这里断言"落盘不再产生任何 UI 队列垃圾"。
  3. **进度判据**：_stage_progressed 曾只看浏览器快照（永不更新）→ 现在以 canonical 为准。

用一次性 session id 并在 finally 自清；不起 dsh、不调 LLM。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent_tools as AT  # noqa: E402
import libraries.dsh_bridge as B  # noqa: E402
import libraries.nav_intent as NI  # noqa: E402
from libraries import build_draft as BD  # noqa: E402

SID = "selftest-build-projection"
LEGACY_SID = "selftest-build-projection-legacy"

# 注意：这里刻意给**裸本体**（缺 world_building 外层）——事故现场 agent 就是这么传的
BARE_WORLD = {
    "core_conflict": "用不断折损的记忆，把随时会被回收的流放地建成谁也送不走的要塞",
    "differentiation": "硬核工程流 × 记忆贴现代价",
    "factions": [{"name": "断脊流放营", "stance": "主角阵营", "desc": "被判死的流放地"},
                 {"name": "岚庭联邦", "stance": "压迫者", "desc": "掌控星门的母国"}],
    "geography": "断脊隘口",
    "rules": ["源流三律"],
    "tone": "硬核冷峻",
}
CHARACTERS = [
    {"name": "陆铮", "role": "主角", "importance": 1, "faction": "断脊流放营",
     "relations": [{"name": "阿苔", "relation": "最初的伙伴"}]},
    {"name": "阿苔", "role": "配角", "importance": 2, "faction": "断脊流放营",
     "relations": [{"name": "陆铮", "relation": "合作者"}]},
    {"name": "裴崇", "role": "反派", "importance": 2, "faction": "岚庭联邦", "relations": []},
]
STORYLINE = {
    "outlines": [{"id": "A", "name": "断脊立足", "start_word": 0, "end_word": 3000, "notes": "目标：立起防线"}],
    # 新情节段粒度：primary_turn 必填、字数硬上限 1200（修掉"validate_build 吞掉
    # storyline 结构错误"之后，缺这两项的草稿会被如实拒收）
    "plots": [{"id": "p1", "name": "醒于刑台", "outline_id": "A", "order": 1, "words": 1000,
               "primary_turn": "从刑台上活下来并接掌流放营"}],
}


def main():
    failed = []

    def check(name, cond, detail=""):
        print(f"[{'OK' if cond else 'FAIL'}] {name}{(' ' + detail) if not cond else ''}")
        if not cond:
            failed.append(name)

    for sid in (SID, LEGACY_SID):
        p = BD.path_for(sid)
        if os.path.exists(p):
            os.remove(p)
    real_session = AT._current_build_session
    real_nav_path = NI._PATH
    AT._current_build_session = lambda explicit="": str(explicit or SID)
    # 把 UI 队列指到临时文件：断言"落盘不再写队列"，且不污染真实 storage/nav_intent.json
    NI._PATH = os.path.join(ROOT, "storage", f"_{SID}_nav.json")
    if os.path.exists(NI._PATH):
        os.remove(NI._PATH)

    try:
        # ── 1) 形状归一（纯函数） ──
        w = BD.normalize_world(BARE_WORLD)
        check("裸本体被包进 world_building", isinstance(w.get("world_building"), dict)
              and w["world_building"]["core_conflict"] == BARE_WORLD["core_conflict"])
        check("tone 留在顶层（不进 world_building）",
              w.get("tone") == "硬核冷峻" and "tone" not in w["world_building"])
        check("已规范形状幂等", BD.normalize_world(w) == w)
        check("空 world → None", BD.normalize_world({}) is None and BD.normalize_world(None) is None)
        check("normalize_draft 缺段补 None",
              BD.normalize_draft({"world": BARE_WORLD})["storyline"] is None)

        # ── 2) 落盘：归一、返回会话/版本、**不写 UI 队列** ──
        BD.transition(SID, step=3, selected_candidate={"title": "2050：铁陨要塞"})
        before_rev = BD.load(SID)["revision"]
        save = AT.save_build_draft(world=BARE_WORLD, storyline=STORYLINE, characters=CHARACTERS,
                                   expected_revision=before_rev, build_session_id=SID)
        check("save 成功", save.get("ok") is True and save.get("saved") is True, str(save)[:160])
        check("save 回报会话 id", save.get("build_session_id") == SID)
        check("save 回报新 revision（页面据此同步 CAS）",
              int(save.get("revision") or 0) == before_rev + 1, f"rev={save.get('revision')}")
        check("save 不再回报 projected/project_errors（已不走队列推送）",
              "projected" not in save and "project_errors" not in save)

        rec = BD.load(SID)
        stored = (rec.get("draft") or {}).get("world") or {}
        check("canonical 里 world 是规范形状（提交端能取到）",
              isinstance(stored.get("world_building"), dict))
        check("候选/规划等字段没被 save 弄丢",
              (rec.get("selected_candidate") or {}).get("title") == "2050：铁陨要塞")

        nav = NI.take_nav_intents()
        check("落盘不产生任何 UI 队列命令（事故主因）", nav == [], str(nav)[:120])

        # ── 3) 老记录（裸 world 直接写盘）读时兼容 ──
        import json
        raw_path = BD.path_for(LEGACY_SID)
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump({"session_id": LEGACY_SID, "step": 3, "revision": 8,
                       "draft": {"world": BARE_WORLD, "storyline": STORYLINE,
                                 "characters": CHARACTERS}},
                      f, ensure_ascii=False)
        legacy = BD.load(LEGACY_SID)
        lw = (legacy.get("draft") or {}).get("world") or {}
        check("老记录（裸 world）读出来已归一", isinstance(lw.get("world_building"), dict)
              and lw["world_building"]["core_conflict"] == BARE_WORLD["core_conflict"])

        # ── 4) 页面拉取端点 ──
        from ui.web_ui import app
        client = app.test_client()
        check("GET /api/build/draft 缺 sid → 400",
              client.get("/api/build/draft").status_code == 400)
        unknown = client.get("/api/build/draft?sid=__no_such_session__").get_json()
        check("未知 sid → ok 但 exists=false", unknown.get("ok") is True
              and unknown.get("exists") is False)
        got = client.get(f"/api/build/draft?sid={SID}").get_json()
        check("已知 sid → 带 draft/revision/候选",
              got.get("exists") is True and got.get("revision") == before_rev + 1
              and (got.get("draft") or {}).get("characters")
              and (got.get("selected_candidate") or {}).get("title") == "2050：铁陨要塞")
        gw = (got.get("draft") or {}).get("world") or {}
        check("端点回带的 world 已归一（页面照此填表）",
              isinstance(gw.get("world_building"), dict))
        check("拉取是只读的（revision 不变）", BD.load(SID)["revision"] == before_rev + 1)

        # ── 5) 侧栏状态事件 ──
        def msg(obj):
            return {"content": [{"content": json.dumps(obj, ensure_ascii=False)}]}
        ev = B._build_draft_status_event("save_build_draft", {"build_session_id": SID},
                                         msg({"ok": True, "saved": True, "revision": 9,
                                              "build_session_id": SID,
                                              "validation": {"passed": True}}))
        check("save 产生 build_draft_status（页面据此拉草稿）",
              ev and ev["type"] == "build_draft_status" and ev["saved"] and ev["revision"] == 9)
        ev2 = B._build_draft_status_event("validate_build", {"build_session_id": SID},
                                          msg({"ok": True, "passed": False,
                                               "issues": ["world 为空", "缺主角"]}))
        check("validate 失败把 issues 带出来（页面可见）",
              ev2 and ev2["passed"] is False and len(ev2["issues"]) == 2)
        check("与建书无关的工具不产生该事件",
              B._build_draft_status_event("list_books", {}, msg({"ok": True})) is None)

        # ── 6) 进度判据以 canonical 为准 ──
        empty_snap = {"phase": "BUILDING", "has_world": False, "has_outline": False}
        check("快照空但 canonical 有草稿 → 判为有进展",
              B._stage_progressed(empty_snap, SID) is True)
        check("快照空且 canonical 无草稿 → 判为无进展",
              B._stage_progressed(empty_snap, "__no_such_session__") is False)
        check("快照显示有产物时仍认可（fill_world 兜底骨架）",
              B._stage_progressed({"phase": "BUILDING", "has_world": True},
                                  "__no_such_session__") is True)

        # ── 7) 提交端：canonical 来源 + 版本冲突不再静默建空书 ──
        conflict = client.post("/books/start", json={
            "build_session_id": SID, "build_source": "canonical",
            "build_revision": 1,   # 陈旧版本
            "idea": "x", "pen_name": "p", "title": "t"})
        check("canonical 来源 + 版本不一致 → 409（不建空书）", conflict.status_code == 409,
              f"status={conflict.status_code}")
        no_draft = client.post("/books/start", json={
            "build_session_id": "__no_such_session__", "build_source": "canonical",
            "build_revision": 1, "idea": "x", "pen_name": "p", "title": "t"})
        check("canonical 来源但服务端没草稿 → 409", no_draft.status_code == 409,
              f"status={no_draft.status_code}")

        # ── 7) 前端契约（无浏览器，用源码契约钉住接线，防被后人删掉） ──
        tpl = open(os.path.join(ROOT, "ui", "templates", "start_book.html"),
                   encoding="utf-8").read()
        panel = open(os.path.join(ROOT, "ui", "static", "js", "agent_panel.js"),
                     encoding="utf-8").read()
        check("向导页有 syncCanonicalDraft / applyCanonicalRecord",
              "syncCanonicalDraft: function" in tpl and "applyCanonicalRecord: function" in tpl)
        check("回灌顺序 world → characters → storyline",
              tpl.index("dispatch('set_world'") < tpl.index("dispatch('set_characters'")
              < tpl.index("dispatch('set_outline'"))
        check("进步 3 / pageshow / build_draft_status 都会同步",
              "if (n === 3 && !this.bookId) this.syncCanonicalDraft();" in tpl
              and "addEventListener('pageshow'" in tpl
              and "addEventListener('ne:build-draft-status'" in tpl)
        check("有草稿状态/错误区与恢复按钮",
              'id="wz-agent-draft-status"' in tpl and 'id="wz-agent-draft-errors"' in tpl
              and 'id="wz-restore-canonical"' in tpl)
        check("提交带 build_source（canonical/form 二选一）",
              "this.state.build_source = 'canonical'" in tpl
              and "this.state.build_source = 'form'" in tpl)
        check("用户改过表单则不自动覆盖（dirty 守卫）",
              "markStep3Dirty: function" in tpl and "_step3Dirty) {" in tpl)
        check("侧栏转发 build_draft_status 且不受 busy 守卫",
              "ne:build-draft-status" in panel and "build_draft_status" in panel
              and "} else if (t === 'build_draft_status') {" in panel)
        check("侧栏 error 写进 history（刷新不丢）",
              "history.push({ role: 'assistant', content: '⚠️ '" in panel)

    finally:
        AT._current_build_session = real_session
        NI._PATH = real_nav_path
        for sid in (SID, LEGACY_SID):
            for p in (BD.path_for(sid), BD.path_for(sid) + ".lock"):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
        try:
            if os.path.exists(os.path.join(ROOT, "storage", f"_{SID}_nav.json")):
                os.remove(os.path.join(ROOT, "storage", f"_{SID}_nav.json"))
        except OSError:
            pass

    print()
    if failed:
        print(f"❌ {len(failed)} 项失败：{failed}")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    main()
