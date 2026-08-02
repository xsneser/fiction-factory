"""
蓝图式写作引擎 v3 — 桥段驱动的逐章增量写作

架构约定（用户明确）：
  · 左→右 = 层次顺序：大纲 → 阶段 → 桥段
  · 上→下 = 故事顺序：沿故事线逐桥段推进
  · 桥段是生成单元：每个桥段写完后累计字数，满 words_per_chapter 即切成一章
  · 短句组生成：每个桥段内逐「短句组」调用 LLM（每次 1-3 个短句，约 50-100 字），
    组与组之间用空行分隔成独立段落（网文短段风格）；每次调用都携带
    「本章已写全部前文 + 上一章结尾 + 本桥段已写」，保证桥段之间故事连贯。
  · 每段/每章写完立即落盘（桥段 written_chapter 写入 timeline，章节正文由 engine 保存）

旧"整本先写全文再分章"（BlueprintWritingPipeline）已废弃删除。
"""
import json
import re

from .timeline import BookTimeline, OutlineSlot, PlotSlot
from core.inject import count_prose_units

CHARS_PER_BEAT = 200          # 每个节拍预计写多少个汉字（用于桥段字数规划）
MAX_BRIDGE_WORDS = 1200       # 单个桥段字数上限（与 frontend story_line.js 共用同一公式）


def planned_words(plot) -> int:
    """桥段预计字数 = cover_beats × 每拍字数，封顶。
    前端 story_line.js 用同一公式渲染，保证「预计 = 实际」。"""
    beats = max(int(getattr(plot, "cover_beats", 0) or 0), 2)
    return min(beats * CHARS_PER_BEAT, MAX_BRIDGE_WORDS)


_SENT_END = re.compile(r'(?<=[。！？…!?])')


def _split_sentences(text: str) -> list:
    """按句末标点切分句子（保留标点）。"""
    return [s.strip() for s in _SENT_END.split(text) if s.strip()]


