"""
节拍级写作引擎（Beat-Level Writing Engine）
模拟人类写作：一句一句思考，每个位置放什么都是设计好的

流程：
  大纲 → 节拍规划 → 逐节拍生成(模板+笑点+风格) → 组装 → 连续性校验 → 成章
"""
from dataclasses import dataclass, field
from typing import Optional
import re


# ═══════════════════════════════════════════
# 节拍模板
# ═══════════════════════════════════════════

@dataclass
class BeatTemplate:
    """微模板 — 一个叙事节拍的写作蓝图"""
    id: str
    name: str
    beat_type: str              # hook/action/dialogue/twist/close/...
    narrative_function: str     # 这个节拍在叙事上完成什么任务
    micro_structure: str        # 微观结构骨架，如 "[触发]→[反应]→[结果]"
    slots: list[dict] = field(default_factory=list)  # 变量槽
    humor_slots: list[dict] = field(default_factory=list)
    # humor_slots: [{"position": "开头第一句", "type": "吐槽/反差/误会", "priority": "必须"}]
    word_target: int = 300
    transition_hint: str = ""   # 与前后节拍的衔接提示
    examples: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════
# 内置节拍模板库
# ═══════════════════════════════════════════

BEAT_TEMPLATES: list[BeatTemplate] = [
    # ─── 开篇钩子 ───
    BeatTemplate(
        id="beat_hook_crisis",
        name="危机开场",
        beat_type="hook",
        narrative_function="用强烈的危机/冲突/反转抓住读者，必须在第一句话就制造悬念或冲击",
        micro_structure="[冲击性画面/对话] → [快速解释发生了什么] → [抛出问题：接下来会怎样]",
        humor_slots=[
            {"position": "解释部分的吐槽句", "type": "主角内心吐槽/黑色幽默",
             "priority": "必须", "note": "用主角的幽默反应化解紧张，让读者笑的同时继续看下去"}
        ],
        word_target=200,
        transition_hint="不承接上文，因为是第一章开头",
        examples=[
            "闹钟响的时候，萧晨正梦见自己站在纳斯达克敲钟。底下掌声雷动。然后他就被人一脚踹下了台。",
        ],
    ),
    BeatTemplate(
        id="beat_hook_mystery",
        name="悬念开场",
        beat_type="hook",
        narrative_function="用一个反常/神秘的现象或一句话制造悬念",
        micro_structure="[反常现象/神秘物品/诡异对话] → [主角的疑惑] → [留下问题]",
        humor_slots=[
            {"position": "主角第一反应", "type": "反差式吐槽",
             "priority": "建议", "note": "主角面对诡异事物的不正经反应制造反差萌"}
        ],
        word_target=200,
        transition_hint="不承接上文",
    ),

    # ─── 现状展示 ───
    BeatTemplate(
        id="beat_status_low",
        name="落魄现状",
        beat_type="status",
        narrative_function="展示主角当前的落魄/困境，为后面的逆袭做铺垫",
        micro_structure="[环境描写(穷/破/惨)] → [主角的反应(认命/不甘/自嘲)] → [一个细节暗示不甘心]",
        humor_slots=[
            {"position": "自嘲句", "type": "自黑式幽默",
             "priority": "必须", "note": "主角越惨越要自嘲，读者才会心疼又好笑"}
        ],
        word_target=300,
        transition_hint="承接钩子，展示日常状态",
    ),
    BeatTemplate(
        id="beat_status_normal",
        name="日常状态",
        beat_type="status",
        narrative_function="展示主角的日常，为后续事件提供对比基线",
        micro_structure="[日常场景] → [一个小事件展示主角性格] → [不经意间埋一个伏笔]",
        humor_slots=[
            {"position": "小事件中的对话/反应", "type": "冷幽默/吐槽",
             "priority": "建议", "note": "日常中的幽默要自然，不能刻意"}
        ],
        word_target=300,
    ),

    # ─── 冲突触发 ───
    BeatTemplate(
        id="beat_conflict_confront",
        name="正面冲突",
        beat_type="conflict",
        narrative_function="主角与对手的直接对抗，展示矛盾升级",
        micro_structure="[挑衅/攻击(对方先动手)] → [主角反应(先忍/直接刚)] → [冲突结果]",
        humor_slots=[
            {"position": "主角出招前/后的犀利吐槽", "type": "嘴炮/毒舌",
             "priority": "必须", "note": "打之前先嘴炮，打完再补刀，网文爽点核心"}
        ],
        word_target=400,
    ),
    BeatTemplate(
        id="beat_conflict_verbal",
        name="言语交锋",
        beat_type="conflict",
        narrative_function="通过对话制造冲突，展示角色关系和性格",
        micro_structure="[对方挑衅] → [主角回击(有层次地)] → [对方反应] → [主角最后一句绝杀]",
        humor_slots=[
            {"position": "主角回击中的关键句", "type": "反讽/打脸式幽默",
             "priority": "必须", "note": "最后一句绝杀要又狠又好笑"}
        ],
        word_target=350,
    ),

    # ─── 行动/展示 ───
    BeatTemplate(
        id="beat_action_show_power",
        name="展现实力",
        beat_type="action",
        narrative_function="主角第一次展示自己的能力/金手指，产生震撼效果",
        micro_structure="[困境需要能力] → [主角犹豫/果断使用能力] → [能力产生效果] → [围观者反应(震惊/质疑)]",
        humor_slots=[
            {"position": "围观者反应处", "type": "夸张反应/反差",
             "priority": "必须", "note": "路人震惊的夸张反应是网文经典爽点+笑点"}
        ],
        word_target=400,
    ),
    BeatTemplate(
        id="beat_action_clever_move",
        name="机智应对",
        beat_type="action",
        narrative_function="主角用智慧而非武力解决问题",
        micro_structure="[棘手局面] → [主角观察到关键细节] → [巧妙化解] → [对手吃瘪]",
        humor_slots=[
            {"position": "对手吃瘪的瞬间", "type": "打脸幽默",
             "priority": "必须", "note": "对手被打脸的戏剧性瞬间，越难堪越好笑"}
        ],
        word_target=350,
    ),

    # ─── 反转 ───
    BeatTemplate(
        id="beat_twist_reveal",
        name="身份/真相揭露",
        beat_type="twist",
        narrative_function="揭示一个隐藏的信息/身份，颠覆读者的认知",
        micro_structure="[铺垫(暗示有秘密)] → [关键时刻揭露] → [各方反应(震惊)] → [主角轻描淡写]",
        humor_slots=[
            {"position": "主角揭露后的淡定反应", "type": "凡尔赛/淡定装逼",
             "priority": "建议", "note": "越是大事越轻描淡写，反差制造笑点"}
        ],
        word_target=300,
    ),
    BeatTemplate(
        id="beat_twist_expectation",
        name="预期反转",
        beat_type="twist",
        narrative_function="读者以为A会发生，结果是B",
        micro_structure="[制造预期(暗示A)] → [关键时刻B发生] → [解释为什么是B(合理)]",
        humor_slots=[
            {"position": "反转揭露的表述方式", "type": "反差萌/欧亨利式",
             "priority": "建议"}
        ],
        word_target=300,
    ),

    # ─── 情感时刻 ───
    BeatTemplate(
        id="beat_emotion_bond",
        name="情感连接",
        beat_type="emotion",
        narrative_function="主角与他人建立情感联系，展示柔软一面",
        micro_structure="[一个温暖的场景/细节] → [主角的反应(可能不擅长表达)] → [情感共鸣]",
        humor_slots=[
            {"position": "主角笨拙的情感表达", "type": "直男式温情",
             "priority": "建议", "note": "越不会表达感情越有喜剧效果"}
        ],
        word_target=300,
    ),

    # ─── 章末 ───
    BeatTemplate(
        id="beat_close_cliffhanger",
        name="章末钩子",
        beat_type="close",
        narrative_function="结束本章，用一个强钩子让读者必须看下一章",
        micro_structure="[本章收束] → [一个意外发现/突然事件] → [切断——留下问题]",
        humor_slots=[],  # 章末钩子一般不搞幽默，要制造紧张
        word_target=200,
    ),
    BeatTemplate(
        id="beat_close_resolve",
        name="圆满收束",
        beat_type="close",
        narrative_function="本章问题解决，主角获得阶段性成果",
        micro_structure="[成果展示] → [主角反思/规划] → [轻松的氛围中埋下一个小伏笔]",
        humor_slots=[
            {"position": "轻松氛围中的对话", "type": "轻松调侃",
             "priority": "建议", "note": "大事件后的放松对话，自然流露的幽默"}
        ],
        word_target=250,
    ),

    # ─── 过渡 ───
    BeatTemplate(
        id="beat_transition_time",
        name="时间过渡",
        beat_type="transition",
        narrative_function="跳转时间/场景，用简练的笔墨完成时空切换",
        micro_structure="[上一节拍结束状态] → [时间/场景切换(一句话)] → [新场景的状态]",
        humor_slots=[],
        word_target=100,
    ),
]


