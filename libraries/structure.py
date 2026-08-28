"""
情节弧库（Structure Library）
各类网文题材的故事骨架结构模板
"""
from dataclasses import dataclass, field
from .base_library import JsonLibrary


@dataclass
class StageNode:
    """情节弧/阶段节点（树形：有 children = 中间弧，描述其下可挂的子弧；无 = 叶弧/阶段）
    只表述字数（min_words/max_words 为该节点建议字数区间，与运行时字数轴一致，不含章数）"""
    name: str            # 阶段/子弧名，如 "先发布局"
    description: str     # 描述
    min_words: int = 9000
    max_words: int = 30000
    key_events: list[str] = field(default_factory=list)
    foreshadow_opportunities: list[str] = field(default_factory=list)  # 埋坑机会
    themes: list = field(default_factory=list)   # 节点级内涵 [{name, position, how}]，含插入位置
    children: list["StageNode"] = field(default_factory=list)   # 子弧（多层嵌套；无 = 叶）

    def to_dict(self) -> dict:
        d = {
            "name": self.name, "description": self.description,
            "min_words": self.min_words, "max_words": self.max_words,
            "key_events": self.key_events,
            "foreshadow_opportunities": self.foreshadow_opportunities,
            "themes": self.themes,
        }
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d

    @classmethod
    def from_dict(cls, s) -> "StageNode":
        if isinstance(s, str):
            return cls(name=s, description="")
        return cls(
            name=s.get("name", ""), description=s.get("description", ""),
            # 兼容旧数据（min_chapters/max_chapters 章节数 ×3000 换算）
            min_words=s.get("min_words", s.get("min_chapters", 3) * 3000),
            max_words=s.get("max_words", s.get("max_chapters", 10) * 3000),
            key_events=s.get("key_events", []),
            foreshadow_opportunities=s.get("foreshadow_opportunities", []),
            themes=s.get("themes", []),
            children=[cls.from_dict(c) for c in s.get("children", [])],
        )


@dataclass
class StructureTemplate:
    """情节弧结构模板（题材已换标签，tags 是唯一题材来源）"""
    id: str
    name: str
    description: str = ""
    total_words: int = 1500000
    stages: list[StageNode] = field(default_factory=list)
    opening_patterns: list[str] = field(default_factory=list)
    climax_patterns: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source: str = ""                 # 来源
    created_at: str = "2026-05-01"   # 收录时间
    enabled: bool = True              # 启用状态

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name,
            "description": self.description,
            "total_words": self.total_words,
            "stages": [s.to_dict() for s in self.stages],
            "opening_patterns": self.opening_patterns,
            "climax_patterns": self.climax_patterns,
            "tags": self.tags,
            "source": self.source,
            "created_at": self.created_at,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StructureTemplate":
        # 旧数据仍可能带 genre/sub_genre 键：忽略即可，下次 _save() 自动清掉
        return StructureTemplate(
            id=d["id"], name=d.get("name", ""),
            description=d.get("description", ""),
            total_words=d.get("total_words", d.get("total_chapters", 500) * 3000),
            stages=[StageNode.from_dict(s) for s in d.get("stages", [])],
            opening_patterns=d.get("opening_patterns", []),
            climax_patterns=d.get("climax_patterns", []),
            tags=d.get("tags", []),
            source=d.get("source", ""),
            created_at=d.get("created_at", "2026-05-01"),
            enabled=d.get("enabled", True),
        )


