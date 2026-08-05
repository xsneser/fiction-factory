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
import re
from collections import OrderedDict

from .timeline import BookTimeline
from core.text_utils import count_prose_units

CHARS_PER_BEAT = 200          # 每个节拍预计写多少个汉字（用于桥段字数规划）
MAX_BRIDGE_WORDS = 1200       # 单个桥段字数上限（与 frontend story_line.js 共用同一公式）
WRITER_MAX_TOKENS = 1600      # 桥段写作输出上限：3-5 短句正文 + flash 推理余量
                              # （flash 先推理再输出，推理过长会吃掉 max_tokens 导致 content 为空）
WRITER_EMPTY_RETRIES = 2      # 写作空响应重试次数（模型偶发返回空内容）
OPENING_WORD_LIMIT = 800      # 炸裂开场：第一章前 800 字
OPENING_MAX_BRIDGES = 3       # 且最多前 3 个桥段


def opening_mode_active(chapter_num: int, chapter_words: int, written_count: int) -> bool:
    """炸裂开场判定：第 1 章、本章未写满 800 字、且已消耗桥段 < 3。"""
    return (chapter_num == 1
            and chapter_words < OPENING_WORD_LIMIT
            and written_count < OPENING_MAX_BRIDGES)


def planned_words(plot) -> int:
    """桥段预计字数 = cover_beats × 每拍字数，封顶。
    前端 story_line.js 用同一公式渲染，保证「预计 = 实际」。"""
    beats = max(int(getattr(plot, "cover_beats", 0) or 0), 2)
    return min(beats * CHARS_PER_BEAT, MAX_BRIDGE_WORDS)


_SENT_END = re.compile(r'(?<=[。！？…!?])')


def _split_sentences(text: str) -> list:
    """按句末标点切分句子（保留标点）。"""
    return [s.strip() for s in _SENT_END.split(text) if s.strip()]


# 连续重复词检测（"底下底下""的的"等 LLM 复读；笑声/拟声叠词白名单放行）
_LAUGH_CHARS = set("哈嘿呵呵嘻哇哼呜啦耶啊咦吼喵咯呀哦哎哟")
_REPEAT_UNIT2 = re.compile(r'([一-鿿]{2})\1')
_REPEAT_CHAR3 = re.compile(r'([一-鿿])\1{2,}')


def has_repeated_token(text: str) -> bool:
    """检测连续重复词/字（如"底下底下""的的的"）。笑声叠词（哈哈哈/呵呵）放行。"""
    if not text:
        return False
    for m in _REPEAT_UNIT2.finditer(text):
        u = m.group(1)
        if u[0] in _LAUGH_CHARS and u[0] == u[1]:
            continue
        return True
    for m in _REPEAT_CHAR3.finditer(text):
        if m.group(1) in _LAUGH_CHARS:
            continue
        return True
    return False


# 疑似错词规则检测（评审实测："婚事先轻轻" 应为 "缓一缓"）—— 命中重写一档
_TYPO_PATTERNS = [
    (re.compile(r'先轻轻(?=[，。！？…；：、\s]|$)'), "「先轻轻」疑为错词（应为「先缓一缓」），请修正后重写本组"),
]


