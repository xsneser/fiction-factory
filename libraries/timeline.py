"""
书籍时间线（Book Timeline）— 多大纲序列 + 桥段嵌套配置

核心理念：
  一本书不是一个大纲走到头，而是多个大纲按时间线串接，
  大纲之间可以重叠交叉（A 还没结束 B 已经开始），
  桥段在大纲阶段内可以嵌套、包含、重叠。
"""
from dataclasses import dataclass, field
from typing import Optional
import json


# ═══════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════

@dataclass
class OutlineSlot:
    """一个大纲在时间线上的位置"""
    id: str                        # 唯一标识
    template_id: str               # 对应 StructureLibrary 里的模板，""=已展开不依赖模板
    name: str                      # 显示名称（如"都市爽文开篇"）
    start_chapter: int = 1         # 从第几章开始
    end_chapter: int = 30          # 到第几章
    stages: list = field(default_factory=list)   # 从模板展开的阶段 [{name,min_ch,max_ch,events}]
    expanded: bool = False         # 是否已展开填充了桥段
    notes: str = ""                # 用户备注

    # 与其他大纲的关系
    overlaps_with: list[str] = field(default_factory=list)  # 与哪些大纲重叠（id 列表）
    predecessor: str = ""          # 前驱大纲 id
    successor: str = ""            # 后继大纲 id
    transition_type: str = "sequential"  # sequential(顺序接续)|overlap(重叠过渡)|merge(融合)

    # 叙事手法（故事线严谨性：顺叙/倒叙/插叙）
    narrative: str = "chronological"   # chronological(顺叙)|flashback(倒叙)|interleaved(插叙)
    narrative_target: str = ""         # flashback: 回忆的时间段/章节；interleaved: 所嵌入的主弧 id


@dataclass
class PlotSlot:
    """一个桥段在大纲阶段内的位置"""
    id: str                        # 唯一标识
    template_id: str               # 对应 PlotLibrary 里的模板
    name: str                      # 显示名称
    category: str = ""             # 爽文/开篇/战斗/...
    sub_category: str = ""         # 子分类
    outline_id: str = ""           # 属于哪个大纲
    stage_index: int = 0           # 属于哪个阶段（outline.stages 的索引）
    parent_plot_id: str = ""       # 嵌套：父桥段 id，空=顶级
    children_plot_ids: list[str] = field(default_factory=list)  # 子桥段

    # 位置信息（用于时间线展示）
    order: int = 0                 # 阶段内排序
    cover_beats: int = 4           # 预计覆盖多少个节拍
    template_structure: str = ""   # 桥段模板结构字符串（箭头流程）
    slots: list = field(default_factory=list)  # 变量槽位

    # 注入的加料
    gag_ids: list[str] = field(default_factory=list)    # 匹配的笑点
    theme_hints: list[str] = field(default_factory=list)  # 内涵提示
    hook_points: list[str] = field(default_factory=list)  # 吸睛点

    confirmed: bool = False        # 用户已确认
    written_chapter: int = 0       # 已写入第几章（0=未写，用于断点续写）