# ═══════════════════════════════════════════
# 节拍模板库
# ═══════════════════════════════════════════

class BeatLibrary:
    """节拍模板库"""

    def __init__(self):
        self.templates: dict[str, BeatTemplate] = {
            t.id: t for t in BEAT_TEMPLATES
        }

    def get(self, beat_id: str) -> Optional[BeatTemplate]:
        return self.templates.get(beat_id)

    def match(self, narrative_need: str, context: str = "") -> Optional[BeatTemplate]:
        """根据叙事需求匹配最合适的节拍模板"""
        scored = []
        for t in self.templates.values():
            score = 0
            need_lower = narrative_need.lower()
            if t.beat_type in need_lower:
                score += 3
            if any(w in need_lower for w in ["冲突", "对抗", "打脸"]):
                if t.beat_type == "conflict": score += 5
            if any(w in need_lower for w in ["展示", "实力", "出手"]):
                if t.beat_type == "action": score += 5
            if any(w in need_lower for w in ["开头", "开场", "钩子"]):
                if t.beat_type == "hook": score += 5
            if any(w in need_lower for w in ["结尾", "收束", "钩子"]):
                if t.beat_type == "close": score += 5
            if any(w in need_lower for w in ["反转", "揭露", "身份"]):
                if t.beat_type == "twist": score += 5
            if any(w in need_lower for w in ["过渡", "切换", "跳过"]):
                if t.beat_type == "transition": score += 5
            scored.append((score, t))
        scored.sort(key=lambda x: -x[0])
        return scored[0][1] if scored and scored[0][0] > 0 else None


