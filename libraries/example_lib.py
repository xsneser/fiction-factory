"""
原文摘录库（Example / Excerpt Library）—— 「肉」库

与四大库互补：四大库是「骨」（桥段/大纲/笑点/内涵的抽象模式），
本库是「肉」（真实网文原文摘录），解决「句子像不像人写的」——语感/节奏/画面。

来源：
  · 内置种子 = 演示范本（source 标「示例·演示」，非真实抓取，仅演示注入效果）
  · 真实摘录 = 番茄侦察兵 extract_excerpts（从下载书样本切 2-3 句真实文本，标 source=书名）

合规：每条摘录限 2-3 句（≤100 字），个人学习用途，不传播不转载；
      与 README 合规声明口径一致（分析以模式/结构/桥段等抽象技巧为主，避免全文存储）。

字段：
  id / type（开头钩子|主角亮相|高张力对白|打脸爽点|章末钩子|结尾余韵|日常对话）
  / tag（标签，如「当众羞辱」「危机压身」）/ category（适配桥段分类）
  / text（原文摘录）/ source / usage_count / banned_in / enabled / created_at
"""
from dataclasses import dataclass, field
from .base_library import JsonLibrary


@dataclass
class ExampleExcerpt:
    """一条原文摘录范本"""
    id: str
    type: str                 # 开头钩子/主角亮相/高张力对白/打脸爽点/章末钩子/结尾余韵/日常对话
    tag: str                  # 标签：当众羞辱/扮猪吃虎/系统激活/危机压身/...
    text: str                 # 原文摘录 2-3 句（≤100 字）
    category: str = ""        # 适配桥段分类（爽文/开篇/战斗/...）；空=通用
    source: str = ""          # 来源（书名；内置种子=示例·演示）
    usage_count: int = 0
    banned_in: list = field(default_factory=list)
    enabled: bool = True
    created_at: str = "2026-08-05"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "type": self.type, "tag": self.tag,
            "text": self.text, "category": self.category,
            "source": self.source, "usage_count": self.usage_count,
            "banned_in": self.banned_in, "enabled": self.enabled,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExampleExcerpt":
        return ExampleExcerpt(**{k: v for k, v in d.items()
                                 if k in ExampleExcerpt.__dataclass_fields__})


class ExampleLibrary(JsonLibrary):
    """原文摘录库管理器（进程内单例）"""
    _instance = None
    _list_attr = "excerpts"
    _key = "excerpts"
    _file_name = "excerpts.json"

    @classmethod
    def _from_dict(cls, d: dict) -> "ExampleExcerpt":
        return ExampleExcerpt.from_dict(d)

    @classmethod
    def _builtin(cls) -> list:
        return BUILTIN_EXCERPTS

    def search(self, type_: str = "", category: str = "", tag: str = "",
               book_id: str = "") -> list:
        """按类型/分类/标签预筛，排除 banned，按 usage_count 升序（少用优先）。"""
        results = self.excerpts
        if type_:
            results = [e for e in results if e.type == type_]
        if category:
            results = [e for e in results
                       if e.category in (category, "")]      # 空 category = 通用
        if tag:
            results = [e for e in results if tag in e.tag]
        if book_id:
            results = [e for e in results if book_id not in e.banned_in]
        results = [e for e in results if e.enabled]
        results.sort(key=lambda e: e.usage_count)
        return results

    def mark_used(self, excerpt_id: str) -> None:
        """计数使用（少用优先轮换，避免反复注入同一范本）。"""
        for e in self.excerpts:
            if e.id == excerpt_id:
                e.usage_count += 1
                break
        try:
            self._save()
        except Exception:
            pass


# ─── 内置种子摘录（演示范本，演示注入效果；真实摘录由侦察兵抽取）───

