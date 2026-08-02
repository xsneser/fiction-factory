"""
书籍组装器（Book Assembler）
核心 Selector/Injector —— 按大纲结构，智能匹配桥段、笑点、内涵，生成写作计划

流程：
  选定大纲 → 逐阶段匹配桥段 → 逐桥段匹配笑点 → 选定全书母题 → 生成计划 → 起名

写作时：ChapterWriter 读写作计划 → LLM 看到 "这个节拍要用的桥段模板 + 笑点模式 + 内涵锚点"
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import json
import random
import time

from .plot import PlotLibrary, PlotTemplate
from .structure import StructureLibrary, StructureTemplate, StageNode
from .gag import GagLibrary, GagPattern
from .theme import ThemeLibrary, ThemeEntry


# ═══════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════

@dataclass
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
# 书籍组装器
# ═══════════════════════════════════════════

class BookAssembler:
    """书籍组装器 —— 把四大库的材料按大纲组装成写作计划"""

    def __init__(
        self,
        plot_lib: Optional[PlotLibrary] = None,
        structure_lib: Optional[StructureLibrary] = None,
        gag_lib: Optional[GagLibrary] = None,
        theme_lib: Optional[ThemeLibrary] = None,
        llm_client=None,
    ):
        self.plots = plot_lib or PlotLibrary()
        self.structures = structure_lib or StructureLibrary()
        self.gags = gag_lib or GagLibrary()
        self.themes = theme_lib or ThemeLibrary()
        self.llm = llm_client

    def assemble_book(
        self,
        genre: str = "玄幻",
        sub_genre: str = "",
        custom_context: str = "",
        title_hint: str = "",
    ) -> BookAssemblerPlan:
        """
        根据流派和上下文组装一本书的写作计划。

        流程：
        1. 按流派+子流派匹配大纲结构
        2. 逐阶段匹配桥段（用大纲阶段描述+流派做关键词）
        3. 逐桥段匹配笑点
        4. 选定全书母题
        5. 为每个阶段生成内涵提示
        6. 生成书名
        """
        # Step 1: 匹配大纲
        matches = self.structures.search(genre=genre, sub_genre=sub_genre)
        if not matches:
            matches = self.structures.search(genre=genre)
        if not matches:
            matches = self.structures.templates
        structure = matches[0]

        # Step 2: 逐阶段匹配桥段
        stage_plans = []
        for i, stage in enumerate(structure.stages):
            sp = self._assemble_stage(i, stage, genre, structure)

            # 阶段级笑点分配
            if sp.plot:
                sp.gags = self._select_gags_for_plot(sp.plot, sp.stage_name, stage)

            stage_plans.append(sp)

        # Step 3: 选定全书母题（1-2个）
        themes = self._select_themes(stage_plans, genre)

        # Step 4: 生成内涵提示
        theme_hints = self._build_theme_hints(themes, structure)

        # Step 5: 为每个阶段注入内涵提示
        for sp in stage_plans:
            sp.theme_hints = self._get_stage_theme_hints(sp, themes, structure)

        # Step 6: 生成书名
        book_title = self._generate_title(
            genre=genre,
            structure=structure,
            themes=themes,
            hint=title_hint,
            custom_context=custom_context,
        )

        return BookAssemblerPlan(
            book_title=book_title,
            genre=genre,
            structure=structure,
            stages=stage_plans,
            themes=themes,
            theme_hints=theme_hints,
            generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    # ─── Stage Assembly ───

    def _assemble_stage(
        self, index: int, stage: StageNode, genre: str,
        structure: StructureTemplate,
    ) -> StageWritingPlan:
        """为一个阶段选择桥段"""
        sp = StageWritingPlan(
            stage_index=index,
            stage_name=stage.name,
            stage_description=stage.description,
            chapter_range=(stage.min_chapters, stage.max_chapters),
        )

        # 构建搜索上下文：阶段名 + 阶段描述 + 关键事件 + 流派
        search_context = f"{stage.name} {stage.description} {' '.join(stage.key_events)}"

        # 用 PlotLibrary 的 match_for_chapter 做匹配
        candidates = self.plots.match_for_chapter(search_context, genre)

        if not candidates:
            # 放宽：只按流派搜索
            candidates = self.plots.search(category=genre)

        if candidates:
            sp.plot = candidates[0]
            sp.plot_match_reason = (
                f"阶段「{stage.name}」关键词匹配："
                f"桥段分类「{sp.plot.category}/{sp.plot.sub_category}」"
            )
        else:
            # 实在没有匹配的，用第一个通用桥段
            sp.plot = self.plots.templates[0] if self.plots.templates else None
            sp.plot_match_reason = "无精确匹配，使用默认桥段"

        return sp

    # ─── Gag Selection ───

    def _select_gags_for_plot(
        self, plot: PlotTemplate, stage_name: str, stage: StageNode,
    ) -> list[GagPattern]:
        """为一个桥段选择笑点"""
        result: list[GagPattern] = []
        used_ids: set[str] = set()

        # 策略：根据桥段的场景类型 + 阶段场景匹配
        scene_map = {
            "打脸": ["打脸后", "身份揭示", "多人场景"],
            "战斗": ["战斗后放松", "战斗间隙", "关键时刻"],
            "情感": ["日常互动", "身份揭露"],
            "成长": ["日常", "社交场合"],
            "日常": ["日常对话", "日常互动"],
            "冒险": ["战斗间", "新地图进入"],
            "冲突": ["冲突边缘", "多人场景"],
            "转折": ["身份揭示", "日常"],
        }

        plot_category = plot.category
        fit_scenes = []
        # 从桥段 fit_contexts 推断场景类型
        for fc in plot.fit_contexts:
            if fc in scene_map:
                fit_scenes.extend(scene_map[fc])
        if not fit_scenes:
            fit_scenes = ["日常", "战斗后", "多人场景"]

        # 去重 + 按场景匹配
        for scene in fit_scenes:
            candidates = self.gags.search(scene=scene)
            candidates = [g for g in candidates if g.id not in used_ids]
            if candidates:
                chosen = candidates[0]  # 按使用次数最少优先
                result.append(chosen)
                used_ids.add(chosen.id)
            if len(result) >= 3:
                break

        return result

    # ─── Theme Selection ───

    def _select_themes(
        self, stage_plans: list[StageWritingPlan], genre: str,
    ) -> list[ThemeEntry]:
        """选定全书母题"""
        # 收集所有使用的桥段的 compatible_plots
        all_plot_ids = set()
        for sp in stage_plans:
            if sp.plot:
                all_plot_ids.add(sp.plot.id)

        # 评分：每个 theme 覆盖了多少个被使用的桥段
        scored: list[tuple[int, ThemeEntry]] = []
        for theme in self.themes.entries:
            if not theme.enabled:
                continue
            match_count = sum(1 for pid in all_plot_ids
                              if pid in theme.compatible_plots)
            scored.append((match_count, theme))

        scored.sort(key=lambda x: -x[0])

        # 选 1-2 个
        return [t for _, t in scored[:2]]

    # ─── Theme Hints ───

    def _build_theme_hints(
        self, themes: list[ThemeEntry], structure: StructureTemplate,
    ) -> list[str]:
        """生成浓缩的内涵提示，注入到每个写作节拍中"""
        hints = []
        for theme in themes:
            hints.append(
                f"【母题】{theme.name}：{theme.description}"
            )
            if theme.tips:
                hints.append(
                    f"  表现手法：{' / '.join(theme.techniques)}。"
                    f"提示：{'; '.join(theme.tips[:2])}"
                )
        return hints

    def _get_stage_theme_hints(
        self, sp: StageWritingPlan, themes: list[ThemeEntry],
        structure: StructureTemplate,
    ) -> list[str]:
        """为特定阶段生成内涵提示"""
        hints = []
        for theme in themes:
            if sp.plot and sp.plot.id in theme.compatible_plots:
                hints.append(
                    f"母题「{theme.name}」与本阶段桥段高度匹配 —— "
                    f"建议通过{theme.techniques[0]}来体现"
                )
        return hints

    # ─── Title Generation ───

    def _generate_title(
        self, genre: str, structure: StructureTemplate,
        themes: list[ThemeEntry], hint: str, custom_context: str,
    ) -> str:
        """生成书名"""
        if self.llm:
            return self._ai_title(genre, structure, themes, hint, custom_context)
        else:
            return self._rule_title(genre, structure, themes, hint)

    def _ai_title(self, genre, structure, themes, hint, custom_context) -> str:
        theme_names = "、".join(t.name for t in themes)
        prompt = f"""为以下设定的小说生成 5 个候选书名：

