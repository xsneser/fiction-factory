"""
蓝图式写作引擎 v2 — 按依赖关系构建全文，最后分章

流程（从左往右、从上往下）：
  Phase 1: 大纲扩写 — 每个大纲 1 次 LLM，生成详细叙事稿
  Phase 2: 桥段填充 — 仅处理完全包含在已扩写大纲内的桥段，每个 1 次 LLM
            跨大纲桥段跳过（等依赖大纲扩写完再回来）
  Phase 3: 笑点/内涵注入 — 每个桥段 1-2 次 LLM
  Phase 4: 一致性审查 — 全文 1 次 LLM
  Phase 5: 分章节 — 全部正文写完后 1 次 LLM 决定切在哪里

全程 SSE 流式输出，UI 左栏蓝图高亮、右栏实时进度+正文
"""
from dataclasses import dataclass, field
from typing import Optional, Callable
import json, re, time

from .timeline import BookTimeline, OutlineSlot, PlotSlot


# ═══════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════

@dataclass
class OutlineExpansion:
    """一个大纲的扩写结果"""
    outline_id: str
    name: str
    text: str           # 扩写正文
    word_count: int
    start_words: int    # 在全文中的起始字数位置
    end_words: int      # 结束位置

@dataclass
class FilledPlot:
    """一个桥段的填充结果"""
    plot_id: str
    name: str
    text: str
    word_count: int
    gags_applied: list[str]
    themes_applied: list[str]

@dataclass
class BuildState:
    """构建状态 — 用于 SSE 进度报告"""
    phase: str = "idle"  # outlining|filling|gags|review|splitting|done
    current_outline: str = ""
    current_plot: str = ""
    message: str = ""
    total_outlines: int = 0
    total_plots: int = 0
    outlines_done: int = 0
    plots_done: int = 0
    plots_skipped: int = 0


# ═══════════════════════════════════════════
# Phase 1: 大纲扩写器
# ═══════════════════════════════════════════

class OutlineExpander:
    """将每个大纲扩写为详细叙事稿"""

    def __init__(self, timeline: BookTimeline, llm_client=None, on_progress=None):
        self.tl = timeline
        self.llm = llm_client
        self.on_progress = on_progress or (lambda s,m: None)

    def expand_all(self) -> list[OutlineExpansion]:
        results = []
        cumulative_words = 0

        for o in self.tl.outlines:
            self.on_progress("outlining", f"大纲扩写: {o.name}")
            wc_target = self.tl.words_per_chapter * (o.end_chapter - o.start_chapter + 1)

            # 构建上下文：前面已扩写的大纲
            prev_context = ""
            if results:
                prev_names = [r.name for r in results]
                prev_context = f"前面已完成的大纲：{' → '.join(prev_names)}\n"

            expanded = self._expand_one(o, wc_target, prev_context, cumulative_words)
            results.append(expanded)
            cumulative_words = expanded.end_words

        return results

    def _expand_one(self, outline: OutlineSlot, word_target: int,
                     prev_context: str, start_pos: int) -> OutlineExpansion:
        # 生成大纲阶段描述
        stage_desc = "\n".join(
            f"  {s.get('name','?')}（{s.get('min_ch',2)}-{s.get('max_ch',5)}章）: {', '.join(s.get('events',[])[:3])}"
            for s in outline.stages[:4]
        ) if outline.stages else "（按标准升级流推进）"

        prompt = f"""根据以下设定，展开 "{outline.name}" 大纲的详细叙事稿。

【本书信息】
流派：{self.tl.genre}{'/'+self.tl.sub_genre if self.tl.sub_genre else ''}
笔名风格：{self.tl.pen_name}
每章目标：{self.tl.words_per_chapter}字

【主角设定】
{json.dumps(self.tl.basic_info.get('protagonist',{}), ensure_ascii=False)}

【世界观】
{self.tl.basic_info.get('world_building',{}).get('description','') or '标准异世界'}

【前面大纲】
{prev_context or '（这是全书开头）'}

【本大纲阶段】
{stage_desc}

【要求】
1. 写出完整的故事叙事稿（不是提纲），是可直接阅读的正文
2. 目标 {word_target} 字，控制在 ±10% 内
3. 主角性格贯穿始终，幽默自嘲风格
4. 每个阶段过渡自然，不要生硬分段标题
5. 可以分段（段落间空行），但不要章节标题/编号"""

        text = self._call_llm(prompt, word_target)

        wc = len(re.findall(r'[\u4e00-\u9fff]', text))
        return OutlineExpansion(
            outline_id=outline.id, name=outline.name,
            text=text, word_count=wc,
            start_words=start_pos, end_words=start_pos + wc,
        )

    def _call_llm(self, prompt: str, word_target: int) -> str:
        if not self.llm:
            return f"[大纲扩写稿 - {word_target}字 - LLM 未配置]"
        from core.llm_client import extract_json
        raw = self.llm.call(
            "你是一位专业的网络小说作者。请根据设定展开大纲叙事稿。",
            prompt, temperature=0.7, max_tokens=max(2048, word_target * 2))
        # 尝试提取 JSON（如果 LLM 返回了 JSON 包裹），否则用原始文本
        try:
            data = json.loads(extract_json(raw))
            return data.get("text", raw)
        except Exception:
            return raw.strip()