BUILTIN_EXCERPTS = [
    # ── 开头钩子 ──
    ExampleExcerpt(
        id="ex_001", type="开头钩子", tag="当众羞辱", category="开篇",
        text="宾客满座的订婚宴，忽然静了。穿着婚纱的女孩把戒指盒拍在桌上，当着三百人的面，一字一句道：\"退婚。\"",
        source="示例·演示"),
    ExampleExcerpt(
        id="ex_002", type="开头钩子", tag="危机压身", category="开篇",
        text="萧晨醒来的时候，手边是一张死亡诊断书。日期写着昨天，名字是他自己。",
        source="示例·演示"),
    ExampleExcerpt(
        id="ex_003", type="开头钩子", tag="系统激活", category="开篇",
        text="\"叮——宿主绑定失败。\"机械音停顿了一秒，\"检测到您自带系统，本系统自愿降级为备胎。\"",
        source="示例·演示"),
    # ── 主角亮相 ──
    ExampleExcerpt(
        id="ex_004", type="主角亮相", tag="扮猪吃虎", category="开篇",
        text="所有人都以为沈七是个只会记账的废物账房。只有赌坊的掌柜知道，这小子算盘一响，连阎王的命都算得出来。",
        source="示例·演示"),
    ExampleExcerpt(
        id="ex_005", type="主角亮相", tag="身世反转", category="开篇",
        text="他低眉顺眼地给小姐端茶三年，没人正眼看过他。直到那天，全城的阵法师跪了一地，朝他喊老祖。",
        source="示例·演示"),
    # ── 高张力对白 ──
    ExampleExcerpt(
        id="ex_006", type="高张力对白", tag="关系破裂", category="情感",
        text="\"这三年，你拿我当什么？\"她问。\"账房先生。\"他答得干脆，\"你爹付我工钱。\"",
        source="示例·演示"),
    ExampleExcerpt(
        id="ex_007", type="高张力对白", tag="对峙", category="悬疑",
        text="\"你不敢杀我。\"黑衣人盯着他，\"你杀了我，就永远不知道你娘在哪。\"萧晨的刀停在半寸外。",
        source="示例·演示"),
    ExampleExcerpt(
        id="ex_008", type="高张力对白", tag="摊牌", category="都市",
        text="\"合同在这，签字就能走。\"林总把笔推过来，\"但你要想清楚——出了这个门，整个行业没人敢用你。\"",
        source="示例·演示"),
    # ── 打脸爽点 ──
    ExampleExcerpt(
        id="ex_009", type="打脸爽点", tag="实力揭晓", category="爽文",
        text="家主张口结舌，指着他半天说不出话。\"你……你什么时候……\"萧晨抖了抖袖口的灰：\"刚才。\"",
        source="示例·演示"),
    ExampleExcerpt(
        id="ex_010", type="打脸爽点", tag="众人震惊", category="爽文",
        text="长老们一个个从座位上弹起来。\"这是失传百年的《断碑诀》！\"人群哗然，方才还嘲笑他的弟子们，脸色白得像纸。",
        source="示例·演示"),
    # ── 章末钩子 ──
    ExampleExcerpt(
        id="ex_011", type="章末钩子", tag="身份暴露", category="悬疑",
        text="他刚转身，就听身后传来一个苍老的声音：\"小友且慢——\"萧晨脚步一滞。那声音，他上辈子死前听过。",
        source="示例·演示"),
    ExampleExcerpt(
        id="ex_012", type="章末钩子", tag="危机逼近", category="冲突",
        text="门被推开了。来的人穿着一身白衣，手里拿着一副画像。画像上的人，正是萧晨。",
        source="示例·演示"),
    # ── 结尾余韵 ──
    ExampleExcerpt(
        id="ex_013", type="结尾余韵", tag="遗憾", category="情感",
        text="她把玉佩留在了门槛上。萧晨捡起来，攥了整整一夜。天亮时，他把它收进了最贴身的口袋。",
        source="示例·演示"),
    ExampleExcerpt(
        id="ex_014", type="结尾余韵", tag="真相", category="悬疑",
        text="雨停了。他站在那间烧毁的院子里，忽然明白——三年前那场大火，从来就不是意外。",
        source="示例·演示"),
    # ── 日常对话 ──
    ExampleExcerpt(
        id="ex_015", type="日常对话", tag="吐槽", category="日常",
        text="\"师兄，你说我什么时候能像你一样强？\"\"等你哪天不喊我师兄的时候。\"",
        source="示例·演示"),
    ExampleExcerpt(
        id="ex_016", type="日常对话", tag="师徒", category="日常",
        text="师父掂了掂他的剑，\"就这？\"\"就这。\"少年答得理直气壮，\"够用了。\"",
        source="示例·演示"),
]
