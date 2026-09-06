# -*- coding: utf-8 -*-
"""样文库 dims 区分度审计(只读,无 LLM)。

背景:style_annotate 旧自动预标会在全池注入低区分度标签(narrative_action 近全
deduce/reveal、pace 近全 slow),让加权随机的「命中分差」坍缩。本工具把问题暴露出来
供人工再审:

- 每维主导值占比(占主导 >60% 的维 = 对选择器几乎没区分度);
- 「选择向量高度雷同」的条目对(scene 交集非空 且 cast/pace/dialogue_density 完全一致,
  且 scene 集合相等)——它们会在一次取样里互相竞争、使「抽到哪篇」接近均匀乱抽;
- 文本子段重复(复用 style_samples.duplicate_warnings)。

用法:
    python tools/audit_style_pool.py            # 只读报告
    python tools/audit_style_pool.py --preview-force   # 另附「按新 annotate 规则重标」预览
                                                        (只打印差异,不写库)
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from libraries import style_annotate as sa  # noqa: E402
from libraries import style_samples as ss  # noqa: E402

DOMINANT_RATIO = 0.6  # 某取值占该维 >60% 记为「主导/低区分」


def _load():
    path = os.path.join(ROOT, "storage", "style_samples", "samples.json")
    if not os.path.exists(path):
        print(f"无样文库 {path}"); return []
    samples = ss.load_samples()
    return samples or []


def main():
    samples = _load()
    if not samples:
        print("样文库为空")
        return
    print(f"词条总数 {len(samples)}\n")
    # 1) 主导值
    single_dims = ["dramatic_state", "cast", "dialogue_density", "information_density", "pace"]
    print("── 单值维取值分布(带 ▸ 的为该维取值中的主导) ──")
    for f in single_dims:
        from collections import Counter
        cnt = Counter((s.dims or {}).get(f, "·未标·") for s in samples)
        tot = len(samples)
        dom = [(v, c / tot) for v, c in cnt.most_common(2)]
        line = "  ".join(f"{v}:{c}" for v, c in cnt.most_common())
        flag = "  ▸ 主导" if dom and dom[0][1] >= DOMINANT_RATIO else ""
        print(f"  {f:18s} {line}{flag}")
    # 2) 雷同向量聚类
    print("\n── 选择向量高度雷同的组(scene 集相同 且 cast/pace/dialogue_density 相同) ──")
    groups = {}
    for s in samples:
        d = s.dims or {}
        key = (tuple(sorted(d.get("scene", []))), d.get("cast", ""),
               d.get("pace", ""), d.get("dialogue_density", ""))
        if d.get("scene"):
            groups.setdefault(key, []).append(s)
    n_cluster = 0
    for key, arr in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(arr) < 2:
            continue
        n_cluster += len(arr)
        scene, cast, pace, dd = key
        print(f"  [{len(arr)}条] scene={','.join(scene) or '∅'} cast={cast or '∅'} "
              f"pace={pace or '∅'} dialogue={dd or '∅'}")
        for s in arr[:8]:
            print(f"      {s.id:4s} {(s.title or '')[:26]}  wc={s.word_count}")
    if not n_cluster:
        print("  (无)")
    else:
        print(f"  → 共 {n_cluster}/{len(samples)} 条落入雷同组")
    # 3) 文本子段重复
    print("\n── 文本子段重复(复用 duplicate_warnings) ──")
    warns = ss.duplicate_warnings(samples)
    if not warns:
        print("  (无)")
    for w in warns:
        print(f"  {w['id']} 是 {w['dup_of']} 的子段")
    # 4) 可选:force 重标预览
    if "--preview-force" in sys.argv:
        print("\n── force 重标预览(按新 annotate 规则,不写库) ──")
        for s in samples:
            sug = sa.suggest_dims(s.text or "", legacy_tags=getattr(s, "scene_tags", None))
            cur = s.dims or {}
            changed = {f: sug.get(f) for f in set(list(cur) + list(sug)) if cur.get(f) != sug.get(f)}
            if changed:
                print(f"  {s.id:4s} {sug.get('scene') or '·无scene·'}: "
                      + " ".join(f"{f} {cur.get(f, '∅')}→{sug.get(f, '∅')}" for f in sorted(changed)))
    print("\n提示:治理 = 对上述雷同/主导条目在 /samples 用 8 维下拉人工微调(数据 gitignored,不随代码提交)。")


if __name__ == "__main__":
    main()
