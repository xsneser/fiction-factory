"""
仿写引擎（Writing Pipeline）
模板驱动的五步仿写管线
"""
from dataclasses import dataclass, field
from typing import Optional
from libraries.plot import PlotLibrary, PlotTemplate
from libraries.gag import GagLibrary, GagPattern
from libraries.theme import ThemeLibrary, ThemeEntry
from libraries.profiles import PenNameProfile
from libraries.de_ai import DeAIEngine
from libraries.cost_tracker import CostTracker


@dataclass
class WritingContext:
    """写作上下文——一次章节生成所需的所有信息"""
    # 模板层面
    plot_template: Optional[PlotTemplate] = None  # 选中的桥段模板
    plot_variables: dict = field(default_factory=dict)  # 变量填充值
    plot_variant: str = ""                     # 桥段变体

    # 注入层
    gags: list[GagPattern] = field(default_factory=list)  # 匹配的笑点
    themes: list[ThemeEntry] = field(default_factory=list)  # 匹配的母题

    # 风格
    style_profile: Optional[PenNameProfile] = None

    # 上下文
    chapter_num: int = 0
    chapter_title: str = ""
    chapter_outline: str = ""            # 本章大纲
    history_summary: str = ""            # 前文摘要
    previous_ending: str = ""            # 上一章结尾
    character_states: str = ""           # 角色当前状态
    active_foreshadows: str = ""         # 活跃伏笔

    # 控制
    target_words: int = 3000
    temperature: float = 0.7
    de_ai: bool = True                   # 是否去AI味


@dataclass
class WritingResult:
    """写作结果"""
    content: str = ""                    # 正文
    word_count: int = 0                  # 字数
    cost: float = 0.0                    # 花费
    de_ai_result: Optional = None        # 去AI味结果
    applied_gags: list[str] = field(default_factory=list)
    applied_themes: list[str] = field(default_factory=list)


