"""
集中式提示词 harness — 书级设定卡 + 三场景上下文渲染器

统一出口：
  · 书级设定卡（Book Bible）：主角/世界观/配角/基调/母题/风格 压缩成紧凑 bullet，
    在全书开始前确立统一的写作风格与世界观，注入所有写作与大纲决策。
  · render_bridge_prompt   ：桥段写作（取代 timeline_writer._group_prompt 的内联拼装）
  · render_detector_prompt ：笑点探测器（gag_injector 用；笑点完全涌现，不写入大纲）
  · render_summary_prompt  ：章节语义摘要（长程记忆）
  · render_outline_context ：大纲各 phase 前置设定卡
  · prescreen_gag_pool     ：候选笑点模式池免费规则预筛

约定：保持 deepseek-v4-flash；不新增"写完质量重写"型后处理；
      探测/摘要调用输入 ≤500 token（字符数 ≤约 750）、输出 ≤256。
"""
from typing import Optional

from .timeline import BookTimeline


# 桥段 category → 适合的笑点 fit_scene 关键词（免费规则，不写进大纲）
CATEGORY_GAG_SCENES = {
    "爽文": ["打脸后", "身份揭示", "多人场景"],
    "开篇": ["身份揭示", "日常对话"],
    "战斗": ["战斗后放松", "战斗间隙"],
    "都市": ["日常对话", "日常互动"],
    "情感": ["日常互动", "身份揭露"],
    "日常": ["日常对话", "日常互动"],
    "悬疑": ["日常互动", "身份揭示"],
}
DEFAULT_GAG_SCENES = ["日常对话", "日常互动"]


# 炸裂开场（第一章前 N 桥段强制）—— 番茄/飞卢式冷开场铁律
# 素材来源：beat_writer 危机/悬念开场、build_chapter1_prompt、番茄平台约束、开篇桥段 usage_notes
OPENING_MODE_RULES = """【开场模式 — 炸裂开场（第一章开篇桥段强制）】
1. 冷开场铁律：前三句直接进入冲突/反转/对话，禁止铺垫环境、天气、世界观、人物背景。
2. 第一句话就要制造悬念或冲击，让读者立刻想知道「接下来会怎样」。
3. 前 200 字必须有强烈钩子；开篇前 500 字必须有冲突或危机（番茄要求）。
4. 前 500 字必须触发主角的第一个危机/矛盾，不拖节奏。
5. 若设定中有金手指/能力，本章内必须引入或激活。
6. 参考微观结构：[冲击性画面/对话] → [快速解释发生了什么] → [抛出问题]。
7. 示例节奏：「闹钟响的时候，萧晨正梦见自己站在纳斯达克敲钟。底下掌声雷动。然后他就被人一脚踹下了台。」
8. 禁止：天气/环境长铺垫、「他醒来，阳光洒在脸上」式平淡开头。
9. 冲突线前置铁律：开篇直接把核心冲突（退婚短信/嘲讽打压/走投无路/系统激活）拍在读者脸上，与主角困境同屏立起，禁止先铺身份背景再讲冲突。"""


# 全书一致性铁律 —— 防 E2E 评审硬伤：系统重复绑定、数值不闭环、时间线穿帮、无时间过渡
CONSISTENCY_RULES = """【全书一致性铁律】
1. 系统/金手指的"激活/绑定"全书只发生一次；此后同类事件用"新模块/新功能解锁"，禁止重复出现"绑定成功"。
2. 引入的数值（压迫值/劳动值/经验值/属性点等）必须在后续情节有回响闭环，禁止只出现一次再无下文。
3. 对话中的身份/背景/时间线信息严格符合当前时间线，禁止把前世/未来记忆混进当前对话。
4. 跨场景/跨天的事件之间要有自然时间过渡（如"当天夜里""三天后"），禁止无衔接跳转。"""


