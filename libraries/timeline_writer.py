"""
蓝图式写作引擎（Timeline Writer）— 按 BookTimeline 施工

与旧写作核心（ChapterWriter 流水线）的区别：
  旧：先有章节大纲文字 → AI 拆节拍 → 逐节拍生成（桥段/笑点按阶段模糊注入）
  新：先有整书蓝图（BookTimeline）→ 按章节位置解析出"该处活跃的大纲+桥段+笑点钉子"
      → 桥段结构直接驱动节拍规划 → 逐节拍生成（精确注入）

流程：
  chapter_num → 累计字数位置 → 解析活跃元素（大纲/桥段嵌套链/笑点/内涵）
  → 桥段结构拆成节拍序列 → 每节拍注入对应桥段+笑点钉子 → LLM 生成 → 组装
"""
from dataclasses import dataclass, field
from typing import Optional
import re

from .timeline import BookTimeline, OutlineSlot, PlotSlot
from .beat_writer import (
    BeatLibrary, Beat, ChapterBeatPlan, BeatExecutor,
    ChapterAssembler, BeatResult,
)


# ═══════════════════════════════════════════
# 位置解析器：章节 → 蓝图元素
# ═══════════════════════════════════════════

@dataclass
class ChapterBlueprint:
    """一章写作需要的全部蓝图上下文"""
    chapter_num: int
    cumulative_words: int          # 本章开始的累计字数（估算）
    chapter_words_target: int      # 本章目标字数

    active_outlines: list[OutlineSlot] = field(default_factory=list)  # 可能多个（重叠）
    active_plots: list[PlotSlot] = field(default_factory=list)        # 本章要走的桥段
    active_gags: list[str] = field(default_factory=list)              # 笑点钉子描述
    active_themes: list[str] = field(default_factory=list)            # 内涵提示

    def has_content(self) -> bool:
        return bool(self.active_plots) or bool(self.active_outlines)


class TimelinePositionResolver:
    """
    把章节号映射到时间线位置，取回该位置活跃的蓝图元素。

    章节→字数映射：按每章 words_per_chapter 估算累计字数，
    大纲/桥段用 start/end（字数）判断是否覆盖当前位置。
    """

    def __init__(self, timeline: BookTimeline):
        self.tl = timeline
        self.words_per_chapter = max(timeline.words_per_chapter or 3000, 500)

    def resolve(self, chapter_num: int) -> ChapterBlueprint:
        # 估算累计字数：本章开头的字数位置
        start_words = (chapter_num - 1) * self.words_per_chapter
        end_words = chapter_num * self.words_per_chapter
        mid_words = (start_words + end_words) // 2

        bp = ChapterBlueprint(
            chapter_num=chapter_num,
            cumulative_words=start_words,
            chapter_words_target=self.words_per_chapter,
        )

        # 1. 活跃大纲：位置被大纲范围覆盖（含重叠）
        for o in self.tl.outlines:
            o_start = (o.start_chapter - 1) * self.words_per_chapter
            o_end = o.end_chapter * self.words_per_chapter
            if mid_words >= o_start and mid_words < o_end:
                bp.active_outlines.append(o)

        # 2. 活跃桥段：属于活跃大纲、阶段对应本章、且位置在桥段覆盖范围内
        #    桥段按 order 排序，作为本章节拍的骨架
        active_outline_ids = {o.id for o in bp.active_outlines}
        candidate_plots = [
            p for p in self.tl.plots
            if p.outline_id in active_outline_ids
        ]

        # 估算桥段的字数位置（按桥段在阶段内的比例）
        for p in sorted(candidate_plots, key=lambda x: (x.outline_id, x.stage_index, x.order)):
            # 用桥段的 cover_beats 占阶段权重粗估位置
            bp.active_plots.append(p)

        # 3. 笑点/内涵钉子：从活跃桥段收集
        for p in bp.active_plots:
            for gid in p.gag_ids[:2]:
                bp.active_gags.append(gid)
            bp.active_themes.extend(p.theme_hints[:1])

        return bp

    def build_plot_chain(self, plot: PlotSlot) -> list[PlotSlot]:
        """返回桥段的嵌套链（自身 + 所有子桥段）"""
        chain = [plot]
        children = [p for p in self.tl.plots if p.parent_plot_id == plot.id]
        for c in sorted(children, key=lambda x: x.order):
            chain.extend(self.build_plot_chain(c))
        return chain


