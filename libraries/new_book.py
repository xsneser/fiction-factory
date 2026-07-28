"""
新书专项流程（New Book Pipeline）
前三章决定一切：钩子→世界观→首次冲突
+ 书名生成 + 简介生成
"""
from dataclasses import dataclass, field
from typing import Optional
from libraries.plot import PlotLibrary, PlotTemplate
from libraries.structure import StructureLibrary, StructureTemplate
from libraries.gag import GagLibrary
from libraries.profiles import PenNameProfile


@dataclass
class NewBookConfig:
    """新书配置"""
    title: str = ""
    pen_name: str = ""
    genre: str = ""                    # 玄幻/都市/言情/悬疑/...
    sub_genre: str = ""                # 系统流/重生/逆袭流/...
    platform: str = "fanqie"           # fanqie/qidian
    chapter_count: int = 500
    words_per_chapter: int = 3000
    # 前三章模板选择
    opening_template_id: str = ""      # 开篇桥段ID
    golden_finger_template_id: str = ""  # 金手指桥段ID（可选）
    structure_template_id: str = ""    # 大纲结构ID
    # 风格
    style_profile_id: str = ""         # 笔名档案ID


@dataclass
class Chapter1Result:
    """第一章结果"""
    hook_text: str = ""                # 第一章正文
    hook_type: str = ""                # 钩子类型
    golden_finger_intro: str = ""      # 金手指引入
    initial_conflict: str = ""         # 初始冲突


@dataclass
class Chapter3Result:
    """前三章完整结果"""
    chapter1: str = ""                 # 第一章：钩子+金手指
    chapter2: str = ""                 # 第二章：世界观展开
    chapter3: str = ""                 # 第三章：首次核心冲突
    titles: list[str] = field(default_factory=list)  # 建议书名（3-5个）
    synopsis: str = ""                 # 简介


