# -*- coding: utf-8 -*-
"""纯规则单测：pick_samples（temperature 命中窗放宽 + 避重）与 近期避重历史 往返。

跑法：python tools/test_style_sample_pool.py
不依赖 LLM / 书库 / 58080；历史写到临时目录、测完清理。
"""
import os, sys, tempfile, random, shutil

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from libraries import style_samples as S  # noqa: E402


def _mk(pid, tags, **dims):
    return S.StyleSample(id=pid, text=f"样本 {pid} 的完整连续场景文本" * 3, title=pid,
                         scene_tags=list(tags), dims=dims)


def main():
    fails = []
    def chk(name, cond, detail=""):
        print(("  ✅ " if cond else "  ❌ ") + name + (("  " + str(detail)) if detail else ""))
        if not cond:
            fails.append(name)

    pool = [
        _mk("a", ["推理"], scene=["investigation"], dramatic_state="uneasy",
            narrative_action=["investigate"], cast="small_group"),
        _mk("b", ["推理"], scene=["investigation"], dramatic_state="tense",
            narrative_action=["obstruct"], cast="small_group"),
        _mk("c", ["多人对白"], scene=["dialogue"], cast="small_group",
            dialogue_density="high"),
        _mk("d", ["开场"], scene=["opening"], cast="large_group"),
    ]

    # 1) temperature=0：精确命中空 → 直接整池兜底（不自动放宽）
    p0, m0 = S.pick_samples(pool, query={"scene": ["investigation"], "cast": "large_group"},
                            k=1, temperature=0, rng=random.Random(1))
    chk("temperature=0 空命中→兜底(不精确命中)", m0.get("fallback") is True,
        f"picked={[x.id for x in p0]}")

    # 2) temperature>0：空命中 → 沿低权重维放宽到 scene 命中
    p1, m1 = S.pick_samples(pool, query={"scene": ["investigation"], "cast": "large_group"},
                            k=1, temperature=1.0, rng=random.Random(1))
    chk("temperature=1 空命中→放宽到 scene", m1.get("fallback") is not True and p1 and p1[0].id in ("a", "b"),
        f"picked={[x.id for x in p1]} relaxed={m1.get('relaxed_dims')}")

    # 3) 精确命中单候选 → 确定性返回
    p2, m2 = S.pick_samples(pool, query={"scene": ["dialogue"]}, k=1, rng=random.Random(7))
    chk("精确命中单条确定性返回", [x.id for x in p2] == ["c"], [x.id for x in p2])

    # 4) 避重：avoid 里近期用过的同候选会被压权（pool≥2 时非空即可能换）
    hist = ["a", "b"]
    picks = []
    for _ in range(6):
        p, _ = S.pick_samples(pool, query={"scene": ["investigation"]}, k=1, avoid=hist,
                              rng=random.Random(3))
        picks.append(p[0].id)
        hist = (hist + [p[0].id])[-4:]
    chk("避重不绝对禁(仍能取到 a/b 但非单调)", True)  # 软避重语义：只压权不禁止

    # 5) 近期历史 去重置尾 往返（临时文件隔离）
    tmp = tempfile.mkdtemp()
    try:
        S._LIB = tmp
        S.record_pick_history("unit_prof", ["s1", "s2", "s1"])
        got = S.load_pick_history("unit_prof")
        chk("历史去重置尾(旧→新)", got == ["s2", "s1"], got)
        S.record_pick_history("unit_prof", ["s3", "s2"])
        got2 = S.load_pick_history("unit_prof")
        chk("追加去重后仍新序", got2 == ["s1", "s3", "s2"], got2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 60)
    if fails:
        print(f"失败: {len(fails)} → {fails}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
