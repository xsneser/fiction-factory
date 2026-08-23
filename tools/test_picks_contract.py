#!/usr/bin/env python3
"""picks 契约验收 — normalize_plot_picks + generate 决策点 B 的确定性单测（零 LLM 成本）。

验证 2026-08-19 picks 扁平化修复：
  1) normalize_plot_picks：list（新契约）/ dict（旧契约，按值序展开）/ 空 / None / 重复 id
  2) generate(agent_picks={"templates": [...], "plots": [...]})，llm_client=None 全链路规则回退：
     - 每个预选 id 在 tl.plots 中至多出现一次
     - 预选不是全挤在前 1-2 个 outline（跨弧分布）
     - 无 picks 时走原 AI/规则路径（回归）

用法：python tools/test_picks_contract.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from libraries.outline_generator import OutlineGenerator, normalize_plot_picks  # noqa: E402
from libraries.prompt_harness import PromptHarness  # noqa: E402
from libraries.storyline import BookStoryline  # noqa: E402
from agent_tools import struct_lib, plot_lib, gag_lib, _profile_for  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}  {detail}")


def build_gen(tl):
    profile = _profile_for(tl)
    harness = PromptHarness(storyline=tl, profile=profile,
                            gag_lib=gag_lib, plot_lib=plot_lib)
    return OutlineGenerator(llm_client=None, structure_lib=struct_lib,
                            plot_lib=plot_lib, gag_lib=gag_lib,
                            profile=profile, harness=harness)


def run_generate(agent_picks):
    tl = BookStoryline(words_per_chapter=3000, pen_name="测试")
    gen = build_gen(tl)
    last = {}
    events = []
    for ev in gen.generate(
            custom_context="都市爽文开挂升级",
            pen_name="测试", storyline=tl, skip_analyze=True,
            agent_picks=agent_picks, on_save=lambda t: last.update(tl=t)):
        events.append(ev)
    return last.get("tl", tl), events


def test_normalize():
    check("list 新契约去重+过滤空", normalize_plot_picks(
        {"templates": ["s1"], "plots": ["a", "b", "a", ""]}) == ["a", "b"])
    check("dict 旧契约按值序展开", normalize_plot_picks(
        {"plots": {"o1": ["x", "y"], "o2": ["z"]}}) == ["x", "y", "z"])
    check("空/None 返回 []", normalize_plot_picks({"plots": []}) == []
          and normalize_plot_picks(None) == []
          and normalize_plot_picks({"plots": None}) == [])


def test_picks_distribute():
    tids = [t.id for t in struct_lib.search(genre="都市")[:2]] \
        or [t.id for t in struct_lib.templates[:2]]
    pids = [t.id for t in plot_lib.templates[:6]]
    if not tids or not pids:
        print("  ⚠️ 三库为空，跳过分布断言")
        return
    tl, events = run_generate({"templates": tids, "plots": pids})
    outlines = tl.outlines
    used = [p.template_id for p in tl.plots]

    print(f"  ℹ️ outlines={len(outlines)}, plots={len(tl.plots)}, 预选={pids}")
    for pid in pids:
        check(f"预选 {pid} 至多出现一次", used.count(pid) <= 1, f"count={used.count(pid)}")

    consumed = [pid for pid in pids if pid in used]
    if len(consumed) >= 3 and len(outlines) >= 2:
        # 预选锚点按阶段消费 → 应跨弧分布，不全挤在 outline[0]
        oid_to_idx = {o.id: i for i, o in enumerate(outlines)}
        idxs = [oid_to_idx.get(p.outline_id) for p in tl.plots if p.template_id in consumed]
        idxs = [i for i in idxs if i is not None]
        span = len(set(idxs))
        check("预选跨 ≥2 个 outline 分布", span >= 2, f"span={span}, idxs={sorted(set(idxs))}")

    # 预选命中锚点至少 yield 了 decision 事件
    decisions = [d for d in events if d[0] == "decision" and d[1] == "plot_choice"
                 and "外部预选" in (d[2] or {}).get("reason", "")]
    check("决策点 B 触发（外部预选 decision 事件）", len(decisions) > 0,
          f"events={len(decisions)}")


def test_no_picks_regression():
    tl, events = run_generate(None)
    check("无 picks 走原 AI/规则路径（tl 正常产出）",
          len(tl.outlines) >= 1 and hasattr(tl, "plots"), f"outlines={len(tl.outlines)}")
    # 无预选 → 不应出现"外部预选"decision
    decisions = [d for d in events if d[0] == "decision" and "外部预选" in (d[2] or {}).get("reason", "")]
    check("无 picks 不触发外部预选", len(decisions) == 0)


def main():
    test_normalize()
    test_picks_distribute()
    test_no_picks_regression()
    print("\n" + "=" * 50)
    print(f"  picks 契约验收: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("  失败项:", "、".join(FAIL))
        sys.exit(1)
    print("  ✅ picks 契约验收通过（扁平优先序 + 跨弧分布 + 向后兼容）")
    print("=" * 50)


if __name__ == "__main__":
    main()