# ═══════════════════════════════════════════
# Phase 2+3: 桥段填充 + 笑点注入
# ═══════════════════════════════════════════

class PlotFiller:
    """在大纲扩写稿基础上填充桥段（仅填充完全包含在已扩写大纲内的桥段）"""

    def __init__(self, timeline: BookTimeline, llm_client=None,
                 gag_lib=None, theme_lib=None, on_progress=None):
        self.tl = timeline
        self.llm = llm_client
        self.gag_lib = gag_lib
        self.theme_lib = theme_lib
        self.on_progress = on_progress or (lambda s,m: None)

    def fill_all(self, expansions: list[OutlineExpansion]) -> tuple[list[FilledPlot], int]:
        """
        按依赖顺序填充所有桥段。
        返回 (已填充的桥段列表, 跳过的桥段数)。
        跨大纲桥段（依赖的大纲未全部扩写）跳过，标记待处理。
        """
        results: list[FilledPlot] = []
        skipped = 0

        # 建立 大纲id → 扩写稿 映射
        exp_map = {e.outline_id: e for e in expansions}
        expanded_outline_ids = set(exp_map.keys())

        # 排序：先按大纲顺序，再按阶段顺序
        sorted_plots = sorted(self.tl.plots,
            key=lambda p: (next((i for i, o in enumerate(self.tl.outlines) if o.id == p.outline_id), 99), p.stage_index, p.order))

        for p in sorted_plots:
            # 检查依赖：桥段所属的大纲必须已扩写
            if p.outline_id not in expanded_outline_ids:
                skipped += 1
                continue

            self.on_progress("filling", f"桥段填充: {p.name}")
            expansion = exp_map[p.outline_id]

            # 填充这个桥段
            filled = self._fill_one(p, expansion)
            if filled:
                results.append(filled)

            # 注入笑点
            if filled and p.gag_ids:
                self.on_progress("gags", f"笑点注入: {p.name}")
                self._inject_gags(filled, p)

        return results, skipped

    def _fill_one(self, plot: PlotSlot, expansion: OutlineExpansion) -> Optional[FilledPlot]:
        """基于大纲扩写稿，按桥段骨架填充一段正文"""
        structure = plot.template_structure or plot.name
        slots_text = ""
        if plot.slots:
            slots_text = "\n".join(
                f"  {s.get('name','?')} = {s.get('default','?')}（可选: {', '.join(s.get('options',[])[:3])}）"
                for s in plot.slots[:4]
            )

        # 取大纲扩写稿中与该桥段阶段相关的部分作为上下文
        context = expansion.text[:1500] + "..." + expansion.text[-500:]

        wc_target = min(plot.cover_beats * 400, 2500)

        prompt = f"""基于以下大纲扩写稿，按桥段骨架填充一段完整正文。

【大纲上下文】
{context}

【桥段骨架】
{structure}

【变量槽位】
{slots_text or '跟随大纲上下文自由发挥'}

【要求】
1. 这段正文是之前大纲的细化/展开，不是独立片段
2. 目标 {wc_target} 字，控制在 ±20%
3. 按骨架步骤自然推进，不要标注"步骤1/2"
4. 如果桥段是嵌套的子桥段，篇幅更短、更聚焦
5. 保持大纲的风格和人物性格一致"""

        text = self._call_llm(prompt, wc_target)
        if not text or len(text) < 30:
            return None

        wc = len(re.findall(r'[\u4e00-\u9fff]', text))
        return FilledPlot(
            plot_id=plot.id, name=plot.name,
            text=text, word_count=wc,
            gags_applied=[], themes_applied=[],
        )

    def _inject_gags(self, filled: FilledPlot, plot: PlotSlot):
        """在已填充的桥段正文中注入笑点和内涵"""
        gags_desc = ""
        if self.gag_lib:
            gag_patterns = []
            for gid in plot.gag_ids[:2]:
                found = next((g for g in self.gag_lib.patterns if g.id == gid), None)
                if found:
                    gag_patterns.append(f"{found.name}: {found.pattern_description}")
            gags_desc = "\n".join(gag_patterns) if gag_patterns else plot.gag_ids

        if not gags_desc and not filled.text:
            return

        prompt = f"""在以下桥段正文中，自然地注入笑点和内涵线索。不要大改原文结构，在合适位置插入/微调 2-3 处即可。

【桥段正文】
{filled.text[:2000]}

【笑点模式】
{gags_desc or '无特殊要求'}

【内涵提示】
{'; '.join(plot.theme_hints[:2]) if plot.theme_hints else '无'}

【要求】
1. 笑点要自然，不能生硬插入
2. 返回完整修改后的正文
3. 返回 JSON：{{"text": "修改后全文"}}"""

        try:
            from core.llm_client import extract_json
            raw = self.llm.call("你是专业的网文编辑。只返回JSON。", prompt,
                                temperature=0.5, max_tokens=min(4096, len(filled.text)*2))
            data = json.loads(extract_json(raw))
            new_text = data.get("text", filled.text)
            filled.text = new_text
            filled.gags_applied = plot.gag_ids
            filled.themes_applied = plot.theme_hints
            filled.word_count = len(re.findall(r'[\u4e00-\u9fff]', new_text))
        except Exception:
            pass  # 注入失败不影响正文

    def _call_llm(self, prompt: str, word_target: int) -> str:
        if not self.llm:
            return f"[桥段 - {word_target}字 - LLM 未配置]"
        raw = self.llm.call(
            "你是一位专业的网络小说作者。请按桥段骨架填充正文。",
            prompt, temperature=0.8, max_tokens=max(1536, word_target * 2))
        return raw.strip()


