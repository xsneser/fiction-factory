#!/usr/bin/env python3
"""规划期伏笔（promise 六态）× 写作期台账：身份、提升、精确兑现、取消边界。

钉死这些不变量：

1. **身份主键是 `promise.id`**：同一设局段埋三条，"哪条是哪条"必须各归各
   （此前 `by_setup` 是一对一的 dict，三条会互相顶掉）；
2. **提升时点**：`planned` 只在**设局段的正文真正写入并提交本章**时才变 `pending`
   ——批准规划不是故事事实；提升**不重复 append**；
3. **精确兑现**：收局段按 `resolves_promise_ids` 只兑现指名的那条，同 setup 的另两条
   仍 `pending`；老书没有精确 id 时回落 plot 级（兑现该设局名下全部未兑现项）；
4. **取消/替代只在正式 planning commit 之后结算**，预览阶段绝不碰正式台账；
5. `scan_promises` 把 `planned` 单列一组——**它还不是欠读者的债**，不参与逾期/停滞。

外加：BuildDraft 的 H1/H2 staging 在建书时**确定性投影**到 planning_state。

用法：python tools/test_planned_promises.py
"""
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from libraries.engine import NovelEngine  # noqa: E402
from libraries.planning_state import _normalize_horizon  # noqa: E402
from libraries.promise_ledger import reconcile_planned_promises, scan_promises  # noqa: E402
from libraries.storyline import BookStoryline, OutlineSlot, PlotSlot  # noqa: E402

PASS, FAIL = [], []


class _FakeBM:
    def __init__(self):
        self.saved = []

    def save_storyline(self, book_id, tl):
        self.saved.append(book_id)


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}  {detail}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def _lead(promise_id, desc, setup="pl21"):
    return {"id": promise_id, "setup_plot_id": setup, "type": "mystery", "desc": desc,
            "status": "planned", "source": "planner", "setup_chapter": 0,
            "deadline_chapter": 0, "payoff_plot_id": "", "payoff_chapter": 0}


def _tl(plots, promises):
    tl = BookStoryline()
    tl.outlines = [OutlineSlot(id="a1", template_id="", name="弧", start_word=0, end_word=9000)]
    tl.plots = plots
    tl.promises = promises
    return tl


def _engine(tl):
    eng = object.__new__(NovelEngine)      # 不跑 __init__：只借真实的方法实现
    eng.storyline = tl
    eng.state = SimpleNamespace(book_id="selftest-promises")
    eng.book_mgr = _FakeBM()
    return eng