# ═══════════════════════════════════════════
# 节拍规划器
# ═══════════════════════════════════════════

@dataclass
class Beat:
    """一个节拍"""
    index: int
    beat_type: str                # hook/status/conflict/action/twist/emotion/close/transition
    description: str              # 这个节拍要写什么
    template_id: str              # 使用的微模板ID
    humor_required: bool = True   # 是否必须有笑点
    word_target: int = 300
    transition_from_prev: str = ""  # 与上一节拍的衔接


@dataclass
class ChapterBeatPlan:
    """章的节拍规划"""
    chapter_num: int
    chapter_title: str
    chapter_outline: str          # 原始大纲
    beats: list[Beat] = field(default_factory=list)
    total_words_target: int = 3000


class ChapterPlanner:
    """章节节拍规划器 — 把大纲拆成节拍序列"""

    def __init__(self, beat_lib: BeatLibrary, llm_client=None):
        self.beat_lib = beat_lib
        self.llm = llm_client

    def plan_chapter(self, chapter_num: int, chapter_outline: str,
                     target_words: int = 3000,
                     genre: str = "都市",
                     previous_chapter_summary: str = "",
                     character_states: str = "",
                     active_foreshadows: str = "") -> ChapterBeatPlan:
        """
        将一章大纲拆解为 6-10 个节拍。

        如果 LLM 可用，用 AI 智能规划；否则用规则。
        """
        if self.llm:
            return self._ai_plan(chapter_num, chapter_outline, target_words,
                                 genre, previous_chapter_summary,
                                 character_states, active_foreshadows)
        else:
            return self._rule_plan(chapter_num, chapter_outline, target_words)

    def _ai_plan(self, chapter_num: int, chapter_outline: str,
                 target_words: int, genre: str,
                 prev_summary: str, char_states: str,
                 foreshadows: str) -> ChapterBeatPlan:
        """AI 智能节拍规划"""
        available_beats = "\n".join(
            f"- {t.id}: {t.name} ({t.beat_type}) — {t.narrative_function[:60]}"
            for t in self.beat_lib.templates.values()
        )

        context_parts = []
        if prev_summary:
            context_parts.append(f"【前文章节摘要】\n{prev_summary}")
        if char_states:
            context_parts.append(f"【角色当前状态】\n{char_states}")
        if foreshadows:
            context_parts.append(f"【活跃伏笔】\n{foreshadows}")

        prompt = f"""你是专业的网文章节策划。将下面的大纲拆解为 6-10 个叙事节拍。

【本章大纲】
{chapter_outline}

【可用节拍模板】
{available_beats}

【要求】
1. 每个节拍描述要具体——说清楚"这一段写什么"，不是泛泛的"展示现状"
2. 节拍之间要有因果递进关系，不是并列罗列
3. 前3个节拍中必须有一个是 hook 类（开头抓人）
4. 必须有至少一个 conflict 或 action 类节拍（核心冲突/展示）
5. 结尾必须是 close 类（钩子或收束）
6. 每个节拍都要指定对应的 template_id（从上面选）
7. 目标总字数 {target_words} 字，各节拍按比例分配

{chr(10).join(context_parts)}

返回 JSON：
{{"beats": [
  {{"index": 1, "beat_type": "hook", "description": "具体写什么",
    "template_id": "beat_hook_crisis", "word_target": 200,
    "humor_required": true}},
  ...
]}}
"""
        try:
            from core.llm_client import extract_json
            raw = self.llm.call(
                "你是专业的网文章节策划。只返回JSON。", prompt,
                temperature=0.5, max_tokens=2048)
            data = __import__('json').loads(extract_json(raw))
            beats = []
            for b in data.get("beats", []):
                beats.append(Beat(
                    index=b["index"],
                    beat_type=b.get("beat_type", "status"),
                    description=b.get("description", ""),
                    template_id=b.get("template_id", "beat_status_normal"),
                    humor_required=b.get("humor_required", True),
                    word_target=b.get("word_target", 300),
                ))
            # 设置衔接
            for i in range(1, len(beats)):
                beats[i].transition_from_prev = beats[i-1].description[:50]
            return ChapterBeatPlan(
                chapter_num=chapter_num,
                chapter_title=f"第{chapter_num}章",
                chapter_outline=chapter_outline,
                beats=beats,
                total_words_target=target_words,
            )
        except Exception:
            return self._rule_plan(chapter_num, chapter_outline, target_words)

    def _rule_plan(self, chapter_num: int, chapter_outline: str,
                   target_words: int) -> ChapterBeatPlan:
        """规则驱动的节拍规划（fallback）"""
        outline = chapter_outline

        # 根据大纲关键词匹配节拍
        beats = []

        # 1. 开头必须有钩子
        if any(w in outline for w in ["危机", "死亡", "冲突", "战斗", "追杀"]):
            beats.append(Beat(1, "hook", "危机开场：直接进入紧张局面", "beat_hook_crisis", True, 200))
        else:
            beats.append(Beat(1, "hook", "悬念开场：用反常现象抓住读者", "beat_hook_mystery", True, 200))

        # 2. 现状展示
        beats.append(Beat(2, "status", "展示主角当前处境和状态", "beat_status_low", True, 300))

        # 3-4. 核心事件（根据大纲内容判断）
        if any(w in outline for w in ["冲突", "对手", "挑衅", "打脸"]):
            beats.append(Beat(3, "conflict", "正面冲突：对手挑衅主角", "beat_conflict_confront", True, 400))
        elif any(w in outline for w in ["展示", "实力", "能力", "出手"]):
            beats.append(Beat(3, "action", "展现实力：主角第一次出手", "beat_action_show_power", True, 400))
        else:
            beats.append(Beat(3, "conflict", "言语交锋：制造第一波张力", "beat_conflict_verbal", True, 350))

        # 4. 反应/后果
        beats.append(Beat(4, "action", "应对与反应：展示冲突后果", "beat_action_clever_move", True, 300))

        # 5-6. 中间节拍
        if any(w in outline for w in ["反转", "身份", "揭露", "秘密"]):
            beats.append(Beat(5, "twist", "反转揭露：揭示隐藏信息", "beat_twist_reveal", True, 300))
        else:
            beats.append(Beat(5, "emotion", "情感连接：展示主角另一面", "beat_emotion_bond", True, 250))

        # 6. 过渡
        beats.append(Beat(6, "transition", "过渡：为新场景铺垫", "beat_transition_time", False, 100))

        # 7. 结尾
        if any(w in outline for w in ["悬念", "钩子", "接下来"]):
            beats.append(Beat(7, "close", "章末钩子：留下悬念", "beat_close_cliffhanger", False, 200))
        else:
            beats.append(Beat(7, "close", "圆满收束：成果展示+小伏笔", "beat_close_resolve", True, 200))

        # 设置衔接
        for i in range(1, len(beats)):
            beats[i].transition_from_prev = beats[i-1].description[:50]

        return ChapterBeatPlan(
            chapter_num=chapter_num,
            chapter_title=f"第{chapter_num}章",
            chapter_outline=chapter_outline,
            beats=beats,
            total_words_target=target_words,
        )