@dataclass
class BookTimeline:
    """整本书的时间线配置 —— 新书启动的核心产出"""
    book_title: str = ""
    genre: str = ""
    sub_genre: str = ""
    words_per_chapter: int = 3000
    pen_name: str = ""

    # 基础信息库（参考 show-me-the-story 的设定体系）
    basic_info: dict = field(default_factory=lambda: {
        "protagonist": {"name": "", "identity": "", "personality": "", "background": "", "golden_finger": ""},
        "world_building": {"era": "", "power_system": "", "factions": [], "rules": []},
        "supporting_cast": [],
        "tone": "",        # 轻松/沉重/热血/幽默
        "target_audience": "",
    })

    # 时间线
    outlines: list[OutlineSlot] = field(default_factory=list)
    plots: list[PlotSlot] = field(default_factory=list)

    # 全书贯穿元素
    themes: list[str] = field(default_factory=list)
    global_gags: list[str] = field(default_factory=list)

    # 状态
    phase: str = "config"          # config|outlines|plots|gags|ready
    generated_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "book_title": self.book_title,
            "genre": self.genre,
            "sub_genre": self.sub_genre,
            "words_per_chapter": self.words_per_chapter,
            "pen_name": self.pen_name,
            "basic_info": self.basic_info,
            "outlines": [{
                "id": o.id, "template_id": o.template_id, "name": o.name,
                "start_chapter": o.start_chapter, "end_chapter": o.end_chapter,
                "stages": o.stages, "expanded": o.expanded, "notes": o.notes,
                "overlaps_with": o.overlaps_with,
                "predecessor": o.predecessor, "successor": o.successor,
                "transition_type": o.transition_type,
                "narrative": o.narrative,
                "narrative_target": o.narrative_target,
            } for o in self.outlines],
            "plots": [{
                "id": p.id, "template_id": p.template_id, "name": p.name,
                "category": p.category, "sub_category": p.sub_category,
                "outline_id": p.outline_id, "stage_index": p.stage_index,
                "parent_plot_id": p.parent_plot_id,
                "children_plot_ids": p.children_plot_ids,
                "order": p.order, "cover_beats": p.cover_beats,
                "template_structure": p.template_structure,
                "slots": p.slots,
                "gag_ids": p.gag_ids, "theme_hints": p.theme_hints,
                "hook_points": p.hook_points,
                "confirmed": p.confirmed,
                "written_chapter": p.written_chapter,
            } for p in self.plots],
            "themes": self.themes,
            "global_gags": self.global_gags,
            "phase": self.phase,
            "generated_at": self.generated_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BookTimeline":
        tl = cls(
            book_title=d.get("book_title", ""),
            genre=d.get("genre", ""),
            sub_genre=d.get("sub_genre", ""),
            words_per_chapter=d.get("words_per_chapter", 3000),
            pen_name=d.get("pen_name", ""),
            basic_info=d.get("basic_info", {}),
            themes=d.get("themes", []),
            global_gags=d.get("global_gags", []),
            phase=d.get("phase", "config"),
            generated_at=d.get("generated_at", ""),
            updated_at=d.get("updated_at", ""),
        )
        tl.outlines = [OutlineSlot(
            id=o.get("id", ""), template_id=o.get("template_id", ""),
            name=o.get("name", ""), start_chapter=o.get("start_chapter", 1),
            end_chapter=o.get("end_chapter", 30), stages=o.get("stages", []),
            expanded=o.get("expanded", False), notes=o.get("notes", ""),
            overlaps_with=o.get("overlaps_with", []),
            predecessor=o.get("predecessor", ""),
            successor=o.get("successor", ""),
            transition_type=o.get("transition_type", "sequential"),
            narrative=o.get("narrative", "chronological"),
            narrative_target=o.get("narrative_target", ""),
        ) for o in d.get("outlines", [])]
        tl.plots = [PlotSlot(
            id=p.get("id", ""), template_id=p.get("template_id", ""),
            name=p.get("name", ""), category=p.get("category", ""),
            sub_category=p.get("sub_category", ""),
            outline_id=p.get("outline_id", ""), stage_index=p.get("stage_index", 0),
            parent_plot_id=p.get("parent_plot_id", ""),
            children_plot_ids=p.get("children_plot_ids", []),
            order=p.get("order", 0), cover_beats=p.get("cover_beats", 4),
            template_structure=p.get("template_structure", ""),
            slots=p.get("slots", []),
            gag_ids=p.get("gag_ids", []),
            theme_hints=p.get("theme_hints", []),
            hook_points=p.get("hook_points", []),
            confirmed=p.get("confirmed", False),
            written_chapter=p.get("written_chapter", 0),
        ) for p in d.get("plots", [])]
        return tl


# ═══════════════════════════════════════════
# 时间线生成器
# ═══════════════════════════════════════════

