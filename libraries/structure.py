"""
情节弧库（Structure Library）
各类网文题材的故事骨架结构模板。

存储模型（2026-09 v3 平级独立）：
  每行 = 一个**平级独立弧模板**（ArcNode），无父子层级、无 parent_arc_id；
  每个弧自带完整内容：字数区间 / 描述(本弧情节怎么发展) / key_events /
  tags(题材) / source / created_at / enabled。原树中的「整段壳」与各层子弧在
  迁移/内置构造时都各自成为独立弧，tags/来源/收录 从原树根平铺到每个独立弧。
  兼容别名 StructureTemplate = ArcNode（旧引用/类型注解可继续用）。
  2026-09 起弧库**已删除「内涵/themes」字段**（不再在弧模板上承载母题）。
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from .base_library import JsonLibrary


# ─── 统一的平级弧节点 ───

@dataclass
class ArcNode:
    """一个平级独立情节弧模板。
    只表述字数（min_words/max_words 为该弧建议字数区间，与运行时字数轴一致，不含章数）。
    全库每个弧字段集完全相同；无父-子关联。"""
    id: str                        # 唯一 id（arc_xxx / scout_src_名）
    name: str
    description: str = ""          # 本弧情节怎么发展（可复用内容主体）
    min_words: int = 0
    max_words: int = 0
    key_events: list[str] = field(default_factory=list)
    foreshadow_opportunities: list[str] = field(default_factory=list)  # 埋坑机会
    tags: list[str] = field(default_factory=list)              # 题材标签（每弧可搜）
    opening_patterns: list[str] = field(default_factory=list)  # 开篇情节段模板引用
    climax_patterns: list[str] = field(default_factory=list)   # 高潮情节段模板引用
    source: str = ""               # 来源
    created_at: str = ""           # 收录时间
    enabled: bool = True           # 启用状态

    @property
    def total_words(self) -> int:
        """整段字数量（兼容旧模板字段读取；整段跨度弧 min=max=整段）。"""
        return self.max_words or self.min_words or 0

    def to_dict(self) -> dict:
        # 永远输出统一键集（无 parent）：全库字段结构一致
        return {
            "id": self.id, "name": self.name,
            "description": self.description,
            "min_words": self.min_words, "max_words": self.max_words,
            "key_events": self.key_events,
            "foreshadow_opportunities": self.foreshadow_opportunities,
            "tags": self.tags,
            "opening_patterns": self.opening_patterns,
            "climax_patterns": self.climax_patterns,
            "source": self.source,
            "created_at": self.created_at,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d) -> "ArcNode":
        if isinstance(d, str):
            return cls(id=d, name=d)
        mn, mx = _words_of(d)
        return cls(
            id=str(d.get("id") or d.get("name") or ""),
            name=str(d.get("name") or ""),
            description=str(d.get("description") or ""),
            min_words=mn, max_words=mx,
            key_events=list(d.get("key_events") or []),
            foreshadow_opportunities=list(d.get("foreshadow_opportunities") or []),
            tags=list(d.get("tags") or []),
            opening_patterns=list(d.get("opening_patterns") or []),
            climax_patterns=list(d.get("climax_patterns") or []),
            source=str(d.get("source") or ""),
            created_at=str(d.get("created_at") or ""),
            enabled=bool(d.get("enabled", True)),
        )


# 兼容别名：旧代码/类型注解（如 libraries/assembler.py）仍可 import StructureTemplate。
StructureTemplate = ArcNode


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _words_of(d: dict) -> tuple:
    """取一个节点 dict 的字数区间 (min,max)，兼容多代字段：
    优先 min_words/max_words；其次整弧 total_words/total_chapters（min=max=整段）；
    最次旧阶段 min_chapters/max_chapters（×3000）。"""
    if "min_words" in d or "max_words" in d:
        return _int(d.get("min_words")), _int(d.get("max_words"))
    whole = d.get("total_words") or d.get("total_chapters")
    if whole:
        v = _int(whole)
        return v, v
    mn, mx = d.get("min_chapters"), d.get("max_chapters")
    if mn or mx:
        return _int(mn) * 3000, max(_int(mn), _int(mx)) * 3000
    return 0, 0


# ─── 平级化 helper（内置构造 / 旧数据迁移） ───

def flatten_nested_tree(tree: dict, root_id: str = "", source: str = "", created_at: str = "") -> list[dict]:
    """把一棵旧嵌套弧树 dict（顶层用 stages，子层用 children，可多层）摊平成
    「带 parent_arc_id 的节点行 dict」列表（父级内 0 基 DFS 链 id）。仅供随后
    independentize_rows 转平级，或旧格式迁移用；最终落盘不含 parent。"""
    root_id = root_id or str(tree.get("id") or "")
    rows: list[dict] = []

    def node_row(node: dict, node_id: str, parent_id: str) -> dict:
        mn, mx = _words_of(node)
        return {
            "id": node_id,
            "name": str(node.get("name") or ""),
            "description": str(node.get("description") or ""),
            "min_words": mn, "max_words": mx,
            "key_events": list(node.get("key_events") or []),
            "foreshadow_opportunities": list(node.get("foreshadow_opportunities") or []),
            "themes": list(node.get("themes") or []),
            "parent_arc_id": parent_id,
            "tags": list(node.get("tags") or []),
            "opening_patterns": list(node.get("opening_patterns") or []),
            "climax_patterns": list(node.get("climax_patterns") or []),
            "source": str(node.get("source") or ""),
            "created_at": str(node.get("created_at") or ""),
            "enabled": bool(node.get("enabled", True)),
        }

    root = dict(tree)
    root["id"] = root_id
    root["parent_arc_id"] = ""
    root.setdefault("key_events", [])
    root.setdefault("themes", [])
    root.setdefault("foreshadow_opportunities", [])
    if source and not root.get("source"):
        root["source"] = source
    if created_at and not root.get("created_at"):
        root["created_at"] = created_at
    rows.append(node_row(root, root_id, ""))

    def walk(parent_row_id: str, children: list) -> None:
        for i, child in enumerate(children):
            cid = f"{parent_row_id}::{i}"
            rows.append(node_row(child, cid, parent_row_id))
            sub = child.get("children") or child.get("stages") or []
            if sub:
                walk(cid, sub)

    top = tree.get("stages") or tree.get("children") or []
    if top:
        walk(root_id, top)
    return rows


def independentize_rows(rows: list[dict]) -> list[dict]:
    """把「带 parent_arc_id 的扁平行」转成**平级独立弧行**：
    ① 每棵树的根 tags/source/created_at 平铺到每个后代（后代原本无 tags）→ 各自可搜；
    ② 每行去掉 parent_arc_id（及可能的 children/stages 残留）。
    返回可直接 ArcNode.from_dict 的统一行。"""
    rows = [dict(r) for r in rows if isinstance(r, dict)]
    if not rows:
        return rows
    id_map = {str(r.get("id")): r for r in rows if r.get("id")}

    def root_of(r: dict) -> dict:
        seen = set()
        pid = str(r.get("parent_arc_id") or "")
        while pid and pid in id_map and pid not in seen:
            seen.add(pid)
            r = id_map[pid]
            pid = str(r.get("parent_arc_id") or "")
        return r

    out = []
    for r in rows:
        root = root_of(r)
        nr = dict(r)
        for k in ("tags", "source", "created_at"):
            if not nr.get(k) and root is not r and root.get(k):
                v = root.get(k)
                nr[k] = list(v) if isinstance(v, list) else v
        nr.pop("parent_arc_id", None)
        nr.pop("children", None)
        nr.pop("stages", None)
        out.append(nr)
    return out


def normalize_structures(items) -> list:
    """入库/评审前的结构载荷归一：弧库 = 平级独立弧，**每个候选即一条独立弧 dict**，
    原样返回（不做任何聚树）。兼容：候选带旧嵌套 stages 时按其顶层节点数摊平为独立弧。"""
    items = list(items or [])
    out = []
    for it in items:
        if isinstance(it, dict) and ("stages" in it or "children" in it) and "parent_arc_id" not in it:
            # 旧嵌套树形态（整棵模板）→ 摊平成平级独立弧（含原根壳 + 各层子节点）
            out.extend(independentize_rows(flatten_nested_tree(it)))
        else:
            out.append(it)
    return out


def make_root_id(name, source: str = "fanqie") -> str:
    """生成 scout 入库弧 id：scout_{source}_{清洗(name)[:40]}。"""
    base = re.sub(r"[^0-9A-Za-z一-鿿\-]", "", str(name or ""))[:40]
    if not base:
        base = "arc"
    return f"scout_{source}_{base}"


# ─── 库管理器 ───

class StructureLibrary(JsonLibrary):
    """情节弧库管理器（进程内单例，JSONL 一行一个**平级独立弧**）。
    旧嵌套/带父子的数据在加载时自动迁移为平级。"""
    _instance = None
    _list_attr = "templates"     # 平级弧列表（每个 = 一条可独立挑选的弧模板）
    _key = "templates"
    _file_name = "structures.jsonl"

    @classmethod
    def _from_dict(cls, d: dict) -> "ArcNode":
        return ArcNode.from_dict(d)

    @classmethod
    def _builtin(cls) -> list:
        return BUILTIN_STRUCTURES

    def _load_jsonl(self):
        """优先读 .jsonl；同名旧单 JSON（.json）自动迁移；v1 嵌套树 / v2 带 parent 的
        数据在此统一迁移为平级独立弧行并落盘。"""
        from core.json_store import read_json, read_jsonl
        save = Path(str(self._save_path))
        legacy = save.with_suffix(".json")
        raw = None
        if not save.exists() and legacy.exists():
            data = read_json(legacy, {})
            raw = data.get(self._key, [])
        elif save.exists():
            raw = read_jsonl(save)
        if raw is None:
            out = list(self._builtin())
            setattr(self, self._list_attr, out)
            return

        migrated = False
        flat: list = []
        for d in raw:
            if isinstance(d, dict) and ("stages" in d or "children" in d) and "parent_arc_id" not in d:
                # v1：一整棵嵌套模板 → 摊平成带 parent 的行（稍后转平级）
                flat.extend(flatten_nested_tree(d))
                migrated = True
            else:
                flat.append(d)
        # v2：扁平行带 parent_arc_id → 平级化（tags 平铺 + 去 parent）
        if any(isinstance(d, dict) and d.get("parent_arc_id") for d in flat):
            flat = independentize_rows(flat)
            migrated = True
        elif any(isinstance(d, dict) and "parent_arc_id" in d for d in flat):
            # 带空 parent 键的旧行：只去掉键
            flat = independentize_rows(flat)
            migrated = True
        out = [ArcNode.from_dict(d) for d in flat if isinstance(d, dict)]
        setattr(self, self._list_attr, out)
        if migrated:
            self._save()

    # ── 查询（无层级：每个弧都是独立模板） ──
    def roots(self, include_disabled: bool = True) -> list:
        """全部弧（无层级 = 全库即候选模板清单）；默认含已禁用（与原全量语义一致）。"""
        return [t for t in self.templates if include_disabled or t.enabled]

    def get_by_id(self, node_id: str):
        for t in self.templates:
            if t.id == node_id:
                return t
        return None

    def get_node(self, node_id: str):
        return self.get_by_id(node_id)

    def children_of(self, parent_id: str) -> list:
        """无父子层级：恒空（兼容旧调用点）。"""
        return []

    def root_of(self, node_id: str):
        return self.get_by_id(node_id)

    def descendant_ids(self, node_id: str) -> set:
        return {node_id} if self.get_by_id(node_id) else set()

    def subtree_dicts(self, node_id: str) -> dict:
        node = self.get_by_id(node_id)
        return node.to_dict() if node else {}

    def display_trees(self, include_disabled: bool = True) -> list:
        """全弧浅拷列表（无 children；兼容旧展示调用点）。"""
        import copy
        return [copy.copy(t) for t in self.templates
                if include_disabled or t.enabled]

    def delete_tree(self, node_id: str) -> int:
        """删除单个弧（无层级，无连坐）。返回删除行数。"""
        ids = self.descendant_ids(node_id)
        before = len(self.templates)
        self.templates = [t for t in self.templates if t.id not in ids]
        self._save()
        return before - len(self.templates)

    def search(self, tags=None, word_count: int = 0) -> list:
        """按标签（任一命中）/整段字数筛选弧模板。tags 为列表或逗号/空格分隔字符串。"""
        results = self.roots(include_disabled=True)
        if isinstance(tags, str):
            tags = [x.strip() for x in tags.replace("，", " ").replace(",", " ").split() if x.strip()]
        if tags:
            tag_set = {str(t).strip() for t in tags if str(t).strip()}
            results = [t for t in results if tag_set.intersection(t.tags or [])]
            results.sort(key=lambda t: -len(tag_set.intersection(t.tags or [])))  # 命中多的排前
        if word_count:
            results.sort(key=lambda t: abs(t.total_words - word_count))
        return results


# ─── 内置情节弧（2026-09 精选库：平级独立弧，真实 min-max 区间，无整段壳单点）───
# ─── 内置情节弧（2026-09-06 重编：从零新编，平级独立弧，真实 min-max 区间）───

_CURATED_ARCS = [
    {"id": "arc_talent_fall_reverse", "name": "天才坠落·试炼翻身",
     "description": "公认的天才一朝沦为废体，从被同门踩进泥里到在入门试炼中反杀登顶的弧：当众跌落遭奚落→藏住残存底牌暗中蓄力→试炼场越级反杀全场→获长老青眼却引动更深的夺因。",
     "min_words": 16000, "max_words": 38000,
     "key_events": ["境界跌落/当众奚落", "藏底牌蛰伏", "试炼越级反杀", "扬名与真相苗头"],
     "foreshadow_opportunities": ["天赋被废并非天灾，而是被人为抽走的引子"],
     "tags": ["玄幻", "宗门", "逆袭", "爽文"]},
    {"id": "arc_pill_rise_fame", "name": "丹道废柴·一味成名",
     "description": "炼丹废柴被逐出丹房，靠旁门偏方一炉成名、被全城争抢的弧：被逐出师门丹房→路遇绝症当街试手一鸣惊人→大宗门招揽与下毒暗算同至→以一味奇丹扬名立万。",
     "min_words": 13000, "max_words": 30000,
     "key_events": ["被逐丹房", "当街救人一丹成名", "招揽与暗算同至", "丹成扬名"],
     "foreshadow_opportunities": ["那味丹方出自一本被烧掉半卷的残谱"],
     "tags": ["玄幻", "丹道", "逆袭", "轻松"]},
    {"id": "arc_sect_contest_champion", "name": "山门大比·力压群雄",
     "description": "一场宗门大比从分组被刻意刁难到横扫对手夺魁的弧：报名分组遭遇排挤→种子选手轮番挑衅→一路碾压晋级决赛→力压群雄夺魁并看清有人在操纵赛制。",
     "min_words": 18000, "max_words": 42000,
     "key_events": ["报名分组遭排挤", "种子选手挑衅", "连胜晋级", "夺魁与赛制黑幕"],
     "foreshadow_opportunities": ["主持大比的长老与对手家有旧"],
     "tags": ["玄幻", "大比", "扬名", "爽文"]},
    {"id": "arc_son_in_law_turnover", "name": "赘婿临门·当众翻盘",
     "description": "被全族看轻的赘婿在满堂宾客前被逼出手，隐藏身份顺势揭开、让前倨后恭的弧：寿宴当众受辱被逼退婚→危局中被迫出手解围→真实身份在众人眼前掀开→看人下菜的亲戚仓皇改口与更大靠山现身。",
     "min_words": 14000, "max_words": 34000,
     "key_events": ["寿宴受辱逼退婚", "被迫出手解围", "隐藏身份曝光", "态度反转与幕后浮现"],
     "foreshadow_opportunities": ["当年替他挡下灾祸的老人并不简单"],
     "tags": ["都市", "赘婿", "逆袭", "爽文"]},
    {"id": "arc_doctor_hidden_rise", "name": "都市神医·低调惊人",
     "description": "隐居街巷的年轻神医卷入豪门恩怨、在众目睽睽下以一手医术镇住全场、被迫从低调走到台前的弧：街巷坐诊被人嘲讽→豪门千金绝症求医先声夺人→当众拆穿庸医骗局→名动全城却惹来旧敌。",
     "min_words": 13000, "max_words": 30000,
     "key_events": ["街头坐诊被看轻", "疑难重症一展身手", "当面拆穿骗局", "声名鹊起树敌"],
     "foreshadow_opportunities": ["他避世行医，是为了躲一桩旧案"],
     "tags": ["都市", "神医", "打脸", "爽文"]},
    {"id": "arc_biz_comeback", "name": "商海浮沉·绝地反攻",
     "description": "被合伙人釜底抽薪、几近破产的创业者从废墟里反手做局、夺回一切的弧：核心团队被挖空濒临清盘→靠一纸旧合同布下暗棋→对手趁胜追击反被套牢→公开清算拿回公司。",
     "min_words": 18000, "max_words": 42000,
     "key_events": ["团队被挖濒临破产", "旧合同暗棋", "诱敌深入", "反杀夺回"],
     "foreshadow_opportunities": ["当年低价卖出的那家公司才是真正的后手"],
     "tags": ["都市", "商战", "权谋", "逆袭"]},
    {"id": "arc_mystery_chain", "name": "连环谜局·抽丝剥茧",
     "description": "数起表面互不相干的命案被逐一串成一张网、最终指向埋藏多年的旧案主使的弧：首起命案现场出现说不通的细节→第二起命案推翻此前的推论→顺着共同线索摸到旧案→当众设局逼出真凶。",
     "min_words": 22000, "max_words": 50000,
     "key_events": ["首案说不通的细节", "次案推翻前论", "顺藤摸到旧案", "设局逼出真凶"],
     "foreshadow_opportunities": ["死者之间只存在一个被刻意抹去的共同点"],
     "tags": ["悬疑", "推理", "连环", "反转"]},
    {"id": "arc_haunted_human_scheme", "name": "灵宅夜访·人祸作祟",
     "description": "受委托调查一座闹鬼老宅、发现所谓冤魂其实是一场精心活人作局、真相翻盘的弧：夜探鬼宅异象频发→线索反而指向宅中旧仆→第二夜守株待兔撞破机关与伪装→揭穿活人扮鬼与夺产阴谋。",
     "min_words": 18000, "max_words": 40000,
     "key_events": ["夜探鬼宅异象", "旧仆疑点", "守夜撞破机关", "活人扮鬼真相"],
     "foreshadow_opportunities": ["老宅地窖里那具无名尸骨的身份才是钥匙"],
     "tags": ["悬疑", "灵异", "探案", "反转"]},
    {"id": "arc_frame_reverse", "name": "替罪之局·真凶另有其人",
     "description": "被推出来顶罪的普通人从认命到反查、发现整局是一场更大阴谋引子的弧：命案发生后被指认顶罪→关键证人一句证词露出破绽→暗中翻查发现真凶有备而来→反将一军把局掀回幕后之人身上。",
     "min_words": 16000, "max_words": 36000,
     "key_events": ["被按头顶罪", "证人证词破绽", "反查真凶", "掀翻幕后"],
     "foreshadow_opportunities": ["安排他顶罪的人反而最不希望他死"],
     "tags": ["悬疑", "反转", "替身", "涉案"]},
    {"id": "arc_apocalypse_holdout", "name": "末世囤积·据守求生",
     "description": "灾变将至时抢先囤货占地、把一处据点守成活人区的弧：预知灾变低价囤货→抢先占住易守难攻的据点并拢人→首波灾变如期而至众人倚仗物资撑过→外部幸存者与内部异心同时考验。",
     "min_words": 15000, "max_words": 34000,
     "key_events": ["预知灾变抢囤", "占点拢人", "首波灾变硬扛", "内忧外患求生"],
     "foreshadow_opportunities": ["混进据点的幸存者里有人知道他的秘密"],
     "tags": ["科幻", "末世", "求生", "囤货"]},
    {"id": "arc_relic_scramble", "name": "星海遗迹·夺宝突围",
     "description": "一座刚开启的远古遗迹引来多方势力，主角在结盟与背叛里抢到关键遗物并全身而退的弧：遗迹开启各方入场→组队探路即遭背叛→核心舱室夺宝混战→挟遗物突围却被更高层盯上。",
     "min_words": 20000, "max_words": 46000,
     "key_events": ["遗迹开启群雄入场", "结盟即背叛", "核心夺宝混战", "突围与被盯上"],
     "foreshadow_opportunities": ["这件遗物是某个已灭绝文明留下的警告"],
     "tags": ["科幻", "星际", "冒险", "爽文"]},
    {"id": "arc_fake_lovers_real", "name": "欢喜冤家·假戏真做",
     "description": "为应付家中安排而假扮情侣的两人从针锋相对到默契升温、最后假戏真做的弧：被迫组队应付双方家长→日常互怼里慢慢对齐生活习惯→一场共同难关让两人看清心意→在亲友起哄里坦白在一起。",
     "min_words": 10000, "max_words": 26000,
     "key_events": ["被迫假扮", "互怼磨合出默契", "共同难关动真心", "坦白定情"],
     "foreshadow_opportunities": ["她当初答应假扮其实另有一桩不能说的盘算"],
     "tags": ["言情", "甜文", "欢喜冤家", "日常"]},
    {"id": "arc_reconcile_truth", "name": "错位真心·破镜重圆",
     "description": "因一场误会分道扬镳的恋人，在真相慢慢浮出水面后从旧伤里重新走到一起的弧：误会被坐实决绝分开→各自辗转却屡屡被共同旧事拉回→当年真相一点一点揭开→旧伤结痂、破镜重圆。",
     "min_words": 16000, "max_words": 38000,
     "key_events": ["误会坐实分手", "各自辗转", "真相揭开", "破镜重圆"],
     "foreshadow_opportunities": ["当年那封没有送到的信其实被另一个人截下了"],
     "tags": ["言情", "误会", "虐恋", "和解"]},
    {"id": "arc_rules_deathgame", "name": "规则副本·极限求生",
     "description": "一群玩家被投入规则怪谈副本，从各自试探、互相设局到合力解出隐藏规则通关的弧：入场规则浮现众人各怀心思→试探与内斗中先折几人→顺着死亡倒推隐藏规则→卡在临界通关并惊动更高存在。",
     "min_words": 22000, "max_words": 52000,
     "key_events": ["副本规则浮现", "试探与内斗", "死亡倒推规则", "极限通关"],
     "foreshadow_opportunities": ["通关者里混着一个上一轮就死过的人"],
     "tags": ["无限流", "副本", "规则", "求生"]},
    {"id": "arc_loop_escape", "name": "无限轮回·破局而出",
     "description": "被困在一天轮回里的主角从按部就班求生到识破循环核心、最终撬动根源逃出生天的弧：第一次循环在混乱中结束→察觉时间重置后开始记录线索→发现循环并非惩罚而是封印→在终点与看守者摊牌破局。",
     "min_words": 20000, "max_words": 48000,
     "key_events": ["初醒循环", "记录线索找规律", "识破循环本质", "摊牌破局"],
     "foreshadow_opportunities": ["每天准时在街角卖糖葫芦的老头从不进入循环"],
     "tags": ["无限流", "轮回", "揭秘", "悬疑"]},
    {"id": "arc_court_dark_undercurrent", "name": "朝堂暗涌·步步为营",
     "description": "初入朝堂的寒门新贵在党争漩涡里从被人当刀使到站稳脚跟、反手布下一盘大棋的弧：初入朝被当作棋子→几番献言立功却触了谁的逆鳞→旧党清算时亮出深埋的底牌→扳倒首恶却也让皇权投来审视目光。",
     "min_words": 24000, "max_words": 56000,
     "key_events": ["入朝为棋子", "献言立功招忌", "清算中亮底牌", "扳倒首恶引皇忌"],
     "foreshadow_opportunities": ["提拔他的那位贵人才是朝局真正的执棋人"],
     "tags": ["历史", "权谋", "朝堂", "成长"]},
]

BUILTIN_STRUCTURES = [ArcNode.from_dict(x) for x in _CURATED_ARCS]