# ═══════════════════════════════════════════
# 节拍执行器
# ═══════════════════════════════════════════

@dataclass
class BeatResult:
    """一个节拍的生成结果"""
    index: int
    text: str
    word_count: int
    humor_applied: bool
    template_used: str


class BeatExecutor:
    """逐节拍执行器 — 模拟人类一句一句写作"""

    def __init__(self, llm_client=None, beat_lib: BeatLibrary = None,
                 gag_lib=None, profile=None):
        self.llm = llm_client
        self.beat_lib = beat_lib or BeatLibrary()
        self.gag_lib = gag_lib
        self.profile = profile

    def execute_beat(self, beat: Beat, context: dict,
                     accumulated_text: str = "",
                     library_enrichment: str = "") -> BeatResult:
        """
        执行一个节拍：匹配模板 → 注入笑点+库材料 → LLM生成 → 返回结果

        library_enrichment: 来自 BookAssembler 的桥段/笑点/内涵 prompt 注入
        """
        template = self.beat_lib.get(beat.template_id)
        if not template:
            template = self.beat_lib.match(beat.beat_type) or \
                       self.beat_lib.get("beat_status_normal")

        # 构建节拍级 prompt
        system, user = self._build_beat_prompt(
            beat, template, context, accumulated_text, library_enrichment)

        # 生成
        if self.llm:
            raw = self.llm.call(system, user, temperature=0.8,
                                max_tokens=max(1024, beat.word_target * 2))
        else:
            raw = f"[节拍 {beat.index}: {beat.description}]"

        import re as _re
        wc = len(_re.findall(r'[\u4e00-\u9fff]', raw))

        return BeatResult(
            index=beat.index,
            text=raw.strip(),
            word_count=wc,
            humor_applied=beat.humor_required,
            template_used=template.id,
        )

    def _build_beat_prompt(self, beat: Beat, template: BeatTemplate,
                           context: dict, accumulated: str,
                           library_enrichment: str = "") -> tuple[str, str]:
        """构建单个节拍的写作 prompt"""
        parts = []

        # 1. 上下文
        parts.append(f"你正在写第 {context.get('chapter_num', '?')} 章的第 {beat.index} 个节拍。")
        parts.append(f"本书流派：{context.get('genre', '')}")
        if context.get('pen_name'):
            parts.append(f"笔名风格：{context.get('pen_name', '')}")

        # 2. 已生成的前文（关键！保证连续性）
        if accumulated:
            # 只保留最后 500 字作为上下文
            recent = accumulated[-500:] if len(accumulated) > 500 else accumulated
            parts.append(f"\n【紧接上文（无缝衔接，不要重复）】\n{recent}")

        # 3. 本章大纲
        if context.get('chapter_outline'):
            parts.append(f"\n【本章整体大纲（注意你的节拍在其中的位置）】\n{context['chapter_outline']}")

        # 4. 角色状态
        if context.get('character_states'):
            parts.append(f"\n【角色状态】\n{context['character_states']}")

        # 5. 本节的叙事任务
        parts.append(f"\n【本节的叙事任务：{template.name}】")
        parts.append(f"完成目标：{template.narrative_function}")
        parts.append(f"微观结构：{template.micro_structure}")

        # 6. 库材料注入（桥段模板 + 笑点模式 + 内涵提示）—— 来自 BookAssembler
        if library_enrichment:
            parts.append(f"\n{library_enrichment}")

        # 7. 笑点指令（节拍级）
        if beat.humor_required and template.humor_slots:
            parts.append("\n【笑点要求 — 必须自然融入，不要生硬】")
            for hs in template.humor_slots:
                parts.append(f"- 位置：{hs['position']}")
                parts.append(f"  类型：{hs.get('type','')} | 优先级：{hs.get('priority','必须')}")
                if hs.get('note'):
                    parts.append(f"  提示：{hs['note']}")
            # 从笑点库匹配
            if self.gag_lib:
                gags = self.gag_lib.search(scene=beat.beat_type)
                if gags:
                    parts.append(f"  可选笑点模式参考：{gags[0].pattern_description[:100]}")

        # 8. 风格约束
        if self.profile:
            parts.append("\n" + self.profile.build_style_prompt())

        # 9. 字数
        parts.append(f"\n目标字数：约 {beat.word_target} 字")
        parts.append("只输出本节拍的正文，不要加标题、编号、'第X节'等元信息。")

        user = "\n".join(parts)
        system = (
            "你是一位专业的网络小说作者。请严格按照指令创作本节拍内容。"
            "确保：1) 与紧接的上文无缝衔接 2) 完成指定的叙事任务 "
            "3) 在指定位置自然地融入幽默 4) 保持笔名风格一致。"
        )
        return system, user


