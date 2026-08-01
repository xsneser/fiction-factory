"""
蓝图式写作引擎 v3 — 桥段驱动的逐章增量写作

架构约定（用户明确）：
  · 左→右 = 层次顺序：大纲 → 阶段 → 桥段
  · 上→下 = 故事顺序：沿故事线逐桥段推进
  · 桥段是生成单元：每个桥段写完后累计字数，满 words_per_chapter 即切成一章
  · 每段/每章写完立即落盘（桥段 written_chapter 写入 timeline，章节正文由 engine 保存）

旧"整本先写全文再分章"（BlueprintWritingPipeline）已废弃删除。
"""
import json
import re

from .timeline import BookTimeline, OutlineSlot, PlotSlot

class TimelineChapterWriter:
    """
    章节级蓝图写作器 — 桥段驱动的逐章增量写作。

    架构约定（用户明确）：桥段是生成单元。
      · 左→右 = 层次顺序：大纲 → 阶段 → 桥段
      · 上→下 = 故事顺序：沿故事线逐桥段推进
    每个桥段写完后累计字数，达到 words_per_chapter 即切成一章；
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

    def _plot_segment_prompt(self, item, chapter_num, prev_ending):
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
        wc_target = min(max(p.cover_beats, 2) * 400, 2500)
        prompt = f"""按桥段骨架写一段网络小说正文。

【所属大纲】{o.name}（第{o.start_chapter}-{o.end_chapter}章，本章第{chapter_num}章）
【当前阶段】{stage_name}：{'、'.join(events[:4]) if events else '按惯例推进'}
【桥段骨架】{structure}
【变量槽位】{slots_text or '跟随上下文自由发挥'}

【要求】
1. 目标 {wc_target} 字左右，围绕桥段骨架自然推进，不标注"步骤1/2"
2. 不写章节标题，正文直接开始
3. 承接上文（若提供），人物与风格一致"""
        if prev_ending:
            prompt += chr(10) + chr(10) + "【上文结尾】" + prev_ending[:150]
        return prompt

    def _write_plot_segment(self, item, chapter_num, prev_ending):
        """写一个桥段的正文（生成单元）。"""
        if not self.llm:
            return f"[桥段:{item['plot'].name} - LLM未配置]"
        prompt = self._plot_segment_prompt(item, chapter_num, prev_ending)
        raw = self.llm.call("你是一位专业的网络小说作者。", prompt,
                            temperature=0.8, max_tokens=min(8192, max(1536, 2800 * 2)))
        return raw.strip()

    def _inject_gags(self, text, item):
        """桥段写完后，注入该桥段的笑点/内涵。"""
        p = item["plot"]
        gags, themes = [], []
        for gid in (p.gag_ids or []):
            g = self.gag_lib.get_by_id(gid) if self.gag_lib else None
            gags.append(g.name if g else gid)
        themes = p.theme_hints or []
        if not gags and not themes:
            return text
        if not self.llm:
            return text
        prompt = f"""在以下网络小说正文中，自然地注入笑点和内涵线索。不要大改原文结构，在合适位置插入/微调 2-3 处即可。

【正文】
{text[:2000]}

【笑点模式】
{'；'.join(gags[:4]) or '无特殊要求'}

【内涵提示】
{'；'.join(themes[:3]) or '无'}

【要求】
1. 笑点要自然，不能生硬插入
2. 返回完整修改后的正文
3. 返回 JSON：{{"text": "修改后全文"}}"""
        try:
            from core.llm_client import extract_json
            raw = self.llm.call("你是专业的网文编辑。只返回JSON。", prompt,
                                temperature=0.5, max_tokens=min(4096, len(text) * 2))
            data = json.loads(extract_json(raw))
            new_text = data.get("text", text)
            return new_text if len(new_text) > len(text) * 0.5 else text
        except Exception:
            return text

    def write_chapter(self, chapter_num: int,
                      previous_chapter_ending: str = "",
                      character_states: str = "") -> dict:
        """写一章：沿故事顺序逐桥段生成，每写完一个桥段累计字数，
        达到 words_per_chapter 即切章；返回后由调用方立即落盘。

        返回 dict:
            {"text": str, "word_count": int, "beats": int,
             "beat_details": list, "blueprint": dict}
        """
        if not self.timeline or not self.timeline.plots:
            return {"text": f"[第{chapter_num}章无桥段可写]",
                    "word_count": 0, "beats": 0, "beat_details": [],
                    "blueprint": {"chapter_title": f"第{chapter_num}章",
                                  "total_chapters": self._total_chapters}}

        target = self.timeline.words_per_chapter or 3000
        # 未写桥段（支持断点续写）
        queue = [q for q in self._story_ordered_plots()
                 if (q["plot"].written_chapter or 0) <= 0]
        if not queue:
            return {"text": f"[第{chapter_num}章无剩余桥段可写]",
                    "word_count": 0, "beats": 0, "beat_details": [],
                    "blueprint": {"chapter_title": f"第{chapter_num}章",
                                  "total_chapters": self._total_chapters}}

        buffer = []
        words = 0
        consumed = []
        for item in queue:
            seg = self._write_plot_segment(item, chapter_num, previous_chapter_ending)
            seg = self._inject_gags(seg, item)
            item["plot"].written_chapter = chapter_num
            consumed.append(item)
            if seg:
                buffer.append(seg)
                words += len(re.findall(r'[一-鿿]', seg))
            if words >= target:
                break  # 满章即切

        text = chr(10).join(buffer)
        wc = len(re.findall(r'[一-鿿]', text))
        return {
            "text": text,
            "word_count": wc,
            "beats": 0,
            "beat_details": [],
            "blueprint": {
                "chapter_title": f"第{chapter_num}章",
                "chapter_num": chapter_num,
                "total_chapters": self._total_chapters,
                "outline": consumed[0]["outline"].name if consumed and consumed[0]["outline"] else "",
                "outlines": [c["outline"].name for c in consumed if c["outline"]],
                "plots": [c["plot"].name for c in consumed],
                "gags": [g for c in consumed for g in (c["plot"].gag_ids or [])][:6],
                "themes": [t for c in consumed for t in (c["plot"].theme_hints or [])][:4],
            },
        }