class StructureLibrary(JsonLibrary):
    """情节弧库管理器（进程内单例，JSONL 一行一模板，持久化由基类按 .jsonl 后缀处理）"""
    _instance = None
    _list_attr = "templates"
    _key = "templates"
    _file_name = "structures.jsonl"

    @classmethod
    def _from_dict(cls, d: dict) -> "StructureTemplate":
        return StructureTemplate.from_dict(d)

    @classmethod
    def _builtin(cls) -> list:
        return BUILTIN_STRUCTURES

    def search(self, tags=None, word_count: int = 0) -> list[StructureTemplate]:
        """按标签（任一命中）/总字数筛选模板。tags 为列表或逗号/空格分隔字符串。"""
        results = self.templates
        if isinstance(tags, str):
            tags = [x.strip() for x in tags.replace("，", " ").replace(",", " ").split() if x.strip()]
        if tags:
            tag_set = {str(t).strip() for t in tags if str(t).strip()}
            results = [t for t in results if tag_set.intersection(t.tags or [])]
            results.sort(key=lambda t: -len(tag_set.intersection(t.tags or [])))  # 命中多的排前
        if word_count:
            # 找总字数最接近的模板
            results.sort(key=lambda t: abs(t.total_words - word_count))
        return results

    def get_by_id(self, template_id: str):
        for t in self.templates:
            if t.id == template_id:
                return t
        return None


# ─── 内置情节弧结构模板 ───

