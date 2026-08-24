"""
集中式提示词 harness — 书级设定卡 + 三场景上下文渲染器

统一出口：
  · 书级设定卡（Book Bible）：主角/世界观/配角/基调/内涵/风格 压缩成紧凑 bullet，
    在全书开始前确立统一的写作风格与世界观，注入所有写作与大纲决策。
  · render_bridge_prompt   ：桥段写作（取代 storyline_writer._group_prompt 的内联拼装）
  · render_detector_prompt ：笑点探测器（gag_injector 用；笑点完全涌现，不写入大纲）
  · render_summary_prompt  ：章节语义摘要（长程记忆）
  · render_outline_context ：大纲各 phase 前置设定卡
  · prescreen_gag_pool     ：候选笑点模式池免费规则预筛

约定：保持 deepseek-v4-flash；不新增"写完质量重写"型后处理；
      探测/摘要调用输入 ≤500 token（字符数 ≤约 750）、输出 ≤256。
"""
from typing import Optional

from .storyline import BookStoryline, get_characters, get_mc, relation_to_mc
from .promise_ledger import promise_op


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


# 写前编辑诊断（模拟人类作者开写前想清楚"这一节要达到什么"）—— 免费规则映射
CATEGORY_READER_DESIRE = {
    "爽文": "打脸快感与身份抬升",
    "打脸": "对手当众出丑、主角反超",
    "战斗": "战斗胜负与实力印证",
    "都市": "现实利益争夺与身份翻盘",
    "情感": "关系推进与情感张力",
    "日常": "轻松互动与人物魅力",
    "悬疑": "解谜欲与真相逼近",
    "成长": "变强确认与突破快感",
    "开篇": "悬念钩子与代入感",
}
DEFAULT_READER_DESIRE = "情节推进与情绪满足"
CATEGORY_ENEMY_LOSS = {
    "爽文": "面子/地位受损",
    "打脸": "脸面当场落地",
    "战斗": "落败/实力被否定",
    "都市": "利益/资源被夺",
    "悬疑": "露出破绽/计划受挫",
    "成长": "威压被打破",
    "职场": "被当众驳倒/失去主动权",
}
DEFAULT_ENEMY_LOSS = "对手付出代价或计划受挫"

# 角色档案字段分类（竞品借鉴：AI-NWA character_hard_facts）——
# 硬事实不得写反；软倾向只作语气参考，不写成旁白确认的事实
_HARD_FIELDS = ("identity", "faction", "power_level", "location")
_SOFT_FIELDS = ("personality", "catchphrase", "mood", "brief")
_HARD_LABELS = {"identity": "身份", "faction": "势力", "power_level": "境界/实力", "location": "位置"}
_SOFT_LABELS = {"personality": "性格", "catchphrase": "口头禅", "mood": "情绪", "brief": "简介"}


def _tail_paragraphs(text: str, max_chars: int = 150) -> str:
    """按 \n\n 取尾部完整段落，累计不超过 max_chars（P0-6 段落边界尾提取）。

    替代 prev_ending[-150:] 字符硬截断：保证上下文以完整段落收尾，
    不把一句话从中间切断；单段超长时保留其尾部（最近内容优先），守住上限。
    """
    paras = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    if not paras:
        return ""
    out, total = [], 0
    for p in reversed(paras):
        if total + len(p) > max_chars:
            if not out:
                return p[-max_chars:]
            break
        out.append(p)
        total += len(p)
    return "\n\n".join(reversed(out))


# 权威层级（竞品借鉴：OpenNovel 三档权威标签）——高权威覆盖低权威
AUTHORITY_CANON = (
    "【权威层级】高权威覆盖低权威："
    "CANON（书级设定/一致性/视角/前文上下文，不可违反）＞ "
    "STATE MEMORY（角色当前状态/章节语义摘要，必须尊重）＞ "
    "OPTIONAL（吸睛点/内涵/灵机一动，仅文风参考）"
)

# 前文上下文块预算：各块已自带安全帽（本桥段300/本章900/上一章150/角色态500/摘要600），
# 预算与各块上限之和同量级 → 常规不触发裁剪；收紧此值即启用「超预算丢低优先级块」
_CONTEXT_BUDGET = 2450


def _assemble_blocks(blocks, budget=_CONTEXT_BUDGET):
    """按 dropOrder 组装上下文块：超预算时优先丢低优先级块（块内不再二次截断）。

    blocks: list[(dropOrder, tier, header, text)]；dropOrder 越小越优先保留，
    tier 为权威分级元数据（canon/state/optional，实际取舍只看 dropOrder）。
    返回 (lines, dropped_header_names)。
    """
    ordered = sorted(blocks, key=lambda b: b[0])
    lines, used, dropped = [], 0, []
    for _order, _tier, header, text in ordered:
        if used + len(text) > budget:
            dropped.append(header.strip("【】"))
            continue
        lines.append(header + text)
        used += len(text)
    return lines, dropped


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


# 全书一致性铁律 —— 防 E2E 评审硬伤：系统重复绑定、数值不闭环、故事线穿帮、无时间过渡
CONSISTENCY_RULES = """【全书一致性铁律】
1. 系统/金手指的"激活/绑定"全书只发生一次；此后同类事件用"新模块/新功能解锁"，禁止重复出现"绑定成功"。
2. 引入的数值（压迫值/劳动值/经验值/属性点等）必须在后续情节有回响闭环，禁止只出现一次再无下文。
3. 对话中的身份/背景/故事线信息严格符合当前故事线，禁止把前世/未来记忆混进当前对话。
4. 跨场景/跨天的事件之间要有自然时间过渡（如"当天夜里""三天后"），禁止无衔接跳转。"""


