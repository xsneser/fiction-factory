"""
蓝图式写作引擎 v3 — 桥段驱动的逐章增量写作

架构约定（用户明确）：
  · 左→右 = 层次顺序：弧 → 阶段 → 桥段
  · 上→下 = 故事顺序：沿故事线逐桥段推进
  · 桥段是生成单元：每个桥段写完后累计字数，满 words_per_chapter 即切成一章
  · 短句组生成：每个桥段内逐「短句组」调用 LLM（每次 1-3 个短句，约 50-100 字），
    组与组之间用空行分隔成独立段落（网文短段风格）；每次调用都携带
    「本章已写全部前文 + 上一章结尾 + 本桥段已写」，保证桥段之间故事连贯。
  · 每段/每章写完立即落盘（桥段 written_chapter 写入 storyline，章节正文由 engine 保存）

旧"整本先写全文再分章"（BlueprintWritingPipeline）已废弃删除。
"""
import json
import re
from collections import OrderedDict

from .storyline import BookStoryline
from .style_ban import LANGUAGE_DISCIPLINE, build_style_ban_prompt
from core.text_utils import count_prose_units

CHARS_PER_BEAT = 200          # 每个节拍预计写多少个汉字（用于桥段字数规划）
MAX_BRIDGE_WORDS = 1200       # 节拍制单个桥段字数上限（与 frontend story_line.js 共用同一公式）
MAX_PLAN_WORDS = 3000         # agent 直接给的目标字数 plot.words 的上限（≈一章上限级，防单桥虚高）
WRITER_MAX_TOKENS = 1600      # 桥段写作输出上限：3-5 短句正文 + flash 推理余量
                              # （flash 先推理再输出，推理过长会吃掉 max_tokens 导致 content 为空）
WRITER_EMPTY_RETRIES = 2      # 写作空响应重试次数（模型偶发返回空内容）
REPAIR_STALL_THRESHOLD = 3    # 连续失败阈值：空响应/重写无改善累计达此值 → repair_stalled 主动停
OPENING_WORD_LIMIT = 800      # 炸裂开场：第一章前 800 字
OPENING_MAX_BRIDGES = 3       # 且最多前 3 个桥段

WRITER_SYSTEM = ("你是一位专业的中文网络小说作者，擅长对话、动作驱动的快节奏网文，正在逐段续写一章正文。"
                 "每轮只输出 3-5 个句子（约 150-250 个汉字），只输出正文，不要任何解释。"
                 "文笔铁律：1) 画面优先，用动作、对话、感官细节推进，拒绝形容词堆砌和抽象抒情；"
                 "2) 短句为基干、一句一行，句长需长短交错（8-15字为主、穿插25-45字），避免全文句式单一；"
                 "3) 对话独立成段并带神态/动作，避免连续纯叙述；"
                 "4) 视角始终锁定主角，不切换；"
                 "5) 严禁使用：然而、不禁、仿佛、似乎、瞬间、顿时、缓缓、微微、眼中闪过、心中一动、微微一笑、嘴角勾起、与此同时、就在这时。"
                 + build_style_ban_prompt()
                 + LANGUAGE_DISCIPLINE)

SELF_CHECK_ENABLED = True     # 有界自评总开关（设计文档 §2.3 设计 B）：每短句组 flash 自检
SELF_CHECK_THRESHOLD = 6      # 自评分 <6 或 has_rewrite=true → 触发一次重写
SELF_CHECK_MAX_TOKENS = 2048  # 自检输出小（≤150字），留 flash 推理余量即可


def opening_mode_active(chapter_num: int, chapter_words: int, written_count: int) -> bool:
    """炸裂开场判定：第 1 章、本章未写满 800 字、且已消耗桥段 < 3。"""
    return (chapter_num == 1
            and chapter_words < OPENING_WORD_LIMIT
            and written_count < OPENING_MAX_BRIDGES)