def _typo_issue(text: str) -> str:
    """规则检测疑似错词，返回重写提示（无命中返回空串）。"""
    if not text:
        return ""
    for pat, hint in _TYPO_PATTERNS:
        if pat.search(text):
            return hint
    return ""


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
                 gag_lib=None, plot_lib=None, profile=None,
                 harness=None, gag_injector=None, book_id: str = "",
                 detector_frequency: int = 1, budget_checker=None):
        self.timeline = timeline
        self.llm = llm_client
        self.de_ai = de_ai_engine
        self.reviewer = reviewer
        self.gag_lib = gag_lib
        self.plot_lib = plot_lib
        self.profile = profile
        self.harness = harness          # PromptHarness：集中式提示词模板（可为 None）
        self.gag_injector = gag_injector  # GagInjector：灵机一动探测环（可为 None）
        self.book_id = book_id
        self.detector_frequency = detector_frequency
        self.budget_checker = budget_checker  # 预算门控：callable 返回剩余预算（元），None=不限制
        self.review_hint = ""  # 上一章规则审查（reviewer）未过的修复提示：一次性注入首个桥段，用完即清
        # 本章输入 prompt 累计（供成本计量）；跨桥段累计、跨章重置
        self._input_chapter = 0
        self._input_texts: list = []
        self._total_chapters = (max((o.end_chapter for o in timeline.outlines), default=0)
                                if timeline else 0)

    # ── 桥段按故事顺序（上→下）与层次（左→右：大纲→阶段→桥段）排列 ──
    def _threaded_ordered_plots(self):
        """按叙事线程轮流排列桥段（主线加权 2:1，副线/伏笔线各 1）。

        线程内按 (大纲, stage, order, thread_seq) 排序；主线每轮取 2 个、其他线程各 1 个。
        向后兼容：全部 thread_id="主线" 时退化为原严格顺序（单组顺序取）。
        """
        outlines = self.timeline.outlines
        o_pos = {o.id: i for i, o in enumerate(outlines)}

        def base_key(p):
            return (o_pos.get(p.outline_id, 99), p.stage_index, p.order,
                    getattr(p, "thread_seq", 0) or 0)

        groups = OrderedDict()
        for p in sorted(self.timeline.plots, key=base_key):
            tid = (getattr(p, "thread_id", "") or "主线")
            groups.setdefault(tid, []).append(p)

        # 线程顺序：主线恒首，其余按各自首个 plot 的 base_key 排（稳定可复现）
        thread_order = sorted((t for t in groups if t != "主线"),
                              key=lambda t: base_key(groups[t][0]))
        if "主线" in groups:
            thread_order = ["主线"] + thread_order

        weights = {"主线": 2}
        idx = {t: 0 for t in groups}
        result = []
        remaining = True
        while remaining:
            remaining = False
            for t in thread_order:
                w = weights.get(t, 1)
                for _ in range(w):
                    if idx[t] < len(groups[t]):
                        result.append(groups[t][idx[t]])
                        idx[t] += 1
                        remaining = True
        return result

    def _story_ordered_plots(self):
        """返回按叙事线程轮流排列的 [(outline, stage, plot), ...]。

        线程穿插（主线/副线/伏笔线轮流取）保证第 1 章即多线并进；
        收局槽位（resolves_plot_id 非空）天然落在线程后段、被其他线程穿插。
        """
        outlines = self.timeline.outlines
        plots = self._threaded_ordered_plots()
        result = []
        for p in plots:
            o = next((x for x in outlines if x.id == p.outline_id), None)
            stage = {}
            if o and 0 <= p.stage_index < len(o.stages or []):
                stage = o.stages[p.stage_index]
            item = {"outline": o, "stage": stage, "plot": p,
                    "is_payoff": bool(getattr(p, "resolves_plot_id", "")),
                    "resolver_name": self._find_resolver_name(p.id)}
            result.append(item)
        return result

    def _find_resolver_name(self, plot_id: str) -> str:
        """返回引用 plot_id 的收局桥段名（设局提示用），无则空。"""
        if not self.timeline:
            return ""
        for q in self.timeline.plots:
            if getattr(q, "resolves_plot_id", "") == plot_id:
                return getattr(q, "name", "")
        return ""

    def _bridge_gag_names(self, item) -> list:
        """解析桥段挂载的笑点 id → 名称（用于注入写作 prompt）。"""
        p = item["plot"]
        gags = []
        for gid in (p.gag_ids or []):
            g = self.gag_lib.get_by_id(gid) if self.gag_lib else None
            gags.append(g.name if g else gid)
        return gags

    def _group_prompt(self, item, chapter_buffer, prev_ending, bridge_text,
                      budget_remaining, character_states="",
                      summaries_context="", inspiration_hint="",
                      is_opening=False, chapter_num=0):
        """短句组生成 prompt：让 LLM 只输出下一小段正文（3-5 个短句）。

        有 harness 时委托 render_bridge_prompt（集中式模板：书级设定卡 + 语义摘要
        + 角色状态 + 灵机一动 + 炸裂开场）；无 harness 时回退极简模板（仅单测/兜底用，生产恒走 harness）。
        """
        if self.harness:
            review_hint = self.review_hint or ""
            if review_hint:
                self.review_hint = ""   # 一次性：只在上一章未过审查后的首个桥段注入，用完即清
            return self.harness.render_bridge_prompt(
                item, chapter_buffer, prev_ending, bridge_text, budget_remaining,
                character_states=character_states,
                summaries_context=summaries_context,
                inspiration_hint=inspiration_hint,
                is_opening=is_opening,
                review_hint=review_hint,
                chapter_num=chapter_num)

        # ── 回退：无 harness 时极简兜底（只保上下文+核心约束，防止双份模板漂移）──
        p = item["plot"]
        ctx = []
        if prev_ending:
            ctx.append("【上一章结尾】" + prev_ending[-200:])
        if chapter_buffer:
            ctx.append("【本章已写正文】" + chapter_buffer[-2000:])
        if bridge_text:
            ctx.append("【本桥段已写】" + bridge_text[-600:])
        context_text = "\n".join(ctx) if ctx else "（本章开头，尚无前文）"

        opening_block = ""
        if is_opening:
            try:
                from .prompt_harness import OPENING_MODE_RULES
                opening_block = OPENING_MODE_RULES + "\n\n"
            except Exception:
                opening_block = ""

        return f"""你是一位专业的中文网络小说作者，正在逐段续写正文。每轮只输出 3-5 个短句（约 150-250 个汉字），一句一行。

{opening_block}【桥段】{p.name}
【前文上下文】
{context_text}

【写作要求】
1. 画面优先，用动作、对话、感官细节推进；短句为基干、句长长短交错。
2. 每组至少含一句对话或一个动作；组尾留一个"接下来会怎样"的悬念。
3. 严禁出现：然而、不禁、仿佛、似乎、瞬间、顿时、缓缓、微微、眼中闪过、心中一动、微微一笑、嘴角勾起、与此同时、就在这时。
4. 只输出正文，不写标题、不加解释。本桥段还剩约 {budget_remaining} 字预算，控制篇幅。"""

    def _write_plot_segment_groups(self, item, chapter_buffer, prev_ending,
                                   budget, character_states="", summaries_context="",
                                   is_opening=False, chapter_num=0):
        """生成一个桥段正文：逐短句组调用 LLM，直到桥段字数预算用尽。

        yield (text, words)：text 为 1-3 句的一组（可能是被拆分的短句）。
        组与组之间由调用方用空行连接成独立段落。
        每写完一组跑一次"灵机一动"探测器：命中 → 把提示注入下一组写作 prompt。
        """
        if not self.llm:
            yield "group", f"[桥段:{item['plot'].name} - LLM未配置]", 0
            return
        bridge_text = ""
        bridge_words = 0
        max_groups = max(4, int(budget / 100) + 4)  # 保护：最多调用次数（每轮约150-250字，实际4-5次即可达到）

        # 探测环状态：候选笑点池每桥段算一次；近 3 组正文供探测器读
        pool = []
        humor_style = ""
        if self.gag_injector:
            pool = self.gag_injector.prescreen_pool(item["plot"], self.book_id)
            humor_style = self._profile_humor_style()
        last_groups = []
        pending_inspiration = ""

        for group_no in range(1, max_groups + 1):
            remaining = budget - bridge_words
            if remaining <= 0:
                break
            prompt = self._group_prompt(item, chapter_buffer, prev_ending,
                                        bridge_text, remaining, character_states,
                                        summaries_context=summaries_context,
                                        inspiration_hint=pending_inspiration,
                                        is_opening=is_opening,
                                        chapter_num=chapter_num)
            pending_inspiration = ""  # 命中只注入下一组，用完即清
            # 空响应重试：flash 先推理再输出，推理过长会吃掉 max_tokens 导致 content 为空
            retry_hint = "上一组输出为空，请重新输出本组正文。"
            text = ""
            for attempt in range(WRITER_EMPTY_RETRIES + 1):
                p_attempt = prompt
                if attempt > 0:
                    p_attempt = prompt + "\n【重写提示】" + retry_hint
                self._input_texts.append(p_attempt)
                raw = self.llm.call(
                    ("你是一位专业的中文网络小说作者，擅长对话、动作驱动的快节奏网文，正在逐段续写一章正文。"
                     "每轮只输出 3-5 个句子（约 150-250 个汉字），只输出正文，不要任何解释。"
                     "文笔铁律：1) 画面优先，用动作、对话、感官细节推进，拒绝形容词堆砌和抽象抒情；"
                     "2) 短句为基干、一句一行，句长需长短交错（8-15字为主、穿插25-45字），避免全文句式单一；"
                     "3) 对话独立成段并带神态/动作，避免连续纯叙述；"
                     "4) 视角始终锁定主角，不切换；"
                     "5) 严禁使用：然而、不禁、仿佛、似乎、瞬间、顿时、缓缓、微微、眼中闪过、心中一动、微微一笑、嘴角勾起、与此同时、就在这时。"),
                    p_attempt, temperature=0.7, max_tokens=WRITER_MAX_TOKENS)
                text = (raw or "").strip().lstrip('"“')
                if not text:
                    retry_hint = "上一组输出为空，请重新输出本组正文。"
                    continue          # 空响应 → 重试
                if has_repeated_token(text) and attempt < WRITER_EMPTY_RETRIES:
                    retry_hint = "上一组出现连续重复词，请完全重写本组，任何词不得连续重复两次以上。"
                    continue          # 连续重复词 → 仅重试（不打断续写）
                typo_hint = _typo_issue(text)
                if typo_hint and attempt < WRITER_EMPTY_RETRIES:
                    retry_hint = typo_hint
                    continue          # 疑似错词 → 重写一档
                break
            if not text:
                break
            words = count_prose_units(text)
            if words <= 0:
                break
            bridge_text += text
            bridge_words += words
            last_groups.append(text)
            last_groups = last_groups[-3:]
            # 过长时按句拆分为独立段落，保证"段落短"的网文要求
            if words > 120:
                for sent in _split_sentences(text):
                    if sent:
                        yield "group", sent, count_prose_units(sent)
            else:
                yield "group", text, words
            # 灵机一动探测：本组写完、预算未用尽、有探测器与候选池时运行
            if (self.detector_frequency
                    and group_no % self.detector_frequency == 0
                    and remaining - words > 100
                    and self.gag_injector and pool):
                recent = "\n".join(last_groups[-2:])
                hit = self.gag_injector.detect(item, recent, humor_style, pool)
                if hit.get("has_opportunity"):
                    pending_inspiration = self.gag_injector.build_inspiration_hint(hit, pool)
                    yield "gag_hit", hit, 0
            if bridge_words >= budget:
                break

    def _profile_humor_style(self) -> str:
        """从 profile（PenNameProfile 或 dict）取主角喜剧声线。"""
        if self.profile is None:
            return ""
        if hasattr(self.profile, "style_fingerprint"):
            fp = self.profile.style_fingerprint or {}
        else:
            fp = self.profile.get("style_fingerprint", {}) or {}
        return str(fp.get("humor_style", "") or "") if isinstance(fp, dict) else ""

    # ── 写一个桥段（新核心：按桥段撰写）──
    def write_bridge_stepwise(self, chapter_num: int, prev_ending: str = "",
                              character_states: str = "",
                              chapter_buffer: str = "", chapter_words: int = 0,
                              summaries_context: str = ""):
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
        if chapter_num != self._input_chapter:
            self._input_chapter = chapter_num
            self._input_texts = []
        o = item["outline"]
        p = item["plot"]
        stage = item["stage"] or {}
        # 炸裂开场：第 1 章前 800 字 / 前 3 个桥段命中开场模式
        written_count = len(self._story_ordered_plots()) - len(queue)
        is_opening = opening_mode_active(chapter_num, chapter_words, written_count)
        target = self.timeline.words_per_chapter or 3000
        planned = planned_words(p)
        # 预算裁剪：一章内不超写（最后一桥段可能被压缩以贴合字数）
        budget = min(planned, max(target - chapter_words, 0))
        if budget <= 0:
            yield {"type": "bridge_skip", "plot_id": p.id, "reason": "本章已满"}
            return

        # 预算门控：LLM 费用预算耗尽时跳过本桥段（engine 侧由 cost_tracker.remaining() 提供）
        if self.budget_checker is not None and self.budget_checker() <= 0:
            yield {"type": "bridge_skip", "plot_id": p.id,
                   "code": "budget_exhausted",
                   "reason": "预算耗尽，暂停写作（可调高单书预算后继续）"}
            return

        yield {"type": "bridge_start",
               "plot_id": p.id, "plot_name": p.name,
               "outline_id": o.id if o else "", "outline_name": o.name if o else "",
               "stage_name": stage.get("name", "") if isinstance(stage, dict) else "",
               "planned_words": planned,
               "hook_points": list(getattr(p, "hook_points", None) or [])}

        seg_parts = []
        seg_words = 0
        for kind, text, words in self._write_plot_segment_groups(
                item, chapter_buffer, prev_ending, budget, character_states,
                summaries_context=summaries_context, is_opening=is_opening,
                chapter_num=chapter_num):
            if kind == "gag_hit":
                yield {"type": "gag_hit",
                       "gag_ids": text.get("gag_ids", []),
                       "reason": text.get("reason", ""),
                       "deploy_hint": text.get("deploy_hint", "")}
                continue
            seg_parts.append(text)
            seg_words += words
            yield {"type": "group_chunk", "plot_id": p.id, "text": text, "words": words,
                   "bridge_words": seg_words, "planned_words": planned}
        seg = "\n\n".join(seg_parts)
        seg_wc = count_prose_units(seg)
        # 空响应保护：桥段没写出内容时**不消耗**（不置 written_chapter），
        # 本章到此结束、下一章重试该桥段，避免全书被空桥段吞掉导致"章节 0 字"。
        if seg_wc <= 0:
            yield {"type": "bridge_skip", "plot_id": p.id,
                   "reason": "LLM 空响应未产出内容，保留桥段待重试"}
            return None
        p.written_chapter = chapter_num
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
            "input_text": "\n".join(self._input_texts),
        }

    # ── 写一章（兼容：循环桥段直至满章，供批处理/自动跑）──
    def write_chapter_stepwise(self, chapter_num: int,
                               previous_chapter_ending: str = "",
                               character_states: str = "",
                               chapter_buffer: str = "", chapter_words: int = 0,
                               summaries_context: str = ""):
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
                chapter_buffer="\n\n".join(buffer), chapter_words=words,
                summaries_context=summaries_context)
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
            "input_text": "\n".join(c.get("input_text", "") for c in consumed if c.get("input_text")),
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
                      summaries_context: str = "",
                      on_step=None) -> dict:
        """写一章（同步版）：内部用 write_chapter_stepwise 逐桥段推进，
        若传了 on_step 则每个桥段写前/写后回调一次（供 UI 展示与高亮）。"""
        gen = self.write_chapter_stepwise(
            chapter_num, previous_chapter_ending, character_states,
            chapter_buffer=chapter_buffer, chapter_words=chapter_words,
            summaries_context=summaries_context)
        result = None
        try:
            while True:
                evt = next(gen)
                if on_step:
                    on_step(evt)
        except StopIteration as si:
            result = si.value
        return result
