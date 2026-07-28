"""
桥段库（Plot Device Library）
网文经典桥段的结构化模板 — 模板 + 变量槽位 + 变体
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class PlotSlot:
    """变量槽位 — 决定桥段的具体呈现"""
    name: str           # 槽位名，如 "主角身份"
    description: str    # 说明，如 "主角在此时的公众认知状态"
    options: list[str]  # 可选值列表
    default: str = ""


@dataclass
class PlotTemplate:
    """单个桥段模板"""
    id: str
    name: str
    category: str                    # 分类：爽文/悬念/情感/战斗...
    sub_category: str = ""           # 子分类：身份反转/扮猪吃虎/...
    description: str = ""
    template_structure: str = ""     # 桥段结构骨架，如 "[甩婚书]→[众人嘲讽]→..."
    slots: list[PlotSlot] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)  # 变体名：严肃版/幽默版/...
    fit_contexts: list[str] = field(default_factory=list)  # 适用场景标签
    word_range: tuple[int, int] = (800, 2500)  # 建议字数范围
    source: str = ""                 # 来源：创作积累 / 扒取:番茄 / ...
    usage_notes: str = ""            # 使用注意事项
    examples: list[str] = field(default_factory=list)  # 示例文本片段
    quality_rating: int = 0          # 效果评分 0-5

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name,
            "category": self.category, "sub_category": self.sub_category,
            "description": self.description,
            "template_structure": self.template_structure,
            "slots": [{"name": s.name, "description": s.description,
                       "options": s.options, "default": s.default}
                      for s in self.slots],
            "variants": self.variants,
            "fit_contexts": self.fit_contexts,
            "word_range": list(self.word_range),
            "source": self.source, "usage_notes": self.usage_notes,
            "examples": self.examples, "quality_rating": self.quality_rating,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlotTemplate":
        return PlotTemplate(
            id=d["id"], name=d["name"],
            category=d.get("category", ""),
            sub_category=d.get("sub_category", ""),
            description=d.get("description", ""),
            template_structure=d.get("template_structure", ""),
            slots=[PlotSlot(**s) for s in d.get("slots", [])],
            variants=d.get("variants", []),
            fit_contexts=d.get("fit_contexts", []),
            word_range=tuple(d.get("word_range", [800, 2500])),
            source=d.get("source", ""),
            usage_notes=d.get("usage_notes", ""),
            examples=d.get("examples", []),
            quality_rating=d.get("quality_rating", 0),
        )


class PlotLibrary:
    """桥段库管理器"""

    def __init__(self, data_dir: str = ""):
        self.path = Path(data_dir) if data_dir else None
        self.templates: list[PlotTemplate] = []
        self._load_builtin()

    def _load_builtin(self):
        """加载内置桥段模板"""
        self.templates = BUILTIN_PLOTS

    def load(self, path: str):
        """从 JSON 文件加载"""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.templates = [PlotTemplate.from_dict(d) for d in data.get("templates", [])]

    def save(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"templates": [t.to_dict() for t in self.templates]},
                      f, ensure_ascii=False, indent=2)

    def search(self, category: str = "", context: str = "",
               min_rating: int = 0) -> list[PlotTemplate]:
        """按分类/场景/评分搜索"""
        results = self.templates
        if category:
            results = [t for t in results
                       if category in t.category or category in t.sub_category]
        if context:
            results = [t for t in results
                       if any(ctx in t.fit_contexts for ctx in [context])
                       or context in t.description]
        if min_rating:
            results = [t for t in results if t.quality_rating >= min_rating]
        return results

    def match_for_chapter(self, chapter_context: str,
                          genre: str = "") -> list[PlotTemplate]:
        """根据章节上下文智能匹配桥段"""
        # 纯规则匹配（后续可升级为语义匹配）
        scored = []
        for t in self.templates:
            score = 0
            if genre and genre in t.category:
                score += 3
            for ctx in t.fit_contexts:
                if ctx in chapter_context:
                    score += 2
            if t.quality_rating:
                score += t.quality_rating
            if score > 0:
                scored.append((score, t))
        scored.sort(key=lambda x: -x[0])
        return [t for _, t in scored[:5]]

    def get_by_id(self, template_id: str) -> Optional[PlotTemplate]:
        for t in self.templates:
            if t.id == template_id:
                return t
        return None

    def categories(self) -> list[str]:
        cats = set(t.category for t in self.templates if t.category)
        return sorted(cats)


# ─── 内置桥段模板 ───

BUILTIN_PLOTS = [
    PlotTemplate(
        id="plot_dating_001", name="退婚打脸",
        category="爽文", sub_category="身份反转",
        description="经典废柴逆袭桥段：被退婚后展露真正实力",
        template_structure="[当众羞辱]→[主角沉默/隐忍]→[关键时刻展现实力]→[全场震惊]→[对方后悔]",
        slots=[
            PlotSlot("主角身份", "主角在此场景中的公众认知",
                      ["公认废柴", "被误解的天才", "扮猪吃老虎"]),
            PlotSlot("对手身份", "施压方",
                      ["世家大小姐", "宗门长老", "情敌", "家族族长"]),
            PlotSlot("场合", "事件发生地点",
                      ["家族大会", "宗门大典", "婚礼现场", "坊市偶遇"]),
            PlotSlot("反转方式", "主角如何证明自己",
                      ["爆发隐藏实力", "亮出神器/功法", "召唤强援", "血脉觉醒"]),
        ],
        variants=["严肃版", "幽默反转版", "草蛇灰线版"],
        fit_contexts=["前期", "转折", "身份揭示", "打脸"],
        word_range=(1500, 3000),
        source="创作积累",
        usage_notes="注意：主角实力展示要留有后手，不可一次亮完底牌",
        quality_rating=4,
    ),
    PlotTemplate(
        id="plot_dating_002", name="拍卖会捡漏",
        category="爽文", sub_category="智取",
        description="在拍卖会上以低价拿下被所有人看走眼的宝物",
        template_structure="[宝物出场被轻视]→[主角认出真正价值]→[众人嘲讽]→[低价拿下]→[事后揭开真相]",
        slots=[
            PlotSlot("宝物类型", "被看走眼的物品",
                      ["残破功法", "不起眼材料", "古旧法器", "来历不明的小兽"]),
            PlotSlot("识破方式", "主角如何看出价值",
                      ["前世记忆", "特殊瞳术", "系统提示", "见多识广"]),
            PlotSlot("对手类型", "和主角争抢的人",
                      ["宿敌", "路人甲", "大家族少爷"]),
        ],
        variants=["正剧版", "捡漏装逼版"],
        fit_contexts=["中期", "日常", "宝物", "打脸", "积累资源"],
        word_range=(1500, 2500),
        source="创作积累",
        usage_notes="建议在主角资源匮乏、需要突破时使用",
        quality_rating=4,
    ),
    PlotTemplate(
        id="plot_dating_003", name="拜师/拜入门派",
        category="成长", sub_category="师承",
        description="拜入隐藏大佬门下或加入宗门",
        template_structure="[测试/试炼]→[主角表现异常]→[大能注意到]→[破格收徒/特殊待遇]→[同门嫉妒/质疑]",
        slots=[
            PlotSlot("师长身份", "收主角为徒的人",
                      ["归隐高人", "宗门掌门", "仙逝大能的残魂", "学院导师"]),
            PlotSlot("拜师契机", "机缘如何发生",
                      ["测试第一名", "一眼识破隐藏关卡", "意外做了件大事", "被强收"]),
            PlotSlot("冲突层面", "拜师后的对立面",
                      ["同门挑衅", "师门内斗", "外部势力反对"]),
        ],
        variants=["正剧版", "搞笑版（拜错师/被强收）", "烧脑版"],
        fit_contexts=["前期", "成长", "资源获取", "世界观展开"],
        word_range=(2000, 3500),
        source="创作积累",
        usage_notes="可用于主角进入新地图/获取新技能树",
        quality_rating=4,
    ),
    PlotTemplate(
        id="plot_dating_004", name="闯关/秘境探险",
        category="冒险", sub_category="战斗",
        description="主角独闯/组队进入秘境，各显神通",
        template_structure="[秘境开启]→[组队/独闯]→[关卡解谜/战斗]→[队友暗中算计/关键时刻]→[主角得最大好处]→[事后争议]",
        slots=[
            PlotSlot("秘境类型", "探险目标",
                      ["上古传承", "仙人洞府", "试炼塔", "禁地"]),
            PlotSlot("组队模式", "主角的伙伴构成",
                      ["独闯", "临时组队（有内鬼）", "固定队伍", "与宿敌被迫合作"]),
            PlotSlot("最大收获", "主角得到什么",
                      ["功法传承", "神器/法宝", "灵药/资源", "强者指点"]),
            PlotSlot("隐藏危机", "探险中的意外",
                      ["秘境即将崩塌", "队友背叛", "更高位面介入", "远古生物苏醒"]),
        ],
        variants=["新手村版", "高难极限版", "与宿敌合作版"],
        fit_contexts=["中期", "后期", "转折", "升级", "资源获取"],
        word_range=(3000, 6000),
        source="创作积累",
        usage_notes="适合用于主角需要突破境界/获取关键道具时",
        quality_rating=5,
    ),
    PlotTemplate(
        id="plot_dating_005", name="装逼打脸连环套",
        category="爽文", sub_category="多重反转",
        description="接连几波打脸，一层比一层狠",
        template_structure="[第一波：小喽啰挑衅]→[碾压]→[第二波：靠山出场]→[再碾压]→[第三波：叫来终极boss]→[依旧碾压]→[全场跪服]",
        slots=[
            PlotSlot("打脸层级", "几层打脸",
                      ["三层（经典）", "两层（快速版）", "四层以上（大场面）"]),
            PlotSlot("最终boss身份", "最后出场的大人物",
                      ["宗门老祖", "一城之主", "皇族成员"]),
            PlotSlot("打脸风格", "怎么打",
                      ["实力碾压", "身份碾压（亮身份吓尿）", "智商碾压（布局反杀）"]),
        ],
        variants=["经典连环版", "身份碾压版", "智商反杀版"],
        fit_contexts=["前期", "中期", "高光时刻", "立威"],
        word_range=(2000, 5000),
        source="创作积累",
        usage_notes="打脸强度要递进，最后的脸要最大",
        quality_rating=5,
    ),
    PlotTemplate(
        id="plot_dating_006", name="英雄救美/关键救援",
        category="情感", sub_category="建立羁绊",
        description="在危急时刻救下重要角色，建立深层羁绊",
        template_structure="[危境]→[主角察觉/路过]→[权衡/犹豫]→[出手]→[逆转危局]→[双方反应]",
        slots=[
            PlotSlot("被救者身份", "谁处于危险中",
                      ["女主/男主", "重要配角", "路人NPC", "敌方阵营成员"]),
            PlotSlot("危险来源", "危局性质",
                      ["反派追杀", "妖兽/怪物袭击", "陷阱/阴谋", "自然灾害"]),
            PlotSlot("主角动机", "为何出手",
                      ["路见不平", "被救者有利用价值", "出于旧情/因果", "被迫卷入"]),
            PlotSlot("救援代价", "主角付出什么",
                      ["无代价轻松解决", "暴露隐藏实力", "受伤/消耗", "结下大仇"]),
            PlotSlot("后续影响", "对剧情的影响",
                      ["建立盟友", "结下因果/好感", "引发新矛盾", "只是一次日常"]),
        ],
        variants=["一见钟情版", "冷面救场版", "救完就走的潇洒版"],
        fit_contexts=["前期", "中期", "情感升温", "转折"],
        word_range=(1500, 3000),
        source="创作积累",
        usage_notes="避免救完后主角和被救者强行黏在一起，自然推进关系",
        quality_rating=3,
    ),
    PlotTemplate(
        id="plot_dating_007", name="擂台/比武大会",
        category="战斗", sub_category="竞技",
        description="参加正式或非正式的武道对决",
        template_structure="[大会开场]→[主角参加/被迫参加]→[前几轮碾压]→[遭遇强敌]→[逆势翻盘或新招式出]→[冠军/关注/暗流]",
        slots=[
            PlotSlot("大会性质", "什么性质的比赛",
                      ["宗门大比", "武举/科举", "坊市竞技", "生死战"]),
            PlotSlot("参赛动机", "主角为何参加",
                      ["为资源/奖励", "为证明/复仇", "被迫参赛", "为进入/接近某地"]),
            PlotSlot("核心强敌", "最大对手",
                      ["宿敌", "设定中的天才", "黑马", "幕后操纵者的傀儡"]),
            PlotSlot("最终结果", "比赛结局",
                      ["夺冠", "惜败（留后手）", "故意弃权/身体出现意外", "发现更大阴谋"]),
        ],
        variants=["热血竞技版", "扮猪吃虎版", "阴谋揭露版"],
        fit_contexts=["中期", "后期", "技能展示", "角色成长", "铺垫大阴谋"],
        word_range=(3000, 6000),
        source="创作积累",
        usage_notes="建议给对手塑造独立动机，不要纯工具人",
        quality_rating=4,
    ),
    PlotTemplate(
        id="plot_dating_008", name="扮猪吃虎日常",
        category="爽文", sub_category="反差",
        description="主角以弱者的伪装，在别人面前突然展现恐怖实力",
        template_structure="[看似普通的场景]→[有人看低/挑衅主角]→[主角默默做某事]→[不经意间显露]→[在场所有人当场石化]",
        slots=[
            PlotSlot("伪装身份", "主角的伪装",
                      ["谁都可以欺负的新人", "路人", "废物徒弟/师弟"]),
            PlotSlot("展示方式", "如何暴露实力",
                      ["秒杀大BOSS", "施展失传绝技", "说出惊天地的话", "气势全开"]),
            PlotSlot("围观群众", "谁被惊呆了",
                      ["同门/同事", "路人甲乙丙丁", "挑衅者及其靠山", "大人物/老前辈"]),
        ],
        variants=["装逼型", "幽默型", "冷酷型"],
        fit_contexts=["前期", "日常", "搞笑", "立威"],
        word_range=(1000, 2500),
        source="创作积累",
        usage_notes="点到为止，频率不宜过高（每10-20章一次即可）",
        quality_rating=4,
    ),
    PlotTemplate(
        id="plot_dating_009", name="修罗场/情感博弈",
        category="情感", sub_category="冲突",
        description="多方关系在同一场景中碰撞，产生微妙的情感张力",
        template_structure="[多方角色无意中聚在一起]→[各自的秘密/情感被触碰]→[言语试探/交锋]→[某一方情绪失控/爆出关键信息]→[关系重新洗牌]",
        slots=[
            PlotSlot("核心冲突", "情感修罗场的本质",
                      ["多角恋", "身份/阵营对立", "旧情VS新缘", "误会连环"]),
            PlotSlot("参与方", "卷入感情博弈的角色",
                      ["主角+2-3位异性", "主角+旧识+新识", "主角+对手+共同在意的人"]),
            PlotSlot("引爆点", "冲突如何升级",
                      ["无意撞破", "言语试探", "第三方挑拨", "危机时本能选择"]),
        ],
        variants=["轻松日常版", "狗血高潮版", "暗流涌动版"],
        fit_contexts=["中期", "情感转折", "关系洗牌"],
        word_range=(2000, 3500),
        source="创作积累",
        usage_notes="情感博弈的精彩在于'未言明'，避免对话过于直白",
        quality_rating=3,
    ),
    PlotTemplate(
        id="plot_dating_010", name="宗门/家族危机",
        category="冲突", sub_category="阵营对抗",
        description="主角所在势力面临外部威胁，激发凝聚力",
        template_structure="[危机预兆]→[敌方亮出底牌]→[主角力排众议/扛住压力]→[生死之战]→[胜利/惨胜]→[威望骤升]",
        slots=[
            PlotSlot("威胁来源", "谁在进攻",
                      ["敌对宗门", "外来势力", "皇朝/官府", "远古封印破裂"]),
            PlotSlot("主角角色", "主角在对抗中的位置",
                      ["最高战力", "诡道策士", "精神领袖", "被排除后强行正名"]),
            PlotSlot("重大牺牲", "代价",
                      ["重要配角阵亡", "主角重伤/实力倒退", "献祭某物/某人", "零代价"]),
        ],
        variants=["热血守护版", "悲壮牺牲版", "翻转碾压版"],
        fit_contexts=["中期", "后期", "阵营转折", "成长高潮"],
        word_range=(3000, 6000),
        source="创作积累",
        usage_notes="危机是让角色成长的最佳催化剂",
        quality_rating=4,
    ),
    PlotTemplate(
        id="plot_dating_011", name="穿越/重生开局",
        category="开篇", sub_category="穿越重生",
        description="主角穿越或重生到另一个世界的开场",
        template_structure="[死亡/穿越]→[醒来]→[接受身份/获取记忆]→[发现处境/危机]→[确立目标]→[首次利用先发优势]",
        slots=[
            PlotSlot("穿越类型", "怎么穿越的",
                      ["死了重生到多年前", "魂穿到异世界", "肉身穿", "游戏世界"]),
            PlotSlot("接收身份", "穿越后是什么人",
                      ["废柴家主", "赘婿", "被退婚的废物", "病秧子", "小家族子弟"]),
            PlotSlot("先发优势", "穿越/重生带来的优势",
                      ["前世记忆", "系统/外挂", "先知先觉", "特殊体质"]),
        ],
        variants=["废柴逆袭版", "系统爽文版", "权谋智者版"],
        fit_contexts=["开篇", "第一章", "穿越", "重生"],
        word_range=(2000, 3000),
        source="创作积累",
        usage_notes="第一章前200字必须有钩子，主角面临的第一个危机要立刻出现",
        quality_rating=5,
    ),
    PlotTemplate(
        id="plot_dating_012", name="获得金手指/系统激活",
        category="开篇", sub_category="金手指",
        description="主角首次获得/激活特殊能力或系统",
        template_structure="[触发契机]→[系统/能力激活]→[功能介绍/探索]→[首次尝试]→[尝到甜头/发现限制]→[确立使用路径]",
        slots=[
            PlotSlot("金手指类型", "什么特殊能力",
                      ["签到系统", "任务系统", "抽奖/商城系统", "天赋觉醒", "神器认主"]),
            PlotSlot("激活方式", "怎么触发的",
                      ["生死危机", "意外事件", "被别人激活的余波波及", "自己研究发现的"]),
            PlotSlot("首次应用", "第一次怎么用的",
                      ["立刻脱困", "修为突破", "发掘隐藏宝物", "预知危险"]),
        ],
        variants=["惊喜版", "冷静分析版", "误打误撞版"],
        fit_contexts=["开篇", "前期", "第一章", "系统流"],
        word_range=(1500, 2500),
        source="创作积累",
        usage_notes="金手指要给限制，否则后面全无敌了没有故事可写",
        quality_rating=4,
    ),
]
