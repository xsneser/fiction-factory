"""
角色原型库（Character Library）— 网文高频人物性格原型 + 代表人物

与桥段/大纲/笑点三库同构（全局 JsonLibrary 单例，libraries/data/characters.json）。
供「设定表单从原型库选」与后续生成注入使用；每本书的人物仍存各自 basic_info.characters。
"""
from dataclasses import dataclass, field
from .base_library import JsonLibrary


@dataclass
class CharacterArchetype:
    """单个角色原型（按性格归类，附代表人物示例）"""
    id: str
    name: str
    personality: str              # 性格关键词/一句话
    description: str              # 原型行为模式描述
    archetypes: list[str] = field(default_factory=list)   # 变体/细分
    examples: list[str] = field(default_factory=list)     # 代表人物（作品/场景）
    catchphrases: list[str] = field(default_factory=list) # 常见口癖/惯用语句
    tags: list[str] = field(default_factory=list)         # 性格标签
    fit_genres: list[str] = field(default_factory=list)   # 适配流派
    source: str = ""              # 来源
    created_at: str = "2026-08-18"
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "personality": self.personality,
            "description": self.description,
            "archetypes": self.archetypes, "examples": self.examples,
            "catchphrases": self.catchphrases, "tags": self.tags,
            "fit_genres": self.fit_genres, "source": self.source,
            "created_at": self.created_at, "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CharacterArchetype":
        return CharacterArchetype(**{k: v for k, v in d.items()
                                     if k in CharacterArchetype.__dataclass_fields__})


class CharacterLibrary(JsonLibrary):
    """角色原型库管理器（进程内单例，避免每实例重复读 JSON）"""
    _instance = None
    _list_attr = "archetypes"
    _key = "archetypes"
    _file_name = "characters.json"

    @classmethod
    def _from_dict(cls, d: dict) -> "CharacterArchetype":
        return CharacterArchetype.from_dict(d)

    @classmethod
    def _builtin(cls) -> list:
        return BUILTIN_CHARACTERS

    def get_by_id(self, char_id: str):
        for a in self.archetypes:
            if a.id == char_id:
                return a
        return None

    def search(self, tag: str = "", genre: str = "",
               kw: str = "") -> list[CharacterArchetype]:
        """按性格标签 / 适配流派 / 关键词搜索原型。"""
        results = self.archetypes
        if tag:
            results = [a for a in results if tag in a.tags]
        if genre:
            results = [a for a in results if genre in a.fit_genres]
        if kw:
            results = [a for a in results
                       if kw in a.name or kw in a.personality or kw in a.description]
        return results

    def categories(self) -> list[str]:
        """返回所有性格标签 + 适配流派并集（供页面 tab / 表单原型选择分组）。"""
        cats = set()
        for a in self.archetypes:
            cats.update(a.tags or [])
            cats.update(a.fit_genres or [])
        return sorted(c for c in cats if c)


# ─── 内置角色原型种子 ───

