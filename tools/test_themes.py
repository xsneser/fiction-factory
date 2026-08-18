# -*- coding: utf-8 -*-
"""内涵嵌入大纲库 单元测试（python tools/test_themes.py）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from libraries.structure import StructureLibrary, StructureTemplate, BUILTIN_STRUCTURES
from libraries.outline_generator import OutlineGenerator
from libraries.storyline import BookStoryline, OutlineSlot, PlotSlot, mount_themes_and_hooks

FAIL = []


def check(name, cond, detail=""):
    print(("✅ " if cond else "❌ ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


# ─── 1) StructureTemplate themes 序列化往返 ───
t = StructureTemplate(id="t1", name="测试", genre="玄幻", themes=["公平（Justice）"])
d = t.to_dict()
t2 = StructureTemplate.from_dict(d)
check("模板 themes 序列化往返", t2.themes == ["公平（Justice）"])

# 存量兼容：无 themes 字段 → []
t3 = StructureTemplate.from_dict({"id": "x", "name": "n", "genre": "g"})
check("模板 themes 缺省 []", t3.themes == [])

# ─── 2) 内置种子带母题 ───
check("内置模板全带 themes", all(x.themes for x in BUILTIN_STRUCTURES),
      str([(x.id, x.themes) for x in BUILTIN_STRUCTURES]))
check("内置母题为中英对照", "公平（Justice）" in BUILTIN_STRUCTURES[1].themes)

# 结构库实例读取（存量 structures.json 已迁移）
lib = StructureLibrary()
empty = [x.id for x in lib.templates if not x.themes]
check("存量结构库模板全带 themes", not empty, str(empty))

# ─── 3) 生成时从大纲模板取母题 ───
gen = OutlineGenerator(llm_client=None, structure_lib=lib)
tl = BookStoryline()
tl.outlines = [
    OutlineSlot(id="o1", template_id="struct_xuanhuan_01", name="a"),
    OutlineSlot(id="o2", template_id="struct_tianwen_01", name="b"),
]
themes = gen._select_book_themes("玄幻", tl)
check("从大纲模板汇总母题", "成长的代价（Cost of Growth）" in themes
      and "传承与突破（Legacy & Breakthrough）" in themes, str(themes))
check("母题去重取前3", len(themes) <= 3 and len(set(themes)) == len(themes), str(themes))

# 空大纲 → 兜底默认（可挂桥段）
tl2 = BookStoryline()
themes2 = gen._select_book_themes("玄幻", tl2)
check("空大纲兜底默认", themes2 == ["成长的代价（Cost of Growth）"], str(themes2))

# ─── 4) 挂载仍可用（THEME_PLOT_COMPAT 载体图）───
tl3 = BookStoryline()
tl3.themes = ["成长的代价（Cost of Growth）"]
p = PlotSlot(id="p1", template_id="plot_dating_006", name="打脸", slots=[])
mount_themes_and_hooks(p, tl3.themes)
check("桥段挂载母题", p.theme_hints == ["成长的代价（Cost of Growth）"], str(p.theme_hints))
check("吸睛点兜底", len(p.hook_points) == 2, str(p.hook_points))


if __name__ == "__main__":
    print("═══ 内涵嵌入大纲库 测试 ═══")
    print("\n结果:", "全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌")
    sys.exit(1 if FAIL else 0)