class NewBookPipeline:
    """
    新书专项流程

    流程：选模板 → 拼接上下文 → 分三章生成 → 书名+简介 → 保存
    """

    def __init__(self, llm_client=None):
        self.llm = llm_client

    def set_llm(self, client):
        self.llm = client

    def plan_opening(self, config: NewBookConfig) -> dict:
        """
        为新书规划开篇方案

        返回: {
            "opening_plot": PlotTemplate,     推荐的第一个桥段
            "golden_finger_plot": PlotTemplate, 金手指桥段（可选）
            "structure": StructureTemplate,    大纲结构
            "planned_foreshadows": [...],      可在此阶段埋的伏笔
        }
        """
        plot_lib = PlotLibrary()
        struct_lib = StructureLibrary()

        # 匹配开篇桥段
        opening = plot_lib.get_by_id(config.opening_template_id) if config.opening_template_id else None
        if not opening:
            openings = plot_lib.search(category="开篇")
            opening = openings[0] if openings else None

        # 匹配金手指桥段
        gf = None
        if config.golden_finger_template_id:
            gf = plot_lib.get_by_id(config.golden_finger_template_id)

        # 匹配大纲结构
        structure = None
        if config.structure_template_id:
            structure = struct_lib.get_by_id(config.structure_template_id)
        if not structure:
            structures = struct_lib.search(genre=config.genre,
                                           chapter_count=config.chapter_count)
            structure = structures[0] if structures else None

        return {
            "opening_plot": opening,
            "golden_finger_plot": gf,
            "structure": structure,
        }

    def build_chapter1_prompt(self, config: NewBookConfig,
                               plan: dict, profile: PenNameProfile = None) -> dict:
        """
        构建第一章的写作 prompt

        第一章 = 钩子 + 金手指激活 + 触发第一个危机
        """
        opening: PlotTemplate = plan["opening_plot"]
        gf: Optional[PlotTemplate] = plan.get("golden_finger_plot")
        structure: StructureTemplate = plan.get("structure", None)

        # 拼接写作指令
        parts = []

        # 1. 核心要求
        parts.append("编写一部小说的第一章。必须：")
        parts.append("1. 前200字必须有强烈的钩子，让读者必须看下去")
        parts.append("2. 本章内必须触发主角的第一个危机/困境")
        if gf:
            parts.append("3. 本章内必须完成金手指/系统的引入或激活")
        parts.append(f"4. 总字数控制在 {config.words_per_chapter} 字左右")

        # 2. 桥段骨架
        parts.append(f"\n【使用桥段：{opening.name}】")
        parts.append(f"桥段骨架: {opening.template_structure}")

        # 3. 金手指结构
        if gf:
            parts.append(f"\n【金手指桥段：{gf.name}】")
            parts.append(f"金手指注入方式: {gf.template_structure}")
            parts.append(f"金手指类型: {', '.join(gf.slots[0].options) if gf.slots else '自行设定'}")

        # 4. 流派约束
        parts.append(f"\n【流派】：{config.genre}/{config.sub_genre}")
        if structure:
            first_stage = structure.stages[0]
            parts.append(f"\n【全书结构】：{structure.name}")
            parts.append(f"当前阶段：{first_stage.name} — {first_stage.description}")
            parts.append(f"本阶段关键事件：{', '.join(first_stage.key_events)}")

        # 5. 风格约束
        if profile:
            parts.append("\n" + profile.build_style_prompt())

        # 6. 平台适配
        parts.append(self._platform_constraints(config.platform))

        return {
            "system": "你是一位专业的网络小说作者。请只输出小说正文，不要加章节标题、"
                      "不要加'第一章'、不要加'本章完'等元信息。",
            "user": "\n".join(parts),
        }

    def build_chapter2_prompt(self, config: NewBookConfig,
                               chapter1: str, profile: PenNameProfile = None) -> dict:
        """
        构建第二章的写作 prompt

        第二章 = 世界观展开 + 能力初试 + 日常节奏
        """
        parts = []

        parts.append("承接上一章，编写第二章。本章核心任务：展开世界观。")
        parts.append("1. 通过主角的行动和见闻，展示这个世界的设定")
        parts.append("2. 主角第一次使用金手指/能力的成果和限制")
        parts.append("3. 建立日常节奏——为后面的冒险/冲突做铺垫")
        parts.append("4. 章末留一个中等强度的钩子")
        parts.append(f"5. 总字数控制在 {config.words_per_chapter} 字左右")

        parts.append(f"\n【流派】：{config.genre}/{config.sub_genre}")

        if profile:
            parts.append("\n" + profile.build_style_prompt())

        parts.append(self._platform_constraints(config.platform))

        return {
            "system": "你是一位专业的网络小说作者。请只输出小说正文，不要加章节标题和元信息。",
            "user": "\n".join(parts),
        }

    def build_chapter3_prompt(self, config: NewBookConfig,
                               chapter_12: str, profile: PenNameProfile = None) -> dict:
        """
        构建第三章的写作 prompt

        第三章 = 首次核心冲突 + 展现实力 + 完成"开篇三部曲"
        """
        parts = []

        parts.append("承接前两章，编写第三章。本章是开篇三部曲的收尾。必须：")
        parts.append("1. 主角面临第一个真正意义上的对手/冲突（不是日常小打小闹）")
        parts.append("2. 主角在危机中展现实力/智慧，首次认真出手")
        parts.append("3. 冲突解决后，主角获得某种认可/收获/地位提升")
        parts.append("4. 埋下一个「更大的世界/更高的山」的伏笔线索")
        parts.append("5. 章末给读者一个「接下来更精彩」的信号")
        parts.append(f"6. 总字数控制在 {config.words_per_chapter} 字左右")

        parts.append(f"\n【流派】：{config.genre}/{config.sub_genre}")

        if profile:
            parts.append("\n" + profile.build_style_prompt())

        parts.append(self._platform_constraints(config.platform))

        return {
            "system": "你是一位专业的网络小说作者。请只输出小说正文，不要加章节标题和元信息。",
            "user": "\n".join(parts),
        }

    def build_title_prompt(self, config: NewBookConfig,
                           chapter_123: str) -> str:
        """生成建议书名（需要 LLM）"""
        return f"""你是一位专业的网文编辑，擅长为小说起名字。

根据以下信息，为这本小说提供 5 个备选书名：

【流派】：{config.genre}/{config.sub_genre}
【平台】：{config.platform}
【开篇内容摘要】：
{chapter_123[:1000]}...

要求：
1. 书名要有网感，能吸引点击
2. 不建议使用"之""录""传"等传统书名列字（除非是玄幻正剧）
3. 每个书名 4-10 字
4. 优选出你认为最好的一两个

请以 JSON 格式返回：
{{"titles": ["书名1", "书名2", ...], "best": "最佳书名", "reason": "理由"}}"""

    def build_synopsis_prompt(self, config: NewBookConfig,
                              chapter_123: str) -> str:
        """生成简介（需要 LLM）"""
        return f"""你是一位专业的网文编辑。

根据以下开篇内容，为这本小说写一段简介（100-200字）：

【流派】：{config.genre}/{config.sub_genre}
【开篇内容摘要】：
{chapter_123[:1000]}...

要求：
1. 简介要吸睛，有钩子
2. 不要剧透太多关键情节
3. 适合放在 {config.platform} 的小说详情页

请以 JSON 格式返回：
{{"synopsis": "简介内容"}}"""

    def _platform_constraints(self, platform: str) -> str:
        """平台特定的写作约束"""
        constraints = {
            "fanqie": (
                "【番茄小说约束】\n"
                "- 每章 2500-3500 字\n"
                "- 每章 6-8 个场景切换（换行隔开）\n"
                "- 章末必须有钩子\n"
                "- 对话比例不低于 30%\n"
                "- 开篇前 500 字必须有冲突或危机"
            ),
            "qidian": (
                "【起点中文网约束】\n"
                "- 每章 3000-5000 字\n"
                "- 描写可以更细致\n"
                "- 章末有悬念即可，不强求钩子"
            ),
        }
        return constraints.get(platform, "")


# ─── 开篇方案推荐 ───

def recommend_opening(genre: str, sub_genre: str = "") -> list[dict]:
    """根据流派推荐开篇方案"""
    plot_lib = PlotLibrary()
    struct_lib = StructureLibrary()

    openings = plot_lib.search(category="开篇")
    structures = struct_lib.search(genre=genre, sub_genre=sub_genre)

    # 为不同流派做额外推荐
    if "系统" in sub_genre or "升级" in sub_genre:
        extra = plot_lib.search(category="开篇", context="系统")
    else:
        extra = plot_lib.search(category="开篇")

    recommendations = []
    for o in openings:
        rec = {
            "id": o.id,
            "name": o.name,
            "description": o.description,
            "template": o.template_structure,
            "match_score": "high" if o in extra else "medium",
        }
        recommendations.append(rec)

    return recommendations