def _profile_style_text(profile) -> str:
    """从 PenNameProfile 或 dict 生成风格 bullet 文本（去掉标题行）。"""
    if profile is None:
        return ""
    if hasattr(profile, "build_style_prompt"):
        text = profile.build_style_prompt()
        lines = [l for l in text.splitlines() if l.strip()]
        # 去掉标题行「【本笔名的风格约束——必须严格遵守】」
        if lines and lines[0].startswith("【"):
            lines = lines[1:]
        return "\n".join(lines)
    fp = profile.get("style_fingerprint", {}) or {}
    wp = profile.get("word_print", {}) or {}
    tr = profile.get("tropes", {}) or {}
    parts = []
    if fp.get("sentence_length"):
        sl_map = {"short": "多用短句，每句8-15字", "medium": "句中偏长，15-25字为主",
                  "long": "可用长句铺陈，25字以上"}
        parts.append(f"- 句子长度：{sl_map.get(fp['sentence_length'], fp['sentence_length'])}")
    if fp.get("dialogue_ratio"):
        parts.append(f"- 对话占比：约{int(fp['dialogue_ratio'] * 100)}%")
    if fp.get("humor_style"):
        parts.append(f"- 幽默风格：{fp['humor_style']}")
    if fp.get("paragraph_style"):
        parts.append(f"- 段落节奏：{fp['paragraph_style']}")
    if fp.get("action_style"):
        parts.append(f"- 动作描写：{fp['action_style']}")
    if wp.get("common_words"):
        parts.append(f"- 常用词汇：{', '.join(wp['common_words'])}")
    if wp.get("avoid_words"):
        parts.append(f"- 绝对禁用词：{', '.join(wp['avoid_words'])}")
    if tr.get("chapter_hook_style"):
        parts.append(f"- 章末钩子风格：{tr['chapter_hook_style']}")
    if tr.get("scene_pacing"):
        parts.append(f"- 场景节奏：{tr['scene_pacing']}")
    return "\n".join(parts)