流派：{genre}（{structure.name if structure else ''}）
核心母题：{theme_names}
风格提示：{custom_context or '无特殊要求'}
用户提示：{hint or '无'}

要求：
1. 书名要吸引眼球，符合网文风格
2. 每个书名不超过 8 个字
3. 可以加入符号如「」「:」
4. 返回 JSON：{{"titles": ["书名1", "书名2", ...]}}"""

        try:
            from core.llm_client import extract_json
            raw = self.llm.call(
                "你是网文编辑，专门为小说起名。只返回JSON。",
                prompt, temperature=0.9, max_tokens=512,
            )
            data = json.loads(extract_json(raw))
            titles = data.get("titles", [])
            return titles[0] if titles else self._rule_title(genre, structure, themes, hint)
        except Exception:
            return self._rule_title(genre, structure, themes, hint)

    def _rule_title(self, genre, structure, themes, hint) -> str:
        if hint:
            return hint

        genre_prefixes = {
            "玄幻": ["苍穹", "万古", "九天", "不朽", "混沌", "鸿蒙", "天帝", "至尊"],
            "都市": ["都市", "逆袭", "重生之", "超级", "低调", "人生"],
            "悬疑": ["谜案", "异闻", "诡案", "暗夜", "破晓"],
            "言情": ["甜蜜", "时光", "与你", "心动", "初见"],
            "穿越": ["穿越之", "重生之", "开局", "回到", "颠覆"],
        }
        genre_suffixes = {
            "玄幻": ["", "纪", "录", "传", "道", "诀", "经"],
            "都市": ["高手", "奶爸", "总裁", "首富", "霸主"],
            "悬疑": ["录", "集", "档案", "手记", "调查"],
            "言情": ["手册", "日记", "日常", "指南"],
            "穿越": ["", "当", "开局", "从…开始"],
        }

        prefixes = genre_prefixes.get(genre, ["传奇"])
        suffixes = genre_suffixes.get(genre, ["录"])

        a = random.choice(prefixes)
        b = random.choice(suffixes)
        return f"{a}{b}"


# ═══════════════════════════════════════════
# 写作计划注入器
# ═══════════════════════════════════════════

class PlanInjector:
    """
    将 BookAssemblerPlan 注入到 LLM 写作 prompt 中。

    职责：把数据库中选定的大纲/桥段/笑点/内涵，转化为 LLM 能理解
    并执行的写作指令。这是连接"库"和"生成管线"的桥梁。
    """

    @staticmethod
    def get_stage_context(
        plan: BookAssemblerPlan, stage_index: int,
    ) -> dict:
        """获取指定阶段写作时要注入的上下文"""
        if 0 <= stage_index < len(plan.stages):
            sp = plan.stages[stage_index]
        else:
            return {}

        ctx = {
            "stage_name": sp.stage_name,
            "stage_description": sp.stage_description,
        }

        # 桥段结构
        if sp.plot:
            ctx["plot_name"] = sp.plot.name
            ctx["plot_structure"] = sp.plot.template_structure
            ctx["plot_slots"] = [
                {"name": s.name, "options": s.options, "default": s.default}
                for s in sp.plot.slots
            ]
            ctx["plot_usage_notes"] = sp.plot.usage_notes

        # 笑点
        if sp.gags:
            ctx["gags"] = [
                {
                    "name": g.name,
                    "pattern": g.pattern_description,
                    "template": g.template,
                }
                for g in sp.gags
            ]

        # 内涵
        ctx["theme_hints"] = plan.theme_hints + sp.theme_hints

        return ctx

    @staticmethod
    def build_chapter_prompt_enrichment(
        plan: BookAssemblerPlan, stage_index: int,
    ) -> str:
        """
        生成一段注入到章节写作 prompt 中的库材料文本。

        这段文本会被追加到 BeatExecutor 的 prompt 中，告诉 LLM：
        - 这一章的桥段结构是什么
        - 笑点在哪个位置插入
        - 要体现什么内涵
        """
        ctx = PlanInjector.get_stage_context(plan, stage_index)
        if not ctx:
            return ""

        parts = []

        # 1. 桥段注入
        if ctx.get("plot_structure"):
            parts.append("【本章桥段模板】")
            parts.append(f"桥段：{ctx.get('plot_name', '')}")
            parts.append(f"结构骨架：{ctx['plot_structure']}")
            if ctx.get("plot_slots"):
                slots_text = "、".join(
                    f"{s['name']}={s['default']}"
                    for s in ctx["plot_slots"]
                )
                parts.append(f"变量槽位：{slots_text}")
            if ctx.get("plot_usage_notes"):
                parts.append(f"使用方法：{ctx['plot_usage_notes']}")

        # 2. 笑点注入
        if ctx.get("gags"):
            gags_text = "\n".join(
                f"- [{g['name']}] {g['pattern']} → 例句模式：{g['template']}"
                for g in ctx["gags"]
            )
            parts.append(f"\n【本章笑点模式（在自然位置融入1-2个）】\n{gags_text}")

        # 3. 内涵注入
        if ctx.get("theme_hints"):
            hints_text = "\n".join(f"- {h}" for h in ctx["theme_hints"])
            parts.append(f"\n【本章要体现的内涵】\n{hints_text}")

        return "\n".join(parts)


# ═══════════════════════════════════════════
# 便捷工厂
# ═══════════════════════════════════════════

def create_assembler(llm_client=None):
    """创建组装器的便捷工厂"""
    return BookAssembler(
        plot_lib=PlotLibrary(),
        structure_lib=StructureLibrary(),
        gag_lib=GagLibrary(),
        theme_lib=ThemeLibrary(),
        llm_client=llm_client,
    )


# ═══════════════════════════════════════════
# 计划持久化
# ═══════════════════════════════════════════

def save_plan(plan: BookAssemblerPlan, path: str):
    """保存写作计划到文件"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(plan.to_dict(), f, ensure_ascii=False, indent=2)


def load_plan(path: str) -> dict:
    """加载写作计划（返回 dict，因需要外部注入 library 对象才能完整反序列化）"""
    with open(path, encoding="utf-8") as f:
        return json.load(f)
