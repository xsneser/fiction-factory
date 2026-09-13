#!/usr/bin/env python3
"""迭代留痕：完整快照 / 无损恢复 / 崩溃对账 / 续规划历史。

钉死四件事：

1. **snapshot 是可恢复的完整版本**，不是 80 字摘要——"点某版回退"要真能恢复；
2. **canonical first, history second**（同一把 session lock）：canonical 领先时
   `read_history` 自动补一条 recovery 快照；
3. **历史领先只标 orphan**：绝不反向用历史推进 canonical（可能来自旧 bug / 手工改文件）；
4. **回退写成新版本**（append-only，历史不删——它同时是审计）。

用法：python tools/test_build_versions.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent_tools as AT  # noqa: E402
from libraries import build_draft as BD  # noqa: E402
from libraries import planning_state as PS  # noqa: E402
from tools.test_build_step3_tools import CHARACTERS, STORYLINE, WORLD  # noqa: E402

SID = "selftest-build-versions"
BOOK = "selftest-replan-history"
PASS, FAIL = [], []

THESIS = {"world_building": {"core_conflict": "用不断折损的记忆建设流放地" * 2,
                             "differentiation": "靠制度成本而非金手指"}}


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}  {detail}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def main():
    print("=" * 60)
    print("  迭代留痕 / 崩溃对账 / 续规划历史")
    print("=" * 60)
    AT._current_build_session = lambda explicit="": str(explicit or SID)

    try:
        for p in (BD.path_for(SID), BD.history_path(SID)):
            if os.path.exists(p):
                os.remove(p)
        BD.transition(SID, step=3, idea="留痕验收", tags=["测试"], pen_name="枫落")

        # ── 1) 每落一版草稿 → 历史多一条完整快照 ─────────────────────────────
        AT.save_build_draft(world=THESIS, build_session_id=SID)
        AT.save_build_draft(world=WORLD, characters=CHARACTERS, build_session_id=SID)
        AT.save_build_draft(world=WORLD, characters=CHARACTERS, storyline=STORYLINE,
                            build_session_id=SID)
        hist = BD.read_history(SID, limit=20)
        check("三次落盘 → 至少三条历史", hist["count"] >= 3, str(hist["count"]))
        check("历史条目带 phase / counts / diff 摘要",
              all(k in hist["entries"][-1] for k in ("phase", "counts", "diff_summary", "actor")),
              str(sorted(hist["entries"][-1].keys())))
        check("列表**不含** snapshot（保持薄，正文按需拉）",
              all("snapshot" not in e for e in hist["entries"]))
        check("actor 记到 agent",
              hist["entries"][-1]["actor"] == "agent", hist["entries"][-1]["actor"])

        # ── 2) 快照可**无损**恢复 ────────────────────────────────────────────
        target = hist["entries"][-2]["revision"]        # 上一版（只有骨架，还没故事线）
        row = BD.read_version(SID, target)
        check("能按版本号取到完整快照", isinstance(row, dict) and row.get("snapshot"),
              str(target))
        snap = row["snapshot"]
        check("该版快照里没有故事线（确实是更早的那一版）",
              not (snap.get("storyline") or {}).get("plots"),
              str((snap.get("storyline") or {}).get("plots"))[:40])
        check("快照含人物（可无损重建）",
              len(snap.get("characters") or []) == len(CHARACTERS),
              str(len(snap.get("characters") or [])))

        # ── 3) 回退 = 写成**新的一版**（append-only）────────────────────────
        cur = BD.load(SID)
        before_count = BD.read_history(SID, limit=99)["count"]
        BD.update(SID, draft=BD.normalize_draft(snap), expected_revision=cur["revision"],
                  snapshot=True, actor="user")
        after = BD.read_history(SID, limit=99)
        check("回退后历史只增不减（审计不丢）", after["count"] > before_count,
              f"{before_count} → {after['count']}")
        check("回退把内容真的写回去了",
              not ((BD.load(SID)["draft"].get("storyline") or {}).get("plots")), "")
        check("回退那版 actor 记为 user",
              after["entries"][-1]["actor"] == "user", after["entries"][-1]["actor"])
        check("新回退版本的快照 == 回退前取到的那一版内容",
              BD.read_version(SID, after["entries"][-1]["revision"])["snapshot"]["world"]
              == BD.normalize_draft(snap)["world"])

        # ── 4) canonical 领先 → 自动补 recovery（崩在 history append 之前）──
        BD.update(SID, draft=BD.normalize_draft({**snap, "storyline": STORYLINE}))   # 无 snapshot
        hist2 = BD.read_history(SID, limit=99)
        check("canonical 领先 → 自动补一条 recovery 快照", hist2["recovered"] is True,
              str(hist2["recovered"]))
        check("recovery 条目被标出来（可审计）",
              any(e.get("recovery") for e in hist2["entries"]), "")
        check("补完之后历史追平 canonical",
              hist2["entries"][-1]["revision"] == BD.load(SID)["revision"],
              f"{hist2['entries'][-1]['revision']} vs {BD.load(SID)['revision']}")

        # ── 5) 历史领先 → 只标 orphan，**不反向改 canonical** ────────────────
        canon_rev = BD.load(SID)["revision"]
        with open(BD.history_path(SID), "a", encoding="utf-8", newline="") as f:
            f.write(json.dumps({"revision": canon_rev + 99, "snapshot": {"world": {}}},
                               ensure_ascii=False) + "\n")
        hist3 = BD.read_history(SID, limit=99)
        check("历史领先 → 标 orphan", hist3["orphan"] is True, str(hist3["orphan"]))
        check("历史领先**不改** canonical revision",
              BD.load(SID)["revision"] == canon_rev, f"{canon_rev} → {BD.load(SID)['revision']}")

        # ── 6) 续规划历史（同一把书锁内 append）──────────────────────────────
        os.makedirs(os.path.join(ROOT, "books", BOOK), exist_ok=True)
        p = PS.replan_history_path(BOOK)
        if p.exists():
            p.unlink()
        PS.save_replan_preview(BOOK, {
            "expected_revision": 6,
            "diagnosis": {"current_pressure": "地底线停摆"},
            "directions": [{"id": "dir_a", "title": "A·向下"}, {"id": "dir_b", "title": "B·向上"}],
            "selected_direction_id": "dir_a",
            "outlines": [{"id": "a1", "name": "第三层", "start_word": 9000, "end_word": 12000}],
            "plots": [{"id": "p1", "name": "下井", "outline_id": "a1", "primary_turn": "下去",
                       "words": 900,
                       "foreshadow": [{"id": "f1", "kind": "setup", "desc": "井底有人"}]}],
            "planning_patch": {"future_intents": [{"id": "fi1", "intent": "星门清算"}]},
        })
        rows = PS.load_replan_history(BOOK)
        check("续规划预览落一行历史", len(rows) == 1, str(len(rows)))
        check("历史含完整 plots 快照（可对照/恢复）",
              (rows[0].get("snapshot") or {}).get("plots"), "")
        check("历史记下方向/选中/伏笔数",
              rows[0]["direction_ids"] == ["dir_a", "dir_b"]
              and rows[0]["selected_direction_id"] == "dir_a"
              and rows[0]["planned_promises"] == 1,
              str({k: rows[0][k] for k in ("direction_ids", "selected_direction_id",
                                           "planned_promises")}))
        check("预览文件仍是单份（提交目标不能有歧义）",
              len(list((PS.ROOT / "books" / BOOK).glob("replan_preview*.json"))) == 1, "")
    finally:
        for path in (BD.path_for(SID), BD.history_path(SID)):
            if os.path.exists(path):
                os.remove(path)
        p = PS.replan_preview_path(BOOK)
        if p.exists():
            p.unlink()
        h = PS.replan_history_path(BOOK)
        if h.exists():
            h.unlink()
        d = os.path.join(ROOT, "books", BOOK)
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)

    print("\n" + "=" * 60)
    print(f"  迭代留痕验收: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("  失败:", "、".join(FAIL))
        sys.exit(1)
    print("  ✅ 全部通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
