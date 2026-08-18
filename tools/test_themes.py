# -*- coding: utf-8 -*-
"""内涵嵌入大纲库 单元测试（python tools/test_themes.py）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from libraries.structure import StructureLibrary, StructureTemplate, StageNode, BUILTIN_STRUCTURES
from libraries.outline_generator import OutlineGenerator
from libraries.storyline import (
    BookStoryline, OutlineSlot, PlotSlot, mount_themes_and_hooks, structure_to_stages,
)
from libraries.prompt_harness import PromptHarness
from core.json_store import read_jsonl, write_jsonl_atomic

FAIL = []


def check(name, cond, detail=""):
    print(("✅ " if cond else "❌ ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


# ─── 1) 模板级内涵已移除（内涵唯一来源 = StageNode.themes）───
t0 = StructureTemplate(id="t1", name="测试", genre="玄幻")
d0 = t0.to_dict()
check("模板级无 themes 字段", "themes" not in d0)
check("模板 from_dict 兼容", StructureTemplate.from_dict({"id": "x", "name": "n", "genre": "g"}).id == "x")

# ─── 2) 内置种子：阶段内涵（非模板级）───
check("内置模板含阶段内涵", any(any(s.themes for s in x.stages) for x in BUILTIN_STRUCTURES))
check("内置内涵为中英对照", any(s.themes and s.themes[0]["name"] == "公平（Justice）"
      for s in BUILTIN_STRUCTURES[1].stages))

# 结构库实例读取（存量 jsonl 已迁移：无模板级 themes）
lib = StructureLibrary()
check("存量模板无顶层 themes", all("themes" not in t.to_dict() for t in lib.templates))

# ─── 3) 生成时从阶段内涵取全书内涵（只读阶段级）───
gen = OutlineGenerator(llm_client=None, structure_lib=lib)
tl = BookStoryline()
tl.outlines = [
    OutlineSlot(id="o1", template_id="struct_xuanhuan_01", name="a",
                stages=structure_to_stages(lib.get_by_id("struct_xuanhuan_01"))),
    OutlineSlot(id="o2", template_id="struct_chuanyue_01", name="b",
                stages=structure_to_stages(lib.get_by_id("struct_chuanyue_01"))),
]
themes = gen._select_book_themes("玄幻", tl)
check("从阶段内涵汇总全书内涵", "成长的代价（Cost of Growth）" in themes
      and "复仇（Revenge）" in themes, str(themes))
check("内涵去重取前3", len(themes) <= 3 and len(set(themes)) == len(themes), str(themes))

# 空大纲 → 兜底默认（可挂桥段）
tl2 = BookStoryline()
themes2 = gen._select_book_themes("玄幻", tl2)
check("空大纲兜底默认", themes2 == ["成长的代价（Cost of Growth）"], str(themes2))

# ─── 4) 挂载 v2：阶段级优先 + THEME_PLOT_COMPAT 兜底 ───
tl3 = BookStoryline()
tl3.themes = ["成长的代价（Cost of Growth）"]
p = PlotSlot(id="p1", template_id="plot_dating_006", name="打脸", slots=[])
mount_themes_and_hooks(p, tl3.themes)
check("兜底挂载内涵", p.theme_hints == ["成长的代价（Cost of Growth）"], str(p.theme_hints))
check("吸睛点兜底", len(p.hook_points) == 2, str(p.hook_points))

MOMENTS = [
    {"name": "复仇（Revenge）", "position": "结尾", "how": "挚友被害真相揭晓，以复仇意志引爆"},
    {"name": "热血（Passion）", "position": "结尾", "how": "背水一战，以意志突破极限"},
]
p2 = PlotSlot(id="p2", template_id="plot_dating_005", name="清算", slots=[],
              theme_moments=MOMENTS)
mount_themes_and_hooks(p2, tl3.themes)
check("阶段级优先 theme_hints", p2.theme_hints == ["复仇（Revenge）", "热血（Passion）"],
      str(p2.theme_hints))

# ─── 5) StageNode.themes 序列化 + structure_to_stages 展开带内涵 ───
st = StageNode("朋友阵亡", "挚友为救主角而死", 3, 8,
               ["身陷重围", "挚友牺牲", "主角含泪立誓"], [],
               MOMENTS)
tt = StructureTemplate(id="t9", name="测试", genre="玄幻", stages=[st])
d = tt.to_dict()
tt2 = StructureTemplate.from_dict(d)
check("阶段 themes 序列化往返", tt2.stages[0].themes == MOMENTS, str(tt2.stages[0].themes))
stages = structure_to_stages(tt)
check("structure_to_stages 带阶段内涵", stages[0].get("themes") == MOMENTS,
      str(stages[0].get("themes")))

# ─── 6) 书级与阶段级内涵汇总 ───
lib2 = StructureLibrary()
gen2 = OutlineGenerator(llm_client=None, structure_lib=lib2)
tl4 = BookStoryline()
tl4.outlines = [OutlineSlot(id="o1", template_id="struct_chuanyue_01", name="穿越",
                            stages=structure_to_stages(lib2.get_by_id("struct_chuanyue_01")))]
themes4 = gen2._select_book_themes("穿越", tl4)
check("书级内涵含阶段级内涵名", "复仇（Revenge）" in themes4, str(themes4))

# ─── 7) theme_block rich 渲染（render_bridge_prompt）───
tl5 = BookStoryline(genre="穿越")
h = PromptHarness(storyline=tl5)
item = {"outline": OutlineSlot(id="o1", template_id="struct_chuanyue_01", name="穿越"),
        "stage": {"name": "最终清算"}, "plot": PlotSlot(id="p3", template_id="x",
                                                       name="清算", slots=[], theme_moments=MOMENTS)}
prompt = h.render_bridge_prompt(item, chapter_buffer="", prev_ending="", bridge_text="正文",
                                budget_remaining=3000)
check("theme_block 阶段级渲染", "内涵（含插入位置）" in prompt
      and "复仇（Revenge）（结尾）：挚友被害真相揭晓" in prompt, prompt[:200])

# ─── 8) JSONL 读写往返 ───
import tempfile, os
with tempfile.TemporaryDirectory() as td:
    jp = os.path.join(td, "t.jsonl")
    write_jsonl_atomic(jp, [{"a": 1}, {"b": 2}])
    items = read_jsonl(jp)
    check("JSONL 往返", items == [{"a": 1}, {"b": 2}], str(items))

# ─── 9) 基类 JsonLibrary 走 .jsonl（含旧单 JSON 自动迁移）───
from libraries.base_library import JsonLibrary
from core.json_store import write_json_atomic


class _Obj:
    def __init__(self, d): self.d = d
    def to_dict(self): return self.d


class _TmpLib(JsonLibrary):
    _instance = None
    _list_attr = "items"
    _key = "items"
    _file_name = "testlib.jsonl"

    @classmethod
    def _from_dict(cls, d): return _Obj(d)

    @classmethod
    def _builtin(cls): return [_Obj({"id": "builtin"})]


with tempfile.TemporaryDirectory() as td:
    write_json_atomic(os.path.join(td, "testlib.json"), {"items": [{"id": "old"}]})
    lib = _TmpLib(td)
    check("jsonl 自动迁移旧单JSON", [x.to_dict() for x in lib.items] == [{"id": "old"}],
          str([x.to_dict() for x in lib.items]))
    check("迁移后 jsonl 落盘", os.path.exists(os.path.join(td, "testlib.jsonl")))
    lib.items.append(_Obj({"id": "new"}))
    lib._save()
    lib.items = []
    lib._load()
    check("jsonl 保存往返", [x.to_dict()["id"] for x in lib.items] == ["old", "new"],
          str([x.to_dict() for x in lib.items]))


if __name__ == "__main__":
    print("═══ 内涵嵌入大纲库 测试 ═══")
    print("\n结果:", "全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌")
    sys.exit(1 if FAIL else 0)
