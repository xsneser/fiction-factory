# -*- coding: utf-8 -*-
"""plot_dims.infer_plot_query 与「k=1 单篇取样」链路的单元测试(不依赖真实样文池)。

覆盖:
- 情节段内容 → dims:谈判/推理/独处/决战 各得其场景与人物组织;
- 空名/无信号 → {} 或只留高置信维(不硬塞);
- 线程/承诺绝不进 query(收局情节段不产出叙事/状态暗示);
- pick_samples(k=1) + pool_for 收窄(打桩 load_samples) → 恒返回恰 1 条。
"""
import sys
import os
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libraries import plot_dims as pd  # noqa: E402
from libraries import style_samples as ss  # noqa: E402

FAILS = []


def ok(name, cond, detail=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("  " + detail if detail else ""))
    if not cond:
        FAILS.append(name)


def plot(**kw):
    d = dict(name="", category="", sub_category="", roles=[], outline_id="",
             stage_index=0, resolves_plot_id="")
    d.update(kw)
    return NS(**d)


def tl_with(arcs):
    return NS(outlines=arcs)


def test_infer():
    print("== infer_plot_query ==")
    o = NS(id="a", stages=[])
    q = pd.infer_plot_query(plot(name="与霍东来谈判组建卫队", roles=["顾铮", "霍东来"], outline_id="a"),
                            tl_with([o]))
    ok("谈判+双人", q.get("scene") == ["negotiation"] and q.get("cast") == "duo"
       and q.get("dialogue_density") == "high", str(q))
    q = pd.infer_plot_query(plot(name="凌霜追查信标线索", roles=["顾铮", "凌霜", "周砚舟"], outline_id="a"),
                            tl_with([o]))
    ok("调查+三人", "investigation" in q.get("scene", []) and q.get("cast") == "small_group", str(q))
    q = pd.infer_plot_query(plot(name="顾铮独自夜观星图", roles=[], outline_id="a"), tl_with([o]))
    ok("独处单人(零台词)→只 cast", q.get("cast") == "solo" and "scene" not in q, str(q))
    q = pd.infer_plot_query(plot(name="望舒号对垒决战", roles=list("abcde"), outline_id="a"), tl_with([o]))
    ok("决战五人+fast", "action" in q.get("scene", []) and q.get("cast") == "large_group"
       and q.get("pace") == "fast", str(q))
    q = pd.infer_plot_query(plot(name="", category="", outline_id="a"), tl_with([o]))
    ok("空信号 → {}", q == {}, str(q))
    # 收局情节段:resolves_plot_id 非空绝不产出 narrative_action 或状态暗示(线程/承诺不进选样)
    q = pd.infer_plot_query(plot(name="真相摊牌", roles=["顾铮"], outline_id="a", resolves_plot_id="p3"),
                            tl_with([o]))
    ok("收局不加叙事/承诺暗示", "narrative_action" not in q and "dramatic_state" not in q, str(q))


def test_pool_k1():
    print("== pool_for + pick_samples k=1(打桩样文池) ==")
    fake = [
        NS(id="s1", title="谈判", word_count=10, text="……", dims={"scene": ["negotiation"], "cast": "duo"}),
        NS(id="s2", title="调查", word_count=10, text="……", dims={"scene": ["investigation"], "cast": "solo"}),
        NS(id="s3", title="战斗", word_count=10, text="……", dims={"scene": ["action"], "cast": "large_group"}),
    ]
    orig_load = ss.load_samples
    ss.load_samples = lambda pen_name="": fake
    try:
        prof = NS(id="pen", sample_ids=["s1", "s2"])   # 收窄到 s1/s2
        pool = ss.pool_for(prof)
        ok("pool_for 收窄到所选", [s.id for s in pool] == ["s1", "s2"], str([s.id for s in pool]))
        import random
        rng = random.Random(7)
        picked, meta = ss.pick_samples(pool, query={"scene": ["investigation"]}, k=1,
                                       avoid=[], rng=rng)
        ok("scene=investigation 硬过滤 + k=1", len(picked) == 1 and picked[0].id == "s2",
           f"{[s.id for s in picked]} meta={meta.get('mode')}")
        picked, meta = ss.pick_samples(pool, query={}, k=1, avoid=["s1"], rng=rng)
        ok("空 query k=1 仍单条", len(picked) == 1, f"{[s.id for s in picked]}")
        # 软避重:×0.5 降权而非禁选 → 跨多次取 s2 应显著多于 s1(非单调仍允许回落到 s1)
        cnt = {"s1": 0, "s2": 0}
        for seed in range(80):
            r = random.Random(seed)
            p, _ = ss.pick_samples(pool, query={}, k=1, avoid=["s1"], rng=r)
            cnt[p[0].id] = cnt.get(p[0].id, 0) + 1
        ok("避重=降权非禁(80 次 s2>s1)", cnt["s2"] > cnt["s1"], str(cnt))
        picked, meta = ss.pick_samples(pool, query={"scene": ["quiet"]}, k=1,
                                       avoid=[], rng=random.Random(3))
        ok("无匹配场景 → 兜底恰 1 条(不空不超)", len(picked) == 1 and meta.get("fallback"),
           f"{[s.id for s in picked]} mode={meta.get('mode')} fallback={meta.get('fallback')}")
    finally:
        ss.load_samples = orig_load


if __name__ == "__main__":
    test_infer()
    test_pool_k1()
    print("\n结果:", "全部通过 ✅" if not FAILS else f"{len(FAILS)} 项失败: {FAILS}")
    sys.exit(1 if FAILS else 0)
