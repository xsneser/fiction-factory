"""
大纲生成引擎（Outline Generator）
6 阶段 LLM 管线：故事分析 → 故事线规划 → 桥段编排 → 线程与呼应 → 内涵挂载 → 一致性验证

输入: 题材方向/题材细分/自定义描述 + 四大库（候选池） + 笔名档案
输出: BookStoryline JSON（多大纲+桥段+内涵+吸睛；笑点完全涌现、不写入大纲）

用法:
    gen = OutlineGenerator(llm, structure_lib, plot_lib, gag_lib)
    for event in gen.generate(...):
        # event = ("phase"|"progress"|"done"|"error", message, data_dict)
        yield sse_event(event)
"""
from typing import Callable, Optional
import json, time

from .storyline import (
    BookStoryline, OutlineSlot, PlotSlot, merge_basic_info, annotate_plot_roles,
    structure_to_stages, mount_themes_and_hooks,
    get_mc, get_characters, normalize_basic_info,
)
from .structure import StructureLibrary
from .plot import PlotLibrary
from .gag import GagLibrary
from libraries.world_tags import genre_from_tags


def basic_info_is_rich(basic_info: dict) -> bool:
    """基础设定是否已由世界观生成器充实（可跳过 Phase 1 LLM 分析）。

    条件：显式打了 _world_generated 标记；或世界观填充维度 ≥4 且主角名非空（双保险）。
    """
    bi = basic_info or {}
    if bi.get("_world_generated"):
        return True
    wb = bi.get("world_building") or {}
    if not isinstance(wb, dict):
        wb = {}
    keys = ["era", "power_system", "factions", "rules", "geography", "culture",
            "history", "social_structure", "core_conflict"]
    filled = 0
    for k in keys:
        v = wb.get(k)
        if (isinstance(v, list) and v) or str(v or "").strip():
            filled += 1
    protag_name = str(get_mc(bi).get("name", "") or "").strip()
    return filled >= 4 and bool(protag_name)


def normalize_plot_picks(picks):
    """把外部预选桥段归一化为「扁平优先序列表」。

    兼容两种形态：
      - 推荐：扁平列表 [plot_id, ...]（与 outline_material_candidates 返回的 plots 形状一致）。
        语义 = 全书出现优先级；每个阶段消费队首第一个命中候选池的未消费预选作锚点，
        其余槽位规则回填，随后阶段继续按序消费；用尽即回退 AI/规则。
      - 兼容（旧契约，已弃用）：{"<outline_id>": ["plot_id", ...]}。
        outline_id 在选材阶段尚不存在（大纲在 generate 内生成），无法按弧映射，
        故按 dict 值序展开成扁平列表处理。
    去重后返回扁平 id 列表；无预选返回 []。
    """
    plots = (picks or {}).get("plots") if isinstance(picks, dict) else []
    if not plots:
        return []
    if isinstance(plots, dict):
        flat = [x for v in plots.values() for x in (v or [])]
    elif isinstance(plots, (list, tuple)):
        flat = list(plots)
    else:
        return []
    seen, out = set(), []
    for x in flat:
        if isinstance(x, str) and x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ═══════════════════════════════════════════
# 生成器
# ═══════════════════════════════════════════

