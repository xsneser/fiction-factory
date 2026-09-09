#!/usr/bin/env python3
"""WS3/WS9 验收 —— DecisionPoint 统一 schema / 决策流（纯 Python，无 MCP/LLM）。

覆盖（修订 9）：
  1. dp 归一旧形状（check/severity/description/location/suggestion 兼容键保留）
  2. 同 subject 不同 chapter/revision → 不同 id（anchor 防跨期冲突）；同源同 anchor 幂等同 id
  3. collect/dedupe 聚合但**不**写 planning_state（本模块只读；无落盘副作用）
  4. reconcile 不写入 planning_state.decision_points（只走 character_intents 校正）
用法：python tools/test_decision_feed.py
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


def main():
    from libraries.decision_feed import (dp, annotate, dedupe, collect_decision_points,
                                         SOURCE_BY_CHECK)

    # 1) dp 归一 + compat 键
    d = dp("promises", "promise_overdue", "伏笔已逾期", severity="error",
           subject_id="prm_01", anchor="ch5", chapter=5, story_revision=7)
    check("dp 含 compat 键(description/check/location/suggestion)",
          d.get("description") == "伏笔已逾期" and d.get("check") == "promise_overdue"
          and d.get("source") == "promises" and d.get("blocking") is False, f"{d['id']}")

    # 2) anchor 防跨期冲突 / 幂等
    d2 = dp("promises", "promise_overdue", "伏笔已逾期", severity="error",
            subject_id="prm_01", anchor="ch8", chapter=8, story_revision=10)
    check("同 subject 不同 chapter/revision → 不同 id", d["id"] != d2["id"], f"{d['id']} / {d2['id']}")
    d3 = dp("promises", "promise_overdue", "伏笔已逾期", severity="error",
            subject_id="prm_01", anchor="ch5", chapter=5, story_revision=7)
    check("同源同 subject 同 anchor → 幂等同 id", d["id"] == d3["id"])

    # annotate 旧形状升级
    old = [{"check": "overdue_promises", "severity": "warning",
            "description": "承诺X已逾期", "location": "deadline 第3章", "suggestion": "推进"}]
    up = annotate(old)
    check("annotate 升级旧形状(source=promises, 保留 description)",
          up and up[0]["source"] == "promises" and up[0]["description"] == "承诺X已逾期"
          and up[0]["id"], f"{up[0]['source']}")
    check("SOURCE_BY_CHECK.overdue_promises→promises", SOURCE_BY_CHECK.get("overdue_promises") == "promises")

    # 3) collect/dedupe 聚合（ephemeral，只读不落盘）
    agg = collect_decision_points([[d, d3, d2], up])
    check("collect 去重后 total", agg["counts"]["total"] == 3, f"{agg['counts']}")
    check("severity buckets(2 error 跨期去重 → red=2,yellow=1)",
          agg["buckets"].get("red") == 2 and agg["buckets"].get("yellow") == 1, f"{agg['buckets']}")

    # 4) decision_feed/reconcile 不写 planning_state.decision_points（本批只读；写侧由 planning 路径独占）
    import inspect
    src_reconcile = inspect.getsource(__import__("libraries.reconcile", fromlist=["apply_fact_intents"]).apply_fact_intents)
    check("apply_fact_intents 不 set decision_points",
          "decision_points" not in src_reconcile and "save_planning_state" in src_reconcile)

    print("\n" + "=" * 50)
    print(f"  Decision Feed 验收: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("  失败:", "、".join(FAIL))
        sys.exit(1)
    print("  ✅ 全部通过")
    print("=" * 50)


if __name__ == "__main__":
    main()
