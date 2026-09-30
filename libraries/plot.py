"""
情节段库（Plot Device Library）
网文经典情节段的结构化模板 — 模板 + 变量槽位 + 变体
"""
from dataclasses import dataclass, field
from typing import Optional

from .base_library import JsonLibrary


@dataclass
class PlotSlot:
    """变量槽位 — 决定情节段的具体呈现"""
    name: str           # 槽位名，如 "主角身份"
    description: str    # 说明，如 "主角在此时的公众认知状态"
    options: list[str]  # 可选值列表
    default: str = ""


@dataclass
class PlotTemplate:
    """单个情节段模板"""
    id: str
    name: str
    category: str                    # 分类：爽文/悬念/情感/战斗...
    sub_category: str = ""           # 子分类：身份反转/扮猪吃虎/...
    description: str = ""
    template_structure: str = ""     # 情节段结构骨架
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


class PlotLibrary(JsonLibrary):
    """情节段库管理器（进程内单例，避免每实例重复读 JSON）"""
    _instance = None
    _list_attr = "templates"
    _key = "templates"
    _file_name = "plots.jsonl"

    def __init__(self, data_dir: str = ""):
        if getattr(self, "_initialized", False):
            return
        super().__init__(data_dir)

    @classmethod
    def _from_dict(cls, d: dict) -> "PlotTemplate":
        return PlotTemplate.from_dict(d)

    @classmethod
    def _builtin(cls) -> list:
        return BUILTIN_PLOTS

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
        """根据章节上下文匹配情节段（轻量语义：bigram 相似度 + 分类多样性）。

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


# ─── 内置情节段模板 ───
BUILTIN_PLOTS = [
    # ══════════════ 对峙冲突（confrontation）══════════════
    PlotTemplate(
        id="plot_confront_001", name="当众奚落·骤然还手", category="对峙冲突", sub_category="公开打脸",
        description="在满是看客的场合被当面贬低，先按捺后爆发、当众反将一军的冲突一幕。",
        template_structure="[众人围观/宿敌出言羞辱]→[主角先礼后兵按捺]→[对方得寸进尺踩线]→[主角当众反制]→[看客态度反转]",
        slots=[
            PlotSlot("羞辱场合", "冲突发生的公开场合", ["寿宴", "聚会", "大殿朝会", "校场比试"]),
            PlotSlot("宿敌身份", "当众施压的一方", ["旧敌", "世家纨绔", "同门师兄", "竞争同行"]),
            PlotSlot("反制手段", "主角如何还击", ["当众亮出底牌", "搬出证据对质", "以子之矛攻子之盾", "引强援撑腰"]),
        ],
        variants=["正剧版", "极致爽感版"],
        fit_contexts=["中期", "打脸", "人际冲突", "立威"],
        word_range=(1500, 3200), source="创作积累",
        usage_notes="反制要点到即止，给后续更大冲突留口子",
    ),
    PlotTemplate(
        id="plot_confront_002", name="堵门对峙·逼出实情", category="对峙冲突", sub_category="当场对质",
        description="拦住去路当面逼问、把一件见不得光的事当场掀到台面上的紧张一幕。",
        template_structure="[主角截住去路]→[对方顾左右而言他]→[证据一件件亮出]→[对方恼羞成怒动手或崩溃松口]",
        slots=[
            PlotSlot("对质对象", "被堵住逼问的人", ["关键证人", "隐瞒真相的故人", "涉事负责人", "内鬼"]),
            PlotSlot("追问之事", "被遮掩的实情", ["旧案真相", "一桩交易", "失踪者下落", "背叛证据"]),
            PlotSlot("逼问筹码", "用来撬开话的凭据", ["目击证词", "物证残片", "账册记录", "利害权衡"]),
        ],
        variants=["正剧版", "高压对峙版"],
        fit_contexts=["转折", "揭秘", "调查推进"],
        word_range=(1400, 3000), source="创作积累",
    ),
    PlotTemplate(
        id="plot_confront_003", name="翻脸决裂·划清界限", category="对峙冲突", sub_category="撕破脸",
        description="昔日同路因底线被触碰当众翻脸、正式分道扬镳并各自亮明立场的一幕。",
        template_structure="[旧日情分铺垫]→[一方越过红线]→[当众点破不再退让]→[各自亮出立场]→[决裂离场留余波]",
        slots=[
            PlotSlot("决裂对象", "与主角分道的一方", ["旧友", "搭档", "师门同门", "暧昧对象"]),
            PlotSlot("越线之事", "不可容忍的那件事", ["背叛利益", "欺压弱者", "触碰亲人", "隐瞒欺骗"]),
            PlotSlot("决裂方式", "以什么姿态分开", ["扬长而去", "留下狠话", "还清旧账", "沉默转身"]),
        ],
        variants=["激烈版", "平静断交版"],
        fit_contexts=["转折", "情感", "势力洗牌"],
        word_range=(1500, 3200), source="创作积累",
    ),
    # ══════════════ 谈判交涉（negotiation）══════════════
    PlotTemplate(
        id="plot_negot_001", name="谈判桌上·寸步不让", category="谈判交涉", sub_category="讨价还价",
        description="双方在关键交易或条件上你来我往、互探底线，最终谈拢或谈崩的一幕。",
        template_structure="[列座开局各自亮价]→[一轮拉扯互探底线]→[亮出筹码试探虚实]→[僵持后拍板或当场谈崩]",
        slots=[
            PlotSlot("谈判议题", "争的是什么", ["结盟条件", "赎回人质", "分利比例", "停火条款"]),
            PlotSlot("我方底牌", "压轴筹码", ["关键情报", "资源优势", "第三方后援", "时间压力"]),
            PlotSlot("对方软肋", "可被拿捏处", ["资金缺口", "致命把柄", "急于求成", "内部分歧"]),
        ],
        variants=["正剧版", "博弈爽感版"],
        fit_contexts=["中期", "势力博弈", "交易", "立威"],
        word_range=(1600, 3400), source="创作积累",
    ),
    PlotTemplate(
        id="plot_negot_002", name="带人上门·逼讨交代", category="谈判交涉", sub_category="逼宫施压",
        description="带着底气登门向掌权者要一个交代、逼其在情与理之间表态的一幕。",
        template_structure="[登门递话客套开场]→[对方推诿拖延]→[主角亮出凭据收紧]→[掌权者权衡后表态]",
        slots=[
            PlotSlot("上门对象", "被逼着表态的人", ["家主", "城主", "宗门执事", "管事上头"]),
            PlotSlot("讨的交代", "要对方给什么说法", ["公道", "赔偿", "放人", "公开认错"]),
            PlotSlot("倚仗", "底气从何而来", ["救过对方命", "握着把柄", "背后有人", "已留后手"]),
        ],
        variants=["正剧版", "强压版"],
        fit_contexts=["中期", "冲突", "伸张", "立规矩"],
        word_range=(1500, 3200), source="创作积累",
    ),
    PlotTemplate(
        id="plot_negot_003", name="游说入伙·晓以利害", category="谈判交涉", sub_category="拉拢说服",
        description="把一方势力或关键人物劝进自己阵营、用利益与前景打动人心的交涉一幕。",
        template_structure="[寻机接近建立信任]→[摆清局势恐吓与利诱]→[戳中对方痛点]→[对方松口谈条件]→[定约]",
        slots=[
            PlotSlot("游说对象", "要拉拢的人", ["隐居高手", "守城将领", "商人头领", "宗门中立派"]),
            PlotSlot("开出的价码", "给对方的承诺", ["庇护", "分利", "前程", "了却旧愿"]),
            PlotSlot("对方顾虑", "迟迟不答应的原因", ["得罪另一头", "信不过主角", "代价太大", "有旧怨"]),
        ],
        variants=["正剧版", "推心置腹版"],
        fit_contexts=["前期", "结盟", "势力建设", "布局"],
        word_range=(1600, 3500), source="创作积累",
    ),
    # ══════════════ 对白交锋（dialogue）══════════════
    PlotTemplate(
        id="plot_dialog_001", name="虚与委蛇·话里有话", category="对白交锋", sub_category="双关试探",
        description="两人表面寒暄实则互相试探底线、每句话都暗藏机锋的交锋一幕。",
        template_structure="[客套寒暄各自落座]→[一方抛话试探]→[另一方以话挡话反探]→[触及要害气氛骤紧]→[各自留一手收场]",
        slots=[
            PlotSlot("交锋双方", "谁和谁过招", ["主角对宿敌", "主角对幕后主使", "两方代表", "主角对不明身份者"]),
            PlotSlot("暗争之事", "彼此都想探的底", ["对方真实意图", "底牌深浅", "是否知悉某秘密", "背靠何人"]),
            PlotSlot("谈话场合", "在哪里过招", ["宴席", "茶楼雅间", "囚室探视", "深夜书房"]),
        ],
        variants=["文戏版", "机锋升级版"],
        fit_contexts=["前期", "谋略", "人物登场"],
        word_range=(1500, 3200), source="创作积累",
    ),
    PlotTemplate(
        id="plot_dialog_002", name="当众舌战·据理力争", category="对白交锋", sub_category="公开辩论",
        description="在众人面前与对手就某桩是非当堂辩驳、靠道理与气势压倒对方的一幕。",
        template_structure="[议题抛出众目睽睽]→[对方咄咄逼人]→[主角条分缕析反击]→[对方言语漏洞被抓]→[舆论倒向主角]",
        slots=[
            PlotSlot("辩题", "争的是哪桩是非", ["一件冤案", "一项决策", "一笔账", "一条规矩"]),
            PlotSlot("对手", "当堂唱对台的人", ["老学究", "指鹿为马的官员", "强词夺理的对手", "利令智昏的长辈"]),
            PlotSlot("决胜依据", "翻盘的铁证", ["人证当堂", "物证拍出", "自相矛盾的破绽", "当众对质"]),
        ],
        variants=["正剧版", "爽快打脸版"],
        fit_contexts=["转折", "公理", "洗冤"],
        word_range=(1800, 3800), source="创作积累",
    ),
    PlotTemplate(
        id="plot_dialog_003", name="夜审攻心·敲开铁嘴", category="对白交锋", sub_category="审讯盘问",
        description="对守口如瓶的俘虏或知情者施加攻心审讯、从只言片语里撬出实话的一幕。",
        template_structure="[把人提来摆开架势]→[软话与硬话轮番]→[抛出对方不知已被泄露的点]→[防线松动吐真言]",
        slots=[
            PlotSlot("受审者", "被盘问的对象", ["敌方细作", "知情俘虏", "嘴硬的同党", "被吓破胆的小人物"]),
            PlotSlot("攻心手法", "用什么撬开嘴", ["挑拨其与主上的信任", "示其软肋家人", "假意招揽", "以沉默施压"]),
            PlotSlot("审出的信息", "要挖的情报", ["对方兵力部署", "幕后主使", "下一步计划", "藏匿地点"]),
        ],
        variants=["高压版", "润物无声版"],
        fit_contexts=["转折", "情报战", "调查"],
        word_range=(1600, 3400), source="创作积累",
        usage_notes="审讯别一次性吐尽，留一两条线到后面再用",
    ),
    # ══════════════ 情感羁绊（dialogue/quiet）══════════════
    PlotTemplate(
        id="plot_rel_001", name="多年重逢·欲语还休", category="情感羁绊", sub_category="重逢",
        description="分隔多年的故人或旧情在某个平常拐角猝然重逢、千头万绪堵在喉头的一幕。",
        template_structure="[人来人往中骤然认出]→[四目相对一时无言]→[几句寒暄各藏心事]→[旧事被一句话勾起]→[匆忙或郑重告别]",
        slots=[
            PlotSlot("重逢对象", "与谁重逢", ["旧日恋人", "青梅竹马", "失散亲人", "故友"]),
            PlotSlot("分离原由", "当年为何分开", ["家族阻挠", "战乱失散", "误会决裂", "身份悬殊"]),
            PlotSlot("重逢场合", "在哪里撞见", ["异乡街角", "同场宴会", "故地重游", "生死关头"]),
        ],
        variants=["克制版", "情绪奔涌版"],
        fit_contexts=["情感", "转折", "人物回归"],
        word_range=(1400, 3000), source="创作积累",
    ),
    PlotTemplate(
        id="plot_rel_002", name="误会摊开·冰释前嫌", category="情感羁绊", sub_category="和解",
        description="横亘多时的误会终于被摊开说清、两人隔阂消融言归于好的一幕。",
        template_structure="[旧怨悬而未解]→[一方终于开口提起当年]→[另一方的苦衷顺势倾泻]→[真相与歉意同时落地]→[和解留温]",
        slots=[
            PlotSlot("误会双方", "谁与谁解开心结", ["挚友", "恋人", "亲子", "同门"]),
            PlotSlot("误会根源", "当初拧成死结的事", ["一句转述失真", "一方隐瞒苦衷", "第三人在中间作梗", "阴差阳错的巧合"]),
            PlotSlot("和解契机", "为何此刻说开", ["生死一线", "见对方仍挂念", "真相找上门", "有人牵线"]),
        ],
        variants=["哽咽版", "释然轻笑版"],
        fit_contexts=["情感", "和解", "人物关系推进"],
        word_range=(1500, 3200), source="创作积累",
    ),
    PlotTemplate(
        id="plot_rel_003", name="深夜交心·互诉衷肠", category="情感羁绊", sub_category="交心",
        description="卸下防备的两人在深夜里把藏在心底的软肋与真心摊给对方的一幕。",
        template_structure="[夜静人稀放下白日的针锋]→[一人无意说起旧伤]→[另一人没接话却也没走]→[心事一层层往外翻]→[关系悄然进了一步]",
        slots=[
            PlotSlot("交心双方", "谁与谁敞开心扉", ["战友", "假扮情侣", "主仆", "至交"]),
            PlotSlot("吐露心事", "摊开了什么", ["过往创伤", "埋藏的动机", "不为人知的软肋", "一句压抑已久的真话"]),
            PlotSlot("深夜场景", "氛围承托", ["篝火旁", "檐下听雨", "屋顶吹风", "病榻前"]),
        ],
        variants=["克制版", "直白动情版"],
        fit_contexts=["情感", "信任建立", "人物深化"],
        word_range=(1500, 3200), source="创作积累",
    ),
    # ══════════════ 推理查证（investigation）══════════════
    PlotTemplate(
        id="plot_invest_001", name="勘察现场·捕捉疑点", category="推理查证", sub_category="取证",
        description="主角亲临案发现场逐寸排查、从人人忽略的细节里抓出关键疑点的一幕。",
        template_structure="[抵达现场只见表象]→[例行问询走个过场]→[主角蹲身细察蹊跷处]→[一处违和细节浮出]→[顺它牵出调查方向]",
        slots=[
            PlotSlot("案件类型", "在查什么", ["密室命案", "离奇失踪", "密室失窃", "灭口疑云"]),
            PlotSlot("现场疑点", "被忽略的违和处", ["开错方向的锁", "多出的一双脚印", "不合时令的残留物", "位置被挪过的物件"]),
            PlotSlot("主角身份", "以何身份介入", ["捕快", "私家侦探", "恰逢其会", "被牵连者"]),
        ],
        variants=["正剧版", "冷静炫技版"],
        fit_contexts=["悬疑", "调查推进", "推理开局"],
        word_range=(2000, 4000), source="创作积累",
    ),
    PlotTemplate(
        id="plot_invest_002", name="顺藤摸瓜·追索线索", category="推理查证", sub_category="追踪调查",
        description="沿着一条线索引出一连串人物、步步逼近真相却在半途被人抢先灭口的调查一幕。",
        template_structure="[锁定一个关键知情者]→[上门扑空只余线索]→[循着线索引向下家]→[半路发现知情者已遭灭口]→[线索断处反见更大人影]",
        slots=[
            PlotSlot("线索源头", "从谁手里拿到线头", ["死者遗物", "匿名告密", "旧卷宗夹页", "买通的下人"]),
            PlotSlot("追查对象", "锁定的目标是谁", ["销赃的中间人", "当年的经手人", "神秘买家", "在逃嫌犯"]),
            PlotSlot("追索阻碍", "途中被什么挡住", ["有人清场", "关键人消失", "线索是假的", "追兵咬尾"]),
        ],
        variants=["正剧版", "步步惊心版"],
        fit_contexts=["悬疑", "调查推进", "设局前置"],
        word_range=(2000, 4200), source="创作积累",
    ),
    PlotTemplate(
        id="plot_invest_003", name="深夜复盘·推翻定论", category="推理查证", sub_category="推理复盘",
        description="把案情在脑中重新走一遍、抓住一处不自洽而推翻既定结论的一幕。",
        template_structure="[所有人认定某结论]→[主角独坐回放关键时间点]→[一处时间/逻辑对不上]→[换一种假设重演]→[新假设把众人疑点串通]",
        slots=[
            PlotSlot("被推翻的定论", "众人认定了什么", ["意外身亡", "仇杀报复", "自尽", "外贼所为"]),
            PlotSlot("关键破绽", "哪处对不上", ["不在场证明太完美", "动机牵强", "凶器不符", "时间线有缝"]),
            PlotSlot("复盘场景", "在哪里想通的", ["夜半灯下", "重走现场", "与人论辩中", "雨后旧地"]),
        ],
        variants=["独白推理版", "与人推演版"],
        fit_contexts=["悬疑", "反转前置", "高潮前"],
        word_range=(2000, 4200), source="创作积累",
    ),
    # ══════════════ 揭秘真相（revelation）══════════════
    PlotTemplate(
        id="plot_reveal_001", name="身份曝光·全场哗然", category="揭秘真相", sub_category="身份揭晓",
        description="隐藏已久的真实身份在众目睽睽下被撕开、满堂震惊的一幕。",
        template_structure="[风平浪静各自盘算]→[一个举动无意露了馅]→[知情者当众点破]→[满堂目光齐刷刷逼来]→[主角坦然/猝然接住]",
        slots=[
            PlotSlot("隐藏身份", "曝光的是什么来头", ["隐世高手", "世家遗孤", "皇族血脉", "传说组织之首"]),
            PlotSlot("曝光方式", "怎么被揭开的", ["信物被认", "出手露实力", "旧识当众相认", "敌人拿此要挟"]),
            PlotSlot("在场人物", "围观的是什么人", ["敌对家族", "满城权贵", "同门众人", "旧日仇家"]),
        ],
        variants=["爽感震撼版", "沉重代价版"],
        fit_contexts=["转折", "身份线", "高潮"],
        word_range=(1800, 3800), source="创作积累",
    ),
    PlotTemplate(
        id="plot_reveal_002", name="当众揭穿·撕下伪装", category="揭秘真相", sub_category="揭穿",
        description="把伪善者维持多年的体面当场戳穿、让真面目暴露于人前的一幕。",
        template_structure="[伪善者正享受拥戴]→[主角携铁证登场]→[一层层当众剥开其话术]→[人设崩塌众人哗然]→[伪善者恼羞反扑被摁下]",
        slots=[
            PlotSlot("被揭穿者", "剥开谁的面具", ["沽名钓誉的名医", "道貌岸然的长老", "假仁假义的善人", "满嘴大义的叛徒"]),
            PlotSlot("揭穿凭据", "靠什么钉死对方", ["账册记录", "现场人证", "其亲口矛盾的录音", "藏匿的证据"]),
            PlotSlot("揭穿场合", "在哪里当众拆台", ["其得意的大典", "公堂之上", "全城瞩目的宴席", "死者灵前"]),
        ],
        variants=["爽快利落版", "沉重公审版"],
        fit_contexts=["中期", "反转", "洗冤", "立威"],
        word_range=(1800, 3800), source="创作积累",
    ),
    PlotTemplate(
        id="plot_reveal_003", name="幕后现身·真相翻转", category="揭秘真相", sub_category="主使登场",
        description="一直藏于幕后的真正主使在收官时现身、把此前所有认知一并翻新的一幕。",
        template_structure="[一切看似尘埃落定]→[主角揪住一处仍不合逻辑]→[顺着追到幕后推手]→[那人淡然现身自揭底牌]→[全局棋盘重新摆过]",
        slots=[
            PlotSlot("幕后主使", "真正的操盘者是谁", ["最不起眼的旁观者", "死者身边人", "已死去的人", "主角信赖之人"]),
            PlotSlot("布局动机", "他图什么", ["夺产", "复仇", "掩盖旧案", "一场更大实验"]),
            PlotSlot("现身时机", "他为何此刻亮明", ["自以为必胜", "被逼到墙角", "给主角设的最后考题", "收网在即"]),
        ],
        variants=["惊悚揭底版", "沉静过招版"],
        fit_contexts=["高潮", "大反转", "主线收束"],
        word_range=(2000, 4200), source="创作积累",
    ),
    # ══════════════ 战斗历练（action）══════════════
    PlotTemplate(
        id="plot_action_001", name="狭路相逢·一战立威", category="战斗历练", sub_category="单挑对决",
        description="在众目睽睽或险要狭道上与对手正面交锋、凭硬实力一战定名声的一幕。",
        template_structure="[狭路相遇气氛凝滞]→[言语试探一触即发]→[交手拆招互有来回]→[主角亮出杀招见真章]→[胜败落定各留后话]",
        slots=[
            PlotSlot("对战对象", "与谁分胜负", ["同阶宿敌", "成名高手", "挡路的恶霸", "来试深浅的使者"]),
            PlotSlot("战场地形", "在哪里打", ["山道窄桥", "擂台之上", "断崖边缘", "闹市街心"]),
            PlotSlot("胜负筹码", "这一战赌什么", ["名声", "一件物事", "一个承诺", "去留去从"]),
        ],
        variants=["凌厉快打版", "蓄势爆发版"],
        fit_contexts=["战斗", "立威", "升级展示"],
        word_range=(2000, 4200), source="创作积累",
    ),
    PlotTemplate(
        id="plot_action_002", name="擂台车轮·连胜扬名", category="战斗历练", sub_category="车轮比武",
        description="在擂台上接连迎战多位对手、愈战愈勇直到无人敢再上场的一幕。",
        template_structure="[登台先赢一局立威]→[台下高手逐个按捺不住]→[第二三人接连上台皆败]→[体力见底却咬牙再胜]→[全场喝彩声名传开]",
        slots=[
            PlotSlot("比试名目", "为什么设这台", ["宗门大比", "招亲比武", "争令牌", "赌坊摆擂"]),
            PlotSlot("连番对手", "先后撞上什么人", ["同门翘楚", "外门天才", "成名的老手", "乔装的高手"]),
            PlotSlot("隐藏变量", "比试之外的暗流", ["有人下黑手", "赛制被人操纵", "强敌压轴未上", "胜了会得罪谁"]),
        ],
        variants=["热血连胜版", "险胜反超版"],
        fit_contexts=["战斗", "扬名", "大比弧中段"],
        word_range=(2400, 4600), source="创作积累",
    ),
    PlotTemplate(
        id="plot_action_003", name="混战突围·死里逃生", category="战斗历练", sub_category="乱战突围",
        description="被卷入多方混战、护着同伴且战且退杀出重围的一幕。",
        template_structure="[变故突起被卷进乱局]→[敌我难辨处处杀机]→[主角护人杀出一条路]→[追兵咬住不放]→[借地形/援手惊险甩脱]",
        slots=[
            PlotSlot("混战缘由", "因何打成一团", ["围剿伏击", "夺宝乱局", "两方火并遭殃", "斩首行动暴露"]),
            PlotSlot("同行者", "要护着谁", ["重伤的同伴", "无关的路人", "重要人质", "拖后腿的累赘"]),
            PlotSlot("脱身依仗", "靠什么杀出", ["地形熟悉", "一件保命物", "虚晃一枪", "外人接应"]),
        ],
        variants=["惨烈悲壮版", "利落反杀版"],
        fit_contexts=["战斗", "危机", "转折"],
        word_range=(2200, 4400), source="创作积累",
    ),
    # ══════════════ 危机求生（danger）══════════════
    PlotTemplate(
        id="plot_danger_001", name="追兵在后·亡命奔逃", category="危机求生", sub_category="逃亡",
        description="身后追兵步步紧逼、主角负伤带物夺路而逃、每一处喘息都随时会被追上的一幕。",
        template_structure="[败势已定夺路便走]→[追兵衔尾咬住]→[险要处设障缓敌]→[眼看要被合围]→[借天时地利惊险甩脱]",
        slots=[
            PlotSlot("逃的缘由", "为何被追杀", ["身怀机密", "背了命案", "刚夺了宝", "暴露了身份"]),
            PlotSlot("追兵构成", "身后是谁", ["官府缇骑", "杀手死士", "兽潮余波", "敌国追骑"]),
            PlotSlot("逃亡处境", "一路上的难处", ["负伤失血", "带着累赘", "天寒断粮", "前有险地"]),
        ],
        variants=["紧绷版", "反杀转折版"],
        fit_contexts=["危机", "过渡", "设局前置"],
        word_range=(1800, 3600), source="创作积累",
    ),
    PlotTemplate(
        id="plot_danger_002", name="两难抉择·舍一保一", category="危机求生", sub_category="生死抉择",
        description="被逼到只能二选一的绝境、主角必须在两样都放不下的事物间咬牙做取舍的一幕。",
        template_structure="[局势骤然收紧只余两路]→[两条路都代价惨重]→[时间不等人逼着立刻定]→[主角闭眼做出决断]→[代价当场兑现余痛难平]",
        slots=[
            PlotSlot("二选一", "在什么之间取舍", ["救人还是取物", "救甲还是救乙", "保命还是保承诺", "止损还是翻盘"]),
            PlotSlot("设局之人", "把局面逼到这步的推手", ["对手的阳谋", "天灾", "队友背叛", "自己种下的因"]),
            PlotSlot("代价后果", "选完失去什么", ["搭上一条命", "断一条财路", "落下心病", "被人记恨"]),
        ],
        variants=["沉重版", "险中求胜版"],
        fit_contexts=["危机", "人物弧光", "转折"],
        word_range=(1800, 3600), source="创作积累",
    ),
    PlotTemplate(
        id="plot_danger_003", name="绝境反扑·背水一战", category="危机求生", sub_category="绝地反击",
        description="被逼到退无可退的墙角、主角把一切押上做最后一次反扑的一幕。",
        template_structure="[后路尽断退无可退]→[对手以为胜券在握]→[主角敛息筹谋最后一搏]→[抓住破绽倾力反扑]→[绝境翻盘或惨胜]",
        slots=[
            PlotSlot("绝境成因", "怎么被逼到这份上", ["中了陷阱", "实力悬殊", "援军被断", "多年布局被破"]),
            PlotSlot("翻盘筹码", "最后一搏押什么", ["隐藏杀招", "以命换命的狠劲", "早就埋好的后手", "对手的致命轻敌"]),
            PlotSlot("结果走向", "翻盘之后的收束", ["惨胜", "引动更大危机", "赢得一线生机", "拼光底牌"]),
        ],
        variants=["惨烈一战版", "漂亮翻盘版"],
        fit_contexts=["危机", "高潮", "反击"],
        word_range=(2000, 4000), source="创作积累",
    ),
    # ══════════════ 余波收尾（aftermath）══════════════
    PlotTemplate(
        id="plot_after_001", name="大战之后·清点残局", category="余波收尾", sub_category="战后",
        description="一场大战落幕、众人带着伤痕清点损失、收拾废墟并商量后事的一幕。",
        template_structure="[硝烟散尽满目疮痍]→[救人清点逐项进行]→[伤亡与得失摆上台面]→[有人红了眼有人沉默了]→[定下下一步去向]",
        slots=[
            PlotSlot("战后果象", "留下什么烂摊子", ["伤亡待葬", "据点半毁", "物资见底", "人心浮动"]),
            PlotSlot("清点之人", "谁在主持局面", ["主角", "幸存的副手", "当地长者", "赶来接应的势力"]),
            PlotSlot("暗藏余波", "平静之下的隐患", ["有人趁乱卷走东西", "伤者里有敌方细作", "复仇种子埋下", "更大敌人将至"]),
        ],
        variants=["沉静哀悼版", "劫后余生版"],
        fit_contexts=["余波", "过渡", "情感缓冲"],
        word_range=(1600, 3400), source="创作积累",
    ),
    PlotTemplate(
        id="plot_after_002", name="尘埃落定·各得其所", category="余波收尾", sub_category="结局收束",
        description="一段恩怨了结之后，众人各自走向应得的结局、主角回望来路定下心念的一幕。",
        template_structure="[旧怨了结一身轻]→[有恩的报恩有仇的释然]→[主角与故人做最后交代]→[启程或留守的抉择]→[留一句引向下程的话]",
        slots=[
            PlotSlot("了结之事", "什么事到此翻篇", ["一桩旧怨", "一场夺权", "一段误会", "一个执念"]),
            PlotSlot("各人归宿", "同行者的去向", ["各奔东西", "留下来追随", "归隐", "去开创自己的路"]),
            PlotSlot("收尾余味", "留下的念想", ["一句未竟之约", "一个新谜", "一件信物", "一封告别信"]),
        ],
        variants=["温暖释然版", "怅然若失版"],
        fit_contexts=["结局", "卷末", "情感收束"],
        word_range=(1500, 3200), source="创作积累",
    ),
    PlotTemplate(
        id="plot_after_003", name="旧账清算·余波未平", category="余波收尾", sub_category="善后追责",
        description="风波过后逐一清算责任、处置内鬼与从犯，却在收尾处发现还漏掉什么的一幕。",
        template_structure="[大局初定开始算账]→[主犯已伏从犯归案]→[对坐的旧人里有人闪躲]→[主角忽然发现一处对不上]→[新的疑影落在幕布上]",
        slots=[
            PlotSlot("清算对象", "追责到谁头上", ["内鬼", "帮凶", "墙头草", "见死不救者"]),
            PlotSlot("清算方式", "怎么处置", ["当众揭罪", "逐出", "交官", "留作后手"]),
            PlotSlot("漏掉的疑影", "收尾处的反常", ["多出的一具尸体", "少了一样证物", "一句圆不上的口供", "本该死的人没死"]),
        ],
        variants=["干净利落版", "细思极恐版"],
        fit_contexts=["余波", "系列开局", "主线推进"],
        word_range=(1600, 3400), source="创作积累",
    ),
    # ══════════════ 谋划布局（planning）══════════════
    PlotTemplate(
        id="plot_plan_001", name="摆子设局·引君入瓮", category="谋划布局", sub_category="布局",
        description="主角提前在棋盘上落子、用一处诱饵把对手一步步引进预设口袋的一幕。",
        template_structure="[看破对手下一步路数]→[借势布下诱饵与空门]→[对手果然循饵而来]→[收网时机还差一线]→[主角按兵等鱼咬实]",
        slots=[
            PlotSlot("设局目标", "要算计的人是谁", ["自负的强敌", "贪婪的对手", "多疑的幕后", "贪功的下属"]),
            PlotSlot("诱饵设置", "拿什么引他上钩", ["一桩假情报", "一处看似空的门户", "一件舍不得的宝物", "一个假的软肋"]),
            PlotSlot("局成关键", "收网靠哪一步", ["等对手贪念上头", "等第三方入局", "等一个时间差", "等对方自曝破绽"]),
        ],
        variants=["步步为营版", "举重若轻版"],
        fit_contexts=["谋略", "权谋", "智斗"],
        word_range=(1800, 3600), source="创作积累",
    ),
    PlotTemplate(
        id="plot_plan_002", name="密会筹谋·共定方略", category="谋划布局", sub_category="定策",
        description="与信得过的几人闭门密商、把一盘散沙的想法理成一条可执行计策的一幕。",
        template_structure="[各执一词谁也说服不了谁]→[主角把零散情报摊在桌上]→[一条线把众人顾虑串通]→[方案被反复推敲补漏]→[分工敲定各自领命]",
        slots=[
            PlotSlot("密商主题", "要定下什么事", ["夺回据点", "救一个人", "破一场局", "应对将至的危机"]),
            PlotSlot("与会之人", "桌边都有谁", ["心腹班底", "盟军代表", "有私心的合作者", "被迫入伙的高手"]),
            PlotSlot("最大分歧", "卡在哪一步", ["谁去打头阵", "要不要冒险", "信任一个可疑的人", "时间赶不赶得及"]),
        ],
        variants=["沙盘推演版", "人心博弈版"],
        fit_contexts=["谋略", "布局", "势力建设"],
        word_range=(1700, 3600), source="创作积累",
    ),
    PlotTemplate(
        id="plot_plan_003", name="投石问路·探人虚实", category="谋划布局", sub_category="试探",
        description="不动声色地放出一件小事或小恩小惠，借对方的反应掂量其深浅与意图的一幕。",
        template_structure="[对某人底细拿不准]→[遣人递一个无伤大雅的试探]→[对方反应被尽收眼底]→[从细枝末节推出其真实盘算]→[定下是拉拢还是提防]",
        slots=[
            PlotSlot("试探对象", "要掂量的人", ["新来的盟友", "深藏不露的管家", "墙头草的商户", "底细不明的合作者"]),
            PlotSlot("试探由头", "拿什么当借口", ["一桩小买卖", "一次无意拜访", "借东西", "邀一场酒局"]),
            PlotSlot("探得的深浅", "从反应看出什么", ["其背后另有主使", "其实是个草包", "对主角已有戒心", "值得深交"]),
        ],
        variants=["心照不宣版", "彼此试探版"],
        fit_contexts=["谋略", "人物登场", "布局"],
        word_range=(1500, 3200), source="创作积累",
    ),
    # ══════════════ 平静日常（quiet）══════════════
    PlotTemplate(
        id="plot_quiet_001", name="市井烟火·寻常一日", category="平静日常", sub_category="生活流",
        description="没有风波的寻常一天，买菜做饭、与人闲话、日子照旧的日常一幕。",
        template_structure="[晨起琐事铺开]→[街市上与人讨价还价]→[邻里三两句闲话]→[一个无伤大雅的小插曲]→[黄昏归家灯火可亲]",
        slots=[
            PlotSlot("日常场景", "在哪过这一天", ["小城街巷", "据点后院", "山野村舍", "集市铺子"]),
            PlotSlot("同行之人", "身边是谁", ["家人", "同伴", "邻里", "新收的小徒弟"]),
            PlotSlot("小插曲", "平淡里的那点波澜", ["被人讹了一笔", "捡到走失的孩子", "旧相识擦肩", "一只猫赖上主角"]),
        ],
        variants=["温馨版", "带点诙谐版"],
        fit_contexts=["日常", "情感缓冲", "节奏调剂"],
        word_range=(1200, 2600), source="创作积累",
        usage_notes="日常段最忌信息满格，留白一点更像活人日子",
    ),
    PlotTemplate(
        id="plot_quiet_002", name="独处静思·梳理心绪", category="平静日常", sub_category="独处",
        description="独自一人安坐下来、把连日纷乱的心绪与抉择静静理清的一幕。",
        template_structure="[周遭安静下来只剩自己]→[一件旧物或往事浮上心头]→[顺着它把连日线索过一遍]→[某处心结悄然松动]→[拿定一个主意]",
        slots=[
            PlotSlot("独处地点", "在哪里独坐", ["檐下", "江边", "空屋", "夜深屋顶"]),
            PlotSlot("心中所扰", "在翻什么心事", ["前路的抉择", "对某人的亏欠", "连日的线索", "一道过不去的坎"]),
            PlotSlot("心绪落点", "想通了什么", ["放下了执念", "下了狠心", "看穿一件事", "决定信一次谁"]),
        ],
        variants=["安静内省版", "带着钝痛版"],
        fit_contexts=["日常", "人物深化", "转折前"],
        word_range=(1200, 2600), source="创作积累",
    ),
    PlotTemplate(
        id="plot_quiet_003", name="围炉夜话·家常闲谈", category="平静日常", sub_category="家常对话",
        description="灯火下几个人有一搭没一搭地闲聊、说着不着边际的话却让关系悄悄变近的一幕。",
        template_structure="[围坐取暖东一句西一句]→[有人说起年轻时一件糗事]→[众人笑作一团]→[话锋忽转到某句心里话]→[夜更深了谁也没舍得散]",
        slots=[
            PlotSlot("围坐的人", "都有谁在", ["同伴", "一家人", "新结识的朋友", "不打不相识的对手"]),
            PlotSlot("闲聊内容", "都扯了些什么", ["往昔旧事", "明天的打算", "无关紧要的闲话", "各自家乡"]),
            PlotSlot("话里的真心", "不经意透出的真心", ["一句惦记", "一个软肋", "一场亏欠", "一句没说完的告白"]),
        ],
        variants=["温暖絮叨版", "带点心酸版"],
        fit_contexts=["日常", "情感", "信任推进"],
        word_range=(1400, 2800), source="创作积累",
    ),
    # ══════════════ 开篇引入（opening/transition）══════════════
    PlotTemplate(
        id="plot_open_001", name="天翻地覆·异变开局", category="开篇引入", sub_category="大变突至",
        description="平静日常被一场突如其来的大变砸碎、主角被迫第一次直面新世界的开局一幕。",
        template_structure="[安稳日子开个头]→[异变征兆毫无预兆降临]→[众人惊慌四散]→[主角被迫做出第一个反应]→[回望时旧生活已被撕开口子]",
        slots=[
            PlotSlot("异变类型", "世界出了什么乱子", ["灾变将至", "穿越睁眼", "系统降临", "末世突袭"]),
            PlotSlot("主角此刻", "异变时他在做什么", ["上班途中", "家中安睡", "野外赶路", "病床上睁眼"]),
            PlotSlot("第一个抉择", "慌乱里先抓什么", ["保住家人", "抢物资", "弄清规则", "找安全处"]),
        ],
        variants=["紧张逃生版", "冷静应对版"],
        fit_contexts=["开篇", "世界观引入", "黄金三章"],
        word_range=(2000, 3600), source="创作积累",
        usage_notes="开局别一次把设定倒完，规则随事件漏一点",
    ),
    PlotTemplate(
        id="plot_open_002", name="初来乍到·立足试探", category="开篇引入", sub_category="新地图探索",
        description="初入一座陌生城镇或宗门、人生地不熟地摸门路、撞见本地人微妙盘算的一幕。",
        template_structure="[踏入新地人生地不熟]→[先落脚再找人打听门道]→[几处暗涌被主角看进眼里]→[有人出面刁难试探深浅]→[主角不动声色先立住脚跟]",
        slots=[
            PlotSlot("新地类型", "来到什么地方", ["边陲小城", "大宗门外门", "鱼龙混杂的坊市", "异国他乡"]),
            PlotSlot("落脚依仗", "凭什么站住脚", ["一技之长", "熟人的引荐", "硬实力", "会看眼色"]),
            PlotSlot("本地暗涌", "撞见什么猫腻", ["地头蛇收保护费", "两派相争", "一场骗局", "隐世的强者"]),
        ],
        variants=["新地图新鲜版", "如履薄冰版"],
        fit_contexts=["开篇", "换地图", "探索"],
        word_range=(1800, 3400), source="创作积累",
    ),
    PlotTemplate(
        id="plot_open_003", name="卷入命案·开局被冤", category="开篇引入", sub_category="开局冲突",
        description="刚踏进故事就被一桩命案砸到头上、自己成了头号嫌犯、百口莫辩的强开局一幕。",
        template_structure="[一个平常照面而已]→[转眼对方暴毙当场]→[凶器/现场全指向主角]→[围观者七嘴八舌坐实罪名]→[主角被迫当众脱身或认下麻烦]",
        slots=[
            PlotSlot("死者身份", "死的是谁", ["刚照面的陌生人", "当街冲突的恶霸", "前来托付的信使", "主角的故交"]),
            PlotSlot("栽赃方式", "怎么把脏水泼上来的", ["凶器在身", "只有主角在场", "一封遗书指向", "人证咬定"]),
            PlotSlot("脱身依仗", "凭什么洗清", ["有人看见真相", "一处时间破绽", "主角当场查出端倪", "真凶自己露了马脚"]),
        ],
        variants=["高能开局版", "被逼入局版"],
        fit_contexts=["开篇", "强冲突", "主线引子"],
        word_range=(2200, 4000), source="创作积累",
    ),
]