# ═══════════════════════════════════════════
# Phase 4: 一致性审查
# ═══════════════════════════════════════════

class ConsistencyChecker:
    """全文一致性检查"""

    def __init__(self, llm_client=None, on_progress=None):
        self.llm = llm_client
        self.on_progress = on_progress or (lambda s,m: None)

    def review(self, full_text: str, timeline: BookTimeline) -> str:
        self.on_progress("review", "审查全文一致性...")
        if not self.llm or len(full_text) < 500:
            return full_text

        prompt = f"""审查以下网络小说全文的一致性和连贯性。特别注意：
- 人物名称前后是否一致
- 时间线是否有跳跃漏洞
- 桥段过渡是否生硬
- 需要修复的地方尽量少改动

流派：{timeline.genre}
主角：{timeline.basic_info.get('protagonist',{}).get('name','') or '主角'}

返回 JSON：{{"needs_fix": true/false, "fixed_text": "修复后全文", "issues": ["问题1"]}}"""

        try:
            from core.llm_client import extract_json
            raw = self.llm.call("你是小说编辑，审查全文一致性。只返回JSON。",
                                prompt + "\n\n【全文】\n" + full_text[:4000],
                                temperature=0.3, max_tokens=4096)
            data = json.loads(extract_json(raw))
            if data.get("needs_fix") and data.get("fixed_text"):
                return data["fixed_text"]
        except Exception:
            pass
        return full_text


# ═══════════════════════════════════════════
# Phase 5: 分章节
# ═══════════════════════════════════════════