class PromptHarness:
    """集中式提示词 harness。timeline 可后续赋值（保持对活对象的引用）。"""

    def __init__(self, timeline: Optional[BookTimeline] = None, profile=None,
                 gag_lib=None, theme_lib=None, plot_lib=None, platform: str = ""):
        self.timeline = timeline
        self.profile = profile
        self.gag_lib = gag_lib
        self.theme_lib = theme_lib
        self.plot_lib = plot_lib
        # 目标平台（fanqie/qidian）：写作 prompt 注入平台写作约束；空=不注入
        self.platform = platform or (timeline.platform if timeline else "") or ""

    # ═══════════════════════════════════════════
    # 书级设定卡（Book Bible）分段渲染
    # ═══════════════════════════════════════════

    def _protagonist_bullets(self) -> str:
        tl = self.timeline
        if not tl:
            return ""
        proto = (tl.basic_info or {}).get("protagonist", {}) or {}
        if not proto.get("name"):
            return ""
        parts = [f"- 主角：{proto['name']}"]
        if proto.get("identity"):
            parts.append(f"  身份：{proto['identity']}")
        if proto.get("personality"):
            parts.append(f"  性格：{proto['personality'][:80]}")
        if proto.get("golden_finger"):
            parts.append(f"  金手指：{proto['golden_finger'][:80]}")
        if proto.get("background"):
            parts.append(f"  背景：{proto['background'][:60]}")
        return "\n".join(parts)

    def _world_bullets(self) -> str:
        tl = self.timeline
        if not tl:
            return ""
        wb = (tl.basic_info or {}).get("world_building", {}) or {}
        if not wb.get("era") and not wb.get("power_system"):
            return ""
        parts = ["- 世界观："]
        if wb.get("era"):
            parts.append(f"  时代：{str(wb['era'])[:40]}")
        if wb.get("power_system"):
            parts.append(f"  力量体系：{str(wb['power_system'])[:60]}")
        factions = [str(f) for f in (wb.get("factions") or [])][:4]
        if factions:
            parts.append("  势力：" + "、".join(factions))
        rules = [str(r) for r in (wb.get("rules") or [])][:5]
        if rules:
            parts.append("  规则：" + "；".join(r[:60] for r in rules))
        parts.append("  设定铁律：数值/技能语义全书唯一口径（如『效率×2』指同一件事），禁止每章换一种解释。")
        return "\n".join(parts)

    def _tone_bullets(self) -> str:
        tl = self.timeline
        if not tl:
            return ""
        bi = tl.basic_info or {}
        tone = str(bi.get("tone", "") or "")
        audience = str(bi.get("target_audience", "") or "")
        if not tone and not audience:
            return ""
        return "- 基调：" + "/".join(x for x in [tone, audience] if x)

    def _theme_bullets(self) -> str:
        tl = self.timeline
        if not tl:
            return ""
        themes = [str(t) for t in (tl.themes or [])][:4]
        if not themes:
            return ""
        return "- 全书母题：" + "、".join(themes)

    def _supporting_cast_bullets(self) -> str:
        tl = self.timeline
        if not tl:
            return ""
        cast = (tl.basic_info or {}).get("supporting_cast", []) or []
        if not cast:
            return ""
        lines = []
        for c in cast[:3]:
            name = c.get("name", "") if isinstance(c, dict) else str(c)
            gender = c.get("gender", "") if isinstance(c, dict) else ""
            title = c.get("title", "") if isinstance(c, dict) else ""
            role = c.get("role", "") if isinstance(c, dict) else ""
            rel = c.get("relation", "") if isinstance(c, dict) else ""
            personality = (c.get("personality", "") if isinstance(c, dict) else "")[:40]
            catchphrase = (c.get("catchphrase", "") if isinstance(c, dict) else "")[:40]
            seg = f"- 配角：{name}"
            if title:
                seg += f"（{title}）"
            if gender:
                seg += f"[{gender}]"
            if role:
                seg += f"，{role}"
            if rel:
                seg += f"，与主角{rel}"
            if personality:
                seg += f"，性格{personality}"
            if catchphrase:
                seg += f"，口头禅「{catchphrase}」"
            lines.append(seg)
        return "\n".join(lines)

    def _pov_bullets(self) -> str:
        tl = self.timeline
        if not tl:
            return ""
        pov = str((tl.basic_info or {}).get("pov", "") or "").strip()
        if not pov:
            return ""
        return f"- 视角：{pov}（全篇统一该人称，禁止第一/第三人称混用）"

    def _era_language_bullets(self) -> str:
        tl = self.timeline
        if not tl:
            return ""
        bi = tl.basic_info or {}
        explicit = str(bi.get("era_language", "") or "").strip()
        lines = []
        if explicit:
            lines.append("- 时代语言：" + explicit[:80])
        # 自动兜底：era 年份 ≤2015 → 禁现代网络词
        era = str((bi.get("world_building") or {}).get("era", "") or "")
        import re as _re
        m = _re.search(r'(19|20)\d{2}', era)
        if m and int(m.group(0)) <= 2015:
            lines.append("- 时代语言：背景约" + m.group(0) + "年，禁止晚于该时代的网络新词/梗（如：搭子、内卷、PUA、破防、摆烂、躺平、绝绝子、yyds）")
        return "\n".join(lines)

    def _style_bullets(self) -> str:
        return _profile_style_text(self.profile)

    def _rebirth_time_bullets(self) -> str:
        """重生文时间词纪律：前世经历禁止用当前时间线近指词。"""
        tl = self.timeline
        if not tl:
            return ""
        bi = tl.basic_info or {}
        protag = bi.get("protagonist") or {}
        identity = str(protag.get("identity", "") or "")
        try:
            death_year = int(protag.get("death_year", 0) or 0)
        except (TypeError, ValueError):
            death_year = 0
        era = str((bi.get("world_building") or {}).get("era", "") or "")
        if not (death_year > 0 or "重生" in identity or "重生" in era):
            return ""
        return ("- 时间纪律（重生文）：指代前世/上辈子的经历一律用「上辈子/前世/当年」，"
                "禁止用「上周/上个月/去年/昨天/那年」等近指词（那属于当前时间线）。")

    def _bible_sections(self, condensed: bool = False):
        """按优先级返回 [(标题, 文本), ...]。condensed 时只留写作必需段。"""
        if condensed:
            return [
                ("主角", self._protagonist_bullets()),
                ("世界观", self._world_bullets()),
                ("风格", self._style_bullets()),
                ("视角", self._pov_bullets()),
                ("时代语言", self._era_language_bullets()),
                ("时间纪律", self._rebirth_time_bullets()),
                ("母题", self._theme_bullets()),
            ]
        return [
            ("主角", self._protagonist_bullets()),
            ("世界观", self._world_bullets()),
            ("风格", self._style_bullets()),
            ("视角", self._pov_bullets()),
            ("时代语言", self._era_language_bullets()),
            ("时间纪律", self._rebirth_time_bullets()),
            ("配角", self._supporting_cast_bullets()),
            ("母题", self._theme_bullets()),
            ("基调", self._tone_bullets()),
        ]

    @staticmethod
    def _join_sections(sections, max_chars: int) -> str:
        out = []
        total = 0
        for title, text in sections:
            if not text:
                continue
            block = f"【{title}】\n{text}"
            remain = max_chars - total
            if remain <= 0:
                break
            if len(block) > remain:
                block = block[:remain]
            out.append(block)
            total += len(block)
        return "\n\n".join(out)

    def build_book_bible(self, max_chars: int = 1200) -> str:
        """全量设定卡，按优先级（主角→世界观→风格→配角→母题→基调）累计截断。"""
        return self._join_sections(self._bible_sections(condensed=False), max_chars)

    def build_book_bible_condensed(self, max_chars: int = 600) -> str:
        """精简版：主角+世界观+风格+母题（写作/探测器用，目标约 440 字）。"""
        return self._join_sections(self._bible_sections(condensed=True), max_chars)

    # ═══════════════════════════════════════════
    # 场景 B：桥段写作 prompt（取代 _group_prompt 内联拼装）
    # ═══════════════════════════════════════════

    def render_bridge_prompt(self, item, chapter_buffer: str, prev_ending: str,
                             bridge_text: str, budget_remaining: int,
                             character_states: str = "",
                             summaries_context: str = "",
                             inspiration_hint: str = "",
                             is_opening: bool = False,
                             review_hint: str = "") -> str:
        """返回 user prompt 字符串（system 沿用 timeline_writer 的铁律，不在本方法内）。

        item = {"outline": OutlineSlot, "stage": dict, "plot": PlotSlot}
        is_opening=True 时注入炸裂开场铁律（第一章前 N 桥段）。
        review_hint：上一章规则审查（reviewer）未过的修复提示，一次性注入首个桥段。
        """
        o = item["outline"]
        stage = item["stage"] or {}
        p = item["plot"]
        stage_name = stage.get("name", "") if isinstance(stage, dict) else ""
        events = stage.get("events", []) if isinstance(stage, dict) else []
        structure = p.template_structure or p.name
        slots_text = ""
        if p.slots:
            slots_text = "\n".join(
                f"  {s.get('name', '?')} = {s.get('default', '?')}"
                f"（可选: {'、'.join(s.get('options', [])[:3])}）"
                for s in p.slots[:4])

        # 吸睛点（mount_themes_and_hooks 已生成，此前从未进写作 prompt）：
        # 把桥段的最强爽点/悬念落地为读者可见的 payoff。
        hook_block = ""
        hooks = list(getattr(p, "hook_points", None) or [])
        if hooks:
            hook_block = ("\n【本桥段吸睛点】" + "、".join(hooks[:2])
                          + "\n（写出实感：用具体画面/结果把这几个吸睛点做成读者想看的爽点/悬念/反转，不直白点破、不加括号注解）")

        # 前文上下文（修复：原 _group_prompt 的 character_states 形参未被渲染）
        ctx = []
        if prev_ending:
            ctx.append("【上一章结尾】" + prev_ending[-150:])
        if chapter_buffer:
            ctx.append("【本章已写正文】" + chapter_buffer[-900:])
        if bridge_text:
            ctx.append("【本桥段已写】" + bridge_text[-300:])
        if character_states:
            ctx.append("【角色当前状态】\n" + character_states.strip()[:500])
        context_text = "\n".join(ctx) if ctx else "（本章开头，尚无前文）"

        # 长程记忆：已完成章节语义摘要
        summaries_block = ""
        if summaries_context:
            summaries_block = ("【已完成章节语义摘要】\n"
                               + summaries_context.strip()[:600] + "\n\n")

        # 内涵跟随桥段：从情节自然流露，不点破
        theme_block = ""
        themes = list(getattr(p, "theme_hints", None) or [])
        if themes:
            theme_block = ("\n【本桥段要自然体现的母题】\n"
                           + "、".join(themes[:3])
                           + "\n（从情节自然流露、用结果说话，不要直白点题、不要加括号注解）")

        # 灵机一动（探测器命中后注入下一组）
        inspiration_block = ""
        if inspiration_hint:
            inspiration_block = "\n【灵机一动】顺势落地\n" + inspiration_hint.strip()

        # 收局槽位：解决/呼应更早埋下的设局钩子（桥段拆分）
        payoff_block = ""
        if getattr(p, "resolves_plot_id", ""):
            rname = getattr(p, "resolves_name", "") or "前文埋下的钩子"
            payoff_block = ("\n【本桥段收束】解决/呼应『" + rname +
                            "』（其钩子在更早处埋下），给出结果/反转，补上闭环。")
        # 设局槽位：为某收局桥段埋钩子
        setup_block = ""
        if item.get("resolver_name"):
            setup_block = ("\n【设局桥段】为『" + str(item.get("resolver_name")) +
                           "』埋钩子，结尾留一个明确未解决的悬念。")

        # 视角铁律（防人称漂移：显式重申，不让模型自己定）
        _pov = str((self.timeline.basic_info or {}).get("pov", "") if self.timeline else "").strip()
        if _pov == "第一人称":
            pov_block = "【视角铁律】全篇第一人称「我」叙事；禁止叙事段落跳出第三人称「他/她」；对话内人物称谓不受限。\n\n"
        elif _pov == "第三人称":
            pov_block = "【视角铁律】全篇第三人称（他/她/名字）叙事；禁止叙事段落突现第一人称「我」（内心独白可保留）；一段内严禁「他」「我」混用。\n\n"
        else:
            pov_block = "【视角铁律】全篇统一人称，禁止第一/第三人称混用。\n\n"

        # 本桥段出场人物（性格/性别/口头禅，防"她"字错误、保持声线）
        roles_block = self._roles_block(p) if getattr(p, "roles", None) else ""

        bible = self.build_book_bible_condensed()
        bible_block = f"【书级设定（简）】\n{bible}\n\n" if bible else ""

        opening_block = (OPENING_MODE_RULES + "\n\n") if is_opening else ""
        consistency_block = CONSISTENCY_RULES + "\n\n"
        review_block = (("【上章审查提示】" + review_hint.strip() + "\n\n") if review_hint else "")
        platform_block = ""
        if self.platform:
            try:
                from .book_meta import platform_constraints
                pc = platform_constraints(self.platform)
                if pc:
                    platform_block = pc + "\n\n"
            except Exception:
                pass

        return f"""你是一位专业的中文网络小说作者，正在逐段续写正文。每轮只输出 3-5 个句子。

{bible_block}{opening_block}{consistency_block}{platform_block}{review_block}{pov_block}【所属大纲】{o.name}（第{o.start_chapter}-{o.end_chapter}章）
【当前阶段】{stage_name}
【本桥段要推动的事件】{'、'.join(events[:4]) if events else '按大纲自然推进'}
【桥段骨架】{structure}
【变量槽位】{slots_text or '跟随上下文自由发挥'}
{hook_block}
{roles_block}
{theme_block}
{payoff_block}
{setup_block}
{inspiration_block}

{summaries_block}【前文上下文】
{context_text}

【写作要求】
1. 只输出下一段正文：3-5 个句子（总共约 150-250 个汉字），一句一行；短句为基干，句长需长短交错（8-15字为主、穿插25-45字），避免全文句式单一。
2. 画面优先：用动作、对话、感官细节推进，不要堆形容词、不要抽象抒情。
3. 每组至少含一句对话或一个动作；对话独立成段并带简短神态/动作。
4. 围绕上方的"要推动的事件"制造推进感：埋冲突、留张力，组尾留一个"接下来会怎样"的悬念钩子（本桥段最后一组可自然收束）。
5. 必须紧接上文继续，人物、视角、设定保持一致，视角始终跟随主角；绝不重开新故事、不换主角。
6. 严禁出现：然而、不禁、仿佛、似乎、瞬间、顿时、缓缓、微微、眼中闪过、心中一动、微微一笑、嘴角勾起、与此同时、就在这时。
7. 不写章节标题、不标注步骤、不加解释性文字。本桥段还剩约 {budget_remaining} 字预算，控制篇幅。"""

    def _roles_block(self, p) -> str:
        """本桥段出场人物：性别/性格/惯用语句/简介（防性别指代错、保持角色声线）。"""
        if not self.timeline:
            return ""
        bi = self.timeline.basic_info or {}
        protag = bi.get("protagonist") or {}
        cast_map = {}
        for c in (bi.get("supporting_cast") or []):
            if isinstance(c, dict) and c.get("name"):
                cast_map[str(c["name"]).strip()] = c
        lines = []
        for rname in (p.roles or [])[:4]:
            if rname == protag.get("name"):
                seg = f"- {rname}（主角）"
                if protag.get("gender"):
                    seg += f"[{protag['gender']}]"
                if protag.get("personality"):
                    seg += f"，性格{str(protag['personality'])[:40]}"
                lines.append(seg)
            else:
                c = cast_map.get(rname)
                seg = f"- {rname}"
                if c:
                    if c.get("title"):
                        seg += f"（{c['title']}）"
                    if c.get("gender"):
                        seg += f"[{c['gender']}]"
                    if c.get("personality"):
                        seg += f"，性格{str(c['personality'])[:40]}"
                    if c.get("catchphrase"):
                        seg += f"，口头禅「{str(c['catchphrase'])[:40]}」"
                    if c.get("brief"):
                        seg += f"，{str(c['brief'])[:40]}"
                lines.append(seg)
        if not lines:
            return ""
        return ("\n【本桥段出场人物——严格保持其性别/声线/口头禅，人称别写错】\n"
                + "\n".join(lines))

    # ═══════════════════════════════════════════
    # 场景 C：笑点探测器 prompt（gag_injector 用）
    # ═══════════════════════════════════════════

    def render_detector_prompt(self, recent_text: str, humor_style: str,
                               pool: list) -> dict:
        """返回 {"system", "user"}。pool 为 GagPattern 列表（≤6 条，实际只用前 4）。"""
        pool_lines = []
        for g in pool[:4]:
            desc = (getattr(g, "pattern_description", "") or "")[:60]
            pool_lines.append(f"- [{g.id}] {g.name}：{desc}")
        pool_text = "\n".join(pool_lines) if pool_lines else "（无可用模式）"

        return {
            "system": "你是一个中文网文主角的「喜剧嗅觉探测器」。只返回 JSON，不要任何其他文字。",
            "user": f"""判断刚写好的正文里，下一句顺势落一个笑点是否「天然、不硬凑」。

【主角喜剧声线】
{humor_style or '无特别要求，本色即可'}

【候选笑点模式池】
{pool_text}

【刚写好的正文（本组+前 1-2 组）】
{recent_text[-450:]}

【判断铁律】
1. 只在这些位置认为「有戏」：人物对话刚结束、一个动作/发现刚发生、
   气氛到了某个情绪高点、存在天然的「落差/反差」可踩。
2. 没有天然缝隙，绝对不要硬造。宁可错过，不可硬塞。（最高优先级）
3. 命中时，gag_ids 最多 1 个，且必须选自候选模式池。

返回 JSON：
{{"has_opportunity": true/false,
  "gag_ids": ["gag_003"],
  "reason": "一句话说明为什么有/没有缝隙",
  "deploy_hint": "若命中：下一句可这样顺势落地（一句、用主角口吻、贴场景、别解释）"}}""",
        }

    # ═══════════════════════════════════════════
    # 场景 D：章节语义摘要 prompt（长程记忆）
    # ═══════════════════════════════════════════

    def render_summary_prompt(self, content_tail: str, bridge_line: str = "") -> dict:
        return {
            "system": "你是网文编辑，为一章正文写客观语义摘要。只返回 JSON。",
            "user": f"""为刚写完的一章写 80-150 字语义摘要。

【本章所属】{bridge_line or '本章'}
【本章内容（尾部最近场景）】
{content_tail[-600:]}

【要求】概括本章发生了什么、主角状态、埋下的钩子/线索；客观陈述，不评价文笔。
返回 JSON：{{"summary": "..."}}""",
        }

    # ═══════════════════════════════════════════
    # 场景 A：大纲各 phase 前置设定卡
    # ═══════════════════════════════════════════

    def render_outline_context(self, phase_kind: str,
                               timeline: Optional[BookTimeline] = None) -> str:
        """返回要拼到大纲 prompt 开头的上下文块（空字符串表示无需前置）。

        phase_kind ∈ analyze/sequence/select_plots/theme_review/validate
        """
        prev_tl = self.timeline
        if timeline is not None:
            self.timeline = timeline
        try:
            if phase_kind in ("sequence", "validate"):
                bible = self.build_book_bible(max_chars=900)
                return f"【书级设定】\n{bible}\n" if bible else ""
            if phase_kind in ("select_plots", "thread_split"):
                bible = self.build_book_bible_condensed(max_chars=500)
                return f"【书级设定（简）】\n{bible}\n" if bible else ""
            if phase_kind == "theme_review":
                themes = [str(t) for t in (self.timeline.themes or [])][:6] if self.timeline else []
                return f"【全书母题】{'、'.join(themes) if themes else '（无）'}\n"
            return ""
        finally:
            self.timeline = prev_tl

    # ═══════════════════════════════════════════
    # 候选笑点模式池预筛（免费规则，不写进大纲）
    # ═══════════════════════════════════════════

    def prescreen_gag_pool(self, plot, book_id: str = "") -> list:
        """按桥段 category → fit_scene 关键词，从笑点库预筛 ≤6 个候选模式。

        过滤 enabled==False 与本 book 的 banned_in，按 usage_count 升序（少用优先）。
        """
        if not self.gag_lib:
            return []
        category = str(getattr(plot, "category", "") or "")
        scenes = CATEGORY_GAG_SCENES.get(category, DEFAULT_GAG_SCENES)
        seen, pool = set(), []
        for kw in scenes:
            for g in self.gag_lib.search(scene=kw):
                if g.id in seen:
                    continue
                if not getattr(g, "enabled", True):
                    continue
                if book_id and book_id in (getattr(g, "banned_in", None) or []):
                    continue
                seen.add(g.id)
                pool.append(g)
        pool.sort(key=lambda g: getattr(g, "usage_count", 0))
        return pool[:6]
