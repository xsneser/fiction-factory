# -*- coding: utf-8 -*-
"""母题（内涵）运行时最小自测（python tools/test_themes.py）

弧库 ArcNode 已彻底删除 themes 字段（2026-09）。本文件覆盖：
  1) 弧库无 themes 字段、from_dict 忽略残留、structure_to_stages 不再带模板内涵；
  2) 平台书运行时母题挂载/渲染（mount_themes_and_hooks / theme_block）仍工作。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from libraries.structure import StructureLibrary, ArcNode
from libraries.storyline import (
    BookStoryline, OutlineSlot, PlotSlot, mount_themes_and_hooks, structure_to_stages,
)
from libraries.prompt_harness import PromptHarness

FAIL = []


def check(name, cond, detail=""):
    print(("✅ " if cond else "❌ ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


# ─── 1) 弧库不再承载 themes ───
lib = StructureLibrary()
keys = set(lib.templates[0].to_dict().keys())
check("弧库统一键集且无 themes", all(set(t.to_dict().keys()) == keys for t in lib.templates)
      and "themes" not in keys, str(sorted(keys)))
check("ArcNode.to_dict 无 themes", "themes" not in ArcNode(id="x", name="n").to_dict())
check("from_dict 忽略残留 themes",
      "themes" not in ArcNode.from_dict({"id": "x", "name": "n",
                                         "themes": [{"name": "a"}]}).to_dict())
stages = structure_to_stages([lib.templates[0]])
check("structure_to_stages 不带模板内涵", bool(stages) and all(s.get("themes") in (None, []) for s in stages))

# ─── 2) 平台书运行时母题挂载（不经弧库）───
MOMENTS = [
    {"name": "复仇（Revenge）", "position": "结尾", "how": "挚友被害真相揭晓，以复仇意志引爆"},
    {"name": "热血（Passion）", "position": "结尾", "how": "背水一战，以意志突破极限"},
]
tl = BookStoryline()
tl.themes = ["成长的代价（Cost of Growth）"]
p = PlotSlot(id="p1", template_id="plot_dating_006", name="打脸", slots=[])
mount_themes_and_hooks(p, tl.themes)
check("兜底挂载内涵", p.theme_hints == ["成长的代价（Cost of Growth）"], str(p.theme_hints))
check("吸睛点兜底", len(p.hook_points) == 2, str(p.hook_points))

p2 = PlotSlot(id="p2", template_id="plot_dating_005", name="清算", slots=[],
              theme_moments=MOMENTS)
mount_themes_and_hooks(p2, tl.themes)
check("阶段级优先 theme_hints", p2.theme_hints == ["复仇（Revenge）", "热血（Passion）"],
      str(p2.theme_hints))

# ─── 3) theme_block rich 渲染（运行时 prompt）───
tl5 = BookStoryline()
h = PromptHarness(storyline=tl5)
item = {"outline": OutlineSlot(id="o1", template_id="arc_x", name="清算"),
        "stage": {"name": "清算现场"}, "plot": p2}
prompt = h.render_bridge_prompt(item, chapter_buffer="", prev_ending="",
                                bridge_text="正文", budget_remaining=3000)
check("theme_block 阶段级渲染", "内涵（含插入位置）" in prompt
      and "复仇（Revenge）（结尾）：挚友被害真相揭晓" in prompt, prompt[:200])


if __name__ == "__main__":
    print("═══ 母题运行时自测 ═══")
    print("\n结果:", "全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌")
    sys.exit(1 if FAIL else 0)
