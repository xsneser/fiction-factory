"""
大纲生成引擎（Outline Generator）
5 阶段 LLM 管线：故事分析 → 大纲时间线规划 → 桥段编排 → 加料注入 → 一致性验证

输入: 流派/子流派/自定义描述 + 四大库（候选池） + 笔名档案
输出: BookTimeline JSON（多大纲+桥段+笑点+内涵+吸睛）

用法:
    gen = OutlineGenerator(llm, structure_lib, plot_lib, gag_lib, theme_lib)
    for event in gen.generate(genre="玄幻", sub_genre="重生", ...):
        # event = ("phase"|"progress"|"done"|"error", message, data_dict)
        yield sse_event(event)
"""
from dataclasses import dataclass, field
from typing import Optional, Callable
import json, re, time

from .timeline import (
    BookTimeline, OutlineSlot, PlotSlot, TimelineBuilder,
    save_timeline, load_timeline,
)
from .structure import StructureLibrary, StructureTemplate
from .plot import PlotLibrary
from .gag import GagLibrary
from .theme import ThemeLibrary


# ═══════════════════════════════════════════
# 生成器
# ═══════════════════════════════════════════

class OutlineGenerator:
    """
    大纲生成引擎 — 从用户想法到 BookTimeline JSON 的完整 LLM 管线。

    5 个阶段，每个阶段 yiled SSE 事件，UI 实时显示进度。
    """

    def __init__(
        self,
        llm_client=None,
        structure_lib: Optional[StructureLibrary] = None,
        plot_lib: Optional[PlotLibrary] = None,
        gag_lib: Optional[GagLibrary] = None,
        theme_lib: Optional[ThemeLibrary] = None,
        profile: Optional[dict] = None,
    ):
        self.llm = llm_client
        self.structures = structure_lib
        self.plots = plot_lib
        self.gags = gag_lib
        self.themes = theme_lib
        self.profile = profile

        # 用于生成唯一 ID
        self._id_counter = 0

    def _next_id(self, prefix: str) -> str:
        self._id_counter += 1
        return f"{prefix}_{self._id_counter:04d}"

    # ═══════════════════════════════════════
    # 公开入口
    # ═══════════════════════════════════════

    def generate(
        self,
        genre: str = "玄幻",
        sub_genre: str = "",
        custom_context: str = "",
        pen_name: str = "",
        words_per_chapter: int = 3000,
        max_outlines: int = 5,
    ):
        """
        生成器：逐步构建 BookTimeline，yield SSE 事件。

        事件格式: (event_type: str, message: str, data: dict)
        """
        tl = BookTimeline(
            genre=genre, sub_genre=sub_genre,
            words_per_chapter=words_per_chapter, pen_name=pen_name,
        )

        total_phases = 5
        try:
            # ── Phase 1: 故事分析 ──
            yield ("phase", "故事分析", {"phase": 1, "total": total_phases,
                   "desc": "分析世界观、主角设定、故事基调..."})
            yield ("progress", "分析故事要素...", {})

            basic_info = self._analyze_story(genre, sub_genre, custom_context, pen_name)
            if basic_info:
                tl.basic_info = basic_info
            yield ("phase_done", "故事分析完成", {
                "phase": 1, "data": {"protagonist": basic_info.get("protagonist", {})}
            })

            # ── Phase 2: 大纲时间线规划 ──
            yield ("phase", "大纲时间线规划", {"phase": 2, "total": total_phases,
                   "desc": f"从大纲库选择 {max_outlines} 个模板，排布时间线..."})
            yield ("progress", "分析大纲库候选...", {})

            outlines = self._plan_timeline(
                genre, sub_genre, custom_context, tl, max_outlines)

            tl.outlines = outlines
            yield ("phase_done", f"大纲时间线规划完成 — {len(outlines)} 条大纲", {
                "phase": 2,
                "data": {
                    "count": len(outlines),
                    "names": [o.name for o in outlines],
                    "chapters": f"1–{max(o.end_chapter for o in outlines) if outlines else 0}",
                }
            })

            # ── Phase 3: 桥段编排 ──
            total_plots_estimate = sum(len(o.stages) for o in outlines) * 2
            yield ("phase", "桥段编排", {"phase": 3, "total": total_phases,
                   "desc": f"为每阶段匹配桥段（预计 ~{total_plots_estimate} 个）..."})

            all_plots = []
            for oi, outline in enumerate(outlines):
                yield ("progress",
                       f"编排桥段: {outline.name} ({oi+1}/{len(outlines)})", {})
                plots = self._arrange_plots_for_outline(outline, tl, genre)
                all_plots.extend(plots)
                yield ("outline_plots", outline.name, {
                    "outline_id": outline.id, "plot_count": len(plots),
                    "plot_names": [p.name for p in plots],
                })

            tl.plots = all_plots
            yield ("phase_done", f"桥段编排完成 — {len(all_plots)} 个桥段", {
                "phase": 3, "data": {"count": len(all_plots)},
            })

            # ── Phase 4: 加料注入 ──
            yield ("phase", "加料注入", {"phase": 4, "total": total_phases,
                   "desc": "匹配笑点模式、分配内涵提示、标注吸睛点..."})

            tl.themes = self._select_book_themes(genre)
            yield ("progress", f"全书母题: {'、'.join(tl.themes[:3])}", {})

            for pi, plot in enumerate(tl.plots):
                self._inject_gags_and_themes(plot, tl, genre)
                if pi % 5 == 0:
                    yield ("progress",
                           f"注入加料: {pi+1}/{len(tl.plots)}", {})

            yield ("phase_done", "加料注入完成", {
                "phase": 4,
                "data": {
                    "themes": tl.themes,
                    "gags_total": sum(len(p.gag_ids) for p in tl.plots),
                }
            })

            # ── Phase 5: 一致性验证 ──
            yield ("phase", "一致性验证", {"phase": 5, "total": total_phases,
                   "desc": "验证时间线合理性、桥段覆盖、笑点密度..."})
            yield ("progress", "检查大纲时间线...", {})

            issues = self._validate(tl)
            if issues:
                yield ("warnings", f"发现 {len(issues)} 个建议", {
                    "issues": issues,
                })

            yield ("phase_done", "验证完成", {"phase": 5, "data": {"issues": len(issues)}})

            # ── 完成 ──
            tl.phase = "ready"
            tl.generated_at = time.strftime("%Y-%m-%d %H:%M:%S")

            yield ("done", "大纲生成完成", {
                "timeline": tl.to_dict(),
                "stats": {
                    "outlines": len(tl.outlines),
                    "plots": len(tl.plots),
                    "total_chapters": max(o.end_chapter for o in tl.outlines) if tl.outlines else 0,
                    "gags_injected": sum(len(p.gag_ids) for p in tl.plots),
                    "themes": len(tl.themes),
                    "issues": len(issues),
                }
            })

        except Exception as e:
            import traceback
            yield ("error", str(e), {"traceback": traceback.format_exc()})

    # ═══════════════════════════════════════
    # Phase 1: 故事分析
    # ═══════════════════════════════════════

    def _analyze_story(
        self, genre: str, sub_genre: str,
        custom_context: str, pen_name: str,
    ) -> dict:
        """分析故事要素 → 主角/世界观/基调/目标读者"""
        if not self.llm:
            return self._default_basic_info(genre)
        if genre == "custom" and not custom_context:
            return self._default_basic_info(genre)

        style_hint = ""
        if self.profile:
            fp = self.profile.get("style_fingerprint", {}) if isinstance(self.profile, dict) else {}
            sname = self.profile.get("pen_name", pen_name) if isinstance(self.profile, dict) else pen_name
            style_hint = (
                f"笔名「{sname}」风格偏好：句子长度={fp.get('sentence_length','中')}，"
                f"幽默风格={fp.get('humor_style','无')}"
            )

        prompt = f"""你是一位资深网文策划编辑。请为以下小说构思基础设定。

【基本信息】
流派：{genre}{'/'+sub_genre if sub_genre else ''}
每章目标：3000字
{style_hint}

【用户想法】
{custom_context or '按该流派标准开局'}

【要求】
1. 主角设定：名字（2-3字中文）、身份（穿越前/重生前是什么人）、性格特征、背景故事、金手指
2. 世界观：时代背景、力量体系、主要势力派系（2-4个）、世界规则
3. 故事基调：轻松/沉重/热血/幽默中选择
4. 目标读者：男频/女频
5. 配角建议：2-3个关键配角（名字+身份+与主角关系）

返回 JSON：
{{
  "protagonist": {{"name": "", "identity": "", "personality": "", "background": "", "golden_finger": ""}},
  "world_building": {{"era": "", "power_system": "", "factions": [], "rules": []}},
  "supporting_cast": [{{"name":"","role":"","relation":""}}],
  "tone": "",
  "target_audience": ""
}}"""

        try:
            from core.llm_client import extract_json
            raw = self.llm.call(
                "你是一位资深网文策划编辑。请严格以JSON格式返回，不要加任何额外文字。",
                prompt, temperature=0.7, max_tokens=2048)
            data = json.loads(extract_json(raw))
            return data
        except Exception:
            return self._default_basic_info(genre)

    def _default_basic_info(self, genre: str) -> dict:
        return {
            "protagonist": {"name": "", "identity": "", "personality": "",
                            "background": "", "golden_finger": ""},
            "world_building": {"era": "异世界", "power_system": "等级制",
                               "factions": [], "rules": []},
            "supporting_cast": [],
            "tone": "轻松爽文",
            "target_audience": "男频",
        }

    # ═══════════════════════════════════════
    # Phase 2: 大纲时间线规划
    # ═══════════════════════════════════════

    def _plan_timeline(
        self, genre: str, sub_genre: str,
        custom_context: str, tl: BookTimeline,
        max_outlines: int = 5,
    ) -> list[OutlineSlot]:
        """从大纲库选模板 → AI 排布时间线 → 展开阶段"""
        if not self.structures:
            return []

        # 获取候选模板
        candidates = self.structures.search(genre=genre, sub_genre=sub_genre)
        if not candidates:
            candidates = self.structures.templates[:5]
        candidates = candidates[:10]  # 最多给 AI 10 个候选

        if not self.llm or len(candidates) <= 1:
            # 规则模式：直接取前几个顺序排布
            return self._rule_sequence(candidates, max_outlines)

        # AI 模式：让 AI 选择合适的模板并排时间线
        return self._ai_sequence(candidates, genre, sub_genre, custom_context, tl, max_outlines)

    def _rule_sequence(
        self, candidates: list, max_outlines: int,
    ) -> list[OutlineSlot]:
        """规则模式：顺序选取大纲模板"""
        outlines = []
        ch = 1
        for i, tmpl in enumerate(candidates[:max_outlines]):
            oid = self._next_id("outline")
            outlines.append(OutlineSlot(
                id=oid, template_id=tmpl.id,
                name=f"{tmpl.name}{f'(第{i+1}部分)' if len(candidates) > 1 else ''}",
                start_chapter=ch,
                end_chapter=ch + min(tmpl.total_chapters, 50) - 1,
                stages=[
                    {"name": s.name, "min_ch": s.min_chapters, "max_ch": s.max_chapters,
                     "events": s.key_events[:5]}
                    for s in tmpl.stages
                ],
                predecessor=outlines[-1].id if outlines else "",
                transition_type="sequential",
            ))
            if outlines and len(outlines) >= 2:
                outlines[-1].predecessor = outlines[-2].id
                outlines[-2].successor = outlines[-1].id
            ch = outlines[-1].end_chapter + 1
        return outlines

    def _ai_sequence(
        self, candidates: list, genre: str, sub_genre: str,
        custom_context: str, tl: BookTimeline, max_outlines: int,
    ) -> list[OutlineSlot]:
        """AI 辅助排布大纲时间线"""

        # 构建候选模板描述
        cand_text = "\n".join(
            f"- {t.id}: {t.name}（{t.total_chapters}章）"
            f" | 阶段: {' → '.join(s.name for s in t.stages[:5])}"
            for t in candidates
        )

        # 前文已有分析结果
        protag = tl.basic_info.get("protagonist", {})
        world = tl.basic_info.get("world_building", {})

        prompt = f"""为一本{genre}/{sub_genre}网络小说设计大纲时间线。

【主角设定】
名字：{protag.get('name','待定')}
身份：{protag.get('identity','')}
金手指：{protag.get('golden_finger','')}
性格：{protag.get('personality','')}

【世界观】
时代：{world.get('era','')}
力量体系：{world.get('power_system','')}

【用户想法】
{custom_context or '标准开局'}

【候选大纲模板（请从中选择 2-{max_outlines} 个）】
{cand_text}

要求：
1. 从候选模板中选择最适合的 2-{max_outlines} 个，按时间线串联
2. 大纲之间可以重叠 2-5 章（transition_type="overlap"），过渡更自然
3. 为每条大纲定义过渡类型：sequential（顺序接续）、overlap（重叠过渡）、merge（融合）
4. 排版应体现"开局爽 → 中段稳 → 高潮燃"的节奏

返回 JSON：
{{
  "outlines": [
    {{
      "template_id": "{candidates[0].id if candidates else ''}",
      "name": "给这段起个名字（如'落魄重生·开局篇'）",
      "start_chapter": 1,
      "end_chapter": 15,
      "transition_type": "sequential|overlap|merge",
      "reason": "为什么选这个模板、放在这个位置"
    }}
  ]
}}"""

        try:
            from core.llm_client import extract_json
            raw = self.llm.call(
                "你是专业网文策划编辑。只返回JSON，不要加额外文字。",
                prompt, temperature=0.7, max_tokens=2048)
            data = json.loads(extract_json(raw))
            outlines_data = data.get("outlines", [])
        except Exception:
            return self._rule_sequence(candidates, max_outlines)

        outlines = []
        prev_id = ""
        for od in outlines_data:
            oid = self._next_id("outline")
            tid = od.get("template_id", "")
            # 从模板库展开阶段
            stages = []
            tmpl = next((t for t in candidates if t.id == tid), None)
            if tmpl:
                stages = [
                    {"name": s.name, "min_ch": s.min_chapters, "max_ch": s.max_chapters,
                     "events": s.key_events[:5]}
                    for s in tmpl.stages
                ]

            start = od.get("start_chapter", outlines[-1].end_chapter - 2 if outlines else 1)
            end = od.get("end_chapter", start + (tmpl.total_chapters if tmpl else 30) - 1)

            # 智能调整重叠
            if outlines and od.get("transition_type") == "overlap":
                start = max(1, outlines[-1].end_chapter - 3)

            outline = OutlineSlot(
                id=oid, template_id=tid,
                name=od.get("name", f"大纲{len(outlines)+1}"),
                start_chapter=start, end_chapter=max(start + 5, end),
                stages=stages,
                predecessor=prev_id,
                transition_type=od.get("transition_type", "sequential"),
            )
            if outlines:
                outlines[-1].successor = oid
                # 填充 overlaps_with
                if start <= outlines[-1].end_chapter:
                    outline.overlaps_with.append(outlines[-1].id)
            outlines.append(outline)
            prev_id = oid

        return outlines if outlines else self._rule_sequence(candidates, max_outlines)

    # ═══════════════════════════════════════
    # Phase 3: 桥段编排
    # ═══════════════════════════════════════

    def _arrange_plots_for_outline(
        self, outline: OutlineSlot, tl: BookTimeline, genre: str,
    ) -> list[PlotSlot]:
        """为一个大纲的每个阶段匹配桥段（AI 选择）"""
        if not self.plots:
            return []

        new_plots = []
        for si, stage in enumerate(outline.stages):
            stage_name = stage.get("name", "")
            events = stage.get("events", [])
            context = f"{outline.name} {stage_name} {' '.join(events)}"

            # 从桥段库搜索候选
            candidates = self.plots.match_for_chapter(context, genre)
            if not candidates:
                candidates = self.plots.search(category=genre)
            if not candidates:
                candidates = self.plots.templates[:3]

            selected = candidates[:min(3, len(candidates))]  # 每阶段 1-3 个桥段

            if self.llm and len(candidates) > 3:
                selected = self._ai_select_plots(
                    outline, stage, candidates, genre)

            # 链式嵌套
            parent_id = ""
            for pi, tmpl in enumerate(selected):
                pid = self._next_id("plot")
                p = PlotSlot(
                    id=pid, template_id=tmpl.id, name=tmpl.name,
                    category=tmpl.category, sub_category=tmpl.sub_category or "",
                    outline_id=outline.id, stage_index=si,
                    parent_plot_id=parent_id,
                    order=pi,
                    cover_beats=tmpl.word_range[1] // 400 if (hasattr(tmpl, 'word_range') and tmpl.word_range) else 4,
                    template_structure=tmpl.template_structure or "",
                    slots=[{"name": s.name, "default": s.default, "options": s.options}
                           for s in tmpl.slots],
                )
                new_plots.append(p)
                if parent_id:
                    for existing in (tl.plots + new_plots):
                        if existing.id == parent_id:
                            existing.children_plot_ids.append(pid)
                            break
                parent_id = pid

        outline.expanded = True
        return new_plots

    def _ai_select_plots(
        self, outline: OutlineSlot, stage: dict,
        candidates: list, genre: str,
    ) -> list:
        """AI 从桥段候选中选择最合适的"""
        stage_name = stage.get("name", "")
        events = stage.get("events", [])

        cand_text = "\n".join(
            f"- {t.id}: {t.name}（{t.category}/{t.sub_category}）"
            f" | 结构: {t.template_structure[:60] if t.template_structure else '无'}"
            for t in candidates[:8]
        )

        prompt = f"""在大纲「{outline.name}」的「{stage_name}」阶段选择合适的桥段。

【阶段事件】
{'、'.join(events) if events else '按流派惯例推进'}

【流派】{genre}

【候选桥段】
{cand_text}

【要求】
从候选中选择 1-3 个最匹配该阶段的桥段。需考虑：
- 桥段类型是否契合阶段节奏（开篇用钩子类、成长用战斗/拜师类）
- 桥段之间能否形成递进关系
- 避免类型重复

返回 JSON：
{{"plot_ids": ["id1", "id2"], "reason": "简要说明选择原因"}}"""

        try:
            from core.llm_client import extract_json
            raw = self.llm.call(
                "你是网文编辑。只返回JSON。", prompt, temperature=0.5, max_tokens=1024)
            data = json.loads(extract_json(raw))
            ids = data.get("plot_ids", [])
            selected = [t for t in candidates if t.id in ids]
            return selected if selected else candidates[:2]
        except Exception:
            return candidates[:2]

    # ═══════════════════════════════════════
    # Phase 4: 加料注入
    # ═══════════════════════════════════════

    def _select_book_themes(self, genre: str) -> list[str]:
        """选定全书母题"""
        default_themes = ["成长蜕变", "命运抗争"]
        if self.themes and hasattr(self.themes, 'search'):
            # ThemeLibrary.search 用 name 参数
            entries = self.themes.search(name=genre)
            if entries:
                return [e.name for e in entries[:2]]
        return default_themes

    def _inject_gags_and_themes(
        self, plot: PlotSlot, tl: BookTimeline, genre: str,
    ):
        """为一个桥段注入笑点和内涵"""
        # 笑点匹配
        if self.gags:
            candidates = self.gags.search(scene=plot.category)
            if candidates:
                plot.gag_ids = [g.id for g in candidates[:2]]
            else:
                # 随机取 1-2 个不同分类的笑点
                all_gags = self.gags.patterns if hasattr(self.gags, 'patterns') else []
                if all_gags:
                    import random
                    picked = random.sample(all_gags, min(2, len(all_gags)))
                    plot.gag_ids = [g.id for g in picked]

        # 内涵匹配
        if tl.themes:
            plot.theme_hints = tl.themes[:2]

        # 吸睛点
        hook_candidates = []
        for slot in plot.slots[:3]:
            sname = slot.get("name", "") if isinstance(slot, dict) else getattr(slot, 'name', '')
            opts = (slot.get("options", []) if isinstance(slot, dict)
                    else getattr(slot, 'options', []))
            if sname and opts:
                hook_candidates.append(f"{plot.name}「{sname}」的{opts[0]}")

        plot.hook_points = hook_candidates[:2] if hook_candidates else [
            f"{plot.name}的开场",
            f"{plot.name}的高潮反转"
        ]

    # ═══════════════════════════════════════
    # Phase 5: 一致性验证
    # ═══════════════════════════════════════

    def _validate(self, tl: BookTimeline) -> list[str]:
        """验证 BookTimeline 的合理性和完整性"""
        issues = []

        # 1. 章节连续性
        for i, o in enumerate(tl.outlines):
            if o.end_chapter < o.start_chapter:
                issues.append(f"大纲「{o.name}」结束章节({o.end_chapter})小于起始({o.start_chapter})")

        # 2. 重叠区合理性
        for i in range(1, len(tl.outlines)):
            prev = tl.outlines[i-1]
            curr = tl.outlines[i]
            gap = curr.start_chapter - prev.end_chapter
            if gap > 5:
                issues.append(
                    f"大纲「{prev.name}」结束于第{prev.end_chapter}章，"
                    f"「{curr.name}」开始于第{curr.start_chapter}章，间隔{gap}章过大")

        # 3. 桥段覆盖率
        for o in tl.outlines:
            o_plots = [p for p in tl.plots if p.outline_id == o.id]
            stage_count = len(o.stages)
            if stage_count > 0 and len(o_plots) < stage_count:
                issues.append(f"大纲「{o.name}」有{stage_count}个阶段但只有{len(o_plots)}个桥段，建议补全")

        # 4. 笑点密度
        total_gags = sum(len(p.gag_ids) for p in tl.plots)
        total_plots = len(tl.plots)
        if total_plots > 0 and total_gags / total_plots < 0.5:
            issues.append(f"笑点覆盖率偏低（{total_gags}/{total_plots}），建议增加笑点注入")

        # 5. 总章节合理性
        if tl.outlines:
            max_ch = max(o.end_chapter for o in tl.outlines)
            if max_ch < 10:
                issues.append(f"全书仅{max_ch}章，建议扩展大纲覆盖范围")
            elif max_ch > 500:
                issues.append(f"全书{max_ch}章，建议拆分或精简")

        return issues


# ═══════════════════════════════════════════
# 便捷入口
# ═══════════════════════════════════════════

def quick_generate(
    llm_client,
    genre: str = "玄幻",
    sub_genre: str = "",
    custom_context: str = "",
    pen_name: str = "",
    words_per_chapter: int = 3000,
    structure_lib=None, plot_lib=None, gag_lib=None, theme_lib=None,
) -> dict:
    """
    同步版本：生成并返回完整 BookTimeline（测试/脚本用）。
    注意：会阻塞直到全部生成完成。
    """
    gen = OutlineGenerator(
        llm_client, structure_lib, plot_lib, gag_lib, theme_lib)
    result = None
    for event_type, message, data in gen.generate(
        genre=genre, sub_genre=sub_genre, custom_context=custom_context,
        pen_name=pen_name, words_per_chapter=words_per_chapter,
    ):
        if event_type == "done":
            result = data
        elif event_type == "error":
            raise RuntimeError(f"生成失败: {message}")
    return result