# ── 世界观/设定生成（启动新书前置）──
WORLD_BUILD_SYSTEM = (
    "你是一位资深中文网文世界观策划编辑。"
    "你的任务是先构思一个完整自洽的世界，再让笔下人物活在这个世界里。"
    "文字要具体、有画面、有网文味，不要空泛说教。"
)
WORLD_BUILD_STRUCT_SYSTEM = (
    "你是资深网文策划编辑，负责把世界观设定文结构化并推导主角配角。"
    "严格以 JSON 格式返回，不要任何额外文字。"
)
WORLD_CANDIDATES_SYSTEM = (
    "你是网文策划编辑，擅长从同一句话发散出几个截然不同的世界观方向。"
    "只返回 JSON。"
)

# 世界观各维度要求 —— 每次生成都注入，确保产出"可写的设定圣经"而非空话
WORLD_BUILDING_SCHEMA_HINT = """世界观各维度要求（每一项都要具体可写，避免空泛）：
- 若已指定题材标签(tags)，世界观各维度必须与其强绑定（标签=读者预期，不可漂移）
- era 时代背景：含年份/纪元（如"灵气复苏后2030年"）
- power_system 力量体系：体系名+层级+晋升路径；金手指的数值/技能语义必须写死（如"效率×2"具体指什么翻倍），全书口径唯一
- factions 势力派系：2-4 个，每个给名称+立场
- geography 地理：主要地域/大陆/城市/秘境/势力地盘
- culture 文化：宗门/家族/流派/风俗/价值观/流行事物
- history 历史：背景大事件/时代断层/被掩盖的秘密
- social_structure 社会结构：阶级划分/权力架构/晋升与压制规则
- core_conflict 核心矛盾：驱动全书的根本冲突（1-2 句）
- world_summary 设定文：一段 200-300 字整体设定概述"""


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
    """集中式提示词 harness。storyline 可后续赋值（保持对活对象的引用）。"""

    def __init__(self, storyline: Optional[BookStoryline] = None, profile=None,
                 gag_lib=None, plot_lib=None, platform: str = "",
                 book_id: str = ""):
        self.storyline = storyline
        self.profile = profile
        self.gag_lib = gag_lib
        self.plot_lib = plot_lib
        self.book_id = book_id                # banned_in 按书过滤
        # 目标平台（fanqie/qidian）：写作 prompt 注入平台写作约束；空=不注入
        self.platform = platform or (storyline.platform if storyline else "") or ""

    # ═══════════════════════════════════════════
    # 书级设定卡（Book Bible）分段渲染
    # ═══════════════════════════════════════════

    def _protagonist_bullets(self) -> str:
        tl = self.storyline
        if not tl:
            return ""
        proto = get_mc(tl.basic_info or {})
        if not proto.get("name"):
            return ""
        parts = [f"- 主角：{proto['name']}"]
        if proto.get("identity"):
            parts.append(f"  身份：{proto['identity']}")
        if proto.get("personality"):
            parts.append(f"  性格：{proto['personality'][:80]}")
        if proto.get("golden_finger"):
            parts.append(f"  金手指：{proto['golden_finger'][:80]}")
        background = proto.get("brief") or proto.get("background") or ""
        if background:
            parts.append(f"  背景：{background[:60]}")
        return "\n".join(parts)

    def _tags_block(self) -> str:
        """【题材标签（硬约束）】块 —— 已选 tags 时注入世界/大纲/写作 prompt。"""
        tl = self.storyline
        if not tl:
            return ""
        wb = (tl.basic_info or {}).get("world_building", {}) or {}
        tags = [str(t).strip() for t in (wb.get("tags") or []) if str(t).strip()]
        if not tags:
            return ""
        return ("【题材标签（硬约束）】本书已确定题材标签：" + "、".join(tags)
                + "。世界观设定、主角/金手指、剧情节奏必须严格契合这些标签的网文套路"
                  "与读者预期，禁止漂移到标签之外题材。")

    def _world_bullets(self) -> str:
        tl = self.storyline
        if not tl:
            return ""
        wb = (tl.basic_info or {}).get("world_building", {}) or {}
        # 老字段兜底：结构化维度全空时，用一句话种子 description 撑住注入
        structured_keys = ("era", "power_system", "geography", "culture", "history",
                           "social_structure", "core_conflict")
        if not any(str(wb.get(k, "") or "").strip() for k in structured_keys) \
                and not (wb.get("factions") or wb.get("rules") or wb.get("world_summary")):
            desc = str(wb.get("description", "") or "").strip()
            tags = [str(t) for t in (wb.get("tags") or []) if str(t).strip()]
            head = f"- 题材标签（硬约束）：{'、'.join(tags)}" if tags else ""
            line = f"- 世界观：{desc[:120]}" if desc else ""
            return "\n".join(x for x in (head, line) if x)

        parts = ["- 世界观："]
        tags = [str(t) for t in (wb.get("tags") or []) if str(t).strip()]
        if tags:
            parts.append(f"  题材标签（硬约束）：{'、'.join(tags)}")
        summary = str(wb.get("world_summary", "") or "").strip()
        if summary:
            parts.append(f"  概览：{summary[:120]}")
        if wb.get("era"):
            parts.append(f"  时代：{str(wb['era'])[:40]}")
        if wb.get("power_system"):
            parts.append(f"  力量体系：{str(wb['power_system'])[:60]}")
        for key, label in (("geography", "地理"), ("culture", "文化"),
                           ("history", "历史"), ("social_structure", "社会结构")):
            val = str(wb.get(key, "") or "").strip()
            if val:
                parts.append(f"  {label}：{val[:50]}")
        factions = []
        for f in (wb.get("factions") or [])[:4]:
            if isinstance(f, dict):
                f = f.get("name", str(f))
            factions.append(str(f))
        if factions:
            parts.append("  势力：" + "、".join(factions))
        rules = [str(r) for r in (wb.get("rules") or [])][:5]
        if rules:
            parts.append("  规则：" + "；".join(r[:60] for r in rules))
        conflict = str(wb.get("core_conflict", "") or "").strip()
        if conflict:
            parts.append(f"  核心矛盾：{conflict[:60]}")
        parts.append("  设定铁律：数值/技能语义全书唯一口径（如『效率×2』指同一件事），禁止每章换一种解释。")
        return "\n".join(parts)

    def _tone_bullets(self) -> str:
        tl = self.storyline
        if not tl:
            return ""
        bi = tl.basic_info or {}
        tone = str(bi.get("tone", "") or "")
        audience = str(bi.get("target_audience", "") or "")
        if not tone and not audience:
            return ""
        return "- 基调：" + "/".join(x for x in [tone, audience] if x)

    def _theme_bullets(self) -> str:
        tl = self.storyline
        if not tl:
            return ""
        themes = [str(t) for t in (tl.themes or [])][:4]
        if not themes:
            return ""
        return "- 全书内涵：" + "、".join(themes)

    def _supporting_cast_bullets(self) -> str:
        tl = self.storyline
        if not tl:
            return ""
        bi = tl.basic_info or {}
        mc_name = str(get_mc(bi).get("name", "") or "").strip()
        cast = [c for c in get_characters(bi)
                if isinstance(c, dict) and c.get("name")
                and str(c["name"]).strip() != mc_name]
        if not cast:
            return ""
        # 按重要度升序（1 最高）取前 3-5 个，展示更重要的角色
        try:
            cast = sorted(cast, key=lambda c: int(c.get("importance") or 2))
        except (TypeError, ValueError):
            pass
        lines = []
        for c in cast[:5]:
            name = c.get("name", "")
            gender = c.get("gender", "")
            title = c.get("title", "")
            role = c.get("identity", "")       # 旧 role(职位) → identity
            rel = relation_to_mc(c, bi)
            personality = (c.get("personality", "") or "")[:40]
            catchphrase = (c.get("catchphrase", "") or "")[:40]
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
        tl = self.storyline
        if not tl:
            return ""
        pov = str((tl.basic_info or {}).get("pov", "") or "").strip()
        if not pov:
            return ""
        return f"- 视角：{pov}（全篇统一该人称，禁止第一/第三人称混用）"

    def _era_language_bullets(self) -> str:
        tl = self.storyline
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
        """重生文时间词纪律：前世经历禁止用当前故事线近指词。"""
        tl = self.storyline
        if not tl:
            return ""
        bi = tl.basic_info or {}
        protag = get_mc(bi)
        identity = str(protag.get("identity", "") or "")
        try:
            death_year = int(protag.get("death_year", 0) or 0)
        except (TypeError, ValueError):
            death_year = 0
        era = str((bi.get("world_building") or {}).get("era", "") or "")
        if not (death_year > 0 or "重生" in identity or "重生" in era):
            return ""
        return ("- 时间纪律（重生文）：指代前世/上辈子的经历一律用「上辈子/前世/当年」，"
                "禁止用「上周/上个月/去年/昨天/那年」等近指词（那属于当前故事线）。")

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
                ("内涵", self._theme_bullets()),
            ]
        return [
            ("主角", self._protagonist_bullets()),
            ("世界观", self._world_bullets()),
            ("风格", self._style_bullets()),
            ("视角", self._pov_bullets()),
            ("时代语言", self._era_language_bullets()),
            ("时间纪律", self._rebirth_time_bullets()),
            ("配角", self._supporting_cast_bullets()),
            ("内涵", self._theme_bullets()),
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
        """全量设定卡，按优先级（主角→世界观→风格→配角→内涵→基调）累计截断。"""
        return self._join_sections(self._bible_sections(condensed=False), max_chars)

    def build_book_bible_condensed(self, max_chars: int = 600) -> str:
        """精简版：主角+世界观+风格+内涵（写作/探测器用，目标约 440 字）。"""
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
                             review_hint: str = "",
                             chapter_num: int = 0,
                             chapter_participants: str = "") -> str:
        """返回 user prompt 字符串（system 沿用 storyline_writer 的铁律，不在本方法内）。

        item = {"outline": OutlineSlot, "stage": dict, "plot": PlotSlot}
        is_opening=True 时注入炸裂开场铁律（第一章前 N 桥段）。
        review_hint：上一章规则审查（reviewer）未过的修复提示，一次性注入首个桥段。
        chapter_num：当前写作章节号（读者承诺台账判断逾期用）。
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
            hook_block = ("\n【OPTIONAL｜本桥段吸睛点】" + "、".join(hooks[:2])
                          + "\n（写出实感：用具体画面/结果把这几个吸睛点做成读者想看的爽点/悬念/反转，不直白点破、不加括号注解）")

        # 前文上下文：按 dropOrder 预算组装（权威分级见 AUTHORITY_CANON；超预算丢低优先级块）
        context_blocks = []
        if bridge_text:
            context_blocks.append((0, "canon", "【本桥段已写】", bridge_text[-300:]))
        if chapter_buffer:
            context_blocks.append((1, "canon", "【本章已写正文】", chapter_buffer[-900:]))
        if prev_ending:
            context_blocks.append((2, "canon", "【上一章结尾】", _tail_paragraphs(prev_ending)))
        if character_states:
            context_blocks.append((3, "state", "【角色当前状态】\n", character_states.strip()[:500]))
        if summaries_context:
            context_blocks.append((4, "state", "【已完成章节语义摘要】\n", summaries_context.strip()[:600]))
        context_lines, dropped = _assemble_blocks(context_blocks)
        context_text = "\n".join(context_lines) if context_lines else "（本章开头，尚无前文）"
        if dropped:
            context_text += "\n（上下文超预算，已省略低优先级块：" + "、".join(dropped) + "）"

        # 内涵跟随桥段：从情节自然流露，不点破。阶段级 theme_moments 优先（含位置/手法）。
        theme_block = ""
        moments = list(getattr(p, "theme_moments", None) or [])
        if moments:
            lines = []
            for m in moments[:3]:
                if not isinstance(m, dict):
                    continue
                seg = m.get("name", "")
                if not seg:
                    continue
                if m.get("position"):
                    seg += f"（{m['position']}）"
                if m.get("how"):
                    seg += f"：{m['how']}"
                lines.append(seg)
            if lines:
                theme_block = ("\n【OPTIONAL｜本桥段要自然体现的内涵（含插入位置）】\n"
                               + "\n".join(lines)
                               + "\n（从情节自然流露、用结果说话，不要直白点题、不要加括号注解）")
        else:
            themes = list(getattr(p, "theme_hints", None) or [])
            if themes:
                theme_block = ("\n【OPTIONAL｜本桥段要自然体现的内涵】\n"
                               + "、".join(themes[:3])
                               + "\n（从情节自然流露、用结果说话，不要直白点题、不要加括号注解）")

        # 灵机一动（探测器命中后注入下一组）
        inspiration_block = ""
        if inspiration_hint:
            inspiration_block = "\n【OPTIONAL｜灵机一动】顺势落地\n" + inspiration_hint.strip()

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
        # 读者承诺合同：本章必达 / 本桥段收束 / 已逾期 / 必出场角色 / 读者可见变化（免费规则）
        promises_block = self._promises_block(p, chapter_num, events) if chapter_num else ""
        # 写前编辑诊断：本节要达到什么（读者欲望/爽点/敌人损失/追更理由）
        diag_block = self._pre_write_diagnosis(p, stage_name)

        # 视角铁律（防人称漂移：显式重申，不让模型自己定）
        _pov = str((self.storyline.basic_info or {}).get("pov", "") if self.storyline else "").strip()
        if _pov == "第一人称":
            pov_block = "【视角铁律】全篇第一人称「我」叙事；禁止叙事段落跳出第三人称「他/她」；对话内人物称谓不受限。\n\n"
        elif _pov == "第三人称":
            pov_block = "【视角铁律】全篇第三人称（他/她/名字）叙事；禁止叙事段落突现第一人称「我」（内心独白可保留）；一段内严禁「他」「我」混用。\n\n"
        else:
            pov_block = "【视角铁律】全篇统一人称，禁止第一/第三人称混用。\n\n"

        # 本桥段出场人物（性格/性别/口头禅，防"她"字错误、保持声线）
        roles_block = self._roles_block(p) if getattr(p, "roles", None) else ""
        # 分角色态势表：本桥段每个出场角色的行动方向/去向/内心/语气（规则层，零成本）
        roles_status_block = self._roles_status_block(item) if getattr(p, "roles", None) else ""

        # 本章参与者（本弧其余桥段出场角色并集）——防逐桥段重复注入、防漏写后续才出场的人
        chapter_participants_block = ""
        if chapter_participants:
            chapter_participants_block = ("\n【本章参与者】" + chapter_participants
                                          + "\n（本章/本弧出场的全部角色，硬事实同样适用；本桥段精确出场见上）")

        bible = self.build_book_bible_condensed()
        bible_block = f"【CANON｜书级设定（简）】\n{bible}\n\n" if bible else ""

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

        authority_block = AUTHORITY_CANON + "\n\n"
        return f"""你是一位专业的中文网络小说作者，正在逐段续写正文。每轮只输出 3-5 个句子。

