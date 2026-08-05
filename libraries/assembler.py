"""书籍组装器（Book Assembler）—— 仅保留旧书兼容

历史：按大纲结构匹配桥段/笑点/内涵、生成写作计划的组装管线。
现状：桥段写作 storyline_writer 不再依赖 assembler_plan；BookAssembler 已在 P2 下线评估中移除。
本模块只保留 BookAssemblerPlan / StageWritingPlan 数据结构与 load_plan，
供 continue_book 加载旧书遗留的 assembler_plan.json（不再生成新计划）。
"""
from dataclasses import dataclass, field
from typing import Optional
import json

from .plot import PlotTemplate
from .structure import StructureTemplate
from .gag import GagPattern
from .theme import ThemeEntry


class StageWritingPlan:
    """一个阶段的写作计划 —— 包含该阶段要用的桥段/笑点/内涵"""
    stage_index: int
    stage_name: str               # 如 "入门试炼"
    stage_description: str        # 阶段描述
    chapter_range: tuple[int, int] # (min_chapters, max_chapters)

    plot: Optional[PlotTemplate] = None       # 选中的桥段（核心）
    plot_match_reason: str = ""               # 为什么匹配这个桥段

    gags: list[GagPattern] = field(default_factory=list)  # 选中的笑点（1-3个）
    gag_slot_assignments: list[dict] = field(default_factory=list)
    # [{gag_id: "gag_003", "scene_type": "开头打脸后", "note": "用围观群众反应制造笑点"}]

    theme_hints: list[str] = field(default_factory=list)  # 本阶段要体现的内涵


@dataclass
class BookAssemblerPlan:
    """整本书的组装计划 —— 写作管线的营养配方"""
    book_title: str = ""
    genre: str = ""
    structure: Optional[StructureTemplate] = None
    stages: list[StageWritingPlan] = field(default_factory=list)
    themes: list[ThemeEntry] = field(default_factory=list)  # 贯穿全书的母题
    theme_hints: list[str] = field(default_factory=list)    # 浓缩的内涵提示
    generated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "book_title": self.book_title,
            "genre": self.genre,
            "structure_id": self.structure.id if self.structure else "",
            "structure_name": self.structure.name if self.structure else "",
            "themes": [{"id": t.id, "name": t.name, "description": t.description}
                       for t in self.themes],
            "theme_hints": self.theme_hints,
            "stages": [
                {
                    "stage_index": s.stage_index,
                    "stage_name": s.stage_name,
                    "stage_description": s.stage_description,
                    "chapter_range": list(s.chapter_range),
                    "plot": {
                        "id": s.plot.id, "name": s.plot.name,
                        "template_structure": s.plot.template_structure,
                        "slots": [{"name": sl.name, "default": sl.default}
                                  for sl in s.plot.slots],
                    } if s.plot else None,
                    "plot_match_reason": s.plot_match_reason,
                    "gags": [{"id": g.id, "name": g.name,
                              "pattern": g.pattern_description,
                              "template": g.template}
                             for g in s.gags],
                    "gag_slot_assignments": s.gag_slot_assignments,
                    "theme_hints": s.theme_hints,
                }
                for s in self.stages
            ],
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(
        cls,
        d: dict,
        structure_lib=None,
        plot_lib=None,
        gag_lib=None,
        theme_lib=None,
    ) -> "BookAssemblerPlan":
        """从磁盘 dict 完整还原计划；库对象由调用方注入（Structure/Plot/Gag/Theme Library）。"""
        plan = cls(
            book_title=d.get("book_title", ""),
            genre=d.get("genre", ""),
            theme_hints=d.get("theme_hints", []),
            generated_at=d.get("generated_at", ""),
        )
        if structure_lib:
            plan.structure = structure_lib.get_by_id(d.get("structure_id", "")) or None
        for t in d.get("themes", []):
            if theme_lib:
                theme = theme_lib.get_by_id(t.get("id", ""))
                if theme:
                    plan.themes.append(theme)
        for s in d.get("stages", []):
            sp = StageWritingPlan(
                stage_index=s.get("stage_index", 0),
                stage_name=s.get("stage_name", ""),
                stage_description=s.get("stage_description", ""),
                chapter_range=tuple(s.get("chapter_range", [0, 0])),
                plot_match_reason=s.get("plot_match_reason", ""),
            )
            pd = s.get("plot") or {}
            if pd and plot_lib:
                sp.plot = plot_lib.get_by_id(pd.get("id", "")) or None
            for g in s.get("gags", []):
                if gag_lib:
                    gp = gag_lib.get_by_id(g.get("id", ""))
                    if gp:
                        sp.gags.append(gp)
            sp.gag_slot_assignments = s.get("gag_slot_assignments", [])
            sp.theme_hints = s.get("theme_hints", [])
            plan.stages.append(sp)
        return plan




# ═══════════════════════════════════════════
# 计划持久化（仅保留 load_plan：旧书 assembler_plan.json 兼容加载）
# ═══════════════════════════════════════════

def load_plan(path: str) -> dict:
    """加载写作计划（返回 dict，因需要外部注入 library 对象才能完整反序列化）"""
    with open(path, encoding="utf-8") as f:
        return json.load(f)