@dataclass
class ChapterBoundary:
    """一个章节边界"""
    chapter_num: int
    title_hint: str
    start_offset: int   # 在全文中的起始字符位置
    end_offset: int     # 结束位置
    word_count: int

class ChapterSplitter:
    """全文写完后，AI 决定切在哪里分章节"""

    def __init__(self, timeline: BookTimeline, llm_client=None, on_progress=None):
        self.tl = timeline
        self.llm = llm_client
        self.on_progress = on_progress or (lambda s,m: None)

    def split(self, full_text: str) -> list[ChapterBoundary]:
        self.on_progress("splitting", "AI 分章节...")
        wc = len(re.findall(r'[\u4e00-\u9fff]', full_text))
        target_per_ch = self.tl.words_per_chapter

        if not self.llm or wc <= target_per_ch * 1.3:
            # 少于一章半，不用分
            return [ChapterBoundary(1, "", 0, len(full_text), wc)]

        approx_chapters = max(1, wc // target_per_ch)

        prompt = f"""以下是一篇网络小说的完整正文（{wc}字）。请将其分为 {approx_chapters} 章左右。

每章约 {target_per_ch} 字，分章位置选在自然停顿处（场景切换/时间跳跃/悬念高潮之后），
不要强行在段落中间切断。

返回 JSON：
{{"chapters": [
  {{"start_marker": "第一章开头前20个字（用于定位）", "title": "章名"}},
  ...
]}}"""

        try:
            from core.llm_client import extract_json
            raw = self.llm.call("你是小说编辑，负责分章节。只返回JSON。",
                                prompt + "\n\n【正文】\n" + full_text[:5000] + "...",
                                temperature=0.4, max_tokens=2048)
            data = json.loads(extract_json(raw))
            chapters = data.get("chapters", [])

            # 用 start_marker 在全文里定位分章点
            boundaries = []
            prev_pos = 0
            for ci, ch in enumerate(chapters):
                marker = ch.get("start_marker", "")
                if marker and ci > 0:
                    pos = full_text.find(marker, prev_pos + 50)
                    if pos < 0:
                        pos = prev_pos + target_per_ch  # fallback: 按字数估算
                else:
                    pos = 0 if ci == 0 else prev_pos

                if ci == 0:
                    pos = 0

                ch_text = full_text[prev_pos:pos] if pos > prev_pos else full_text[prev_pos:]
                ch_wc = len(re.findall(r'[\u4e00-\u9fff]', ch_text))
                boundaries.append(ChapterBoundary(
                    chapter_num=ci + 1,
                    title_hint=ch.get("title", f"第{ci+1}章"),
                    start_offset=prev_pos,
                    end_offset=pos,
                    word_count=ch_wc,
                ))
                prev_pos = pos

            return boundaries if boundaries else self._fallback_split(full_text, target_per_ch)
        except Exception:
            return self._fallback_split(full_text, target_per_ch)

    def _fallback_split(self, full_text: str, words_per_ch: int) -> list[ChapterBoundary]:
        """规则分章：按字数均匀切"""
        wc = len(re.findall(r'[\u4e00-\u9fff]', full_text))
        ch_count = max(1, wc // words_per_ch)
        boundaries = []
        for i in range(ch_count):
            start = i * words_per_ch * 4  # 粗略估算字符位置（中文每字约2字符）
            end = min((i+1) * words_per_ch * 4, len(full_text))
            boundaries.append(ChapterBoundary(
                chapter_num=i+1, title_hint=f"第{i+1}章",
                start_offset=start, end_offset=end,
                word_count=words_per_ch,
            ))
        return boundaries


# ═══════════════════════════════════════════
# 完整蓝图写作管线 v2（SSE 流式）
# ═══════════════════════════════════════════

class BlueprintWritingPipeline:
    """
    蓝图写作管线 v2 — 先写完全文再分章，SSE 流式输出进度

    用法:
        pipeline = BlueprintWritingPipeline(timeline, llm, ...)
        for event in pipeline.build():
            # event = (phase, message, data_dict)
            yield sse_event(event)
    """

    def __init__(self, timeline: BookTimeline, llm_client=None,
                 gag_lib=None, theme_lib=None, de_ai_engine=None):
        self.tl = timeline
        self.llm = llm_client
        self.gag_lib = gag_lib
        self.theme_lib = theme_lib
        self.de_ai = de_ai_engine
        self.state = BuildState()

    def build(self):
        """生成器：逐步构建全文，yield (phase, message, data) 事件"""
        expansions: list[OutlineExpansion] = []
        filled_plots: list[FilledPlot] = []

        # ── Phase 1: 大纲扩写 ──
        self.state.total_outlines = len(self.tl.outlines)
        self.state.total_plots = len(self.tl.plots)
        yield ("phase", "大纲扩写", {})

        expander = OutlineExpander(self.tl, self.llm,
            on_progress=lambda s, m: self._update("outlining", s, m))

        for i, o in enumerate(self.tl.outlines):
            self.state.current_outline = o.name
            self.state.message = f"大纲扩写: {o.name}"
            yield ("outline_start", o.name, {"index": i+1, "total": len(self.tl.outlines)})

            expanded = expander._expand_one(
                o,
                self.tl.words_per_chapter * (o.end_chapter - o.start_chapter + 1),
                self._prev_context(expansions),
                expansions[-1].end_words if expansions else 0,
            )
            expansions.append(expanded)
            self.state.outlines_done = len(expansions)

            yield ("outline_done", o.name, {
                "index": i+1, "words": expanded.word_count,
                "outline_id": o.id, "text_preview": expanded.text[:200] + "...",
            })

        # ── Phase 2: 桥段填充 ──
        yield ("phase", "桥段填充", {})
        filler = PlotFiller(self.tl, self.llm, self.gag_lib, self.theme_lib,
            on_progress=lambda s, m: self._update("filling", s, m))

        filled, skipped = filler.fill_all(expansions)
        self.state.plots_done = len(filled)
        self.state.plots_skipped = skipped

        for fp in filled:
            yield ("plot_done", fp.name, {
                "plot_id": fp.plot_id, "words": fp.word_count,
                "gags": fp.gags_applied, "text_preview": fp.text[:200] + "...",
            })

        if skipped:
            yield ("info", f"跳过了 {skipped} 个跨大纲桥段", {})

        # ── Phase 4: 一致性审查 ──
        yield ("phase", "一致性审查", {})
        full_text = self._assemble_full_text(expansions, filled)
        checker = ConsistencyChecker(self.llm, on_progress=lambda s,m: None)
        reviewed_text = checker.review(full_text, self.tl)

        # ── Phase 5: 分章节 ──
        yield ("phase", "分章节", {})
        splitter = ChapterSplitter(self.tl, self.llm)
        chapters = splitter.split(reviewed_text)

        # 组装最终结果
        chapters_data = []
        for ch in chapters:
            ch_text = reviewed_text[ch.start_offset:ch.end_offset] if ch != chapters[-1] else reviewed_text[ch.start_offset:]
            chapters_data.append({
                "num": ch.chapter_num, "title": ch.title_hint,
                "word_count": ch.word_count,
                "text": ch_text,
            })

        yield ("done", "构建完成", {
            "total_words": sum(c["word_count"] for c in chapters_data),
            "chapters": len(chapters_data),
            "outlines_expanded": len(expansions),
            "plots_filled": len(filled),
            "plots_skipped": skipped,
            "chapters_data": chapters_data,
        })

    def _prev_context(self, expansions: list[OutlineExpansion]) -> str:
        if not expansions:
            return ""
        return f"前面已完成的大纲：{' → '.join(e.name for e in expansions)}"

    def _assemble_full_text(self, expansions: list[OutlineExpansion],
                            filled: list[FilledPlot]) -> str:
        """组装全文：大纲扩写稿为主干，桥段填充稿替换/补充对应部分"""
        # 简单策略：拼接所有扩写稿 + 填充稿
        parts = []
        parts.extend(e.text for e in expansions)
        parts.extend(f.text for f in filled)
        return "\n\n".join(parts)

    def _update(self, phase: str, state: str, message: str):
        self.state.phase = phase
        self.state.message = message