# ═══════════════════════════════════════════
# 章组装器
# ═══════════════════════════════════════════

class ChapterAssembler:
    """章组装器 — 拼接节拍 + 连续性校验 + 过渡润滑"""

    def __init__(self, llm_client=None, de_ai_engine=None, reviewer=None):
        self.llm = llm_client
        self.de_ai = de_ai_engine
        self.reviewer = reviewer

    def assemble(self, plan: ChapterBeatPlan, beat_results: list[BeatResult],
                 previous_chapter_ending: str = "",
                 style_profile=None) -> str:
        """
        组装所有节拍成完整一章。

        步骤：
        1. 按顺序拼接节拍文本
        2. 在节拍之间插入过渡句（如果需要）
        3. 去 AI 味
        4. 整体一致性审查
        """
        # Step 1: 拼接
        sections = []
        for i, br in enumerate(beat_results):
            text = br.text.strip()
            if i > 0 and beat_results[i-1].text.strip():
                # 检查是否需要过渡句
                prev_end = beat_results[i-1].text.strip()[-50:]
                curr_start = text[:50]
                if self._needs_transition(prev_end, curr_start):
                    text = self._insert_transition(prev_end, curr_start, text)
            sections.append(text)

        full_text = "\n\n".join(sections)

        # Step 2: 去 AI 味
        if self.de_ai:
            result = self.de_ai.process_rule_based(full_text)
            full_text = result.processed

        # Step 3: 连续性自检
        if self.llm and previous_chapter_ending:
            full_text = self._continuity_check(full_text, previous_chapter_ending)

        return full_text

    def _needs_transition(self, prev_end: str, curr_start: str) -> bool:
        """判断两个节拍之间是否需要过渡"""
        # 简单规则：如果场景/人物/时间跳跃超过一句话的跨度，需要过渡
        prev_chars = set(re.findall(r'[\u4e00-\u9fff]{2,4}', prev_end))
        curr_chars = set(re.findall(r'[\u4e00-\u9fff]{2,4}', curr_start))
        if prev_chars and curr_chars and not (prev_chars & curr_chars):
            return True  # 没有共同角色名 → 场景切换
        return False

    def _insert_transition(self, prev_end: str, curr_start: str, curr_text: str) -> str:
        """插入过渡句"""
        # 用简单的时间/空间过渡
        transitions = [
            "\n\n转眼间，",
            "\n\n与此同时，",
            "\n\n另一边，",
            "\n\n不多时，",
            "\n\n镜头一转，",
        ]
        import random
        return random.choice(transitions) + curr_text.lstrip()

    def _continuity_check(self, full_text: str, prev_ending: str) -> str:
        """LLM 做前后章连续性检查"""
        try:
            prompt = f"""检查以下两段文本的前后衔接是否自然。如果有断裂（角色名变化、时间跳跃不合逻辑、性格突变），请修复。

【上一章结尾】
{prev_ending[-300:]}

【本章开头】
{full_text[:500]}

如果衔接自然，返回原文。如果需修复，只修复衔接部分（本章前100字），不要改动其他内容。
返回 JSON：{{"needs_fix": true/false, "fixed_opening": "修复后的开头"}}"""

            from core.llm_client import extract_json
            raw = self.llm.call("你是专业的小说编辑。只返回JSON。", prompt,
                                temperature=0.3, max_tokens=1024)
            data = __import__('json').loads(extract_json(raw))
            if data.get("needs_fix") and data.get("fixed_opening"):
                # 替换前500字
                return data["fixed_opening"] + full_text[500:]
        except Exception:
            pass
        return full_text


