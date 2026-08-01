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
    template_structure: str = ""     # 桥段结构骨架
    slots: list[PlotSlot] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)
    fit_contexts: list[str] = field(default_factory=list)
    word_range: tuple[int, int] = (800, 2500)
    source: str = ""                 # 来源
    usage_notes: str = ""
    examples: list[str] = field(default_factory=list)
    quality_rating: int = 0
    created_at: str = "2026-05-01"   # 收录时间
    enabled: bool = True              # 启用状态

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
            "examples": self.examples,
            "created_at": self.created_at,
            "enabled": self.enabled,
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
            created_at=d.get("created_at", "2026-05-01"),
            enabled=d.get("enabled", True),
        )


class PlotLibrary:
    """桥段库管理器（进程内单例，避免每实例重复读 JSON）"""
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, data_dir: str = ""):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.path = Path(data_dir) if data_dir else None
        # 修复: 使用脚本所在目录的 data/ 子目录，避免CWD问题
        base = Path(__file__).parent / "data"
        self.save_path = Path(data_dir) / "plots.json" if data_dir else base / "plots.json"
        self.templates: list[PlotTemplate] = []
        self._load()

    def _load(self):
        """加载：优先从持久文件，否则内置"""
        if self.save_path.exists():
            with open(self.save_path, encoding="utf-8") as f:
                data = json.load(f)
            self.templates = [PlotTemplate.from_dict(d) for d in data.get("templates", [])]
        else:
            self.templates = BUILTIN_PLOTS

    def _save(self):
        self.save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.save_path, "w", encoding="utf-8") as f:
            json.dump({"templates": [t.to_dict() for t in self.templates]},
                      f, ensure_ascii=False, indent=2)

    def load(self, path: str):
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
        return results

    @staticmethod
    def _bigrams(text: str) -> set[str]:
        """中文字符二元组（bigram）集合，用于轻量语义相似度。"""
        import re as _re
        t = _re.sub(r'[\s，。、；：！？…《》【】()（）　]+', '', text or '')
        return {t[i:i+2] for i in range(len(t) - 1) if t[i:i+2].strip()}

    def match_for_chapter(self, chapter_context: str,
                          genre: str = "") -> list[PlotTemplate]:
        """根据章节上下文匹配桥段（轻量语义：bigram 相似度 + 分类多样性）。

        相比纯子串包含，bigram 能捕捉"线索/调查/真凶"这类近义表达，
        再按分类轮询返回多样候选池，供上层 AI 二次挑选，避免候选单一。
        """
        ctx_bigrams = self._bigrams(chapter_context)
        scored = []
        for t in self.templates:
            index_text = " ".join([t.name, t.category, t.sub_category, t.description,
                                   " ".join(t.fit_contexts), t.template_structure])
            idx_bigrams = self._bigrams(index_text)
            overlap = (len(ctx_bigrams & idx_bigrams) / max(len(ctx_bigrams), 1)) if ctx_bigrams else 0.0
            score = overlap * 10
            if genre and (genre in t.category or t.category in genre or genre in index_text):
                score += 3
            for ctx in t.fit_contexts:
                if ctx and (ctx in chapter_context or chapter_context in ctx):
                    score += 2
            if score > 0:
                scored.append((score, t))
        scored.sort(key=lambda x: -x[0])

        # 分类轮询取多样池：每类各取一个，循环至取满 8 个，避免被单一分类刷屏
        by_cat = {}
        for score, t in scored:
            by_cat.setdefault(t.category or "未分类", []).append((score, t))
        result = []
        cats = [c for c, l in by_cat.items() if l]
        i = 0
        while len(result) < 8 and cats:
            cat = cats[i % len(cats)]
            lst = by_cat[cat]
            if lst:
                result.append(lst.pop(0)[1])
            else:
                cats.pop(i % len(cats))
                if not cats:
                    break
                continue
            i += 1
        return result or [t for _, t in scored[:3]] or self.templates[:3]

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
    ),
    # ── 悬疑/推理（补充库缺口） ──
    PlotTemplate(
        id="plot_sus_001", name="线索追踪与调查",
        category="悬疑", sub_category="推理调查",
        description="主角顺着蛛丝马迹调查一桩案件或异常事件",
        template_structure="[异常事件浮现]→[初步线索]→[走访/取证]→[线索中断/被误导]→[关键突破口]→[逼近真相]",
        slots=[
            PlotSlot("调查对象", "在查什么", ["离奇命案", "失踪事件", "异常死亡", "超自然现象"]),
            PlotSlot("关键线索", "破局抓手", ["一件遗物", "目击者证词", "现场痕迹", "一段记录"]),
            PlotSlot("误导来源", "谁在混淆视听", ["真凶伪装", "无关巧合", "内部人误导", "势力掩盖"]),
        ],
        variants=["硬核推理版", "灵异诡案版", "都市刑侦版"],
        fit_contexts=["悬疑", "调查", "线索", "推理", "谜团"],
        word_range=(2000, 4000),
        source="创作积累",
        usage_notes="线索要可回收：埋下的细节后文要有回应",
    ),
    PlotTemplate(
        id="plot_sus_002", name="密室谜局/不可能犯罪",
        category="悬疑", sub_category="推理诡计",
        description="一起看似不可能完成的案件，主角揭穿手法",
        template_structure="[案发·密室状态]→[在场者众说纷纭]→[主角提出反常细节]→[排除伪答案]→[演示真实手法]→[真凶现形]",
        slots=[
            PlotSlot("诡计类型", "如何制造密室", ["机关自尽", "藏匿再潜入", "替身/双胞胎", "时间差作案"]),
            PlotSlot("揭穿方式", "主角怎么破局", ["还原现场", "心理侧写", "系统/能力辅助", "对质逼问"]),
            PlotSlot("凶手动机", "为何行凶", ["复仇", "灭口", "争夺利益", "掩盖身份"]),
        ],
        variants=["本格推理版", "超能力破案版", "心理博弈版"],
        fit_contexts=["悬疑", "密室", "命案", "诡计", "推理"],
        word_range=(2500, 4500),
        source="创作积累",
        usage_notes="先给读者一个合理但错误的解释，再揭真手法",
    ),
    PlotTemplate(
        id="plot_sus_003", name="推理对决/真凶反转",
        category="悬疑", sub_category="智斗",
        description="与对手在推理上正面交锋，最后真凶身份大反转",
        template_structure="[两方对同一案件不同解读]→[各执证据交锋]→[主角被逼入死角]→[发现被忽略的关键点]→[真凶竟是意想不到之人]",
        slots=[
            PlotSlot("对手身份", "与主角对峙的人", ["名侦探", "涉案嫌疑人", "反派", "看似好人"]),
            PlotSlot("反转方向", "真凶是谁", ["最信任的人", "已死之人", "受害者本人", "主角亲近者"]),
            PlotSlot("决胜证据", "一击制胜的点", ["时间线漏洞", "一句话破绽", "物证矛盾", "动机盲区"]),
        ],
        variants=["本格反转版", "情感反转版", "多线陷阱版"],
        fit_contexts=["悬疑", "推理对决", "反转", "高光", "谜团"],
        word_range=(2500, 4500),
        source="创作积累",
        usage_notes="反转要早埋伏笔，让读者回想时觉得'原来如此'",
    ),
    PlotTemplate(
        id="plot_sus_004", name="追查幕后黑手",
        category="悬疑", sub_category="主线阴谋",
        description="从表面事件一层层追到更大的幕后势力",
        template_structure="[小事件入手]→[发现事件背后有人]→[顺藤摸瓜]→[黑手察觉并反扑]→[正面交锋]→[掀开冰山一角]",
        slots=[
            PlotSlot("表层事件", "切入的由头", ["一桩意外", "一笔异常交易", "一封密信", "一次袭击"]),
            PlotSlot("幕后势力", "真正的黑手", ["神秘组织", "顶层权贵", "异界势力", "同伴背叛"]),
            PlotSlot("推进方式", "怎么往上查", ["收买线人", "卧底", "反用敌人情报", "以身作饵"]),
        ],
        variants=["都市阴谋版", "权谋版", "超自然黑幕版"],
        fit_contexts=["悬疑", "阴谋", "追查", "主线", "幕后"],
        word_range=(2500, 5000),
        source="创作积累",
        usage_notes="黑手要分层次，先打小喽啰再一步步逼近核心",
    ),
    # ── 智斗/权谋 ──
    PlotTemplate(
        id="plot_strat_001", name="布局反杀/请君入瓮",
        category="智斗", sub_category="布局",
        description="主角提前布局，等对手踏入陷阱再一网打尽",
        template_structure="[对手步步紧逼]→[主角表面退让/示弱]→[暗布棋子]→[对手自以为得手]→[收网反杀]→[对手震惊]",
        slots=[
            PlotSlot("布局方式", "怎么设局", ["商业陷阱", "情报诱导", "借刀杀人", "舆论反制"]),
            PlotSlot("对手弱点", "切入的破绽", ["贪婪", "傲慢", "情报缺失", "家丑/软肋"]),
            PlotSlot("收网时机", "何时反杀", ["对手最高兴时", "对手以为胜券在握", "对方内部最乱时"]),
        ],
        variants=["商战版", "权谋版", "江湖仇杀版"],
        fit_contexts=["智斗", "布局", "反杀", "权谋", "中期"],
        word_range=(2000, 4000),
        source="创作积累",
        usage_notes="主角的棋要提前几章埋，读者回头看才拍案",
    ),
    PlotTemplate(
        id="plot_strat_002", name="谈判博弈",
        category="智斗", sub_category="交锋",
        description="在谈判桌上以智谋争取最大利益",
        template_structure="[谈判开局·各怀心思]→[互相试探底线]→[僵局]→[亮出筹码/虚张声势]→[达成或破裂]",
        slots=[
            PlotSlot("谈判标的", "争的是什么", ["资源分配", "势力结盟", "人质/伙伴", "地盘划分"]),
            PlotSlot("主角筹码", "手里有什么", ["独家情报", "对方把柄", "稀缺资源", "谈判技巧"]),
            PlotSlot("谈判走向", "结果如何", ["大胜", "双赢", "破裂转武力", "埋下后患"]),
        ],
        variants=["商战版", "外交版", "江湖地盘版"],
        fit_contexts=["智斗", "谈判", "博弈", "商战", "权谋"],
        word_range=(1800, 3500),
        source="创作积累",
        usage_notes="对话要暗藏机锋，别把底牌直接摆上桌",
    ),
    # ── 情感（补充） ──
    PlotTemplate(
        id="plot_emo_001", name="误会与和解",
        category="情感", sub_category="波折",
        description="因误会生出裂痕，真相大白后和解",
        template_structure="[亲密关系]→[误会产生]→[拒绝解释/越描越黑]→[裂痕加深]→[真相以意想不到方式揭开]→[和解/心结解开]",
        slots=[
            PlotSlot("误会内容", "误会了什么", ["被误会背叛", "被误会贪图利益", "被误会隐瞒病情", "被误会脚踏多船"]),
            PlotSlot("揭开方式", "真相如何浮出", ["第三者作证", "当事人自证", "意外撞破", "对方醒悟"]),
            PlotSlot("和解代价", "付出了什么", ["一方低头", "失去了一段时间", "共同经历险境", "彻底坦诚"]),
        ],
        variants=["虐心版", "轻喜剧版", "深沉版"],
        fit_contexts=["情感", "误会", "和解", "关系洗牌", "波折"],
        word_range=(2000, 4000),
        source="创作积累",
        usage_notes="误会导致的伤害要真实，和解才动人",
    ),
    PlotTemplate(
        id="plot_emo_002", name="久别重逢",
        category="情感", sub_category="重逢",
        description="多年分离后重逢，物是人非却情意未改",
        template_structure="[时过境迁]→[不期而遇]→[试探与尴尬]→[旧事浮现]→[抉择：再续或告别]",
        slots=[
            PlotSlot("分离原因", "当年为何分开", ["身不由己", "误会决裂", "生死相隔", "各自使命"]),
            PlotSlot("重逢场景", "在哪里再见", ["故地", "职场", "战场上", "一场婚礼"]),
            PlotSlot("重逢走向", "结局如何", ["破镜重圆", "彼此成全", "旧情发酵成新局", "物是人非"]),
        ],
        variants=["甜虐版", "洒脱版", "中年回望版"],
        fit_contexts=["情感", "重逢", "回忆", "旧情", "转折"],
        word_range=(2000, 3500),
        source="创作积累",
        usage_notes="重逢的情绪靠细节（一个动作/一句话）而非直抒",
    ),
    PlotTemplate(
        id="plot_emo_003", name="表白定情",
        category="情感", sub_category="升华",
        description="关系推至顶点，一方主动表明心意",
        template_structure="[日常积累的好感]→[契机到来]→[犹豫/试探]→[破釜沉舟的表白]→[对方回应]",
        slots=[
            PlotSlot("表白场合", "在哪里说出口", ["月下", "生死关头", "日常餐桌上", "公开场合"]),
            PlotSlot("表白方式", "怎么说", ["直球", "借物喻情", "行动证明", "被迫摊牌"]),
            PlotSlot("对方回应", "结果如何", ["欣然接受", "犹豫后接受", "被拒(留后手)", "反被表白"]),
        ],
        variants=["甜文版", "搞笑版", "悲情版"],
        fit_contexts=["情感", "表白", "定情", "升温", "高光"],
        word_range=(1800, 3000),
        source="创作积累",
        usage_notes="表白前要有足够铺垫，感情水到渠成才动人",
    ),
    # ── 战斗（补充） ──
    PlotTemplate(
        id="plot_bat_001", name="生死逃亡",
        category="战斗", sub_category="追逐",
        description="实力悬殊下的逃命，边逃边成长",
        template_structure="[强敌追杀]→[断后/牺牲]→[一路奔逃]→[绝境反击机会]→[摆脱或反杀]",
        slots=[
            PlotSlot("追杀者", "谁在追", ["更强的高手", "军队/势力", "未知怪物", "背叛的同伴"]),
            PlotSlot("逃亡手段", "怎么跑", ["地形利用", "伪装易容", "空间法宝", "极限速度"]),
            PlotSlot("出路", "如何脱困", ["强援赶到", "实力突破", "进入险地借势", "计谋甩脱"]),
        ],
        variants=["肾上腺素版", "悲壮牺牲版", "智勇双全版"],
        fit_contexts=["战斗", "逃亡", "追杀", "绝境", "成长"],
        word_range=(2500, 4500),
        source="创作积累",
        usage_notes="逃亡中要有小胜利，不能一路挨打",
    ),
    PlotTemplate(
        id="plot_bat_002", name="清理门户/师徒对决",
        category="战斗", sub_category="对决",
        description="面对叛徒或黑化的师长，必须正面清算",
        template_structure="[背叛揭露]→[昔日情分挣扎]→[立场抉择]→[正面对决]→[决断与代价]",
        slots=[
            PlotSlot("对决对象", "清理谁", ["叛徒师兄弟", "黑化师父", "堕落同门", "昔日战友"]),
            PlotSlot("对决理由", "为何必须打", ["清理门户", "守护大义", "为死者讨公道", "阻止阴谋"]),
            PlotSlot("结果", "如何收场", ["正面取胜", "惨胜", "以德报怨收服", "两败俱伤"]),
        ],
        variants=["悲壮版", "快意版", "挣扎版"],
        fit_contexts=["战斗", "师徒", "背叛", "清算", "后期"],
        word_range=(2500, 4500),
        source="创作积累",
        usage_notes="师徒情分是情绪核心，别变成纯武力打斗",
    ),
    PlotTemplate(
        id="plot_bat_003", name="围剿突围",
        category="战斗", sub_category="混战",
        description="陷入重重包围，绝境求生",
        template_structure="[包围成形]→[试探性突围]→[损失与决断]→[制造突破口]→[杀出重围]",
        slots=[
            PlotSlot("包围者", "谁在围", ["敌军", "多方势力联手", "上古大阵", "兽潮"]),
            PlotSlot("突破口", "怎么破", ["擒王", "声东击西", "燃烧潜能", "里应外合"]),
            PlotSlot("代价", "付出了什么", ["有人牺牲", "主力重伤", "失去宝物", "零代价(留悬念)"]),
        ],
        variants=["惨烈版", "智取版", "热血版"],
        fit_contexts=["战斗", "围剿", "突围", "危机", "高光"],
        word_range=(3000, 5500),
        source="创作积累",
        usage_notes="突围后要有余波（追兵、内奸、舆论），别干净利落就完了",
    ),
    # ── 成长（补充） ──
    PlotTemplate(
        id="plot_grow_001", name="突破瓶颈/顿悟",
        category="成长", sub_category="突破",
        description="卡在境界/心结多年，一朝顿悟突破",
        template_structure="[瓶颈困境]→[尝试各种办法无效]→[放下执念/触动心弦]→[顿悟]→[破境·实力质变]",
        slots=[
            PlotSlot("瓶颈类型", "卡在哪", ["修为境界", "心魔心结", "武学领悟", "能力上限"]),
            PlotSlot("顿悟契机", "因何突破", ["生死关头", "旁观他人", "一句话点醒", "自我和解"]),
            PlotSlot("突破效果", "破境后", ["实力暴涨", "掌握新能力", "解开封印", "获得新的道"]),
        ],
        variants=["热血版", "禅意版", "日常顿悟版"],
        fit_contexts=["成长", "突破", "瓶颈", "顿悟", "蜕变"],
        word_range=(2000, 4000),
        source="创作积累",
        usage_notes="顿悟要有铺垫，别'突然就突破了'",
    ),
    PlotTemplate(
        id="plot_grow_002", name="独闯险地取机缘",
        category="成长", sub_category="历练",
        description="孤身深入危险之地，取回关键机缘",
        template_structure="[得知险地有机缘]→[明知危险仍去]→[险象环生]→[获得机缘]→[活着回来(或引出新线)]",
        slots=[
            PlotSlot("险地", "哪里", ["禁地", "上古遗迹", "绝境", "敌方腹地"]),
            PlotSlot("机缘", "去拿什么", ["功法传承", "神兵", "灵药", "关键情报"]),
            PlotSlot("意外收获", "额外得到", ["结识强者", "发现阴谋", "身体蜕变", "脱胎换骨"]),
        ],
        variants=["苦修版", "奇遇版", "计谋版"],
        fit_contexts=["成长", "历练", "机缘", "冒险", "蜕变"],
        word_range=(2500, 4500),
        source="创作积累",
        usage_notes="险地要有规则感，不是主角光环硬闯",
    ),
    # ── 都市（补充） ──
    PlotTemplate(
        id="plot_urban_001", name="商战交锋",
        category="都市", sub_category="商战",
        description="商场博弈，资本与人心之战",
        template_structure="[商业对手出招]→[主角接招/暗布]→[股价/市场动荡]→[反制手段]→[胜负分晓]",
        slots=[
            PlotSlot("战场", "在哪里打", ["并购战", "招标会", "股市", "新品发布会"]),
            PlotSlot("对手", "与谁交手", ["财团", "同行巨头", "背叛的合伙人", "资本大鳄"]),
            PlotSlot("制胜手段", "怎么赢", ["技术专利", "内幕信息", "人心背向", "釜底抽薪"]),
        ],
        variants=["爽文版", "写实版", "烧脑版"],
        fit_contexts=["都市", "商战", "职场", "博弈", "资本"],
        word_range=(2000, 4000),
        source="创作积累",
        usage_notes="商战要有人物动机，别只是数字上的碾压",
    ),
    PlotTemplate(
        id="plot_urban_002", name="职场逆袭/背锅反杀",
        category="都市", sub_category="职场",
        description="被抢功背锅后，职场绝地反击",
        template_structure="[功劳被抢/被甩锅]→[隐忍收集证据]→[对手上位得意]→[当众揭穿]→[地位反转]",
        slots=[
            PlotSlot("职场类型", "什么环境", ["互联网大厂", "体制内", "家族企业", "创业公司"]),
            PlotSlot("被坑方式", "怎么被整", ["抢功", "甩锅", "PUA打压", "职场霸凌"]),
            PlotSlot("反杀方式", "怎么翻盘", ["当众对质", "用业绩说话", "揭穿内幕", "借势上位"]),
        ],
        variants=["打脸版", "爽文版", "写实版"],
        fit_contexts=["都市", "职场", "逆袭", "打脸", "现代"],
        word_range=(1800, 3500),
        source="创作积累",
        usage_notes="职场情节要踩中读者痛点（加班/背锅/晋升不公）",
    ),
    # ── 科幻（补充） ──
    PlotTemplate(
        id="plot_scifi_001", name="末世求生",
        category="科幻", sub_category="末世",
        description="灾难降临，在崩溃的秩序中求生",
        template_structure="[灾难爆发]→[秩序崩塌]→[资源争夺]→[生存选择]→[建立据点/发现真相]",
        slots=[
            PlotSlot("灾难类型", "什么末世", ["丧尸", "天灾", "灵气/异变", "外星入侵"]),
            PlotSlot("求生方式", "怎么活", ["建立小队", "独占资源", "变异强化", "科技造物"]),
            PlotSlot("人性考验", "考验什么", ["信任", "底线", "利益分配", "牺牲谁"]),
        ],
        variants=["硬核求生版", "人性拷问版", "轻松种田版"],
        fit_contexts=["科幻", "末世", "求生", "危机", "灾难"],
        word_range=(2500, 4500),
        source="创作积累",
        usage_notes="末世最抓人的是人性抉择，不是打丧尸",
    ),
    PlotTemplate(
        id="plot_scifi_002", name="金手指反噬",
        category="科幻", sub_category="代价",
        description="外挂/系统的代价显现，主角付出惨痛代价",
        template_structure="[依赖金手指一路顺风]→[异常信号出现]→[代价反噬]→[仓皇应对]→[被迫改变依赖方式]",
        slots=[
            PlotSlot("代价类型", "付出什么", ["寿命", "记忆", "身边人", "自由/人格"]),
            PlotSlot("反噬形式", "怎么反噬", ["能力暴走", "系统失控", "付出被加倍索取", "隐藏条件触发"]),
            PlotSlot("应对", "主角怎么选", ["戒掉依赖", "反向利用", "谈判/和解", "硬扛到底"]),
        ],
        variants=["悬疑版", "悲壮版", "爽文反杀版"],
        fit_contexts=["科幻", "系统", "代价", "反噬", "转折"],
        word_range=(2000, 4000),
        source="创作积累",
        usage_notes="代价要跟主角的核心欲望挂钩才疼",
    ),
    # ── 日常/生活流 ──
    PlotTemplate(
        id="plot_slice_001", name="日常经营/生活流",
        category="日常", sub_category="生活",
        description="种田、开店、经营一方天地的温情日常",
        template_structure="[安身之所]→[经营/劳作]→[街坊人情]→[小危机/小确幸]→[日子越过越好]",
        slots=[
            PlotSlot("经营内容", "靠什么过活", ["种田", "开店", "厨艺", "医术", "手艺"]),
            PlotSlot("邻里关系", "围绕谁", ["街坊邻居", "同行竞争", "收留的孤儿", "老手艺人家"]),
            PlotSlot("日常插曲", "发生什么", ["一场大病", "一场喜事", "一次危机", "一个贵人"]),
        ],
        variants=["温馨种田版", "治愈版", "轻喜剧版"],
        fit_contexts=["日常", "生活", "经营", "温情", "种田"],
        word_range=(1500, 3000),
        source="创作积累",
        usage_notes="生活流的魅力在细节与温度，别急着推主线",
    ),
]