BUILTIN_STRUCTURES = [
    StructureTemplate(
        id="arc_chuanyue_01", name="穿越重生·先发优势弧",
        description="重生/穿越后利用先知先觉抢占先机的一段弧：确认处境→布局→第一次碾压→局势反转",
        total_words=36000,
        stages=[
            StageNode("确认处境", "穿越/重生、弄清身份与时间点、盘算先发优势",
                      3000, 6000,
                      ["高能开局（穿越/重生）", "弄清身份处境", "盘点先知信息"],
                      ["穿越/重生的原因存疑"]),
            StageNode("先发布局", "抢在未来关键节点前埋下棋子、避开前世雷区",
                      9000, 15000,
                      ["提前获取关键资源", "拉拢关键人物", "避开前世踩过的坑"],
                      ["蝴蝶效应引发的新变量"],
                      children=[
                          StageNode("提前埋子", "在关键节点前布下棋子", 3000, 6000, ["占住资源位", "提前示好关键人"]),
                          StageNode("拉拢关键人物", "收编前世可用的盟友", 3000, 6000, ["救下前世恩人", "结盟军需官"]),
                          StageNode("避开雷区", "绕开前世踩过的坑", 3000, 3000, ["识破前世陷阱", "改变致命选择"]),
                      ]),
            StageNode("第一次碾压", "用先发优势正面碾压第一个前世仇人/竞争者",
                      6000, 12000,
                      ["打脸第一个敌人", "身份地位突变", "被多方关注"],
                      ["更高层对手投来的目光"]),
            StageNode("局势反转", "顺风局的暗涌：新对手出手、旧雷区爆炸",
                      6000, 9000,
                      ["新对手试探", "此前布局被反将一军", "亮出更深底牌"],
                      ["幕后黑手的阴影"],
                      [{"name": "复仇（Revenge）", "position": "结尾",
                        "how": "先发碾压与局势反转的高光时刻以复仇意志引爆"}]),
        ],
        opening_patterns=["plot_dating_011", "plot_dating_012"],
        climax_patterns=["plot_dating_001", "plot_dating_005"],
        tags=["穿越", "重生", "爽文", "快节奏"],
        source="创作积累", created_at="2026-08-27",
    ),
    StructureTemplate(
        id="arc_xuanhuan_01", name="玄幻·试炼扬名弧",
        description="入门后在一场试炼/赛事中快速扬名的一段弧：入门危机→初试锋芒→试炼夺魁",
        total_words=30000,
        stages=[
            StageNode("入门危机", "初入势力即遭打压/考验，证明资格",
                      3000, 9000,
                      ["被看轻/刁难", "第一次出手", "赢得入门资格"],
                      ["考验背后有人在布局"]),
            StageNode("初试锋芒", "在局部冲突中展露实力、攒下第一波声名",
                      6000, 12000,
                      ["越级战胜对手", "获得长辈/组织认可", "结交第一批盟友"],
                      ["被更强的同辈盯上"]),
            StageNode("试炼夺魁", "试炼/赛事中挫败劲敌、脱颖而出",
                      9000, 15000,
                      ["试炼开启", "与种子选手硬碰硬", "夺魁/达成目标"],
                      ["试炼背后更大的图谋"],
                      [{"name": "成长的代价（Cost of Growth）", "position": "结尾",
                        "how": "付出代价换取的胜利，在夺魁时刻点题成长"}],
                      children=[
                          StageNode("试炼开启", "入场、立规则、初见强敌", 3000, 6000, ["抽签/分组", "种子选手亮相"]),
                          StageNode("硬碰强敌", "与劲敌正面交锋", 3000, 6000, ["越级硬刚", "压箱底底牌"]),
                      ]),
        ],
        opening_patterns=["plot_dating_012"],
        climax_patterns=["plot_dating_007", "plot_dating_010"],
        tags=["玄幻", "修仙", "升级", "爽文"],
        source="创作积累", created_at="2026-08-27",
    ),
    StructureTemplate(
        id="arc_dushi_01", name="都市·逆袭打脸弧",
        description="低谷中借金手指逆袭、当众打脸的反转爽感弧：低谷受辱→金手指初现→正面打脸→立足声名",
        total_words=30000,
        stages=[
            StageNode("低谷受辱", "展示最狼狈处境、被当众羞辱",
                      3000, 6000,
                      ["被退婚/被辞退/被看不起", "当众难堪", "绝境中触发金手指"],
                      ["羞辱者背后的靠山"]),
            StageNode("金手指初现", "第一次用金手指扳回局面、让人刮目相看",
                      3000, 9000,
                      ["首次施展能力", "小范围证明自己", "赢得初步尊重"],
                      ["金手指的升级条件"]),
            StageNode("正面打脸", "在公开场合碾压此前羞辱者、彻底翻盘",
                      6000, 12000,
                      ["约战/对赌/竞争", "当众反杀", "靠山出手又被反制"],
                      ["更大的对手记恨上主角"],
                      children=[
                          StageNode("约战对赌", "当众立约、把事闹大", 3000, 6000, ["立下赌约", "围观起哄"]),
                          StageNode("当众反杀", "在众目睽睽下翻盘", 3000, 6000, ["绝境反转", "当众打脸"]),
                      ]),
            StageNode("立足声名", "逆袭后的余波：收获人脉、露出更大的舞台",
                      6000, 9000,
                      ["声名传开", "新势力抛来橄榄枝", "埋下下一段冲突"],
                      ["幕后黑手浮现"]),
        ],
        opening_patterns=["plot_dating_001", "plot_dating_011"],
        climax_patterns=["plot_dating_001", "plot_dating_005"],
        tags=["都市", "逆袭", "爽文", "现代"],
        source="创作积累", created_at="2026-08-27",
    ),
    StructureTemplate(
        id="arc_xuanyi_01", name="悬疑·设局揭晓弧",
        description="一起离奇事件从入局到真相浮出的完整弧：异常入局→线索排查→设局反杀→真相浮现",
        total_words=36000,
        stages=[
            StageNode("异常入局", "主角被卷入一起明显不对的离奇事件",
                      3000, 9000,
                      ["目击/卷入异常事件", "发现第一个疑点", "确认自己被盯上"],
                      ["事件与主角过往的隐秘关联"]),
            StageNode("线索排查", "走访/调查，拼凑碎片、遭遇阻力",
                      9000, 15000,
                      ["收集线索", "关键证人/物证", "调查方向被误导"],
                      ["每个线索都指向更大阴谋"],
                      children=[
                          StageNode("走访收集", "逐点取证、拼图", 3000, 6000, ["目击者访谈", "现场勘验"]),
                          StageNode("方向被误导", "假线索引偏调查", 3000, 6000, ["伪证出现", "追查落空"]),
                      ]),
            StageNode("设局反杀", "识破误导、反将一军、逼近核心",
                      6000, 12000,
                      ["识破谎言", "设局引蛇出洞", "当面揭穿伪证"],
                      ["真正的幕后另有其人"]),
            StageNode("真相浮现", "核心真相揭晓、事件收束（可留悬念）",
                      6000, 12000,
                      ["动机真相", "与真凶正面交锋", "事件落幕/新疑点"],
                      ["更大的局等下一次揭晓"]),
        ],
        opening_patterns=["plot_dating_004"],
        climax_patterns=["plot_dating_004", "plot_dating_010"],
        tags=["悬疑", "推理", "反转", "阴谋"],
        source="创作积累", created_at="2026-08-27",
    ),
    StructureTemplate(
        id="arc_tianwen_01", name="言情·误会和解弧",
        description="从意外相识到关系确认的甜中带虐小弧：意外初遇→暧昧升温→误会波折→和解确认",
        total_words=30000,
        stages=[
            StageNode("意外初遇", "被迫/巧合的相遇，留下第一印象",
                      3000, 6000,
                      ["意外相遇", "一方先动心或双方嘴硬", "留下一个共同的小秘密"],
                      ["未说出口的心结"]),
            StageNode("暧昧升温", "日常互动里感情悄悄加深",
                      6000, 12000,
                      ["多次碰面/合作", "体贴细节", "第一个脸红/心动场景"],
                      ["对方的过去痛点"]),
            StageNode("误会波折", "小误会或外部压力让关系跌入冰点",
                      6000, 12000,
                      ["误会产生", "一方受伤/遇险", "第三方搅局"],
                      ["误会的真正来源"],
                      children=[
                          StageNode("误会产生", "一句话/一个误会引爆", 3000, 6000, ["被撞见暧昧", "旧事被翻出"]),
                          StageNode("第三方搅局", "外人加剧误会", 3000, 3000, ["绿茶/情敌挑拨", "家人反对"]),
                      ]),
            StageNode("和解确认", "误会解开、关系正式确认/升级",
                      3000, 9000,
                      ["真相大白", "告白/和解", "关系升温定格"],
                      ["下一段感情线伏笔"]),
        ],
        opening_patterns=["plot_dating_006"],
        climax_patterns=["plot_dating_006", "plot_dating_009"],
        tags=["言情", "甜文", "日常", "短篇"],
        source="创作积累", created_at="2026-08-27",
    ),
    StructureTemplate(
        id="arc_scifi_01", name="科幻·末日求生弧",
        description="灾变降临后从求存到重建秩序的一段弧：灾变降临→求存囤积→冲突突围→秩序重建",
        total_words=36000,
        stages=[
            StageNode("灾变降临", "秩序崩塌的瞬间，主角失去一切",
                      3000, 9000,
                      ["灾变爆发", "逃出生天", "确认幸存者身份"],
                      ["灾变的真正源头成谜"]),
            StageNode("求存囤积", "搜集物资、加固据点、为活下去积累底牌",
                      9000, 15000,
                      ["搜集物资", "加固据点", "与第一批幸存者结盟"],
                      ["幸存者中混入异类"],
                      children=[
                          StageNode("搜集物资", "搜刮补给、装备", 3000, 6000, ["超市/军械库搜刮", "抢到第一辆车"]),
                          StageNode("加固据点", "把落脚点改造成堡垒", 3000, 6000, ["选址封堵", "囤粮储水"]),
                      ]),
            StageNode("冲突突围", "遭遇强敌/人性之恶，杀出重围",
                      6000, 12000,
                      ["被掠夺者围困", "背水一战", "付出代价换生存"],
                      ["更深层的阴谋浮出"],
                      children=[
                          StageNode("被掠夺者围困", "恶徒围攻据点", 3000, 3000, ["围城", "人质要挟"]),
                          StageNode("背水一战", "绝境反击杀出血路",
                                    3000, 6000, ["突破包围", "火并头目"],
                                    children=[
                                        StageNode("绝境反击", "绝处逢生的反杀", 3000, 3000, ["引爆弹药库", "斩首头目"]),
                                    ]),
                      ]),
            StageNode("秩序重建", "短暂的喘息与新秩序的萌芽（可续接下一弧）",
                      6000, 12000,
                      ["重建小秩序", "收容更多幸存者", "灾变真相露出一角"],
                      ["更大危机的信号"]),
        ],
        opening_patterns=["plot_dating_011"],
        climax_patterns=["plot_dating_007", "plot_dating_010"],
        tags=["科幻", "末世", "求生", "爽文"],
        source="创作积累", created_at="2026-08-27",
    ),
]