{authority_block}{bible_block}{opening_block}{consistency_block}{platform_block}{review_block}{pov_block}【所属大纲】{o.name}（第{o.start_chapter}-{o.end_chapter}章）
【当前阶段】{stage_name}
【本桥段要推动的事件】{'、'.join(events[:4]) if events else '按大纲自然推进'}
【桥段骨架】{structure}
【变量槽位】{slots_text or '跟随上下文自由发挥'}
{diag_block}
{hook_block}
{roles_block}
{roles_status_block}
{chapter_participants_block}
{theme_block}
{payoff_block}
{setup_block}
{promises_block}
{inspiration_block}

【CANON｜前文上下文】
{context_text}

【写作要求】
1. 只输出下一段正文：3-5 个句子（总共约 150-250 个汉字），一句一行；短句为基干，句长需长短交错（8-15字为主、穿插25-45字），避免全文句式单一。
2. 画面优先：用动作、对话、感官细节推进，不要堆形容词、不要抽象抒情。
3. 每组至少含一句对话或一个动作；对话独立成段并带简短神态/动作。
4. 围绕上方的"要推动的事件"制造推进感：埋冲突、留张力，组尾留一个"接下来会怎样"的悬念钩子（本桥段最后一组可自然收束）。
5. 必须紧接上文继续，人物、视角、设定保持一致，视角始终跟随主角；绝不重开新故事、不换主角。
6. 严禁出现：然而、不禁、仿佛、似乎、瞬间、顿时、缓缓、微微、眼中闪过、心中一动、微微一笑、嘴角勾起、与此同时、就在这时。
7. 不写章节标题、不标注步骤、不加解释性文字。本桥段还剩约 {budget_remaining} 字预算，控制篇幅。"""

    def _pre_write_diagnosis(self, p, stage_name: str = "") -> str:
        """写前编辑诊断：这一节要达到什么（读者欲望/最强爽点/敌人损失/章尾追更理由）。

        免费规则，模拟人类作者开写前的瞬间框架；对应人类创作思考路线第 6 步。
        """
        category = str(getattr(p, "category", "") or "")
        desire = CATEGORY_READER_DESIRE.get(category, DEFAULT_READER_DESIRE)
        enemy_loss = CATEGORY_ENEMY_LOSS.get(category, DEFAULT_ENEMY_LOSS)
        payoff = ""
        if getattr(p, "hook_points", None):
            payoff = p.hook_points[0]
        elif stage_name:
            payoff = f"{stage_name}的高潮"
        else:
            payoff = getattr(p, "name", "") + "的高潮"
        return (
            "【写前诊断——本节要达到什么】\n"
            f"- 读者此刻欲望：{desire}\n"
            f"- 本节最强可见爽点：{payoff}\n"
            f"- 敌人/阻力可见损失：{enemy_loss}\n"
            f"- 章尾追更理由：本节结尾留一个具体悬念，让读者想知道「接下来会怎样」\n\n"
        )

    def _promises_block(self, p, chapter_num: int, events=None) -> str:
        """读者承诺合同块：本章必达 / 本桥段收束 / 已逾期 / 必出场角色 / 读者可见变化。

        竞品借鉴：AI-NWA obligation_contract + reader_experience（简化版）。
        免费规则，从 storyline.promises 现算——模拟人类作者的"伏笔账本"：
        写前扫一眼还有哪些欠读者没还、哪个逾期了、本章必须兑现什么。
        """
        if not self.storyline:
            return ""
        promises = getattr(self.storyline, "promises", None) or []
        active = [q for q in promises if q.get("status") == "pending"]
        if not active:
            return ""
        resolving = [q for q in active
                     if q.get("setup_plot_id") and q.get("setup_plot_id") == getattr(p, "resolves_plot_id", "")]
        must_hit = [q for q in active
                    if (q.get("deadline_chapter") or 0) == chapter_num]
        overdue = [q for q in active
                   if (q.get("deadline_chapter") or 0) and (q.get("deadline_chapter") or 0) < chapter_num]
        reserved = {id(q) for q in resolving} | {id(q) for q in must_hit} | {id(q) for q in overdue}
        others = [q for q in active if id(q) not in reserved][:2]

        _OP_TAG = {
            "seed": "刚埋设·保持存在感",
            "touch": "维持·轻提即可",
            "pressure": "施压·临期/逾期",
            "partial_reveal": "部分揭示·留悬念",
            "payoff": "兑现",
        }
        lines = []
        if must_hit:
            lines.append("【伏笔·兑现】本章必达：兑现「" + (must_hit[0].get("desc", "") or "前文钩子")
                         + "」，本章内必须让读者看到结果/推进。")
        if resolving:
            lines.append("【伏笔·兑现】本桥段收束：兑现读者承诺「" + (resolving[0].get("desc", "") or "前文钩子")
                         + "」，给出结果/反转、补上闭环。")
        if overdue:
            lines.append("【伏笔·施压】已逾期读者承诺（本章内请推进或兑现其一）："
                         + "；".join((q.get("desc", "") or "钩子") for q in overdue[:2]))
        for q in others:
            op = promise_op(q, chapter_num)
            tag = _OP_TAG.get(op, "touch")
            lines.append(f"【伏笔·{tag}】{q.get('desc', '') or '钩子'}（可择机自然推进）")
        # 必出场角色：承诺 desc 里提到的本桥段角色（兑现承诺的关键人物）
        required_roles = [r for r in (p.roles or [])
                          if any(r in (q.get("desc", "") or "") for q in active)]
        if required_roles:
            lines.append("必出场角色：" + "、".join(required_roles[:3])
                         + "（兑现承诺的关键人物，本章必须出场）")
        # 读者可见变化（netChange）：本桥段读者应看到什么变了
        net_change = self._net_change_block(p, events)
        if net_change:
            lines.append("读者可见变化：" + net_change)
        if not lines:
            return ""
        return "【读者承诺台账】\n" + "\n".join(lines) + "\n\n"

    def _net_change_block(self, p, events=None) -> str:
        """读者可见变化（netChange）：本桥段读者应看到什么变了。

        从 stage events + p.hook_points 推导（免费规则，不调 LLM）。
        """
        parts = []
        evs = [str(e) for e in (events or []) if e][:2]
        if evs:
            parts.append("、".join(evs) + " 有结果")
        hooks = list(getattr(p, "hook_points", None) or [])
        if hooks:
            parts.append(hooks[0] + " 落地")
        return "；".join(parts) if parts else ""

    def _role_hard_soft(self, c: dict) -> tuple[str, str]:
        """把角色档案拆成「硬事实 vs 软倾向」两段文案（中文标签）。"""
        hard = [f"{_HARD_LABELS.get(f, f)}={str(c.get(f, ''))[:30]}" for f in _HARD_FIELDS if c.get(f)]
        soft = [f"{_SOFT_LABELS.get(f, f)}={str(c.get(f, ''))[:30]}" for f in _SOFT_FIELDS if c.get(f)]
        return "、".join(hard), "、".join(soft)

    def _roles_block(self, p) -> str:
        """本桥段出场人物：硬事实（身份/势力/境界/位置）+ 软倾向（性格/口头禅/简介）。

        竞品借鉴：AI-NWA character_hard_facts——软倾向只作语气参考，
        不写成旁白确认的事实（防「她字错误/身份穿帮」）。
        """
        if not self.storyline:
            return ""
        bi = self.storyline.basic_info or {}
        protag = get_mc(bi)
        mc_name = str(protag.get("name", "") or "").strip()
        cast_map = {}
        for c in get_characters(bi):
            if isinstance(c, dict) and c.get("name"):
                n = str(c["name"]).strip()
                if n != mc_name:
                    cast_map[n] = c
        lines = []
        for rname in (p.roles or [])[:4]:
            if rname == mc_name:
                hard, soft = self._role_hard_soft(protag)
                seg = f"- {rname}（主角）"
                if protag.get("gender"):
                    seg += f"[{protag['gender']}]"
                if hard:
                    seg += f" 硬事实：{hard}"
                if soft:
                    seg += f"；软倾向：{soft}"
                lines.append(seg)
            else:
                c = cast_map.get(rname)
                seg = f"- {rname}"
                if c:
                    if c.get("title"):
                        seg += f"（{c['title']}）"
                    if c.get("gender"):
                        seg += f"[{c['gender']}]"
                    hard, soft = self._role_hard_soft(c)
                    if hard:
                        seg += f" 硬事实：{hard}"
                    if soft:
                        seg += f"；软倾向：{soft}"
                lines.append(seg)
        if not lines:
            return ""
        return ("\n【STATE｜本桥段出场人物——硬事实不得写反；软倾向只作语气参考，不写成旁白确认的事实】\n"
                + "\n".join(lines))

    def _roles_status_block(self, item) -> str:
        """分角色态势表：本桥段每个出场角色的行动方向/去向/内心/语气。

        规则层零成本：主角占主导推进位、配角按性格反应；每个角色给独立声线，
        避免多角色同质化（设计文档 §2.3 设计 A）。
        """
        if not self.storyline:
            return ""
        p = item.get("plot")
        if not p:
            return ""
        roles = list(getattr(p, "roles", None) or [])[:4]
        if not roles:
            return ""
        bi = self.storyline.basic_info or {}
        protag = get_mc(bi)
        mc_name = str(protag.get("name", "") or "").strip()
        cast_map = {str(c.get("name", "")).strip(): c
                    for c in get_characters(bi) if isinstance(c, dict) and c.get("name")}
        stage = item.get("stage") or {}
        events = stage.get("events", []) if isinstance(stage, dict) else []
        event_txt = "、".join(str(e) for e in events[:2]) if events else "本阶段事件"
        lines = []
        for rname in roles:
            rname = str(rname or "").strip()
            if not rname:
                continue
            if rname == mc_name:
                lines.append(
                    f"- 「{rname}」（主角）：本桥段{event_txt}的主角位——主动行动/决断/推进剧情；"
                    f"内心可流露但克制，视角锁定主角；语气："
                    f"{str(protag.get('personality', ''))[:30] or '果断、干练'}")
            else:
                c = cast_map.get(rname, {}) or {}
                rel = str(c.get("relation", "") or "")
                lines.append(
                    f"- 「{rname}」（{'主角的' + rel if rel else '配角'}）："
                    f"对{event_txt}做出符合其性格的反应，去向跟随剧情走向；"
                    f"给一句符合人设的言行或心声，与主角声线区分；"
                    f"性格（软倾向，只作语气参考）：{str(c.get('personality', ''))[:30] or '待定'}，"
                    f"口头禅「{str(c.get('catchphrase', ''))[:20] or '无'}」")
        if not lines:
            return ""
        return ("\n【STATE｜分角色态势表——每个出场角色要有各自的行动/去向/内心/语气，避免同质化】\n"
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
                               storyline: Optional[BookStoryline] = None) -> str:
        """返回要拼到大纲 prompt 开头的上下文块（空字符串表示无需前置）。

        phase_kind ∈ analyze/sequence/select_plots/theme_review/validate
        """
        prev_storyline = self.storyline
        if storyline is not None:
            self.storyline = storyline
        try:
            if phase_kind in ("sequence", "validate"):
                bible = self.build_book_bible(max_chars=900)
                return f"【书级设定】\n{bible}\n" if bible else ""
            if phase_kind in ("select_plots", "thread_split"):
                bible = self.build_book_bible_condensed(max_chars=500)
                return f"【书级设定（简）】\n{bible}\n" if bible else ""
            if phase_kind == "theme_review":
                themes = [str(t) for t in (self.storyline.themes or [])][:6] if self.storyline else []
                return f"【全书内涵】{'、'.join(themes) if themes else '（无）'}\n"
            return ""
        finally:
            self.storyline = prev_storyline

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

    # ═══════════════════════════════════════════
    # 场景 D：世界观/设定生成（启动新书前置；集中式 prompt）
    # ═══════════════════════════════════════════

    def _seed_block(self, seed_basic_info: dict) -> str:
        """把借鉴种子格式化为 prompt 硬约束块（懒 import 避免与 world_builder 循环）。"""
        if not seed_basic_info:
            return ""
        from .world_builder import WorldBuildingGenerator
        text = WorldBuildingGenerator.seed_to_text(seed_basic_info)
        return f"【已借鉴设定 —— 新书必须继承，仅按微调句改变】\n{text}\n" if text else ""

    def render_world_build_draft_prompt(self, idea: str, genre: str = "",
                                        sub_genre: str = "", seed_basic_info=None,
                                        platform: str = "") -> str:
        """Call A：一句话 → 世界观设定短文（非 JSON，300-500 字叙事化覆盖各维度）。"""
        style = _profile_style_text(self.profile)
        parts = [
            "请根据以下一句话设定，构思一段【世界观设定短文】（300-500 字叙事化文字，不要列条目）：",
            "",
            f"【一句话设定】{idea or '（请按该题材方向标准开局自由构思）'}",
        ]
        if platform:
            parts.append(f"【目标平台】{platform}")
        if style:
            parts.append(f"【风格偏好】\n{style}")
        if seed_basic_info:
            parts.append(self._seed_block(seed_basic_info))
        tb = self._tags_block()
        if tb:
            parts.append(tb)
        parts.append("")
        parts.append("短文要用叙事化的语言把这个世界讲清楚，覆盖：时代、力量体系、地理、文化、历史、社会结构、核心矛盾。具体有画面，禁止空泛说教。")
        return "\n".join(parts)

    def render_world_build_struct_prompt(self, world_summary: str, idea: str,
                                         genre: str = "", sub_genre: str = "",
                                         seed_basic_info=None, profile=None) -> str:
        """Call B：设定短文 → 结构化 JSON（扩展世界观 + 主角/配角推导）。"""
        style = _profile_style_text(profile if profile is not None else self.profile)
        parts = [
            f"【一句话设定】{idea or ''}",
        ]
        if str(world_summary or "").strip():
            parts.append(f"\n【已构思的世界观设定文】\n{str(world_summary).strip()}")
        if style:
            parts.append(f"\n【风格偏好】\n{style}")
        tb = self._tags_block()
        if tb:
            parts.append(f"\n{tb}")
        parts.append(
            "\n【任务】先审视上面的世界观设定文，再在其上完成以下结构化提取，严格返回 JSON（不要任何额外文字）：\n"
            "1. 先确认/补全世界观各维度（与设定文一致，可适度延伸）。\n"
            "2. 再据此推导主角：身份、性格、背景、金手指必须与世界观自洽（主角诞生于这个世界/这个时代）。\n"
            "3. 再推导 2-3 个关键配角：与主角关系明确，称谓/口头禅具体。\n"
            "4. 金手指的数值/技能语义写死（如\"效率×2\"指具体什么翻倍），全书口径唯一。\n"
            "5. 基调/目标读者/视角/时代语言约束与世界观时代匹配。\n\n"
            + WORLD_BUILDING_SCHEMA_HINT
            + "\n\n返回 JSON：\n"
            + '{\n'
            + '  "world_building": {"era":"","power_system":"","factions":[{"name":"","stance":""}],"rules":[],"geography":"","culture":"","history":"","social_structure":"","core_conflict":"","world_summary":""},\n'
            + '  "protagonist": {"name":"","identity":"","personality":"","background":"","golden_finger":"","gender":"","age":0,"death_year":0},\n'
            + '  "supporting_cast": [{"name":"","role":"","relation":"","gender":"男/女","title":"","personality":"","catchphrase":"","brief":""}],\n'
            + '  "tone": "",\n'
            + '  "target_audience": "",\n'
            + '  "pov": "第三人称",\n'
            + '  "era_language": ""\n'
            + '}'
        )
        return "\n".join(parts)

    def render_world_candidates_prompt(self, idea: str, genre: str = "",
                                       sub_genre: str = "", count: int = 5,
                                       tags=None, existing_candidates=None) -> str:
        """示例候选：一次产出 count 个差异化世界观候选。

        步 1 用户给一句话设定 + 题材标签、不另选题材——【题材方向】未指定时要求 AI
        从一句话/题材标签自行推导，避免输出被示例模板固化。
        existing_candidates 传入时改为「增量」模式：只生成 **1 个**与已有候选
        差异明显的新方向（逐个生成、给足思考空间，质量高于一次多个）。
        """
        tb = self._tags_block()   # 书内已有标签时走它（书内部分/存量书路径）
        if not tb and tags:
            _tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
            if _tags:
                tb = ("【题材标签（硬约束）】" + "、".join(_tags)
                      + "。世界观候选必须契合这些标签的网文套路与读者预期，"
                        "禁止漂移到标签之外题材。")
        parts = [
            f"【一句话设定】{idea or '（无，按题材方向自由发散）'}",
            tb if tb else "",
        ]
        if existing_candidates:
            listed = "\n".join(
                f"- {i}.「{c.get('title') or ''}」{(c.get('one_liner') or '')}"
                for i, c in enumerate(existing_candidates, 1))
            parts.append(
                f"【已生成候选】\n{listed}\n"
                f"【要求】在已有候选基础上，只产出 **1 个**与它们差异最明显、最出彩的**新**世界观方向"
                f"（严禁与已有候选重复/雷同；给足这一方向的深度设定与差异化亮点）。"
                f"若未指定题材方向：先自行推导这句话隐含的题材方向，再在该框架内挑一个与众不同的走向。")
        else:
            parts.append(
                f"【要求】从这句话发散出 {count} 个截然不同的世界观方向，方向之间差异要明显。"
                f"若未指定题材方向：先自行推导这句话隐含的题材方向（例如用户想写的是都市、玄幻、科幻、悬疑、历史等），"
                f"再在该题材框架内发散差异明显的方向；不要套用固定的题材细分模板，候选只体现同源设定下的不同走向。")
        parts.append('返回 JSON：{"candidates":[{"title":"候选名/书名","one_liner":"一句话核心设定（可直接作为新书的一句话种子）","world_brief":"120-200字世界观简述","genre_hint":"题材细分标签"}]}')
        return "\n".join(parts)

    def render_characters_prompt(self, idea: str, genre: str = "",
                                 sub_genre: str = "", tags=None, title: str = "",
                                 archetypes=None, core_conflict: str = "",
                                 factions=None, outline_preview: str = "") -> str:
        """根据世界观（一句话 + 题材标签 + 书名 + 角色原型库）生成角色候选（向导③，Agent 经 set_characters 填入）。

        题材标签在向导步 1 选择、书名由步 2 选中候选带入步 3；角色从原型库挑选
        archetype_id 并适配到本书，输出统一字段（姓名/身份/性格/口癖/重要度/金手指(主角)/关系(其他)）。
        可带已定核心矛盾/势力/开篇大纲桥段上下文（分阶段构建的 ①③② 阶段产出），让角色与之自洽。
        """
        tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        parts = [
            f"【书名】{title or '（待定）'}",
            f"【世界观】{idea or '（无，按题材方向自由发散）'}",
        ]
        if tags:
            parts.append(f"【题材标签】{'、'.join(tags)}（硬约束，必须契合）")
        if core_conflict:
            parts.append(f"【已定核心矛盾】{core_conflict}")
        if factions:
            _fl = []
            for f in factions:
                if isinstance(f, dict):
                    _fl.append((f.get("name") or "") + ("：" + f.get("stance") if f.get("stance") else ""))
                else:
                    _fl.append(str(f))
            if _fl:
                parts.append("【已定势力】" + "、".join(_fl))
        if outline_preview:
            parts.append(f"【已选开篇大纲与桥段】{outline_preview}")
        if archetypes:
            lines = []
            for a in archetypes[:10]:
                a_ph = "、".join((a.get("catchphrases") or [])[:2]) or "—"
                a_tags = "、".join(a.get("tags") or []) or "—"
                a_fit_tags = "、".join(a.get("fit_tags") or []) or "—"
                lines.append(f"- {a.get('id')} {a.get('name')}（性格:{a.get('personality') or '—'}｜"
                             f"标签:{a_tags}｜适配:{a_fit_tags}｜口癖:{a_ph}）")
            parts.append("【可选角色原型（从中挑选 archetype_id 并适配到本书）】\n" + "\n".join(lines))
        parts.append(
            "你是网文人物策划。根据上述世界观、书名与可选角色原型：\n"
            "1. 发散 3 个主角候选（每个含 姓名/身份/性格/口癖/金手指，重要度 importance=1，"
            "必须与世界观和书名自洽）；\n"
            "2. 发散 5 个其他角色候选（每个含 姓名/身份/与主角的关系/性格/口癖，"
            "重要度 importance=2~5 按戏份递减，彼此要有区分度）。\n"
            "3. 每个角色必须从【可选角色原型】中挑选一个 archetype_id 作为原型基础，"
            "并把原型适配成符合本书世界观的具体角色；没有合适原型时可省略 archetype_id。\n"
            "4. 每个角色补充：gender 性别、age 年龄（数字，未知填 0）、death_year 死亡年份"
            "（数字，未定/健在填 0）、title 称呼/称号、brief 100 字内人物简介——"
            "这些字段让书详情页人物卡片完整，不要留空。\n"
            "5. 势力标注：若世界观已有势力（factions），为角色填 faction 所属势力名"
            "（与势力名一致），无势力可留空归『未归属』；金手指 golden_finger 仅主角/"
            "关键角色需要，配角一律留空。\n"
            '只返回 JSON：{"protagonists":[{"name":"","identity":"","personality":"","catchphrase":"","golden_finger":"","faction":"","importance":1,"archetype_id":"","gender":"","age":0,"death_year":0,"title":"","brief":""},'
            '{"name":"","identity":"","personality":"","catchphrase":"","golden_finger":"","faction":"","importance":1,"archetype_id":"","gender":"","age":0,"death_year":0,"title":"","brief":""},'
            '{"name":"","identity":"","personality":"","catchphrase":"","golden_finger":"","faction":"","importance":1,"archetype_id":"","gender":"","age":0,"death_year":0,"title":"","brief":""}],'
            '"supporting_cast":[{"name":"","identity":"","relation":"","personality":"","catchphrase":"","faction":"","importance":2,"archetype_id":"","gender":"","title":"","brief":"","age":0,"death_year":0},'
            '{"name":"","identity":"","relation":"","personality":"","catchphrase":"","faction":"","importance":2,"archetype_id":"","gender":"","title":"","brief":"","age":0,"death_year":0},'
            '{"name":"","identity":"","relation":"","personality":"","catchphrase":"","faction":"","importance":3,"archetype_id":"","gender":"","title":"","brief":"","age":0,"death_year":0},'
            '{"name":"","identity":"","relation":"","personality":"","catchphrase":"","faction":"","importance":3,"archetype_id":"","gender":"","title":"","brief":"","age":0,"death_year":0},'
            '{"name":"","identity":"","relation":"","personality":"","catchphrase":"","faction":"","importance":4,"archetype_id":"","gender":"","title":"","brief":"","age":0,"death_year":0}]}'
        )
        return "\n".join(parts)

    def render_core_conflict_prompt(self, idea: str, genre: str = "",
                                    sub_genre: str = "", tags=None,
                                    profile=None) -> str:
        """分阶段构建①：从一句话设定+题材标签推导故事主线的核心矛盾（驱动全书的根本冲突，1-2 句）。

        返回纯文本，供 generate_core_conflict 使用。
        """
        tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        style = ""
        if profile and getattr(profile, "summary", ""):
            style = f"\n【笔名风格】{profile.summary}"
        return "\n".join([
            f"【一句话设定】{idea or '（无）'}",
            f"【题材标签】{'、'.join(tags) if tags else '（未选）'}（硬约束，必须契合）",
            style,
            "【任务】你是网文故事架构师。思考这本书的主线应该由什么样的核心矛盾驱动——"
            "这是贯穿全书的根本冲突（人物目标 × 世界阻力 × 无法两全），1-2 句说清，"
            "要具体可驱动后续势力/人物/桥段，不要空泛（例：'主角的复制异能每升级一次就吞噬一段记忆，"
            "他必须在变强与找回自己之间抉择，而幕后组织正等着他失去自我'）。",
            "只返回核心矛盾一句话，不要解释、不要多余内容。",
        ])

    def render_factions_prompt(self, idea: str, core_conflict: str = "",
                               genre: str = "", sub_genre: str = "",
                               tags=None, outline_preview: str = "") -> str:
        """分阶段构建③：基于一句话设定 + 核心矛盾 + 题材标签 + 已定大纲桥段，发散世界里的主要势力派系。

        返回 JSON list，供 generate_factions 使用。
        """
        tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        parts = [
            f"【一句话设定】{idea or '（无）'}",
            f"【核心矛盾】{core_conflict or '（未定）'}",
            f"【题材标签】{'、'.join(tags) if tags else '（未选）'}（硬约束，必须契合）",
        ]
        if outline_preview:
            parts.append(f"【已定大纲与桥段】\n{outline_preview}")
        parts += [
            "【任务】你是网文世界观架构师。思考这个世界应该存在哪些势力/派系（2-4 个），"
            "它们围绕【核心矛盾】各自持什么立场、追求什么，彼此冲突或结盟。每个势力给出："
            "name 名称、stance 立场（一句）、desc 背景与目标（一句）。势力要呼应核心矛盾与已定故事线，不要泛泛的'官方''反派'。",
            '只返回 JSON：{"factions":[{"name":"","stance":"","desc":""}]}',
        ]
        return "\n".join(parts)