class TimelineBuilder:
    """根据流派和用户需求，生成大纲时间线 + 桥段配置"""

    def __init__(self, structure_lib=None, plot_lib=None, gag_lib=None, theme_lib=None, llm_client=None):
        self.structures = structure_lib
        self.plots = plot_lib
        self.gags = gag_lib
        self.themes = theme_lib
        self.llm = llm_client
        self._counter = 0

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._counter:04d}"

    def build_outline_sequence(
        self,
        genre: str = "玄幻",
        sub_genre: str = "",
        custom_context: str = "",
        max_outlines: int = 5,
        mode: str = "ai",
    ) -> list[OutlineSlot]:
        """
        生成大纲序列。
        mode="ai" → AI 辅助（需要 llm）；mode="rule" → 纯规则。
        """
        if mode == "rule" or not self.llm:
            return self._rule_build_sequence(genre)
        return self._ai_build_sequence(genre, sub_genre, custom_context, max_outlines)

    def _rule_build_sequence(self, genre: str) -> list[OutlineSlot]:
        """规则拼接：按流派选 2-3 个大纲，默认顺序接续"""
        if not self.structures:
            return []

        # 流派→常见大纲序列
        genre_map = {
            "玄幻": ["struct_xuanhuan_01", "struct_xuanhuan_01"],  # 升级×2
            "都市": ["struct_urban_01", "struct_urban_01"],
            "言情": ["struct_romance_01", "struct_romance_01"],
            "悬疑": ["struct_mystery_01", "struct_mystery_01"],
            "穿越": ["struct_time_travel_01", "struct_xuanhuan_01"],
        }

        template_ids = genre_map.get(genre, ["struct_xuanhuan_01"])
        outlines = []
        ch = 1
        for i, tid in enumerate(template_ids):
            tmpl = self.structures.get_by_id(tid)
            if not tmpl:
                continue
            oid = self._next_id("outline")
            outlines.append(OutlineSlot(
                id=oid,
                template_id=tid,
                name=f"{tmpl.name}{f'(第{i+1}部分)' if len(template_ids)>1 else ''}",
                start_chapter=ch,
                end_chapter=ch + tmpl.total_chapters - 1,
                stages=[
                    {"name": s.name, "min_ch": s.min_chapters, "max_ch": s.max_chapters,
                     "events": s.key_events[:5]}
                    for s in tmpl.stages
                ],
                predecessor=outlines[-1].id if outlines else "",
                transition_type="sequential",
            ))
            if len(outlines) > 1:
                outlines[-2].successor = outlines[-1].id
            ch = outlines[-1].end_chapter + 1
        return outlines

    def _ai_build_sequence(
        self, genre: str, sub_genre: str, context: str, max_outlines: int
    ) -> list[OutlineSlot]:
        """AI 辅助生成大纲序列"""
        available = ""
        if self.structures:
            templates = self.structures.templates[:20]  # 最多 20 个候选
            available = "\n".join(
                f"- {t.id}: {t.name} ({t.total_chapters}章) | 阶段: {'→'.join(s.name for s in t.stages[:5])}"
                for t in templates
            )

        prompt = f"""为一本{genre}/{sub_genre}类网络小说设计大纲时间线。

用户想法：{context if context else '标准开局'}

请从可用大纲模板中选择 2-{max_outlines}个，按时间线串联。大纲之间可以重叠交叉（前一个还没结束，后一个已经开始）。

返回JSON：
{{
  "outlines": [
    {{
      "template_id": "struct_xxx",
      "name": "给这段起个名",
      "start_chapter": 1,
      "end_chapter": 30,
      "overlaps_with": [],
      "transition_type": "sequential|overlap|merge",
      "reason": "为什么在这个位置选这个大纲"
    }}
  ]
}}

注意：
- 相邻大纲建议有 3-5 章的重叠区（过渡更自然）
- 同一流派下可以有不同风格的大纲（如开局爽文→中期正剧）
- 总章节数控制在合理范围内（不要超过 500）

可用大纲模板：
{available[:2000]}"""

        try:
            raw = self.llm.call(
                "你是一位专业的网络小说策划编辑。请只返回JSON，不要加任何额外文字。",
                prompt, temperature=0.7, max_tokens=2048)
            from core.llm_client import extract_json
            data = json.loads(extract_json(raw))
            outlines_data = data.get("outlines", [])
        except Exception:
            return self._rule_build_sequence(genre)

        outlines = []
        prev_id = ""
        for od in outlines_data:
            oid = self._next_id("outline")
            tid = od.get("template_id", "")
            # 从模板库展开阶段
            stages = []
            if self.structures:
                tmpl = self.structures.get_by_id(tid)
                if tmpl:
                    stages = [
                        {"name": s.name, "min_ch": s.min_chapters, "max_ch": s.max_chapters,
                         "events": s.key_events[:5]}
                        for s in tmpl.stages
                    ]
            outline = OutlineSlot(
                id=oid,
                template_id=tid,
                name=od.get("name", f"大纲{len(outlines)+1}"),
                start_chapter=od.get("start_chapter", outlines[-1].end_chapter + 1 if outlines else 1),
                end_chapter=od.get("end_chapter", (outlines[-1].end_chapter if outlines else 0) + 30),
                stages=stages,
                overlaps_with=od.get("overlaps_with", []),
                predecessor=prev_id,
                transition_type=od.get("transition_type", "sequential"),
            )
            if outlines:
                outlines[-1].successor = oid
            outlines.append(outline)
            prev_id = oid

        # 填充 overlaps_with（引用前面大纲的 id）
        for i, o in enumerate(outlines):
            if i > 0 and o.start_chapter <= outlines[i-1].end_chapter:
                o.overlaps_with.append(outlines[i-1].id)

        return outlines

    def fill_plots_for_outline(
        self, outline: OutlineSlot, timeline: BookTimeline,
    ) -> list[PlotSlot]:
        """
        给一个大纲的每个阶段填充桥段。

        支持嵌套：第一个桥段作为"框"，后续桥段嵌入其中。
        """
        if not self.plots:
            return []

        new_plots = []
        for si, stage in enumerate(outline.stages):
            stage_name = stage.get("name", "")
            events = stage.get("events", [])

            # 匹配桥段：阶段名+事件描述+流派
            context = f"{outline.name} {stage_name} {' '.join(events)}"
            candidates = self.plots.match_for_chapter(context, timeline.genre)
            if not candidates:
                candidates = self.plots.search(category=timeline.genre)
                if not candidates:
                    candidates = self.plots.templates[:1]

            # 取 1-3 个桥段（支持嵌套）
            selected = candidates[:min(3, len(candidates))]
            parent_id = ""
            for pi, tmpl in enumerate(selected):
                pid = self._next_id("plot")
                p = PlotSlot(
                    id=pid,
                    template_id=tmpl.id,
                    name=tmpl.name,
                    category=tmpl.category,
                    sub_category=tmpl.sub_category or "",
                    outline_id=outline.id,
                    stage_index=si,
                    parent_plot_id=parent_id,
                    order=pi,
                    cover_beats=tmpl.word_range[1] // 400 if tmpl.word_range else 4,
                    template_structure="→".join(tmpl.template_structure) if tmpl.template_structure else "",
                    slots=[{"name": s.name, "default": s.default, "options": s.options}
                           for s in tmpl.slots],
                )
                new_plots.append(p)
                if parent_id:
                    # 找到父桥段并添加子关系
                    for existing in timeline.plots + new_plots:
                        if existing.id == parent_id:
                            existing.children_plot_ids.append(pid)
                            break
                parent_id = pid  # 链式嵌套（每个桥段包下一个）

        outline.expanded = True
        return new_plots

    def fill_gags_and_hooks(self, plots: list[PlotSlot], timeline: BookTimeline):
        """给桥段注入笑点和吸睛点"""
        if not self.gags or not self.themes:
            return

        for p in plots:
            # 笑点匹配
            candidates = self.gags.search(scene=p.category)
            p.gag_ids = [g.id for g in candidates[:2]]

            # 内涵匹配
            if timeline.themes:
                p.theme_hints = timeline.themes[:2]

            # 吸睛点
            p.hook_points = [
                f"{p.name}的{slot.get('name','?')}" for slot in p.slots[:2]
            ]


# ═══════════════════════════════════════════
# 持久化
# ═══════════════════════════════════════════

def save_timeline(timeline: BookTimeline, path: str):
    """保存时间线到文件"""
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(timeline.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def load_timeline(path: str) -> Optional[BookTimeline]:
    """从文件加载时间线"""
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return BookTimeline.from_dict(data)
    except Exception:
        return None


def merge_basic_info(existing: dict, generated: dict) -> dict:
    """以 generated 为基础，保留 existing 里用户已填的非空字段（原地累加用）。

    供大纲生成器与 web_ui 共用，避免同一逻辑两份拷贝。
    """
    existing = existing or {}
    generated = generated or {}
    merged = {}
    for key, gv in generated.items():
        ev = existing.get(key)
        if isinstance(gv, dict) and isinstance(ev, dict):
            sub = dict(gv)
            for sk, sv in ev.items():
                if sv not in (None, "", [], {}):
                    sub[sk] = sv
            merged[key] = sub
        elif ev not in (None, "", [], {}):
            merged[key] = ev
        else:
            merged[key] = gv
    for key, ev in existing.items():  # generated 没覆盖的字段也保留
        if key not in merged:
            merged[key] = ev
    return merged