def main():
    print("=" * 60)
    print("  规划期伏笔 × 写作期台账")
    print("=" * 60)

    # ── 1) 同一设局段三条伏笔：各归各 ────────────────────────────────────
    setup = PlotSlot(id="pl21", template_id="", name="第三层",
                     primary_turn="第一次下探", foreshadow=[
                         {"id": "f1", "kind": "setup", "promise_id": "pr17", "desc": "钥匙来源"},
                         {"id": "f2", "kind": "setup", "promise_id": "pr18", "desc": "第三层禁区"},
                         {"id": "f3", "kind": "setup", "promise_id": "pr19", "desc": "角色身份"}])
    payoff_one = PlotSlot(id="pl24", template_id="", name="只兑现钥匙",
                          primary_turn="找到钥匙出处", resolves_promise_ids=["pr17"])
    tl = _tl([setup, payoff_one],
             [_lead("pr17", "钥匙来源"), _lead("pr18", "第三层禁区"), _lead("pr19", "角色身份")])
    eng = _engine(tl)

    check("三条 planned 同在 pl21 名下（索引允许一对多）",
          len([q for q in tl.promises if q["setup_plot_id"] == "pl21"]) == 3)

    # 设局段写入第 1 章 → 三条一起提升 pending
    setup.written_chapter = 1
    st = eng._update_promises_ledger(1)
    check("设局段写进正文 → 三条一起 pending",
          st["promoted"] == 3 and all(q["status"] == "pending" for q in tl.promises),
          str(st))
    check("提升**不重复 append**（还是三条）", len(tl.promises) == 3, str(len(tl.promises)))
    check("提升时记下 setup_chapter",
          all(q["setup_chapter"] == 1 for q in tl.promises))

    # 再跑一次（同一章）→ 不该再动
    st2 = eng._update_promises_ledger(1)
    check("同一章重复扫描不再提升", st2["promoted"] == 0, str(st2))

    # ── 2) 精确兑现：只兑现 pr17，另两条仍 pending ───────────────────────
    payoff_one.written_chapter = 2
    st3 = eng._update_promises_ledger(2)
    by_id = {q["id"]: q for q in tl.promises}
    check("只兑现指名的那一条", st3["fulfilled"] == 1 and by_id["pr17"]["status"] == "fulfilled",
          str(st3))
    check("同 setup 的另两条仍 pending",
          by_id["pr18"]["status"] == "pending" and by_id["pr19"]["status"] == "pending")
    check("兑现记下 payoff_chapter", by_id["pr17"]["payoff_chapter"] == 2)

    # ── 3) legacy 回落：无精确 id 时按 plot 级兑现该 setup 名下全部 ───────
    setup2 = PlotSlot(id="pl30", template_id="", name="旧设局", primary_turn="埋")
    legacy_pay = PlotSlot(id="pl31", template_id="", name="旧收局",
                          primary_turn="收", resolves_plot_id="pl30")
    tl2 = _tl([setup2, legacy_pay],
              [{"id": "pr90", "setup_plot_id": "pl30", "type": "mystery", "desc": "旧钩子",
                "status": "pending", "setup_chapter": 1, "deadline_chapter": 5,
                "payoff_plot_id": "", "payoff_chapter": 0}])
    eng2 = _engine(tl2)
    setup2.written_chapter = 1
    eng2._update_promises_ledger(1)
    legacy_pay.written_chapter = 3
    st4 = eng2._update_promises_ledger(3)
    check("老书没有精确 id → 按 resolves_plot_id 回落兑现",
          st4["fulfilled"] == 1 and tl2.promises[0]["status"] == "fulfilled", str(st4))

    # ── 4) 取消/替代只在正式 commit 后结算 ──────────────────────────────
    tl3 = _tl([PlotSlot(id="pl40", template_id="", name="还在", primary_turn="t")],
              [_lead("prA", "被放弃的钩子", setup="pl40"),
               _lead("prB", "被放弃的钩子", setup="pl99"),   # 设局段已不存在
               _lead("prC", "另一个被放弃的", setup="pl98")])
    # still-alive 的同 desc 新承诺 → superseded
    tl3.promises.append(_lead("prD", "被放弃的钩子", setup="pl40"))
    stats = reconcile_planned_promises(tl3)
    by_id3 = {q["id"]: q for q in tl3.promises}
    # 替代者取"任一仍在的同 desc 承诺"即可（prA / prD 都合法），只要不是悬空
    check("设局段没了且有同 desc 的存活承诺 → superseded（带替代者）",
          by_id3["prB"]["status"] == "superseded"
          and by_id3["prB"]["superseded_by"] in ("prA", "prD"), str(stats))
    check("设局段没了且没有替代 → cancelled 并记原因",
          by_id3["prC"]["status"] == "cancelled" and by_id3["prC"].get("cancelled_reason"),
          str(stats))
    check("设局段still在 → 不动", by_id3["prA"]["status"] == "planned")
    check("只动 planned（pending 及以后是已欠的债）",
          all(q["status"] == "planned" for q in tl3.promises
              if q["id"] in ("prA", "prD")))

    # ── 5) 接线：结算挂在正式 commit 上，预览阶段不碰 ────────────────────
    rs = open(os.path.join(ROOT, "libraries", "replan_service.py"), encoding="utf-8").read()
    ps = open(os.path.join(ROOT, "libraries", "planning_state.py"), encoding="utf-8").read()
    check("正式 commit 里调用结算", "reconcile_planned_promises" in rs)
    check("预览落盘里**不**调用结算（预测层不得改写事实层）",
          "reconcile_planned_promises" not in ps)
    check("结算在 save_outlines 成功之后（不在 preview 阶段）",
          rs.index("reconcile_planned_promises") > rs.index("save_outlines("))

    # ── 6) 台账扫描：planned 单列，不参与逾期/停滞 ──────────────────────
    tl4 = _tl([PlotSlot(id="pl50", template_id="", name="设局", primary_turn="t")],
              [_lead("prX", "规划中的钩子", setup="pl50"),
               {"id": "prY", "setup_plot_id": "pl50", "type": "mystery", "desc": "已埋的钩子",
                "status": "pending", "setup_chapter": 1, "deadline_chapter": 2,
                "payoff_plot_id": "", "payoff_chapter": 0}])
    scan = scan_promises(tl4, [], current_chapter=10)
    check("planned 单列一组", len(scan["planned"]) == 1 and scan["counts"]["planned"] == 1,
          str(scan["counts"]))
    check("planned 不算逾期（它还没欠读者的债）",
          all(o["id"] != "prX" for o in scan["overdue"]), str(scan["overdue"]))
    check("pending 照常判逾期", any(o["id"] == "prY" for o in scan["overdue"]))
    check("planned 的 op 是 planned", scan["planned"][0]["op"] == "planned")

    # ── 7) H 层：配置/内容分离 + 老记录读侧归一 + 建书期投影 ─────────────
    legacy = {"schema_version": 1, "horizon": {"h0_executable_plots": 5, "h1_near_arcs": 3,
                                               "h2_intents": 7, "h2": [{"id": "x"}]},
              "future_intents": [{"id": "fi1", "intent": "星门"}]}
    _normalize_horizon(legacy)
    check("v1 的数字键搬到 horizon_policy",
          legacy["horizon_policy"] == {"h0": 5, "h1": 3, "h2": 7},
          str(legacy["horizon_policy"]))
    check("horizon 只留内容键（h1 列表）", legacy["horizon"] == {"h1": []},
          str(legacy["horizon"]))
    check("H2 不再另存一份（唯一真源是 future_intents）",
          "h2" not in legacy["horizon"] and legacy["future_intents"])

    dash = open(os.path.join(ROOT, "ui", "web_blueprints", "dashboard.py"),
                encoding="utf-8").read()
    check("建书时把 H1/H2 staging 确定性投影到 planning_state",
          'data["horizon"] = {"h1": _h1}' in dash
          and '({"horizon": data["horizon"]} if data.get("horizon") else {})' in dash)
    sl_py = open(os.path.join(ROOT, "ui", "web_blueprints", "storyline.py"),
                 encoding="utf-8").read()
    check("payload 不再兼容 near 别名（H1 只认 horizon.h1）",
          'raw_horizon.get("near")' not in sl_py)

    print("\n" + "=" * 60)
    print(f"  规划期伏笔验收: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("  失败:", "、".join(FAIL))
        sys.exit(1)
    print("  ✅ 全部通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