# ═══════════════════════════════════════════
# 蓝图 → 节拍规划
# ═══════════════════════════════════════════

class TimelinePlanner:
    """
    蓝图驱动的节拍规划器：
    桥段模板结构（[步骤1]→[步骤2]→...）本身就是节拍序列骨架，
    嵌套桥段作为子节拍插入父桥段之后。
    """

    def __init__(self, beat_lib: BeatLibrary = None, llm_client=None):
        self.beat_lib = beat_lib or BeatLibrary()
        self.llm = llm_client

    def plan_chapter(self, bp: ChapterBlueprint) -> ChapterBeatPlan:
        """根据蓝图生成节拍计划"""
        if self.llm and bp.active_plots:
            return self._ai_plan(bp)
        return self._rule_plan(bp)

    def _rule_plan(self, bp: ChapterBlueprint) -> ChapterBeatPlan:
        """规则规划：桥段结构 → 节拍"""
        beats: list[Beat] = []
        idx = 1

        # 1. 开头钩子（如果本章是第一个大纲的第一章）
        if bp.chapter_num <= 3 or idx == 1:
            beats.append(Beat(
                index=idx, beat_type="hook",
                description="开场钩子：用冲击性画面/对话抓住读者",
                template_id="beat_hook_crisis",
                humor_required=True, word_target=200,
            ))
            idx += 1

        # 2. 每个活跃桥段 → 结构步骤 → 节拍
        for p in bp.active_plots[:3]:  # 一章最多走 3 个主桥段
            chain = self._chain_for(p, bp)
            for plot in chain:
                beats.extend(self._plot_to_beats(plot, bp, idx))
                idx = len(beats) + 1

        # 3. 兜底：如果没有桥段，用通用节拍
        if not beats:
            beats = self._fallback_beats(bp)

        # 4. 章末钩子
        beats.append(Beat(
            index=len(beats) + 1, beat_type="close",
            description="章末钩子：收束本章+留悬念",
            template_id="beat_close_cliffhanger",
            humor_required=False, word_target=200,
        ))

        # 设置衔接
        for i in range(1, len(beats)):
            beats[i].transition_from_prev = beats[i-1].description[:50]

        return ChapterBeatPlan(
            chapter_num=bp.chapter_num,
            chapter_title=f"第{bp.chapter_num}章",
            chapter_outline=self._outline_text(bp),
            beats=beats,
            total_words_target=bp.chapter_words_target,
        )

    def _chain_for(self, p: PlotSlot, bp: ChapterBlueprint) -> list[PlotSlot]:
        """取桥段的嵌套链（父→子）"""
        return [p] + [c for c in bp.active_plots if c.parent_plot_id == p.id]

    def _plot_to_beats(self, plot: PlotSlot, bp: ChapterBlueprint, start_idx: int) -> list[Beat]:
        """一个桥段 → 1-3 个节拍"""
        structure = plot.template_structure or ""
        steps = [s.strip() for s in re.split(r'[→>]', structure) if s.strip()]
        if not steps:
            steps = [plot.name]

        # 桥段的每个结构步骤 → 一个节拍
        beats = []
        for si, step in enumerate(steps[:3]):  # 一个桥段最多 3 个步骤节拍
            beat_type = self._step_to_beat_type(si, len(steps))
            template_id = {
                "hook": "beat_hook_crisis",
                "conflict": "beat_conflict_confront",
                "action": "beat_action_show_power",
                "twist": "beat_twist_reveal",
                "status": "beat_status_low",
            }.get(beat_type, "beat_status_normal")

            beats.append(Beat(
                index=start_idx + si,
                beat_type=beat_type,
                description=f"桥段「{plot.name}」步骤{si+1}：{step}",
                template_id=template_id,
                humor_required=bool(plot.gag_ids),
                word_target=max(200, bp.chapter_words_target // max(len(bp.active_plots)*3, 3)),
            ))
        return beats

    def _step_to_beat_type(self, idx: int, total: int) -> str:
        """结构步骤 → 节拍类型"""
        if idx == 0:
            return "hook" if total > 1 else "status"
        if idx == total - 1:
            return "twist" if idx > 1 else "conflict"
        return "conflict" if idx == 1 else "action"

    def _fallback_beats(self, bp: ChapterBlueprint) -> list[Beat]:
        return [
            Beat(1, "status", "展示主角当前处境", "beat_status_low", True, 300),
            Beat(2, "conflict", "引入本章核心矛盾", "beat_conflict_verbal", True, 350),
            Beat(3, "action", "主角应对与展现实力", "beat_action_clever_move", True, 350),
        ]

    def _outline_text(self, bp: ChapterBlueprint) -> str:
        """生成章节大纲文字（供 prompt 使用）"""
        parts = []
        for o in bp.active_outlines:
            parts.append(f"大纲：{o.name}（第{o.start_chapter}-{o.end_chapter}章）")
        for p in bp.active_plots[:3]:
            parts.append(f"桥段：{p.name} — {p.template_structure[:80]}")
        return "\n".join(parts)

    def _ai_plan(self, bp: ChapterBlueprint) -> ChapterBeatPlan:
        """AI 规划（规则规划的增强版：让 AI 细化桥段步骤）"""
        # 复用规则规划的骨架，但用 AI 优化节拍描述
        plan = self._rule_plan(bp)
        try:
            plots_desc = "\n".join(
                f"- {p.name}: {p.template_structure[:120]}"
                for p in bp.active_plots[:3]
            )
            prompt = f"""根据以下蓝图配置第{bp.chapter_num}章的节拍计划。

【本章活跃元素】
{plots_desc or '（无桥段，通用推进）'}

【现有节拍骨架】
{' | '.join(b.description for b in plan.beats)}

请优化每个节拍的描述，使其更具体（说清写什么、谁出场、发生什么），
保持节拍数量不变。返回 JSON：
{{"beats": [{{"index":1, "description":"具体描述"}}, ...]}}"""
            from core.llm_client import extract_json
            import json
            raw = self.llm.call("你是网文章节策划。只返回JSON。", prompt,
                                temperature=0.5, max_tokens=1536)
            data = json.loads(extract_json(raw))
            descs = {b.get("index"): b.get("description", "") for b in data.get("beats", [])}
            for b in plan.beats:
                if b.index in descs and descs[b.index]:
                    b.description = descs[b.index]
        except Exception:
            pass
        return plan


# ═══════════════════════════════════════════
# 蓝图注入器：把桥段/笑点/内涵转化为 prompt 指令
# ═══════════════════════════════════════════

class TimelineInjector:
    """把 ChapterBlueprint 转化为 LLM 写作指令（精确到桥段/笑点钉子）"""

    @staticmethod
    def build_enrichment(bp: ChapterBlueprint, plot_lib=None, gag_lib=None) -> str:
        parts = []

        # 1. 活跃大纲（重叠说明）
        if bp.active_outlines:
            o_desc = "、".join(f"{o.name}" for o in bp.active_outlines)
            parts.append(f"【当前大纲】{o_desc}")
            if len(bp.active_outlines) > 1:
                parts.append("⚠️ 当前处于多个大纲重叠区——自然过渡，不要生硬切换")

        # 2. 桥段（含嵌套链）
        for p in bp.active_plots[:3]:
            parts.append(f"\n【桥段：{p.name}】")
            if p.template_structure:
                parts.append(f"结构骨架：{p.template_structure}")
            if p.slots:
                slots = "、".join(f"{s.get('name','?')}={s.get('default','?')}" for s in p.slots[:4])
                parts.append(f"变量槽位：{slots}")
            if p.parent_plot_id:
                parts.append("（嵌套在父桥段中，篇幅更短，起辅助作用）")

        # 3. 笑点钉子（精确指令）
        if bp.active_gags:
            parts.append(f"\n【本节笑点钉子（必须在对应桥段中自然呈现）】\n{chr(10).join('- ' + g for g in bp.active_gags[:3])}")

        # 4. 内涵提示
        if bp.active_themes:
            parts.append(f"\n【内涵提示】{chr(10).join('- ' + t for t in bp.active_themes[:2])}")

        return "\n".join(parts)


# ═══════════════════════════════════════════
# 完整蓝图章节写作管线
# ═══════════════════════════════════════════

class TimelineChapterWriter:
    """
    蓝图式章节写作管线：

    1. TimelinePositionResolver: 章节号 → ChapterBlueprint（活跃大纲/桥段/笑点钉子）
    2. TimelinePlanner: 蓝图 → 节拍计划（桥段结构驱动）
    3. BeatExecutor: 逐节拍生成（注入蓝图指令）
    4. ChapterAssembler: 拼接+过渡+去AI+校验
    """

    def __init__(self, timeline: BookTimeline, llm_client=None,
                 de_ai_engine=None, reviewer=None, gag_lib=None,
                 plot_lib=None, profile=None):
        self.tl = timeline
        self.llm = llm_client
        self.resolver = TimelinePositionResolver(timeline)
        self.beat_lib = BeatLibrary()
        self.planner = TimelinePlanner(self.beat_lib, llm_client)
        self.executor = BeatExecutor(llm_client, self.beat_lib, gag_lib, profile)
        self.assembler = ChapterAssembler(llm_client, de_ai_engine, reviewer)
        self.plot_lib = plot_lib
        self.gag_lib = gag_lib

    def write_chapter(self, chapter_num: int,
                      previous_chapter_ending: str = "",
                      previous_summary: str = "",
                      character_states: str = "",
                      on_beat=None) -> dict:
        """按蓝图写一章"""
        # Step 1: 解析蓝图
        bp = self.resolver.resolve(chapter_num)
        if not bp.has_content():
            # 超出时间线范围：用通用推进
            bp.active_outlines = [self.tl.outlines[-1]] if self.tl.outlines else []
            bp.active_plots = [
                p for p in self.tl.plots
                if p.outline_id == (self.tl.outlines[-1].id if self.tl.outlines else "")
            ][:2]

        # Step 2: 节拍规划
        plan = self.planner.plan_chapter(bp)

        # Step 3: 蓝图指令注入
        enrichment = TimelineInjector.build_enrichment(bp, self.plot_lib, self.gag_lib)

        # Step 4: 逐节拍生成
        context = {
            "chapter_num": chapter_num,
            "chapter_outline": plan.chapter_outline,
            "genre": self.tl.genre,
            "pen_name": self.tl.pen_name,
            "character_states": character_states,
        }
        beat_results: list[BeatResult] = []
        accumulated = ""
        for beat in plan.beats:
            result = self.executor.execute_beat(
                beat, context, accumulated,
                library_enrichment=enrichment)
            beat_results.append(result)
            accumulated += result.text + "\n\n"
            if on_beat:
                on_beat(beat.index, result)

        # Step 5: 组装
        full_text = self.assembler.assemble(
            plan, beat_results, previous_chapter_ending)

        total_wc = len(re.findall(r'[\u4e00-\u9fff]', full_text))
        return {
            "chapter_num": chapter_num,
            "text": full_text,
            "word_count": total_wc,
            "beats": len(beat_results),
            "blueprint": {
                "outlines": [o.name for o in bp.active_outlines],
                "plots": [p.name for p in bp.active_plots],
                "gags": bp.active_gags,
            },
            "beat_details": [
                {"index": br.index, "words": br.word_count,
                 "template": br.template_used, "humor": br.humor_applied}
                for br in beat_results
            ],
        }