# ═══════════════════════════════════════════
# 完整章写作管线
# ═══════════════════════════════════════════

class ChapterWriter:
    """
    完整的一章写作管线：

    1. ChapterPlanner: 大纲 → 节拍列表
    2. BeatExecutor: 逐节拍生成（模板+笑点+库材料+风格）
    3. ChapterAssembler: 拼接+过渡+去AI+校验
    """

    def __init__(self, llm_client=None, de_ai_engine=None, reviewer=None,
                 gag_lib=None, profile=None):
        self.llm = llm_client
        self.beat_lib = BeatLibrary()
        self.planner = ChapterPlanner(self.beat_lib, llm_client)
        self.executor = BeatExecutor(llm_client, self.beat_lib, gag_lib, profile)
        self.assembler = ChapterAssembler(llm_client, de_ai_engine, reviewer)

    def write_chapter(self, chapter_num: int, chapter_outline: str,
                      target_words: int = 3000,
                      genre: str = "都市",
                      pen_name: str = "",
                      previous_chapter_ending: str = "",
                      previous_summary: str = "",
                      character_states: str = "",
                      active_foreshadows: str = "",
                      on_beat=None,
                      assembler_plan=None,
                      stage_index: int = 0) -> dict:
        """
        写完整一章。

        assembler_plan: BookAssemblerPlan，如果提供，会自动注入当前阶段的桥段/笑点/内涵
        stage_index: 当前处于哪个大纲阶段（用于从计划中取对应材料）
        on_beat: 可选回调，每完成一个节拍时调用 on_beat(beat_index, beat_result)
        """
        # Step 0: 生成库材料注入文本
        library_enrichment = ""
        if assembler_plan:
            try:
                from .assembler import PlanInjector
                library_enrichment = PlanInjector.build_chapter_prompt_enrichment(
                    assembler_plan, stage_index)
            except Exception:
                pass

        # Step 1: 节拍规划
        plan = self.planner.plan_chapter(
            chapter_num, chapter_outline, target_words, genre,
            previous_summary, character_states, active_foreshadows)

        # Step 2: 逐节拍生成
        context = {
            "chapter_num": chapter_num,
            "chapter_outline": chapter_outline,
            "genre": genre,
            "pen_name": pen_name,
            "character_states": character_states,
            "active_foreshadows": active_foreshadows,
        }
        beat_results = []
        accumulated = ""
        for beat in plan.beats:
            result = self.executor.execute_beat(
                beat, context, accumulated,
                library_enrichment=library_enrichment)
            beat_results.append(result)
            accumulated += result.text + "\n\n"
            if on_beat:
                on_beat(beat.index, result)

        # Step 3: 组装
        full_text = self.assembler.assemble(
            plan, beat_results, previous_chapter_ending)

        import re as _re
        total_wc = len(_re.findall(r'[\u4e00-\u9fff]', full_text))

        return {
            "chapter_num": chapter_num,
            "text": full_text,
            "word_count": total_wc,
            "beats": len(beat_results),
            "beat_details": [
                {"index": br.index, "words": br.word_count,
                 "template": br.template_used, "humor": br.humor_applied}
                for br in beat_results
            ],
        }