class OutlineGenerator:
    """
    大纲生成引擎 — 从用户想法到 BookStoryline JSON 的完整 LLM 管线。

    6 个阶段，每个阶段 yield SSE 事件，UI 实时显示进度。
    """

    def __init__(
        self,
        llm_client=None,
        structure_lib: Optional[StructureLibrary] = None,
        plot_lib: Optional[PlotLibrary] = None,
        gag_lib: Optional[GagLibrary] = None,
        profile: Optional[dict] = None,
        harness=None,
    ):
        self.llm = llm_client
        self.structures = structure_lib
        self.plots = plot_lib
        self.gags = gag_lib
        self.profile = profile
        self.harness = harness   # PromptHarness：为各 phase 前置书级设定卡

        # 用于生成唯一 ID
        self._id_counter = 0

    def _next_id(self, prefix: str) -> str:
        self._id_counter += 1
        return f"{prefix}_{self._id_counter:04d}"

    def _stream_decision_content(self, kind: str, system: str, prompt: str,
                                 temperature: float = 0.7, max_tokens: int = 8192):
        """流式调用 LLM 并转发思考事件，返回 content 全文。

        生成器：对每个 delta 块 yield ("thinking", kind, {"stream": text, "mode": delta_key})，
        其中 reasoning = 模型内部思考（DeepSeek reasoning_content），content = 最终输出；
        只把 content 收集起来作为返回值，供调用方解析 JSON。
        """
        collected = []
        if self.llm:
            for delta_key, text in self.llm.stream_deltas(
                    system, prompt, temperature=temperature, max_tokens=max_tokens):
                yield ("thinking", kind, {"stream": text, "mode": delta_key})
                if delta_key == "content":
                    collected.append(text)
        return "".join(collected)

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
        storyline: Optional[BookStoryline] = None,
        on_save: Optional[Callable[[BookStoryline], None]] = None,
        skip_analyze: bool = False,
        agent_picks: Optional[dict] = None,
    ):
        """
        生成器：逐步构建 BookStoryline，yield SSE 事件。

        事件格式: (event_type: str, message: str, data: dict)
        新增事件：
          ("thinking", kind, {"stream": str})  — LLM 流式思考片段
                  kind ∈ analyze/outline_choice/plot_choice/theme_review/validate
          ("decision", kind, {...})            — 决策完成（候选→选中→理由），
                  让用户看到"确定了哪个大纲/桥段/内涵"及 AI 的理由

        storyline: 传入现有 BookStoryline 则原地累加（供逐步落盘）；None 则新建。
        on_save:  每阶段完成后回调 on_save(tl)，用于把大纲/桥段/内涵"挨个步骤写进配置文件"。
        agent_picks: 决策点预选（可选）。{"templates": [structure_id, ...],
            "plots": [plot_id, ...]}——plots 为扁平优先序列表（normalize_plot_picks 归一化），
            语义=全书出现优先级，跨 outline/跨阶段按序消费；兼容旧 dict 形态（按值序展开，已弃用）。
        """
        tl = storyline if storyline is not None else BookStoryline(
            words_per_chapter=words_per_chapter, pen_name=pen_name,
        )
        if self.harness:
            self.harness.storyline = tl

        # 决策点 B 状态：外部预选桥段（扁平优先序）→ 跨 outline/跨阶段按序消费
        plot_queue = normalize_plot_picks(agent_picks)
        picks_state = {"queue": plot_queue, "consumed": set()} if plot_queue else None

        total_phases = 6
        issues = []
        storyline_warnings = []
        try:
            # ── Phase 1: 故事分析 ──
            yield ("phase", "故事分析", {"phase": 1, "total": total_phases,
                   "desc": "分析世界观、主角设定、故事基调..."})
            yield ("progress", "分析故事要素...", {})

            # 世界观已由启动前置的设定生成器产出时跳过 LLM 分析，直接复用（不二次覆盖）
            skip = skip_analyze or self._basic_info_is_rich(tl.basic_info)
            if skip:
                yield ("progress", "复用已生成的世界观/主角设定，跳过 LLM 故事分析...", {})
            else:
                _wb_tags = ((tl.basic_info or {}).get("world_building") or {}).get("tags") or []
                basic_info = self._analyze_story(genre, sub_genre, custom_context, pen_name,
                                                 tags=_wb_tags)
                if basic_info:
                    # 原地累加：保留用户已填的基础设定（主角/世界观等非空字段不覆盖）
                    tl.basic_info = merge_basic_info(tl.basic_info, basic_info)
            # 故事线规则校验（重生/年龄/年份自洽）—— 跳过分析时也必须执行
            storyline_warnings.extend(self._validate_storyline_math(tl.basic_info))
            if storyline_warnings:
                yield ("warnings", f"故事线校验发现 {len(storyline_warnings)} 个问题", {
                    "issues": list(storyline_warnings), "phase": 1,
                })
            yield ("phase_done", "故事分析完成", {
                "phase": 1, "data": {"protagonist": get_mc(tl.basic_info)}
            })
            if on_save:
                on_save(tl)

            # ── Phase 2: 故事线规划 ──
            yield ("phase", "故事线规划", {"phase": 2, "total": total_phases,
                   "desc": f"从情节弧库选择 {max_outlines} 个模板，排布故事线..."})
            yield ("progress", "分析情节弧库候选...", {})

            tl.outlines = []  # 原地累加：每条大纲确定后立即写入，供实时刷新
            outlines = yield from self._plan_storyline(
                genre, sub_genre, custom_context, tl, max_outlines,
                agent_picks=agent_picks)
            if not tl.outlines and outlines:
                tl.outlines = outlines

            yield ("phase_done", f"故事线规划完成 — {len(outlines)} 条大纲", {
                "phase": 2,
                "data": {
                    "count": len(outlines),
                    "names": [o.name for o in outlines],
                    "chapters": f"1–{max(o.end_chapter for o in outlines) if outlines else 0}",
                }
            })
            if on_save:
                on_save(tl)

            # ── Phase 3: 桥段编排 ──
            total_plots_estimate = sum(len(o.stages) for o in outlines) * 2
            yield ("phase", "桥段编排", {"phase": 3, "total": total_phases,
                   "desc": f"为每阶段匹配桥段（预计 ~{total_plots_estimate} 个）..."})

            tl.plots = []   # 全新排布：桥段在 _arrange_plots_for_outline 内逐个写入
            all_plots = []
            for oi, outline in enumerate(outlines):
                yield ("progress",
                       f"编排桥段: {outline.name} ({oi+1}/{len(outlines)})", {})
                plots = yield from self._arrange_plots_for_outline(
                    outline, tl, genre, picks_state=picks_state)
                all_plots = tl.plots  # 桥段已逐个落库，供实时刷新
                yield ("outline_plots", outline.name, {
                    "outline_id": outline.id, "plot_count": len(plots),
                    "plot_names": [p.name for p in plots],
                })

            yield ("phase_done", f"桥段编排完成 — {len(all_plots)} 个桥段", {
                "phase": 3, "data": {"count": len(all_plots)},
            })
            if on_save:
                on_save(tl)

            # ── Phase 4: 线程与呼应（线程穿插 + 桥段拆分设局→收局）──
            yield ("phase", "线程与呼应", {"phase": 4, "total": total_phases,
                   "desc": "规划叙事线程（主线/副线/伏笔线）与设局→收局呼应..."})
            yield ("progress", "分析桥段线程归属与设局收局...", {})
            yield from self._plan_threads_and_splits(tl, genre)
            # 出场人物规则标注（主角恒在 + 配角按名匹配；含新增的收局槽位）
            annotate_plot_roles(tl)
            yield ("phase_done", "线程与呼应规划完成", {
                "phase": 4,
                "data": {
                    "threads": [t.get("id", "") for t in tl.threads],
                    "splits": sum(1 for p in tl.plots if p.resolves_plot_id),
                }
            })
            if on_save:
                on_save(tl)

            # ── Phase 5: 内涵挂载（笑点完全涌现，不在此分配）──
            yield ("phase", "内涵挂载", {"phase": 5, "total": total_phases,
                   "desc": "把内涵挂到能承载它的桥段、标注吸睛点（笑点在写作时涌现）..."})

            tl.themes = self._select_book_themes(genre, tl)
            yield ("progress", f"全书内涵: {'、'.join(tl.themes[:3])}", {})

            for pi, plot in enumerate(tl.plots):
                self._inject_themes_and_hooks(plot, tl)
                # 每个桥段挂载完成后即推送，前端据此实时刷新内涵通道
                yield ("theme_injected", f"挂载 {plot.name}", {
                    "plot_id": plot.id,
                    "theme_hints": plot.theme_hints,
                })

            # Phase 4.5: LLM 复查内涵挂载（流式思考）
            yield ("progress", "LLM 复查内涵挂载...", {})
            yield from self._review_theme_assignments(tl, genre)

            yield ("phase_done", "内涵挂载完成", {
                "phase": 5,
                "data": {
                    "themes": tl.themes,
                    "themes_total": sum(len(p.theme_hints) for p in tl.plots),
                }
            })
            if on_save:
                on_save(tl)

            # ── Phase 6: 一致性验证 ──
            yield ("phase", "一致性验证", {"phase": 6, "total": total_phases,
                   "desc": "验证故事线合理性、桥段覆盖、内涵挂载..."})
            yield ("progress", "检查故事线...", {})

            issues = self._validate(tl) + storyline_warnings
            yield from self._validate_with_llm(tl)
            if issues:
                yield ("warnings", f"发现 {len(issues)} 个建议", {
                    "issues": issues,
                })

            yield ("phase_done", "验证完成", {"phase": 6, "data": {"issues": len(issues)}})
            if on_save:
                on_save(tl)

            # ── 完成 ──
            tl.phase = "ready"
            tl.generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
            if on_save:
                # 关键：把 phase=ready 持久化。此前 ready 只在内存置位后直接 yield done，
                # MCP 路径（agent_tools.generate_full_outline）消费完从磁盘重读仍是 config，
                # 导致已完成大纲的书永久停在 config → 写作门控拒写、大纲被反复重跑清空。
                on_save(tl)

            yield ("done", "大纲生成完成", {
                "timeline": tl.to_dict(),
                "stats": {
                    "outlines": len(tl.outlines),
                    "plots": len(tl.plots),
                    "total_chapters": max(o.end_chapter for o in tl.outlines) if tl.outlines else 0,
                    "themes_total": sum(len(p.theme_hints) for p in tl.plots),
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
        tags: list = None,
    ) -> dict:
        """分析故事要素 → 主角/世界观/基调/目标读者。tags 为题材标签硬约束。"""
        if not self.llm:
            return self._default_basic_info(genre)
        if genre == "custom" and not custom_context:
            return self._default_basic_info(genre)

        style_hint = ""
        pov_pref = ""
        if self.profile:
            if isinstance(self.profile, dict):
                fp = self.profile.get("style_fingerprint", {}) or {}
                sname = self.profile.get("pen_name", pen_name)
            elif hasattr(self.profile, "style_fingerprint"):
                fp = self.profile.style_fingerprint or {}
                sname = getattr(self.profile, "pen_name", pen_name)
            else:
                fp = {}
                sname = pen_name
            pov_pref = fp.get("pov_preference", "")
            style_hint = (
                f"笔名「{sname}」风格偏好：句子长度={fp.get('sentence_length','中')}，"
                f"幽默风格={fp.get('humor_style','无')}"
                + (f"，视角偏好={pov_pref}" if pov_pref else "")
            )

        tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        tags_block = ("\n【题材标签（硬约束）】" + "、".join(tags)
                      + "。主角/世界观/剧情必须严格契合这些标签的网文套路与读者预期，禁止漂移。"
                      if tags else "")
        prompt = f"""你是一位资深网文策划编辑。请为以下小说构思基础设定。

【基本信息】
每章目标：3000字
{style_hint}
{tags_block}

【用户想法】
{custom_context or '按该题材方向标准开局'}

【要求】
1. 主角设定：名字（2-3字中文）、身份（穿越前/重生前是什么人）、性格特征、背景故事、金手指、性别、当前年龄、死亡年份（若重生设定）
2. 世界观：时代背景（含年份）、力量体系、主要势力派系（2-4个）、世界规则
3. 故事基调：轻松/沉重/热血/幽默中选择
4. 目标读者：男频/女频
5. 写作视角：第一人称/第三人称（默认第三人称，全书统一，禁止漂移）
6. 配角建议：2-3个关键配角（名字+身份+与主角关系+性别+称呼+性格+惯用语句+一句话简介）
7. 时代语言约束：根据世界观时代给出"禁止晚于该时代的网络新词"（如 2008 语境禁"搭子/内卷/PUA"）
8. 世界规则（world_building.rules）必须把系统/金手指的数值与技能语义写死（如"效率×2"具体指什么翻倍），全书口径唯一，禁止每章换一种解释

返回 JSON：
{{
  "protagonist": {{"name": "", "identity": "", "personality": "", "background": "", "golden_finger": "", "gender": "", "age": 0, "death_year": 0}},
  "world_building": {{"era": "", "power_system": "", "factions": [], "rules": []}},
  "supporting_cast": [{{"name":"","role":"","relation":"","gender":"男/女","title":"","personality":"","catchphrase":"","brief":""}}],
  "tone": "",
  "target_audience": "",
  "pov": "第三人称",
  "era_language": ""
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
            "characters": [],
            "world_building": {"era": "异世界", "power_system": "等级制",
                               "factions": [], "rules": []},
            "tone": "轻松爽文",
            "target_audience": "男频",
            "pov": "第三人称",
            "era_language": "",
        }

    def _basic_info_is_rich(self, basic_info: dict) -> bool:
        """薄委托：模块级 basic_info_is_rich（供 web 层 confirm 复用）。"""
        return basic_info_is_rich(basic_info)

    def _validate_storyline_math(self, basic_info: dict) -> list[str]:
        """Phase 1 后规则校验：重生/年龄/年份关系自洽（纯规则，不调 LLM）。"""
        warnings = []
        bi = basic_info or {}
        protag = get_mc(bi)
        world = bi.get("world_building") or {}
        import re as _re
        m = _re.search(r'(19|20)\d{2}', str(world.get("era", "") or ""))
        story_year = int(m.group(0)) if m else 0
        try:
            age = int(protag.get("age", 0) or 0)
        except (TypeError, ValueError):
            age = 0
        try:
            death_year = int(protag.get("death_year", 0) or 0)
        except (TypeError, ValueError):
            death_year = 0

        if death_year and story_year and story_year >= death_year:
            warnings.append(
                f"重生故事线矛盾：主角死亡于 {death_year} 年，故事却设定在 {story_year} 年（重生应回到死亡之前）")
        if story_year and age > 0 and story_year - age < 1900:
            warnings.append(
                f"年龄/年份不自洽：{story_year} 年主角 {age} 岁（出生年 {story_year - age} 过晚）")
        if age <= 0 and protag.get("birth_year"):
            try:
                by = int(protag["birth_year"])
            except (TypeError, ValueError):
                by = 0
            if story_year and by:
                protag["age"] = story_year - by
                warnings.append(f"已按出生年 {by} 补齐主角年龄 {story_year - by}")
        return warnings

    # ═══════════════════════════════════════
    # Phase 2: 故事线规划
    # ═══════════════════════════════════════

    def _plan_storyline(
        self, genre: str, sub_genre: str,
        custom_context: str, tl: BookStoryline,
        max_outlines: int = 5,
        agent_picks: Optional[dict] = None,
    ):
        """从情节弧库选模板 → AI 排布故事线 → 展开阶段。

        生成器：AI 模式下 yield thinking/decision 事件，最终 return list[OutlineSlot]。
        每条大纲确定后立即写入 tl.outlines 并 yield outline_added，供前端实时刷新。
        决策点 A：agent_picks["templates"] 为外部预选模板 id（优先使用，失败回退原逻辑）。
        """
        if not self.structures:
            return []

        # 获取候选模板（题材已换标签：按书的题材标签任一命中）
        _tags = ((tl.basic_info or {}).get("world_building") or {}).get("tags") or []
        candidates = self.structures.search(tags=_tags)
        if not candidates:
            candidates = self.structures.templates[:5]
        candidates = candidates[:10]  # 最多给 AI 10 个候选

        # 决策点 A：外部 agent 预选模板（有有效预选则按其排布，无需 LLM）
        if agent_picks:
            picks = (agent_picks.get("templates") or [])[:max_outlines]
            picked = [t for t in candidates if t.id in picks]
            if picked:
                result = yield from self._sequence_from_picks(picked, max_outlines, tl)
                return result

        if not self.llm or len(candidates) <= 1:
            # 规则模式：直接取前几个顺序排布
            result = yield from self._rule_sequence(candidates, max_outlines, tl)
            return result

        # AI 模式：让 AI 选择合适的模板并排故事线
        result = yield from self._ai_sequence(
            candidates, genre, sub_genre, custom_context, tl, max_outlines)
        return result

    def _rule_sequence(
        self, candidates: list, max_outlines: int, tl: BookStoryline,
    ):
        """规则模式：顺序选取大纲模板（生成器，每条确定后写入 tl 并 yield outline_added）"""
        outlines = []
        ch = 1
        for i, tmpl in enumerate(candidates[:max_outlines]):
            oid = self._next_id("outline")
            outline = OutlineSlot(
                id=oid, template_id=tmpl.id,
                name=f"{tmpl.name}{f'(第{i+1}部分)' if len(candidates) > 1 else ''}",
                start_chapter=ch,
                end_chapter=ch + min(tmpl.total_chapters, 50) - 1,
                stages=structure_to_stages(tmpl),
                predecessor=outlines[-1].id if outlines else "",
                transition_type="sequential",
            )
            if outlines:
                outlines[-1].successor = outline.id
            outlines.append(outline)
            ch = outline.end_chapter + 1
            tl.outlines.append(outline)
            yield ("outline_added", outline.name, {
                "outline_id": outline.id, "name": outline.name,
                "start_chapter": outline.start_chapter, "end_chapter": outline.end_chapter,
            })
        return outlines

    def _sequence_from_picks(
        self, picked: list, max_outlines: int, tl: BookStoryline,
    ):
        """决策点 A：按外部 agent 预选模板排布故事线（生成器）。

        预选不足 max_outlines 时用候选模板补足；yield decision 说明来源为「外部预选」。
        """
        templates = list(picked)
        used = {t.id for t in templates}
        if len(templates) < max_outlines and self.structures:
            for t in self.structures.templates:
                if t.id not in used:
                    templates.append(t)
                    used.add(t.id)
                    if len(templates) >= max_outlines:
                        break
        outlines = []
        ch = 1
        for i, tmpl in enumerate(templates[:max_outlines]):
            oid = self._next_id("outline")
            outline = OutlineSlot(
                id=oid, template_id=tmpl.id,
                name=f"{tmpl.name}{f'(第{i+1}部分)' if len(templates) > 1 else ''}",
                start_chapter=ch,
                end_chapter=ch + min(tmpl.total_chapters, 50) - 1,
                stages=structure_to_stages(tmpl),
                predecessor=outlines[-1].id if outlines else "",
                transition_type="sequential",
            )
            if outlines:
                outlines[-1].successor = outline.id
            outlines.append(outline)
            ch = outline.end_chapter + 1
            tl.outlines.append(outline)
            yield ("outline_added", outline.name, {
                "outline_id": oid, "name": outline.name,
                "start_chapter": outline.start_chapter, "end_chapter": outline.end_chapter,
            })
        yield ("decision", "outline_choice", {
            "step": "故事线规划（外部 agent 预选模板）",
            "candidates": [{"id": t.id, "name": t.name} for t in templates[:max_outlines]],
            "chosen": [{"id": o.template_id, "name": o.name} for o in outlines],
            "reason": "模板由外部 agent 预选，按顺序排布（决策点 A）",
        })
        return outlines

    def _ai_sequence(
        self, candidates: list, genre: str, sub_genre: str,
        custom_context: str, tl: BookStoryline, max_outlines: int,
    ):
        """AI 辅助排布故事线。

        生成器：yield thinking（LLM 流式 token）+ decision（每确定一条大纲），
        return list[OutlineSlot]。
        """

        # 构建候选模板描述
        cand_text = "\n".join(
            f"- {t.id}: {t.name}（{t.total_chapters}章）"
            f" | 阶段: {' → '.join(s.name for s in t.stages[:5])}"
            for t in candidates
        )

        # 前文已有分析结果
        protag = get_mc(tl.basic_info)
        world = tl.basic_info.get("world_building", {})

        bible_block = self.harness.render_outline_context("sequence", tl) if self.harness else ""

        prompt = f"""{bible_block}为一本{genre}/{sub_genre}网络小说设计故事线。

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
1. 从候选模板中选择最适合的 2-{max_outlines} 个，按故事线串联
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

        cand_list = [{"id": c.id, "name": c.name} for c in candidates[:8]]

        # 流式调用：先把候选放上桌，再逐 token 展示 AI 怎么选
        yield ("decision", "outline_choice", {
            "step": "候选大纲模板",
            "candidates": cand_list,
            "chosen": {},
            "reason": "以下模板来自情节弧库，AI 将从其中挑选并排布故事线",
        })

        outlines_data = None
        if self.llm:
            try:
                from core.llm_client import extract_json
                raw = yield from self._stream_decision_content(
                    "outline_choice",
                    "你是专业网文策划编辑。只返回JSON，不要加额外文字。",
                    prompt, temperature=0.7, max_tokens=8192)
                data = json.loads(extract_json(raw))
                outlines_data = data.get("outlines", [])
            except Exception:
                outlines_data = None

        if not outlines_data:
            # 回退规则模式
            fallback = yield from self._rule_sequence(candidates, max_outlines, tl)
            if fallback:
                yield ("decision", "outline_choice", {
                    "step": "故事线规划（回退规则模式）",
                    "candidates": cand_list,
                    "chosen": {"id": fallback[0].template_id, "name": fallback[0].name},
                    "reason": "LLM 流式输出解析失败，改用情节弧库模板顺序排布",
                })
            return fallback

        outlines = []
        prev_id = ""
        for i, od in enumerate(outlines_data):
            oid = self._next_id("outline")
            tid = od.get("template_id", "")
            # 从模板库展开阶段
            stages = []
            tmpl = next((t for t in candidates if t.id == tid), None)
            if tmpl:
                stages = structure_to_stages(tmpl)

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
            # 原地累加：每条大纲确定后立即写入 tl，供前端实时刷新左侧故事线
            tl.outlines.append(outline)
            yield ("outline_added", outline.name, {
                "outline_id": oid, "name": outline.name,
                "start_chapter": outline.start_chapter, "end_chapter": outline.end_chapter,
            })

            yield ("decision", "outline_choice", {
                "step": f"确定第 {i+1} 条大纲",
                "candidates": cand_list,
                "chosen": {"id": oid, "template_id": tid, "name": outline.name},
                "reason": od.get("reason", ""),
            })

        if not outlines:
            outlines = yield from self._rule_sequence(candidates, max_outlines, tl)
        if outlines:
            # 多次思考保证稳定：选材复查 pass（决策点 A 强化），失败静默沿用原选序
            outlines = yield from self._review_outline_sequence(
                candidates, outlines, genre, custom_context, tl)
        return outlines

    def _review_outline_sequence(
        self, candidates: list, outlines: list,
        genre: str, custom_context: str, tl: BookStoryline,
    ):
        """选材复查（多次思考保证稳定）：LLM 复查模板选序是否契合前提/顺序，可换模板修正。

        生成器：yield thinking/decision；无 LLM 或解析失败时静默返回原选序，不打断整链。
        """
        if not self.llm or len(outlines) <= 1:
            return outlines
        view = [{
            "index": i, "template_id": o.template_id, "name": o.name,
            "chapters": f"第{o.start_chapter}-{o.end_chapter}章",
            "transition": o.transition_type,
        } for i, o in enumerate(outlines)]
        cand_text = "\n".join(f"- {t.id}: {t.name}（{t.total_chapters}章）"
                              for t in candidates[:12])
        protag = get_mc(tl.basic_info)
        prompt = f"""你是资深网文策划编辑。复查下面这条故事线的大纲模板选序是否契合主角设定与前提节奏。

【主角】{protag.get('name', '')}（{protag.get('identity', '')}）金手指 {protag.get('golden_finger', '')}
【前提】{custom_context or '标准开局'}
【候选模板】
{cand_text}
【当前选序】
{json.dumps(view, ensure_ascii=False, indent=1)}

要求：若某条模板与前提或前后衔接明显不匹配，返回修正；都合理则返回空。
返回 JSON：
{{"swaps": [{{"index": 0, "template_id": "候选id", "reason": "..."}}], "summary": "一句话结论"}}"""

        try:
            from core.llm_client import extract_json
            raw = yield from self._stream_decision_content(
                "outline_review", "你是网文编辑。只返回JSON。", prompt,
                temperature=0.3, max_tokens=8192)
            data = json.loads(extract_json(raw))
            swaps = data.get("swaps") or []
            applied = 0
            for sw in swaps:
                try:
                    idx = int(sw.get("index", -1))
                except (TypeError, ValueError):
                    continue
                if not (0 <= idx < len(outlines)):
                    continue
                tmpl = next((t for t in candidates if t.id == sw.get("template_id")), None)
                if not tmpl:
                    continue
                o = outlines[idx]
                o.template_id = tmpl.id
                o.name = f"{tmpl.name}(复查修正)"
                o.stages = structure_to_stages(tmpl)
                o.end_chapter = max(
                    o.end_chapter, o.start_chapter + min(tmpl.total_chapters, 50) - 1)
                applied += 1
            yield ("decision", "outline_review", {
                "step": "选材复查（稳定性）",
                "candidates": [{"id": t.id, "name": t.name} for t in candidates[:10]],
                "chosen": {"swaps": applied},
                "reason": data.get("summary", "") or (
                    f"复查修正 {applied} 处" if applied else "复查通过，未调整"),
            })
        except Exception:
            yield ("decision", "outline_review", {
                "step": "选材复查（稳定性）",
                "candidates": [{"id": t.id, "name": t.name} for t in candidates[:10]],
                "chosen": {"swaps": 0},
                "reason": "复查未返回有效结果，沿用原选序",
            })
        return outlines

    # ═══════════════════════════════════════
    # Phase 3: 桥段编排
    # ═══════════════════════════════════════

    def _arrange_plots_for_outline(
        self, outline: OutlineSlot, tl: BookStoryline, genre: str,
        picks_state: Optional[dict] = None,
    ):
        """为一个大纲的每个阶段匹配桥段（AI 选择，避免跨阶段重复与类型错配）。

        生成器：yield thinking/decision/plot_added 事件，最终 return list[PlotSlot]。
        决策点 B：picks_state["queue"] 为外部预选桥段的扁平优先序（normalize_plot_picks
        归一化）；每个阶段从候选池中命中「未消费的最高优先级预选」，消费 1 个作锚点，
        其余槽位规则回填（回填排除全部预选 id，避免把后续阶段的预选提前顺走）；
        无预选命中则回退 AI/规则。
        """
        if not self.plots:
            return []

        queue = (picks_state or {}).get("queue")
        consumed = (picks_state or {}).get("consumed")
        if queue is None:
            queue = []
        if consumed is None:
            consumed = set()
        pri = {pid: i for i, pid in enumerate(queue)}   # 优先序索引（候选池保序≠优先序）

        new_plots = []
        used_ids = set()   # 本大纲内已用桥段模板 id，避免跨阶段重复
        for si, stage in enumerate(outline.stages):
            stage_name = stage.get("name", "")
            events = stage.get("events", [])
            context = f"{outline.name} {stage_name} {' '.join(events)}"

            # 候选池：上下文匹配 + 题材方向匹配 + 阶段事件关键词，去重保序
            candidates = self._collect_plot_candidates(context, genre, stage)
            if not candidates:
                candidates = self.plots.templates[:5]

            # 已用桥段从候选剔除（保证弧内不重复），不足时再回填
            unused = [t for t in candidates if t.id not in used_ids]
            if len(unused) >= 2:
                candidates = unused
            elif unused:
                candidates = unused + [t for t in candidates if t.id in used_ids]

            selected = candidates[:min(3, len(candidates))]  # 默认兜底（防空阶段）
            pre = [t for t in candidates if t.id in pri and t.id not in consumed]
            # 预选只能经「锚点」机制进入：reserved 把本阶段候选池里全部预选（含已消费）
            # 挡在规则/AI 回填之外，防止未消费预选被 filler 顺走造成跨弧重复
            reserved = {t.id for t in candidates if t.id in pri}
            filler_pool = [t for t in candidates if t.id not in reserved]
            if pre:
                # 决策点 B：按全局优先序每阶段消费 1 个锚点 + 非预选规则回填（零额外 LLM 成本）
                pre.sort(key=lambda t: pri[t.id])
                chosen = pre[0]
                consumed.add(chosen.id)
                selected = [chosen] + filler_pool[:2]
                yield ("decision", "plot_choice", {
                    "step": f"「{outline.name}」· 阶段「{stage_name}」桥段（外部预选）",
                    "candidates": [{"id": t.id, "name": t.name}
                                   for t in candidates[:10]],
                    "chosen": [{"id": t.id, "name": t.name} for t in selected],
                    "reason": "外部预选（决策点 B）· 按优先序每阶段消费 1 个，其余规则回填",
                })
            elif self.llm and len(candidates) >= 2 and filler_pool:
                selected = yield from self._ai_select_plots(
                    outline, stage, filler_pool, genre, used_ids)
            elif filler_pool:
                selected = filler_pool[:min(3, len(filler_pool))]

            # 链式嵌套 + 记录已用
            parent_id = ""
            for pi, tmpl in enumerate(selected):
                if tmpl.id in used_ids and pi > 0:
                    continue  # 兜底：本阶段首个可复用，其余去重
                used_ids.add(tmpl.id)
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
                    theme_moments=stage.get("themes", []),
                )
                # 立即写入 tl.plots：左侧故事线随每个桥段逐个刷新
                tl.plots.append(p)
                new_plots.append(p)
                if parent_id:
                    for existing in tl.plots:
                        if existing.id == parent_id:
                            existing.children_plot_ids.append(pid)
                            break
                parent_id = pid
                yield ("plot_added", p.name, {
                    "plot_id": p.id,
                    "outline_id": outline.id,
                    "outline_name": outline.name,
                    "category": p.category,
                })

        outline.expanded = True
        return new_plots

    def _collect_plot_candidates(self, context: str, genre: str, stage: dict) -> list:
        """聚合桥段候选池：上下文匹配 + 题材方向匹配 + 阶段事件关键词，去重保序。"""
        seen = {}

        def add(t):
            if t.id not in seen:
                seen[t.id] = t

        for t in self.plots.match_for_chapter(context, genre):
            add(t)
        if genre:
            for t in self.plots.search(category=genre):
                add(t)
        if context:
            for t in self.plots.search(context=context[:30]):
                add(t)
        for ev in (stage.get("events") or [])[:3]:
            if ev:
                for t in self.plots.search(context=ev[:20]):
                    add(t)
        # 池子太小就补全库，保证 AI 有足够多样选择
        if len(seen) < 6:
            for t in self.plots.templates:
                add(t)
        return list(seen.values())[:12]

    def _ai_select_plots(
        self, outline: OutlineSlot, stage: dict,
        candidates: list, genre: str, used_ids: set = None,
    ):
        """AI 从桥段候选中选择最合适的。

        生成器：yield thinking（LLM 流式 token）+ decision（每确定一批桥段），
        return list[PlotTemplate]。used_ids 为本弧已用桥段模板 id，供 AI 避免重复。
        """
        used_ids = used_ids or set()
        stage_name = stage.get("name", "")
        events = stage.get("events", [])

        cand_text = "\n".join(
            f"- {t.id}: {t.name}（{t.category}/{t.sub_category}）"
            f" | 结构: {t.template_structure[:60] if t.template_structure else '无'}"
            for t in candidates[:10]
        )

        cand_list = [{"id": t.id, "name": t.name} for t in candidates[:10]]

        used_names = "、".join(t.name for t in candidates if t.id in used_ids)
        avoid_hint = (f"\n【已在本弧前阶段用过的桥段，请避免重复】{used_names}"
                      if used_names else "")

        bible_block = self.harness.render_outline_context("select_plots") if self.harness else ""

        prompt = f"""{bible_block}在大纲「{outline.name}」的「{stage_name}」阶段选择合适的桥段。

【阶段事件】
{'、'.join(events) if events else '按题材惯例推进'}
【本弧主题】{outline.name}

【候选桥段】
{cand_text}
{avoid_hint}

【要求】
从候选中选择 1-3 个最匹配该阶段的桥段。需考虑：
- 桥段类型须契合本弧/本阶段主题（开篇用钩子/开篇类、成长用战斗/拜师类、
  悬疑/调查类弧线用线索/推理类，避免爽文桥段乱入正剧/悬疑弧）
- 避免与已用桥段重复
- 桥段之间能否形成递进关系
- 避免类型重复

返回 JSON：
{{"plot_ids": ["id1", "id2"], "reason": "简要说明选择原因"}}"""

        yield ("decision", "plot_choice", {
            "step": f"「{outline.name}」· 阶段「{stage_name}」的候选桥段",
            "candidates": cand_list,
            "chosen": {},
            "reason": "以下桥段来自桥段库，AI 将按阶段节奏挑选",
        })

        reason = ""
        selected = []
        if self.llm:
            try:
                from core.llm_client import extract_json
                raw = yield from self._stream_decision_content(
                    "plot_choice", "你是网文编辑。只返回JSON。", prompt,
                    temperature=0.5, max_tokens=8192)
                data = json.loads(extract_json(raw))
                reason = data.get("reason", "")
                ids = data.get("plot_ids", [])
                selected = [t for t in candidates if t.id in ids]
            except Exception:
                selected = []
        if not selected:
            selected = candidates[:2]

        yield ("decision", "plot_choice", {
            "step": f"确定「{outline.name}」· 阶段「{stage_name}」桥段",
            "candidates": cand_list,
            "chosen": [{"id": t.id, "name": t.name} for t in selected],
            "reason": reason or "规则兜底",
        })
        return selected

    # ═══════════════════════════════════════
    # Phase 4: 内涵挂载
    # ═══════════════════════════════════════

    def _select_book_themes(self, genre: str,
                            tl: Optional[BookStoryline] = None) -> list[str]:
        """选定全书内涵（只读阶段级，内涵唯一来源 = StageNode.themes）。

        汇总各 outline.stages[].themes[].name（去重取前 3）；
        全部为空 → 兜底默认（用可挂桥段的中英内涵名）。
        """
        default_themes = ["成长的代价（Cost of Growth）"]
        matched = []
        if tl:
            for o in (tl.outlines or []):
                for stage in (o.stages or []):
                    for m in (stage.get("themes") or []):
                        name = m.get("name") if isinstance(m, dict) else m
                        if name and name not in matched:
                            matched.append(name)
        return matched[:3] if matched else default_themes

    def _inject_themes_and_hooks(
        self, plot: PlotSlot, tl: BookStoryline,
    ):
        """为一个桥段匹配内涵（跟随桥段）并标注吸睛点（委托共享 mount_themes_and_hooks）。"""
        mount_themes_and_hooks(plot, tl.themes)

    # ═══════════════════════════════════════
    # Phase 4: 线程与呼应（线程穿插 + 桥段拆分设局→收局）
    # ═══════════════════════════════════════

    def _default_thread_for_category(self, cat: str) -> str:
        """桥段分类 → 默认线程（规则兜底）。"""
        c = (cat or "")
        if any(k in c for k in ("悬疑", "阴谋", "调查", "推理", "诡计")):
            return "伏笔阴谋线"
        if any(k in c for k in ("情感", "日常", "羁绊", "成长")):
            return "副线"
        return "主线"

    def _apply_thread_fallback(self, tl: BookStoryline):
        """LLM 线程规划失败时回退：按分类归线程，不拆。"""
        tl.threads = [
            {"id": "主线", "name": "主线", "desc": "主角核心推进线"},
            {"id": "副线", "name": "副线", "desc": "情感/日常/配角线"},
            {"id": "伏笔阴谋线", "name": "伏笔阴谋线", "desc": "悬疑/阴谋暗线，早埋晚收"},
        ]
        for p in tl.plots:
            p.thread_id = self._default_thread_for_category(p.category)
            p.thread_seq = 0

    _THREAD_ID_NORM = {
        "main": "主线", "side": "副线", "sub": "副线",
        "hidden": "伏笔阴谋线", "mystery": "伏笔阴谋线", "foreshadow": "伏笔阴谋线",
    }

    def _normalize_thread_id(self, tid) -> str:
        tid = (tid or "主线").strip()
        if not tid:
            return "主线"
        return self._THREAD_ID_NORM.get(tid.lower(), tid)

    def _apply_thread_assignments(self, tl: BookStoryline, threads, assignments):
        """把 LLM 的线程分配应用到 plots（未分配的按分类兜底；英文 id 规范化为中文）。"""
        if isinstance(threads, list) and threads:
            tl.threads = [{"id": self._normalize_thread_id(t.get("id")),
                           "name": t.get("name") or self._normalize_thread_id(t.get("id")),
                           "desc": t.get("desc", "")} for t in threads if isinstance(t, dict)]
        if not tl.threads:
            tl.threads = [{"id": "主线", "name": "主线", "desc": "主角核心推进线"}]
        if not any(t.get("id") == "主线" for t in tl.threads):
            tl.threads.insert(0, {"id": "主线", "name": "主线", "desc": "主角核心推进线"})
        thread_ids = {t.get("id") for t in tl.threads if t.get("id")}

        assigned_ids = set()
        for a in assignments or []:
            pid = a.get("plot_id", "")
            if not pid:
                continue
            plot = next((p for p in tl.plots if p.id == pid), None)
            if not plot:
                continue
            assigned_ids.add(pid)
            tid = self._normalize_thread_id(a.get("thread", "主线"))
            if tid not in thread_ids:
                tl.threads.append({"id": tid, "name": tid, "desc": ""})
                thread_ids.add(tid)
            plot.thread_id = tid
            try:
                plot.thread_seq = int(a.get("seq", 0) or 0)
            except (TypeError, ValueError):
                plot.thread_seq = 0

        for p in tl.plots:
            if p.id not in assigned_ids:
                p.thread_id = self._default_thread_for_category(p.category)
                p.thread_seq = 0

    def _apply_split_payoffs(self, tl: BookStoryline, splits):
        """为选中的设局桥段创建收局槽位（resolves_plot_id=设局.id），放后几个 stage。

        生成器：每个收局 yield plot_added 供前端实时刷新。
        """
        for s in splits or []:
            setup = next((p for p in tl.plots if p.id == s.get("plot_id", "")), None)
            if not setup:
                continue
            outline = next((o for o in tl.outlines if o.id == setup.outline_id), None)
            n_stages = len(outline.stages) if outline else 0
            try:
                gap = max(int(s.get("payoff_after_stage", 2) or 2), 1)
            except (TypeError, ValueError):
                gap = 2
            payoff_stage = min(setup.stage_index + gap,
                               max(n_stages - 1, setup.stage_index + 1))
            order = max([p.order for p in tl.plots
                         if p.outline_id == setup.outline_id
                         and p.stage_index == payoff_stage] or [-1]) + 1
            tid = self._normalize_thread_id(s.get("payoff_thread", "") or setup.thread_id)
            payoff = PlotSlot(
                id=self._next_id("plot"), template_id=setup.template_id,
                name=s.get("payoff_name", "") or (setup.name + "·收局"),
                category=setup.category, sub_category=setup.sub_category,
                outline_id=setup.outline_id, stage_index=payoff_stage,
                order=order, cover_beats=setup.cover_beats,
                template_structure=setup.template_structure,
                thread_id=tid, thread_seq=setup.thread_seq,
                resolves_plot_id=setup.id, resolves_name=setup.name,
            )
            tl.plots.append(payoff)
            yield ("plot_added", payoff.name, {
                "plot_id": payoff.id,
                "outline_id": payoff.outline_id,
                "outline_name": next((o.name for o in tl.outlines
                                      if o.id == payoff.outline_id), ""),
                "category": payoff.category,
            })

    def _plan_threads_and_splits(self, tl: BookStoryline, genre: str):
        """Phase 4：LLM 规划叙事线程 + 桥段拆分设局→收局。

        生成器：yield thinking/decision/plot_added；失败回退规则。
        """
        if not self.llm or not tl.plots:
            self._apply_thread_fallback(tl)
            yield ("decision", "thread_split", {
                "step": "线程与呼应（规则兜底）",
                "candidates": [], "chosen": {"threads": [], "splits": 0},
                "reason": "LLM 未配置或无桥段，按分类规则归线程",
            })
            return

        outlines_view = [{
            "id": o.id, "name": o.name,
            "range": f"第{o.start_chapter}-{o.end_chapter}章",
            "stages": [s.get("name", "") for s in (o.stages or [])],
        } for o in tl.outlines[:10]]
        o_pos = {o.id: i for i, o in enumerate(tl.outlines)}
        ordered = sorted(tl.plots,
                         key=lambda p: (o_pos.get(p.outline_id, 99), p.stage_index, p.order))
        plots_snapshot = [{
            "id": p.id, "name": p.name, "category": p.category,
            "outline_id": p.outline_id, "stage": p.stage_index, "order": p.order,
        } for p in ordered[:60]]

        bible_block = self.harness.render_outline_context("thread_split", tl) if self.harness else ""
        prompt = f"""{bible_block}为以下{genre}小说的故事线规划「叙事线程」和「桥段拆分设局→收局」。

【大纲】
{json.dumps(outlines_view, ensure_ascii=False)}

【全部桥段（按大纲顺序）】
{json.dumps(plots_snapshot, ensure_ascii=False)}

【叙事线程】
- 主线：主角当前最核心推进目标（职场反击/修炼升级/权谋等）；主角可有多条线并存。
- 副线：情感、日常、配角、成长线。
- 伏笔线：悬疑、阴谋、暗线，早埋晚收。
- 每条桥段必须归入一个线程；第1章附近应多线程并进（主线2步+其他线各1步节奏）。

【桥段拆分】
- 选中适合"设局→收局"的桥段（阴谋/悬疑/智斗/成长转折），生成一个"收局"槽位，
  放回同一大纲的后几个 stage（payoff_after_stage≥2），中间被其他线程/桥段穿插，早埋钩子晚回收。
- 纯即时爽点（打脸/战斗/开篇）不要拆。

返回 JSON：
{{"threads": [{{"id":"主线","name":"","desc":""}}],
  "assignments": [{{"plot_id":"","thread":"","seq":1}}],
  "splits": [{{"plot_id":"","payoff_after_stage":2,"payoff_name":"","payoff_thread":""}}],
  "reason": "一句话说明线程/拆分思路"}}"""

        # 非流式 + 空响应重试：max_tokens 必须留足推理余量（该任务输出含全部桥段分配，
        # 8192 会被推理+正文吃满导致 content 空；16384 实测稳定）。
        data = None
        for _attempt in range(3):
            try:
                from core.llm_client import extract_json
                raw = self.llm.call(
                    "你是网文策划编辑，负责叙事线程与钩子呼应规划。只返回JSON。",
                    prompt, temperature=0.3, max_tokens=16384)
                if not raw or not raw.strip():
                    continue  # flash 偶发空返回 → 重试
                data = json.loads(extract_json(raw))
                break
            except Exception:
                continue
        if data is None:
            self._apply_thread_fallback(tl)
            yield ("decision", "thread_split", {
                "step": "线程与呼应（解析失败回退）",
                "candidates": [], "chosen": {"threads": [], "splits": 0},
                "reason": "LLM 多次空返回/解析失败，按分类规则回退",
            })
            return

        self._apply_thread_assignments(tl, data.get("threads"), data.get("assignments"))
        split_count = 0
        for evt in self._apply_split_payoffs(tl, data.get("splits")):
            split_count += 1
            yield evt

        yield ("decision", "thread_split", {
            "step": "线程与呼应",
            "candidates": [{"id": p.id, "name": p.name} for p in tl.plots[:20]],
            "chosen": {
                "threads": [t.get("id", "") for t in tl.threads],
                "splits": split_count,
            },
            "reason": data.get("reason", "") or "规划完成",
        })

    def _review_theme_assignments(self, tl: BookStoryline, genre: str):
        """Phase 4 内涵挂载后的 LLM 复查（流式思考）。

        生成器：yield thinking（token 流）+ decision（复查结论），
        复查"内涵是否挂到了能承载它的桥段、分布是否均匀、有无硬挂"。
        笑点已完全涌现（不在此复查）。
        """
        if not self.llm or not tl.plots:
            return

        plots_snapshot = []
        for p in tl.plots[:20]:
            plots_snapshot.append({
                "id": p.id, "name": p.name, "category": p.category,
                "template_id": p.template_id, "theme_hints": p.theme_hints[:2],
            })

        prompt = f"""以下是某本{genre}小说故事线的桥段内涵挂载配置（规则匹配结果）。请复查内涵是否挂到了能承载它的桥段、分布是否均匀、有无明显硬挂（桥段承载不了某个内涵却挂着）。

{json.dumps(plots_snapshot, ensure_ascii=False, indent=1)}

返回 JSON：
{{"corrections": [{{"plot_id": "...", "theme_hints": [...], "reason": "..."}}], "summary": "整体评价（一句话）"}}"""

        try:
            from core.llm_client import extract_json
            raw = yield from self._stream_decision_content(
                "theme_review", "你是网文编辑，负责内涵复查。只返回JSON。",
                prompt, temperature=0.3, max_tokens=8192)
            data = json.loads(extract_json(raw))

            corrections = data.get("corrections", []) or []
            summary = data.get("summary", "")
            for corr in corrections:
                plot = next((p for p in tl.plots if p.id == corr.get("plot_id", "")), None)
                if not plot:
                    continue
                if corr.get("theme_hints"):
                    # 只接受书级内涵库内的名字
                    valid = [t for t in corr["theme_hints"] if t in tl.themes]
                    if valid:
                        plot.theme_hints = valid[:2]

            yield ("decision", "theme_review", {
                "step": "内涵挂载复查",
                "candidates": [{"id": p.id, "name": p.name} for p in tl.plots[:20]],
                "chosen": {"themes": tl.themes[:3]},
                "reason": summary or "复查完成，未做调整",
            })
        except Exception:
            yield ("decision", "theme_review", {
                "step": "内涵挂载复查",
                "candidates": [{"id": p.id, "name": p.name} for p in tl.plots[:20]],
                "chosen": {"themes": tl.themes[:3]},
                "reason": "复查调用未返回有效结果，沿用规则匹配",
            })

    # ═══════════════════════════════════════
    # Phase 5: 一致性验证
    # ═══════════════════════════════════════

    def _validate(self, tl: BookStoryline) -> list[str]:
        """验证 BookStoryline 的合理性和完整性"""
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

        # 4. 内涵覆盖率（内涵跟随桥段，建议性，不强求）
        total_plots = len(tl.plots)
        theme_plots = sum(1 for p in tl.plots if p.theme_hints)
        if tl.themes and total_plots > 0 and theme_plots / total_plots < 0.3:
            issues.append(f"内涵覆盖率偏低（{theme_plots}/{total_plots}），建议把内涵挂到更多能承载的桥段")

        # 5. 总章节合理性
        if tl.outlines:
            max_ch = max(o.end_chapter for o in tl.outlines)
            if max_ch < 10:
                issues.append(f"全书仅{max_ch}章，建议扩展大纲覆盖范围")
            elif max_ch > 500:
                issues.append(f"全书{max_ch}章，建议拆分或精简")

        return issues

    def _validate_with_llm(self, tl: BookStoryline):
        """Phase 5 一致性验证的 LLM 复查（流式思考）。

        生成器：yield thinking（token 流）+ decision（验证结论），
        在规则校验之外再做一次 AI 抽查，输出建议与总结。
        """
        if not self.llm or not tl.outlines:
            return

        outlines_view = [{
            "name": o.name,
            "range": f"第{o.start_chapter}-{o.end_chapter}章",
            "transition": o.transition_type,
            "stages": [s.get("name", "") for s in (o.stages or [])],
        } for o in tl.outlines[:10]]

        prompt = f"""请审查下面这本小说故事线的合理性：
大纲：{json.dumps(outlines_view, ensure_ascii=False, indent=1)}
桥段总数：{len(tl.plots)}；内涵已按桥段挂载。

请检查：故事线重叠/间隔是否合理、桥段覆盖是否均匀、有无明显漏洞。

返回 JSON：
{{"issues": ["...", "..."], "summary": "一句话结论"}}"""

        try:
            from core.llm_client import extract_json
            raw = yield from self._stream_decision_content(
                "validate", "你是资深网文策划，审查故事线。只返回JSON。",
                prompt, temperature=0.3, max_tokens=8192)
            data = json.loads(extract_json(raw))
            yield ("decision", "validate", {
                "step": "一致性验证",
                "candidates": [{"id": o.id, "name": o.name} for o in tl.outlines[:10]],
                "chosen": {"issues": data.get("issues", []) or []},
                "reason": data.get("summary", ""),
            })
        except Exception:
            pass


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
    structure_lib=None, plot_lib=None, gag_lib=None,
) -> dict:
    """
    同步版本：生成并返回完整 BookStoryline（测试/脚本用）。
    注意：会阻塞直到全部生成完成。
    """
    gen = OutlineGenerator(
        llm_client, structure_lib, plot_lib, gag_lib)
    result = None
    for event_type, message, data in gen.generate(
        custom_context=custom_context,
        pen_name=pen_name, words_per_chapter=words_per_chapter,
    ):
        if event_type == "done":
            result = data
        elif event_type == "error":
            raise RuntimeError(f"生成失败: {message}")
    return result