class WritingPipeline:
    """
    模板驱动的五步仿写管线：

    Step 1: 模板匹配 — 根据上下文匹配桥段
    Step 2: 变量填充 — 构建写作 prompt
    Step 3: 内容注入 — 注入笑点/母题/风格约束
    Step 4: LLM 生成 — API 调用
    Step 5: 后处理 — 去AI味 + 一致性检查
    """

    def __init__(self, llm_client=None, profile_manager=None):
        self.llm = llm_client
        self.plot_lib = PlotLibrary()
        self.gag_lib = GagLibrary()
        self.theme_lib = ThemeLibrary()
        self.de_ai = DeAIEngine(llm_client)
        self.profiles = profile_manager

    def match_templates(self, context: str, genre: str = "",
                        chapter_outline: str = "",
                        existing_ctx: WritingContext = None) -> WritingContext:
        """
        Step 1: 根据章节上下文智能匹配桥段、笑点、母题
        
        如果传入了 existing_ctx，会在其基础上添加匹配结果（保留已有的配置）
        """
        ctx = existing_ctx or WritingContext(chapter_outline=chapter_outline)

        # 匹配桥段
        plots = self.plot_lib.match_for_chapter(
            f"{context} {chapter_outline}", genre)
        if plots:
            ctx.plot_template = plots[0]

        # 匹配笑点
        gags = self.gag_lib.search(scene="日常")
        if gags:
            ctx.gags = gags[:2]  # 每次最多 2 个笑点

        # 匹配母题（如果桥段有关联）
        if ctx.plot_template:
            for theme in self.theme_lib.entries:
                if ctx.plot_template.id in theme.compatible_plots:
                    ctx.themes.append(theme)

        return ctx

    def build_prompt(self, ctx: WritingContext) -> tuple[str, str]:
        """
        Step 2+3: 构建 system + user prompt
        包含：桥段骨架 + 变量 + 笑点 + 母题 + 风格约束 + 去AI约束
        """
        parts = []

        # 1. 本章信息
        if ctx.chapter_title:
            parts.append(f"写作本章：第 {ctx.chapter_num} 章《{ctx.chapter_title}》")
        else:
            parts.append(f"写作本章：第 {ctx.chapter_num} 章")
        parts.append(f"目标字数：{ctx.target_words} 字")

        # 2. 桥段模板
        if ctx.plot_template:
            parts.append(f"\n【桥段模板：{ctx.plot_template.name}（{ctx.plot_variant or '标准版'}）】")
            parts.append(f"结构骨架：{ctx.plot_template.template_structure}")
            if ctx.plot_template.usage_notes:
                parts.append(f"使用注意：{ctx.plot_template.usage_notes}")

            # 变量填充
            if ctx.plot_variables:
                parts.append("变量设定：")
                for slot in ctx.plot_template.slots:
                    val = ctx.plot_variables.get(slot.name, slot.default)
                    if val:
                        parts.append(f"  {slot.name} = {val}")

        # 3. 大纲约束
        if ctx.chapter_outline:
            parts.append(f"\n【本章大纲（必须遵守）】\n{ctx.chapter_outline}")

        # 4. 前文上下文
        if ctx.history_summary:
            parts.append(f"\n【前文摘要】\n{ctx.history_summary}")

        if ctx.previous_ending:
            parts.append(f"\n【上一章结尾（无缝承接）】\n{ctx.previous_ending}")

        # 5. 角色状态
        if ctx.character_states:
            parts.append(f"\n【角色当前状态】\n{ctx.character_states}")

        # 6. 伏笔
        if ctx.active_foreshadows:
            parts.append(f"\n【活跃伏笔（注意推进/回收）】\n{ctx.active_foreshadows}")

        # 7. 笑点注入
        if ctx.gags:
            parts.append("\n【笑点注入——必须自然融入】")
            for g in ctx.gags:
                parts.append(f"- {g.name}：{g.pattern_description}")
                if g.examples:
                    parts.append(f"  参考：{g.examples[0]}")

        # 8. 母题暗示
        if ctx.themes:
            parts.append("\n【本章隐含的母题——通过角色行动展示，不要直接说教】")
            for t in ctx.themes:
                parts.append(f"- {t.name}：{t.description}")
                if t.tips:
                    parts.append(f"  提示：{t.tips[0]}")

        # 9. 风格约束
        if ctx.style_profile:
            parts.append("\n" + ctx.style_profile.build_style_prompt())

        # 10. 去AI味约束
        if ctx.de_ai:
            parts.append(self.de_ai.build_deai_prompt_snippet())

        user = "\n".join(parts)

        system = (
            "你是一位专业的网络小说作者。请按照以下所有约束创作小说段落。"
            "只输出小说正文，不要加章节标题、'本章完'等任何元信息。"
        )

        return system, user

    def generate(self, ctx: WritingContext, cost_tracker: CostTracker = None,
                 on_chunk=None) -> WritingResult:
        """
        Step 4+5: 生成正文 + 后处理
        """
        result = WritingResult()

        # 构建 prompt
        system, user = self.build_prompt(ctx)

        # 预算检查
        if cost_tracker and not cost_tracker.check_budget(user):
            raise RuntimeError(f"预算不足：已花费 {cost_tracker.spent}/{cost_tracker.budget}")

        # 生成
        if not self.llm:
            raise RuntimeError("LLM 客户端未设置")

        if on_chunk:
            raw = self.llm.call_stream(system, user, on_chunk=on_chunk,
                                       temperature=ctx.temperature, max_tokens=8192)
        else:
            raw = self.llm.call(system, user, temperature=ctx.temperature, max_tokens=8192)

        # 记录成本
        if cost_tracker:
            cost_tracker.record("chapter_draft", user, raw)

        # 字数统计
        import re
        result.word_count = len(re.findall(r'[\u4e00-\u9fff]', raw))

        # 去AI味
        if ctx.de_ai:
            de_ai_result = self.de_ai.process_rule_based(raw)
            result.content = de_ai_result.processed
            result.de_ai_result = de_ai_result
        else:
            result.content = raw

        # 记录使用的笑点
        result.applied_gags = [g.name for g in ctx.gags]
        result.applied_themes = [t.name for t in ctx.themes]

        return result

    def generate_single(self, plot_template: PlotTemplate,
                        variables: dict, chapter_context: str = "",
                        profile: PenNameProfile = None,
                        word_count: int = 3000) -> WritingResult:
        """
        最简接口：一个桥段模板 + 变量 → 一段正文

        这是最常用的入口。
        """
        ctx = self.match_templates(chapter_context)
        ctx.plot_template = plot_template
        ctx.plot_variables = variables
        ctx.style_profile = profile
        ctx.target_words = word_count
        return self.generate(ctx)
