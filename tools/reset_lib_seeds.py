# -*- coding: utf-8 -*-
"""重编种子后重置两库运行时数据(只删不写,无 LLM)。

情节段库/情节弧库的权威现在是代码内置种子(libraries/plot.py BUILTIN_PLOTS /
libraries/structure.py _CURATED_ARCS);gitignored 的 libraries/data/*.jsonl 只是
运行时增量叠加(历史 scout 提取)。本脚本把 jsonl 备份后删除,使下次读取(及重启后)
回到新种子:

1. 备份 plots/structures 的 .jsonl(+同名旧 .json 若在) → .bak-reseed-<ts>;
2. 删除它们;
3. 破单例重载,断言两库 == 新内置种子规模/结构,打印通过。

用法:  python tools/reset_lib_seeds.py
数据是 gitignored 运行时文件,不入库;重启 58080 让运行中服务读到新种子。
"""
import os
import shutil
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DATA = os.path.join(ROOT, "libraries", "data")

EXPECT_PLOTS = 36
EXPECT_ARCS = 16
PLOT_CATS = ["对峙冲突", "谈判交涉", "对白交锋", "情感羁绊", "推理查证", "揭秘真相",
             "战斗历练", "危机求生", "余波收尾", "谋划布局", "平静日常", "开篇引入"]


def main():
    ts = time.strftime("%Y%m%d%H%M%S")
    for name in ("plots", "structures"):
        for ext in (".jsonl", ".json"):
            p = os.path.join(DATA, name + ext)
            if os.path.exists(p):
                bak = p + f".bak-reseed-{ts}"
                shutil.copyfile(p, bak)
                os.remove(p)
                print(f"  · {os.path.basename(p)} → {os.path.basename(bak)} (已删原文件)")
    # 破单例 → 下次构造从内置种子加载
    from libraries import plot as PL
    from libraries import structure as ST
    PL.PlotLibrary._instance = None
    ST.StructureLibrary._instance = None
    plots = PL.PlotLibrary().templates
    arcs = ST.StructureLibrary().templates
    # 断言
    assert len(plots) == EXPECT_PLOTS, f"段库 {len(plots)} != {EXPECT_PLOTS}"
    assert len(arcs) == EXPECT_ARCS, f"弧库 {len(arcs)} != {EXPECT_ARCS}"
    assert len({p.id for p in plots}) == EXPECT_PLOTS, "段 id 重复"
    assert len({a.id for a in arcs}) == EXPECT_ARCS, "弧 id 重复"
    from collections import Counter
    cc = Counter(p.category for p in plots)
    assert set(cc) == set(PLOT_CATS) and all(v == 3 for v in cc.values()), dict(cc)
    zhx = sum(1 for a in arcs if (a.tags or [""])[0] == "玄幻")
    assert zhx >= 3, zhx
    for p in plots:
        w = p.word_range
        assert isinstance(w, tuple) and len(w) == 2 and all(isinstance(x, int) for x in w), p.id
    for a in arcs:
        assert a.min_words >= 8000, a.id
        assert not hasattr(a, "themes")
    print(f"\n✅ 重置成功并断言通过: 段库 {len(plots)} 条(12 类×3) | 弧库 {len(arcs)} 条(玄幻首tag {zhx})")
    print("已删运行时 jsonl → 下次读取/重启即用新内置种子。建议重启 58080。")


if __name__ == "__main__":
    main()