def planned_words(plot) -> int:
    """桥段预计字数（规划/预估，实际正文仍按书写自然浮动）。

    优先 agent 按内容浓淡给的目标字数 plot.words（clamp [200, MAX_PLAN_WORDS]）；
    未给（0/None）回退节拍制 cover_beats × CHARS_PER_BEAT 封顶 MAX_BRIDGE_WORDS。
    前端 story_line.js plannedWords() 与 agent_tools validate 用同一口径（words 覆盖 + beat 兜底）。"""
    words = int(getattr(plot, "words", 0) or 0)
    if words > 0:
        return max(200, min(words, MAX_PLAN_WORDS))
    beats = max(int(getattr(plot, "cover_beats", 0) or 0), 2)
    return min(beats * CHARS_PER_BEAT, MAX_BRIDGE_WORDS)


# 句尾 = 句末标点（。！？…!?，连续标点如「……」整体）+ 可选右引号（"」』’），
# 防「草！」" 在引号处被切开成孤立引号段（引号孤行 bug，archive 审查报告实测复现）
_SENT_RE = re.compile(r'[^。！？…!?]*[。！？…!?]+["」』’]*')


def _split_sentences(text: str) -> list:
    """按句末标点切分句子（保留标点与收尾右引号）。"""
    if not text:
        return []
    parts, consumed = [], 0
    for m in _SENT_RE.finditer(text):
        seg = m.group(0).strip()
        consumed += len(m.group(0))
        if seg:
            parts.append(seg)
    # 尾部无句末标点的残留（对话被截断等）单独成句
    if consumed < len(text):
        tail = text[consumed:].strip()
        if tail:
            parts.append(tail)
    return parts


# 未自然收束判定（竞品借鉴：deep-novel-system「截断检测续写」）：末字符须为句末标点或自然段尾
_END_CHARS = set("。！？…!?\"”」』’\n")


def _ends_naturally(text: str) -> bool:
    """文本末尾是否自然收束（末字符在句末标点/右引号/换行内）。"""
    t = (text or "").rstrip(" \t")  # 只去空格不去换行：段尾换行也是自然收束
    return bool(t) and t[-1] in _END_CHARS


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