class TimelineChapterWriter:
    """
    章节级蓝图写作器 — 桥段驱动的逐章增量写作。

    架构约定（用户明确）：桥段是生成单元。
      · 左→右 = 层次顺序：大纲 → 阶段 → 桥段
      · 上→下 = 故事顺序：沿故事线逐桥段推进
    每个桥段写完累计字数，达到 words_per_chapter 即切成一章；
    桥段 written_chapter 写入 timeline 便于断点续写，章节正文由调用方立即落盘。
    """

    def __init__(self, timeline: BookTimeline, llm_client=None,
                 de_ai_engine=None, reviewer=None,
                 gag_lib=None, plot_lib=None, profile=None):
        self.timeline = timeline
        self.llm = llm_client
        self.de_ai = de_ai_engine
        self.reviewer = reviewer
        self.gag_lib = gag_lib
        self.plot_lib = plot_lib
        self.profile = profile
        self._total_chapters = (max((o.end_chapter for o in timeline.outlines), default=0)
                                if timeline else 0)

    # ── 桥段按故事顺序（上→下）与层次（左→右：大纲→阶段→桥段）排列 ──
    def _story_ordered_plots(self):
        """返回按 (大纲顺序, 阶段顺序, 桥段顺序) 排列的 [(outline, stage, plot), ...]。"""
        outlines = self.timeline.outlines
        order = {o.id: i for i, o in enumerate(outlines)}
        plots = sorted(self.timeline.plots, key=lambda p: (
            order.get(p.outline_id, 99), p.stage_index, p.order))
        result = []
        for p in plots:
            o = next((x for x in outlines if x.id == p.outline_id), None)
            stage = {}
            if o and 0 <= p.stage_index < len(o.stages or []):
                stage = o.stages[p.stage_index]
            result.append({"outline": o, "stage": stage, "plot": p})
        return result

    def _bridge_gag_names(self, item) -> list:
        """解析桥段挂载的笑点 id → 名称（用于注入写作 prompt）。"""
        p = item["plot"]
        gags = []
        for gid in (p.gag_ids or []):
            g = self.gag_lib.get_by_id(gid) if self.gag_lib else None
            gags.append(g.name if g else gid)
        return gags

    def _group_prompt(self, item, chapter_buffer, prev_ending, bridge_text,
                      budget_remaining, character_states=""):
        """短句组生成 prompt：让 LLM 只输出下一小段正文（1-3 个短句）。

        chapter_buffer = 本章已写全文（保证桥段间连贯）
        bridge_text    = 本桥段已写文本（保证桥段内部连贯）
        """
        o = item["outline"]
        stage = item["stage"] or {}
        p = item["plot"]
        stage_name = stage.get("name", "") if isinstance(stage, dict) else ""
        events = stage.get("events", []) if isinstance(stage, dict) else []
        structure = p.template_structure or p.name
        slots_text = ""
        if p.slots:
            slots_text = chr(10).join(
                f"  {s.get('name','?')} = {s.get('default','?')}（可选: {'、'.join(s.get('options',[])[:3])}）"
                for s in p.slots[:4])

        ctx = []
        if prev_ending:
            ctx.append("【上一章结尾】" + prev_ending[-200:])
        if chapter_buffer:
            ctx.append("【本章已写正文】" + chapter_buffer[-2600:])
        if bridge_text:
            ctx.append("【本桥段已写】" + bridge_text[-800:])
        context_text = "\n".join(ctx) if ctx else "（本章开头，尚无前文）"

        gags = self._bridge_gag_names(item)
        themes = p.theme_hints or []

        inject = []
        if gags:
            inject.append(f"笑点模式：{'；'.join(gags[:4])}")
        if themes:
            inject.append(f"内涵线索：{'；'.join(themes[:3])}")
        inject_text = ("\n【加料】\n" + "\n".join(inject)) if inject else ""

        return f"""你是一位专业的网络小说作者，正在逐句续写正文。每轮只输出 1-3 个短句。

【所属大纲】{o.name}（第{o.start_chapter}-{o.end_chapter}章）
【当前阶段】{stage_name}：{'、'.join(events[:4]) if events else '按惯例推进'}
【桥段骨架】{structure}
【变量槽位】{slots_text or '跟随上下文自由发挥'}
{inject_text}

【前文上下文】
{context_text}

【写作要求】
1. 只输出下一小段正文：1-3 个短句（总共约 50-100 个汉字），用句号/感叹号/省略号结尾。
2. 短句要短促有力，符合中文网络小说风格；对话、动作、心理紧凑推进，句子不宜超过 40 字。
3. 必须紧接上文继续，人物、视角、设定保持一致。绝不重开新故事、不更换主角、不回到开头。
4. 围绕桥段骨架自然推进，不写章节标题，不标注"步骤1/2"，不加任何解释性文字。
5. 本桥段还剩约 {budget_remaining} 字预算，控制篇幅，不要一次写太长。"""

    def _write_plot_segment_groups(self, item, chapter_buffer, prev_ending,
                                   budget, character_states=""):
        """生成一个桥段正文：逐短句组调用 LLM，直到桥段字数预算用尽。

        yield (text, words)：text 为 1-3 句的一组（可能是被拆分的短句）。
        组与组之间由调用方用空行连接成独立段落。
        """
        if not self.llm:
            yield f"[桥段:{item['plot'].name} - LLM未配置]", 0
            return
        bridge_text = ""
        bridge_words = 0
        max_groups = max(3, int(budget / 25) + 4)   # 保护：最多调用次数（预算总能达到）
        for _ in range(max_groups):
            remaining = budget - bridge_words
            if remaining <= 0:
                break
            prompt = self._group_prompt(item, chapter_buffer, prev_ending,
                                        bridge_text, remaining, character_states)
            raw = self.llm.call(
                "你是一位专业的网络小说作者。每轮只输出 1-3 个短句正文，不要输出任何多余文字。",
                prompt, temperature=0.8, max_tokens=300)
            text = raw.strip().lstrip('"“')
            if not text:
                break
            words = count_prose_units(text)
            if words <= 0:
                break
            bridge_text += text
            bridge_words += words
            # 过长时按句拆分为独立段落，保证"段落短"的网文要求
            if words > 120:
                for sent in _split_sentences(text):
                    if sent:
                        yield sent, count_prose_units(sent)
            else:
                yield text, words
            if bridge_words >= budget:
                break

    # ── 写一个桥段（新核心：按桥段撰写）──
    def write_bridge_stepwise(self, chapter_num: int, prev_ending: str = "",
                              character_states: str = "",
                              chapter_buffer: str = "", chapter_words: int = 0):
        """写「一个」桥段（生成器）：沿故事顺序取下一个未写桥段。

        事件：
            - bridge_start   ：开始写某桥段（含预计字数 planned_words）
            - group_chunk    ：一组短句正文（流式）
            - bridge_done    ：桥段完成（含本桥段字数/预计、本章累计字数、是否切章）
            - bridge_skip    ：本章已满无法再写（由引擎切章后重试）
            - complete       ：没有剩余桥段可写（全书完成）

        返回（StopIteration.value）：
            {"text", "words", "planned_words", "cut_chapter", "chapter_words"}
        """
        if not self.timeline or not self.timeline.plots:
            yield {"type": "complete", "message": "没有桥段可写"}
            return
        queue = [q for q in self._story_ordered_plots()
                 if (q["plot"].written_chapter or 0) <= 0]
        if not queue:
            yield {"type": "complete", "message": "没有剩余桥段可写（全书完成）"}
            return
        item = queue[0]
        o = item["outline"]
        p = item["plot"]
        stage = item["stage"] or {}
        target = self.timeline.words_per_chapter or 3000
        planned = planned_words(p)
        # 预算裁剪：一章内不超写（最后一桥段可能被压缩以贴合字数）
        budget = min(planned, max(target - chapter_words, 0))
        if budget <= 0:
            yield {"type": "bridge_skip", "plot_id": p.id, "reason": "本章已满"}
            return

        yield {"type": "bridge_start",
               "plot_id": p.id, "plot_name": p.name,
               "outline_id": o.id if o else "", "outline_name": o.name if o else "",
               "stage_name": stage.get("name", "") if isinstance(stage, dict) else "",
               "planned_words": planned}

        seg_parts = []
        seg_words = 0
        for text, words in self._write_plot_segment_groups(
                item, chapter_buffer, prev_ending, budget, character_states):
            seg_parts.append(text)
            seg_words += words
            yield {"type": "group_chunk", "plot_id": p.id, "text": text, "words": words,
                   "bridge_words": seg_words, "planned_words": planned}
        seg = "\n\n".join(seg_parts)

        p.written_chapter = chapter_num
        seg_wc = count_prose_units(seg)
        new_chapter_words = chapter_words + seg_wc
        cut_chapter = new_chapter_words >= target
        yield {"type": "bridge_done",
               "plot_id": p.id, "plot_name": p.name,
               "text": seg, "words": seg_wc, "planned_words": planned,
               "chapter_words": new_chapter_words, "chapter_target": target,
               "cut_chapter": cut_chapter}
        return {
            "text": seg, "words": seg_wc, "planned_words": planned,
            "cut_chapter": cut_chapter, "chapter_words": new_chapter_words,
            "plot_id": p.id, "plot_name": p.name,
            "outline_name": o.name if o else "",
            "gag_ids": p.gag_ids or [],
            "theme_hints": p.theme_hints or [],
        }

    # ── 写一章（兼容：循环桥段直至满章，供批处理/自动跑）──
    def write_chapter_stepwise(self, chapter_num: int,
                               previous_chapter_ending: str = "",
                               character_states: str = "",
                               chapter_buffer: str = "", chapter_words: int = 0):
        """写一章（生成器版）：沿故事顺序逐桥段生成，直到本章字数达标。

        兼容旧接口：yield bridge_start/group_chunk/bridge_done 事件，
        所有桥段完成后 return 章节结果 dict（通过 StopIteration.value 取回）。
        chapter_buffer/chapter_words：进行中章节草稿（按桥段撰写中断后续写）。
        """
        if not self.timeline or not self.timeline.plots:
            return {"text": f"[第{chapter_num}章无桥段可写]",
                    "word_count": 0, "beats": 0, "beat_details": [],
                    "blueprint": {"chapter_title": f"第{chapter_num}章",
                                  "total_chapters": self._total_chapters}}
        target = self.timeline.words_per_chapter or 3000
        buffer = [s for s in (chapter_buffer or "").split("\n\n") if s]
        words = chapter_words or 0
        consumed = []
        while True:
            sub = yield from self.write_bridge_stepwise(
                chapter_num, previous_chapter_ending, character_states,
                chapter_buffer="\n\n".join(buffer), chapter_words=words)
            if sub is None:
                break  # complete / 无剩余桥段
            buffer.append(sub["text"])
            words = sub["chapter_words"]
            consumed.append(sub)
            if sub.get("cut_chapter") or words >= target:
                break

        text = "\n\n".join(buffer)
        wc = count_prose_units(text)
        return {
            "text": text,
            "word_count": wc,
            "beats": 0,
            "beat_details": [],
            "blueprint": {
                "chapter_title": f"第{chapter_num}章",
                "chapter_num": chapter_num,
                "total_chapters": self._total_chapters,
                "outline": consumed[0].get("outline_name", "") if consumed else "",
                "outlines": [c.get("outline_name", "") for c in consumed if c.get("outline_name")],
                "plots": [c.get("plot_name", "") for c in consumed],
                "gags": [g for c in consumed for g in c.get("gag_ids", [])][:6],
                "themes": [t for c in consumed for t in c.get("theme_hints", [])][:4],
            },
        }

    def write_chapter(self, chapter_num: int,
                      previous_chapter_ending: str = "",
                      character_states: str = "",
                      chapter_buffer: str = "", chapter_words: int = 0,
                      on_step=None) -> dict:
        """写一章（同步版）：内部用 write_chapter_stepwise 逐桥段推进，
        若传了 on_step 则每个桥段写前/写后回调一次（供 UI 展示与高亮）。"""
        gen = self.write_chapter_stepwise(
            chapter_num, previous_chapter_ending, character_states,
            chapter_buffer=chapter_buffer, chapter_words=chapter_words)
        result = None
        try:
            while True:
                evt = next(gen)
                if on_step:
                    on_step(evt)
        except StopIteration as si:
            result = si.value
        return result