BUILTIN_CHARACTERS = [
    CharacterArchetype(
        id="char_001", name="高冷毒舌", personality="高冷毒舌、嘴硬心软",
        description="表面拒人千里、说话带刺，关键时刻却默默兜底护人。外冷内热的反差制造笑点与张力。",
        archetypes=["冰山大佬", "毒舌前辈", "冷面管家"],
        examples=["权谋剧里算无遗策的军师", "动漫里嘴毒心软的前辈"],
        catchphrases=["呵。", "脑子是个好东西，可惜你没有。"],
        tags=["高冷", "毒舌"], fit_genres=["都市", "玄幻", "悬疑"],
    ),
    CharacterArchetype(
        id="char_002", name="沙雕谐星", personality="沙雕乐观、气氛担当",
        description="插科打诨的开心果，正经事常翻车，但总能靠乐观把场面盘活。",
        archetypes=["活宝队友", "损友", "整活大师"],
        examples=["热血漫里扛起笑点的活宝队友", "日常番里的损友"],
        catchphrases=["包在我身上！（然后翻车）", "这事儿我熟，虽然上次也这么说。"],
        tags=["沙雕", "搞笑"], fit_genres=["都市", "日常", "情感"],
    ),
    CharacterArchetype(
        id="char_003", name="温柔治愈系", personality="细腻体贴、温柔坚定",
        description="情绪稳定的暖流，善解人意，是角色们的避风港；软而不懦，关键处有骨气。",
        archetypes=["治愈系女主", "知心大姐姐", "温和前辈"],
        examples=["治愈系故事的女主", "食堂老板娘般暖心长辈"],
        catchphrases=["没关系的，慢慢来。", "先喝碗汤吧。"],
        tags=["温柔", "治愈"], fit_genres=["情感", "日常", "都市"],
    ),
    CharacterArchetype(
        id="char_004", name="热血莽夫", personality="热血冲动、直来直去",
        description="行动派，遇事先上后想，重义气护短；成长弧线往往是学会克制。",
        archetypes=["热血少年", "铁头小师弟", "热血班长"],
        examples=["热血少年漫主角", "宗门里一言不合就开打的师弟"],
        catchphrases=["打就完了！", "我先上，你们跟上！"],
        tags=["热血", "莽撞"], fit_genres=["玄幻", "战斗", "都市"],
    ),
    CharacterArchetype(
        id="char_005", name="腹黑军师", personality="谋定后动、腹黑从容",
        description="习惯藏一手，说话留三分，步步为营；往往掌控全局却不显山露水。",
        archetypes=["幕后大管家", "棋盘操盘手", "笑眯眯的谋士"],
        examples=["笑里藏刀的幕后主事人", "把所有人当棋子的军师"],
        catchphrases=["有意思。", "都在计划之内。"],
        tags=["腹黑", "谋略"], fit_genres=["玄幻", "悬疑", "权谋"],
    ),
    CharacterArchetype(
        id="char_006", name="傲娇大小姐", personality="傲娇别扭、口是心非",
        description="嘴上嫌弃得不行，行动上却处处照顾；嘴上说不，身体很诚实。",
        archetypes=["贵族大小姐", "宗族嫡女", "天才师妹"],
        examples=["贵族学院的大小姐", "嘴上不饶人的家族嫡女"],
        catchphrases=["才……才不是特意等你！", "哼，随便你。"],
        tags=["傲娇", "大小姐"], fit_genres=["都市", "情感", "玄幻"],
    ),
    CharacterArchetype(
        id="char_007", name="忠犬伙伴", personality="忠诚可靠、行动派",
        description="认准一个人就死心塌地，执行力强，是主角最靠得住的左膀右臂。",
        archetypes=["老部下", "忠犬队友", "得力助手"],
        examples=["追随多年的老部下", "捡来的忠心耿耿的队友"],
        catchphrases=["您说了算。", "我永远站在您这边。"],
        tags=["忠诚", "伙伴"], fit_genres=["玄幻", "都市", "战斗"],
    ),
    CharacterArchetype(
        id="char_008", name="阴险反派", personality="笑里藏刀、城府深",
        description="表面和善，背后使绊；善于伪装与借刀杀人，是主角最难缠的对手。",
        archetypes=["伪善长老", "微笑幕后黑手", "笑面虎对手"],
        examples=["道貌岸然的伪善长老", "全程微笑的幕后黑手"],
        catchphrases=["年轻人，还是太嫩。", "你的底牌，我早就看穿了。"],
        tags=["反派", "阴险"], fit_genres=["悬疑", "玄幻", "权谋"],
    ),
    CharacterArchetype(
        id="char_009", name="市侩商人", personality="精明重利、见风使舵",
        description="算盘打得精，嘴上抹了蜜，却也有自己的底线与情分；常是情报与资源的中转站。",
        archetypes=["茶馆老板", "丹药铺掌柜", "情报贩子"],
        examples=["两头讨好的茶馆老板", "坐地起价的丹药铺掌柜"],
        catchphrases=["价格好商量，质量你放心。", "一文钱一分货。"],
        tags=["精明", "市侩"], fit_genres=["都市", "玄幻", "日常"],
    ),
    CharacterArchetype(
        id="char_010", name="吐槽役青梅", personality="吐槽补刀、毒舌亲近",
        description="从小一起长大的损友，专拆台补刀，一句话戳穿主角的伪装，关系铁到可以互怼。",
        archetypes=["青梅竹马", "损友邻居", "吐槽担当"],
        examples=["从小一起长大的青梅", "天天互相嫌弃的邻居"],
        catchphrases=["又来了又来了。", "你这猪脑子，我说过八百遍了。"],
        tags=["吐槽", "青梅"], fit_genres=["日常", "情感", "都市"],
    ),
]