class StorylineChapterWriter:
    """
    章节级蓝图写作器 — 桥段驱动的逐章增量写作。

    架构约定（用户明确）：桥段是生成单元。
      · 左→右 = 层次顺序：弧 → 阶段 → 桥段
      · 上→下 = 故事顺序：沿故事线逐桥段推进
    每个桥段写完累计字数，达到 words_per_chapter 即切成一章；
    桥段 written_chapter 写入 storyline 便于断点续写，章节正文由调用方立即落盘。
    """

    def __init__(self, storyline: BookStoryline, llm_client=None,
                 de_ai_engine=None, reviewer=None,
                 gag_lib=None, plot_lib=None, profile=None,
                 harness=None, gag_injector=None, book_id: str = "",
                 detector_frequency: int = 1, budget_checker=None):
        self.storyline = storyline
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
        self._repair_failures = 0  # 连续失败计数（空响应/重写无改善）；达 REPAIR_STALL_THRESHOLD → repair_stalled
        self.budget_checker = budget_checker  # 预算门控：callable 返回剩余预算（元），None=不限制
        self.review_hint = ""  # 上一章规则审查（reviewer）未过的修复提示：一次性注入首个桥段，用完即清
        # 本章输入 prompt 累计（供成本计量）；跨桥段累计、跨章重置
        self._input_chapter = 0
        self._input_texts: list = []
        # 章节轴→字数轴：总章数由桥段预计字数推导（ceil(总字数/每章字数)），不再读弧的 end_chapter
        self._total_chapters = 0
        if storyline:
            wpc = getattr(storyline, "words_per_chapter", None) or 3000
            _w = sum(planned_words(p) for p in storyline.plots) if storyline.plots else 0
            self._total_chapters = max(1, (_w + wpc - 1) // wpc)

    # ── 桥段按故事顺序（上→下）与层次（左→右：弧→阶段→桥段）排列 ──
    def _threaded_ordered_plots(self):
        """按叙事线程轮流排列桥段（主线加权 2:1，副线/伏笔线各 1）。

        线程内按 (弧, stage, order, thread_seq) 排序；主线每轮取 2 个、其他线程各 1 个。
        向后兼容：全部 thread_id="主线" 时退化为原严格顺序（单组顺序取）。
        """
        outlines = self.storyline.outlines
        o_pos = {o.id: i for i, o in enumerate(outlines)}

        def base_key(p):
            return (o_pos.get(p.outline_id, 99), p.stage_index, p.order,
                    getattr(p, "thread_seq", 0) or 0)

        groups = OrderedDict()
        for p in sorted(self.storyline.plots, key=base_key):
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
        outlines = self.storyline.outlines
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
        if not self.storyline:
            return ""
        for q in self.storyline.plots:
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

    def _chapter_participants(self, item) -> str:
        """本章/本弧参与者：当前弧内所有桥段出场角色并集（紧凑名串，≤6 个）。

        竞品借鉴：AI-NWA participant_subset——「按本章出场角色精准筛选」，
        避免逐桥段重复注入、也覆盖本弧后续才出场的人。免费规则，零 LLM。
        """
        if not self.storyline:
            return ""
        o = item.get("outline") if isinstance(item, dict) else None
        if not o:
            return ""
        seen, out = set(), []
        for q in getattr(self.storyline, "plots", None) or []:
            if getattr(q, "outline_id", "") != o.id:
                continue
            for r in (getattr(q, "roles", None) or []):
                r = str(r or "").strip()
                if r and r not in seen:
                    seen.add(r)
                    out.append(r)
        return "、".join(out[:6]) if out else ""

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
                chapter_num=chapter_num,
                chapter_participants=self._chapter_participants(item))

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

    def _complete_unnatural_end(self, text: str, item, max_continues: int = 2) -> str:
        """桥段组末尾未自然收束（被 max_tokens 截断）时，追加续写直到自然收尾。

        竞品借鉴：deep-novel-system「截断检测续写（append≤3次）」；受次数硬上限与
        预算约束（追加内容会计入 bridge_words），仍不收束则原样返回，不无限循环。
        """
        for _ in range(max_continues):
            if _ends_naturally(text):
                break
            cont_prompt = (
                "上一段末尾句子被截断、尚未收尾。请紧接上一段最后一个字继续写，"
                "只补到一句完整收尾（含句末标点），不要另起新内容、不要重复已写内容。\n"
                "上一段结尾：…" + text[-60:])
            self._input_texts.append(cont_prompt)
            try:
                raw = self.llm.call(WRITER_SYSTEM, cont_prompt,
                                    temperature=0.6, max_tokens=WRITER_MAX_TOKENS)
            except Exception:
                break
            extra = (raw or "").strip().lstrip('"“')
            if not extra:
                break
            text += extra
        return text

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
                    WRITER_SYSTEM,
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
                # 空响应保护失败：连续达阈值 → 主动停（对齐 AI-NWA「遇错主动停」），不静默吞掉
                self._repair_failures += 1
                if self._repair_failures >= REPAIR_STALL_THRESHOLD:
                    yield ("repair_stalled",
                           f"连续 {self._repair_failures} 次未产出正文（空响应），暂停写作，请检查后继续", 0)
                    return
                continue  # 未达阈值：换下一组重试（跨组累计失败次数）
            # 未自然收束续写（deep-novel-system append≤N）：组尾被截断时补到自然收尾
            if not _ends_naturally(text):
                text = self._complete_unnatural_end(text, item)
            words = count_prose_units(text)
            if words <= 0:
                self._repair_failures += 1
                if self._repair_failures >= REPAIR_STALL_THRESHOLD:
                    yield ("repair_stalled",
                           f"连续 {self._repair_failures} 次未产出正文，暂停写作，请检查后继续", 0)
                    return
                continue
            # 有界自评（P3）：flash 自检 0-10 分 + 是否重写，低分/标记重写 → 重写 1 次（硬上限）；
            # 连续多次重写无改善 → repair_stalled 主动停（而非静默回落原文），保持成本有界
            repair_ok = True
            if SELF_CHECK_ENABLED:
                verdict = self._self_check_group(text, item)
                if verdict.get("rewrite") or int(verdict.get("score", 10) or 10) < SELF_CHECK_THRESHOLD:
                    rewritten = self._rewrite_group_once(
                        text, item, verdict.get("reason") or "质量未达标",
                        verdict.get("quote", ""))
                    if rewritten and rewritten != text:
                        text = rewritten
                        words = count_prose_units(text)
                        if words <= 0:
                            repair_ok = False
                    else:
                        repair_ok = False  # 重写无改善 → 记为一次修复失败
            if repair_ok:
                self._repair_failures = 0
            else:
                self._repair_failures += 1
                if self._repair_failures >= REPAIR_STALL_THRESHOLD:
                    yield ("repair_stalled",
                           f"连续 {self._repair_failures} 次修复无改善，暂停写作，请检查后继续", 0)
                    return
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

    # ── 有界自评（设计文档 §2.3 设计 B）：flash 自检 + 限 1 次重写 ──
    def _self_check_group(self, text: str, item) -> dict:
        """flash 自检：0-10 分 + 是否需重写 + 一句理由 + 问题句引文（quote）。

        竞品借鉴：OpenNovel anchored hot-fix——重写时只修引文所在句子，不误伤整组。
        失败返回高分放行，不打断写作。
        """
        if not self.llm or not text:
            return {"score": 10, "rewrite": False, "reason": "", "quote": ""}
        p = item.get("plot")
        bridge_name = getattr(p, "name", "") if p else ""
        prompt = (f"你是小说审校编辑。为下面这段网文正文打分（这是「{bridge_name}」桥段的一小段，"
                  f"约150-250字）。\n\n【正文】\n{text}\n\n"
                  f"【检查要点】1) 有无AI腔/模板词（然而/不禁/仿佛/瞬间/顿时/缓缓/微微等）；"
                  f"2) 是否画面感强、靠动作/对话推进；3) 是否与桥段目标契合；"
                  f"4) 有无重复啰嗦/多角色同质化。\n"
                  f"若 rewrite=true，必须指出具体问题句（quote：从正文摘录 20-50 字原文，"
                  f"供定点重写）。\n"
                  f"返回 JSON：{{\"score\": 0-10的整数, \"rewrite\": true/false, "
                  f"\"reason\": \"一句话理由\", \"quote\": \"问题句原文（20-50字）\"}}")
        try:
            from core.llm_client import extract_json
            raw = self.llm.call(
                "你是资深网文审校编辑。只返回JSON，不要任何额外文字。", prompt,
                temperature=0.2, max_tokens=SELF_CHECK_MAX_TOKENS)
            data = json.loads(extract_json(raw))
            try:
                score = int(data.get("score", 10) or 10)
            except (TypeError, ValueError):
                score = 10
            return {"score": max(0, min(10, score)),
                    "rewrite": bool(data.get("rewrite", False)),
                    "reason": str(data.get("reason", ""))[:80],
                    "quote": str(data.get("quote", ""))[:50]}
        except Exception:
            return {"score": 10, "rewrite": False, "reason": "", "quote": ""}

    def _rewrite_group_once(self, text: str, item, reason: str, quote: str = "") -> str:
        """有界自评重写：带自评提示重写一次（硬上限 1 次），失败/反而有重复词则返回原文。

        竞品借鉴：OpenNovel anchored hot-fix——有 quote 时只重写引文所在句子，
        保留其余部分（不误伤整组）；无 quote 时整组重写（向后兼容）。
        """
        p = item.get("plot")
        bridge_name = getattr(p, "name", "") if p else ""
        try:
            if quote:
                user = (f"上一段正文经审校未达标：{reason}\n\n"
                        f"【原正文】\n{text}\n\n"
                        f"【问题句引文】{quote}\n\n"
                        f"请只重写引文所在的那一句/几句，保留其余部分不变，"
                        f"仍写桥段「{bridge_name}」的正文，一句一行，只输出正文。")
            else:
                user = (f"上一段正文经审校未达标：{reason}\n\n【原正文】\n{text}\n\n"
                        f"请重写这一段：改进上述问题，仍写桥段「{bridge_name}」的正文，"
                        f"3-5 个句子（约150-250字），一句一行，只输出正文。")
            raw = self.llm.call(WRITER_SYSTEM, user,
                                temperature=0.7, max_tokens=WRITER_MAX_TOKENS)
            rewritten = (raw or "").strip().lstrip('"“')
            if not rewritten or has_repeated_token(rewritten):
                return text
            return rewritten
        except Exception:
            return text

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
        if not self.storyline or not self.storyline.plots:
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
        target = self.storyline.words_per_chapter or 3000
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

        diagnosis = ""
        if self.harness and hasattr(self.harness, "_pre_write_diagnosis"):
            try:
                diagnosis = self.harness._pre_write_diagnosis(
                    p, stage.get("name", "") if isinstance(stage, dict) else "")
            except Exception:
                diagnosis = ""
        yield {"type": "bridge_start",
               "plot_id": p.id, "plot_name": p.name,
               "outline_id": o.id if o else "", "outline_name": o.name if o else "",
               "stage_name": stage.get("name", "") if isinstance(stage, dict) else "",
               "planned_words": planned,
               "hook_points": list(getattr(p, "hook_points", None) or []),
               "diagnosis": diagnosis}

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
            if kind == "repair_stalled":
                yield {"type": "repair_stalled", "plot_id": p.id, "reason": text}
                return
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
                               summaries_context: str = "",
                               bridge_meta: list | None = None):
        """写一章（生成器版）：沿故事顺序逐桥段生成，直到本章字数达标。

        兼容旧接口：yield bridge_start/group_chunk/bridge_done 事件，
        所有桥段完成后 return 章节结果 dict（通过 StopIteration.value 取回）。
        chapter_buffer/chapter_words：进行中章节草稿（按桥段撰写中断后续写）。
        bridge_meta：草稿里已写的桥段元数据 [{plot_id, plot_name, text}]，
        与本次新写桥段合并进返回的 "bridges"，供引擎落盘 per-bridge segments。
        """
        if not self.storyline or not self.storyline.plots:
            return {"text": f"[第{chapter_num}章无桥段可写]",
                    "word_count": 0, "beats": 0, "beat_details": [],
                    "blueprint": {"chapter_title": f"第{chapter_num}章",
                                  "total_chapters": self._total_chapters}}
        target = self.storyline.words_per_chapter or 3000
        buffer = [s for s in (chapter_buffer or "").split("\n\n") if s]
        words = chapter_words or 0
        consumed = []
        written = [dict(x) for x in (bridge_meta or [])
                   if isinstance(x, dict) and x.get("text")]
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
            written.append({"plot_id": sub.get("plot_id"),
                            "plot_name": sub.get("plot_name"),
                            "text": sub["text"]})
            if sub.get("cut_chapter") or words >= target:
                break

        text = "\n\n".join(buffer)
        wc = count_prose_units(text)
        return {
            "text": text,
            "word_count": wc,
            "input_text": "\n".join(c.get("input_text", "") for c in consumed if c.get("input_text")),
            "bridges": written,
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
                      on_step=None, bridge_meta: list | None = None) -> dict:
        """写一章（同步版）：内部用 write_chapter_stepwise 逐桥段推进，
        若传了 on_step 则每个桥段写前/写后回调一次（供 UI 展示与高亮）。"""
        gen = self.write_chapter_stepwise(
            chapter_num, previous_chapter_ending, character_states,
            chapter_buffer=chapter_buffer, chapter_words=chapter_words,
            summaries_context=summaries_context, bridge_meta=bridge_meta)
        result = None
        try:
            while True:
                evt = next(gen)
                if on_step:
                    on_step(evt)
        except StopIteration as si:
            result = si.value
        return result
