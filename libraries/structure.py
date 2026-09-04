"""
情节弧库（Structure Library）
各类网文题材的故事骨架结构模板。

存储模型（2026-09 扁平化）：
  每行 = 一个弧节点（ArcNode），任意深度平铺；父子关系用 parent_arc_id 指针表达，
  子弧永不内嵌在父行里。所有弧（根/子/孙…）字段集完全一致、渲染一致。
  根弧 = parent_arc_id=="" 且把模板元数据（tags/opening_patterns/climax_patterns/
  source/created_at/enabled）作为普通字段填值的节点，不是另一类型。
  兼容别名 StructureTemplate = ArcNode（旧引用/类型注解可继续用）。
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from .base_library import JsonLibrary


# ─── 统一的弧节点 ───

@dataclass
class ArcNode:
    """一个情节弧节点（根弧/子弧统一结构）。
    只表述字数（min_words/max_words 为该节点建议字数区间，与运行时字数轴一致，不含章数）。
    root 与任意深度子节点的字段集完全相同；差异仅体现在 parent_arc_id 与取值。"""
    id: str                        # 根: 模板 id(arc_xxx / scout_src_名)；子: "{父id}::{序}"(父级内 0 基 DFS 链)
    name: str
    description: str = ""
    min_words: int = 0
    max_words: int = 0
    key_events: list[str] = field(default_factory=list)
    foreshadow_opportunities: list[str] = field(default_factory=list)  # 埋坑机会
    themes: list = field(default_factory=list)   # 节点级内涵 [{name, position, how}]，含插入位置
    parent_arc_id: str = ""        # 父节点 id；"" = 根弧（一棵模板树的根）
    tags: list[str] = field(default_factory=list)              # 根弧填题材标签、子弧留空（键一致）
    opening_patterns: list[str] = field(default_factory=list)  # 开篇桥段模板引用（根弧元数据）
    climax_patterns: list[str] = field(default_factory=list)   # 高潮桥段模板引用（根弧元数据）
    source: str = ""               # 来源
    created_at: str = ""           # 收录时间
    enabled: bool = True           # 启用状态

    @property
    def total_words(self) -> int:
        """整段字数量（兼容旧模板字段读取；根弧 min=max=整弧跨度）。"""
        return self.max_words or self.min_words or 0

    @property
    def is_root(self) -> bool:
        return self.parent_arc_id == ""

    def to_dict(self) -> dict:
        # 永远输出统一键集：任意深度字段结构一致
        return {
            "id": self.id, "name": self.name,
            "description": self.description,
            "min_words": self.min_words, "max_words": self.max_words,
            "key_events": self.key_events,
            "foreshadow_opportunities": self.foreshadow_opportunities,
            "themes": self.themes,
            "parent_arc_id": self.parent_arc_id,
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
            themes=list(d.get("themes") or []),
            parent_arc_id=str(d.get("parent_arc_id") or ""),
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


# ─── 扁平化 helper（迁移/入库/评审/judge 共用） ───

def flatten_nested_tree(tree: dict, root_id: str = "", source: str = "", created_at: str = "") -> list[dict]:
    """把一棵嵌套弧树 dict（顶层用 stages，子层用 children，均可多层）摊平成
    「统一键的扁平节点行 dict」列表；子行 id 规范为 '{父id}::{序}'（父级内 0 基，
    DFS 前序）。返回的行可直接 ArcNode.from_dict / 原样落盘。"""
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


def partition_rows_by_root(rows: list[dict]) -> list[tuple]:
    """把扁平行列表聚成 [(根行, [后代行...])]。
    根 = parent_arc_id 为空，或其父 id 不在批内（健壮兜底）。保文件/数组顺序。"""
    rows = [r for r in (rows or []) if isinstance(r, dict) and r]
    id_set = {str(r.get("id")) for r in rows if r.get("id")}
    children_map: dict = {}
    for r in rows:
        children_map.setdefault(str(r.get("parent_arc_id") or ""), []).append(r)

    roots = []
    seen = set()
    for r in rows:
        pid = str(r.get("parent_arc_id") or "")
        if (not pid) or (pid not in id_set):
            if id(r) not in seen:
                seen.add(id(r))
                roots.append(r)

    def dfs(node, acc):
        acc.append(node)
        for c in children_map.get(str(node.get("id") or ""), []):
            dfs(c, acc)

    out = []
    for root in roots:
        acc = []
        dfs(root, acc)
        out.append((root, acc[1:]))
    return out


def rows_to_tree_dicts(rows: list[dict]) -> list[dict]:
    """扁平行列表 → 每棵根弧一个「临时嵌套树 dict」（含 stages 递归）。
    仅作 judge/评审/展示用，从不落盘。"""
    if not rows:
        return []
    groups = partition_rows_by_root(rows)
    if not groups:  # 全部孤立（无 id 等），退化为每行一棵
        return [dict(r) for r in rows if isinstance(r, dict)]

    id_map = {}
    children_map: dict = {}
    for r in rows:
        id_map[str(r.get("id"))] = r
        children_map.setdefault(str(r.get("parent_arc_id") or ""), []).append(r)

    def nest(node) -> dict:
        d = dict(node)
        kids = children_map.get(str(node.get("id") or ""), [])
        if kids:
            d["stages"] = [nest(k) for k in kids]
        return d

    return [nest(root) for root, _ in groups]


def normalize_structures(items) -> list[dict]:
    """入库/评审前的结构载荷归一：扁平行列表 → 每棵根弧一棵临时嵌套树 dict。
    输入已是树形态（含 stages）则原样返回。
    判定：任一项含 parent_arc_id 键（含空值）或缺少 stages → 视为扁平行。"""
    items = list(items or [])
    if not items:
        return []
    flat = any(isinstance(x, dict) and ("parent_arc_id" in x or "stages" not in x) for x in items)
    if flat:
        return rows_to_tree_dicts(items)
    return items


def make_root_id(name, source: str = "fanqie") -> str:
    """生成 scout 入库根弧 id：scout_{source}_{清洗(name)[:40]}。"""
    base = re.sub(r"[^0-9A-Za-z一-鿿\-]", "", str(name or ""))[:40]
    if not base:
        base = "arc"
    return f"scout_{source}_{base}"


# ─── 库管理器 ───

class StructureLibrary(JsonLibrary):
    """情节弧库管理器（进程内单例，JSONL 一行一弧节点）。
    旧嵌套模板在加载时自动迁移成扁平行。"""
    _instance = None
    _list_attr = "templates"     # 扁平列表：含全部节点（根 + 任意深度子节点）
    _key = "templates"
    _file_name = "structures.jsonl"

    @classmethod
    def _from_dict(cls, d: dict) -> "ArcNode":
        return ArcNode.from_dict(d)

    @classmethod
    def _builtin(cls) -> list:
        return BUILTIN_STRUCTURES

    @classmethod
    def _from_payload(cls, dicts: list) -> list:
        """把原始载荷（可能含旧嵌套模板）统一转成扁平行节点列表。"""
        out = []
        for d in dicts:
            if isinstance(d, dict) and "stages" in d and "parent_arc_id" not in d:
                out.extend(ArcNode.from_dict(r) for r in flatten_nested_tree(d))
            else:
                out.append(ArcNode.from_dict(d))
        return out

    def _load_jsonl(self):
        """优先读 .jsonl；同名旧单 JSON（.json）存在则自动迁移；jsonl 内旧嵌套行也自动展平。"""
        legacy = Path(str(self._save_path)).with_suffix(".json")
        if not self._save_path.exists() and legacy.exists():
            from core.json_store import read_json
            data = read_json(legacy, {})
            items = self._from_payload(data.get(self._key, []))
            setattr(self, self._list_attr, items)
            self._save()
            return
        migrated = False
        if self._save_path.exists():
            from core.json_store import read_jsonl
            out = []
            for d in read_jsonl(self._save_path):
                if isinstance(d, dict) and "stages" in d and "parent_arc_id" not in d:
                    out.extend(ArcNode.from_dict(r) for r in flatten_nested_tree(d))
                    migrated = True
                else:
                    out.append(ArcNode.from_dict(d))
        else:
            out = list(self._builtin())
        setattr(self, self._list_attr, out)
        if migrated:
            self._save()

    # ── 查询（扁平世界的新/旧接口） ──
    def roots(self, include_disabled: bool = True) -> list:
        """全部根弧（一棵模板树一个根），文件顺序。默认含已禁用（与原 .templates 全量语义一致）。"""
        return [t for t in self.templates if t.parent_arc_id == ""
                and (include_disabled or t.enabled)]

    def get_by_id(self, node_id: str):
        """任意节点（根或子弧）按 id 取；找不到返回 None。"""
        for t in self.templates:
            if t.id == node_id:
                return t
        return None

    def get_node(self, node_id: str):
        """get_by_id 的显式别名。"""
        return self.get_by_id(node_id)

    def children_of(self, parent_id: str) -> list:
        """某节点的直接子弧（文件顺序）；父 id 不存在返回空。"""
        return [t for t in self.templates if t.parent_arc_id == parent_id]

    def root_of(self, node_id: str):
        """沿 parent_arc_id 上溯到根弧；找不到返回 None。"""
        cur = self.get_by_id(node_id)
        seen = set()
        while cur and cur.parent_arc_id and cur.parent_arc_id != cur.id:
            if cur.parent_arc_id in seen:
                return None
            seen.add(cur.parent_arc_id)
            cur = self.get_by_id(cur.parent_arc_id)
        return cur

    def descendant_ids(self, node_id: str) -> set:
        """某节点的全部后代 id（含自身），BFS。"""
        out = {node_id}
        frontier = list(self.children_of(node_id))
        while frontier:
            nxt = []
            for c in frontier:
                if c.id in out:
                    continue
                out.add(c.id)
                nxt.extend(self.children_of(c.id))
            frontier = nxt
        return out

    def subtree_dicts(self, node_id: str) -> dict:
        """把某节点及其后代组装成嵌套展示 dict：{**node.to_dict(), "children":[...]}。"""
        node = self.get_by_id(node_id)
        if not node:
            return {}

        def nest(n) -> dict:
            d = n.to_dict()
            kids = self.children_of(n.id)
            if kids:
                d["children"] = [nest(k) for k in kids]
            return d

        return nest(node)

    def display_trees(self, include_disabled: bool = True) -> list:
        """给模板页用的根树克隆：根节点临时挂 .children（仅浅拷贝副本，
        非 dataclass 字段，绝不进 to_dict/落盘）。"""
        import copy
        trees = []
        for root in self.roots(include_disabled=include_disabled):
            root_c = copy.copy(root)
            root_c.children = [self._node_clone(c) for c in self.children_of(root.id)]
            trees.append(root_c)
        return trees

    def _node_clone(self, node) -> object:
        import copy
        c = copy.copy(node)
        kids = self.children_of(node.id)
        c.children = [self._node_clone(k) for k in kids]
        return c

    def delete_tree(self, node_id: str) -> int:
        """删除某节点及其全部后代，返回删除行数。"""
        ids = self.descendant_ids(node_id)
        before = len(self.templates)
        self.templates = [t for t in self.templates if t.id not in ids]
        self._save()
        return before - len(self.templates)

    def search(self, tags=None, word_count: int = 0) -> list:
        """按标签（任一命中）/整段字数筛选**根弧**模板。tags 为列表或逗号/空格分隔字符串。"""
        results = self.roots(include_disabled=True)
        if isinstance(tags, str):
            tags = [x.strip() for x in tags.replace("，", " ").replace(",", " ").split() if x.strip()]
        if tags:
            tag_set = {str(t).strip() for t in tags if str(t).strip()}
            results = [t for t in results if tag_set.intersection(t.tags or [])]
            results.sort(key=lambda t: -len(tag_set.intersection(t.tags or [])))  # 命中多的排前
        if word_count:
            # 找整段字数最接近的根弧模板
            results.sort(key=lambda t: abs(t.total_words - word_count))
        return results


# ─── 内置情节弧结构模板（代码里以嵌套字面量书写，加载即扁平化成节点行） ───

_BUILTIN_SPECS = [
    {
        "id": "arc_chuanyue_01", "name": "穿越重生·先发优势弧",
        "description": "重生/穿越后利用先知先觉抢占先机的一段弧：确认处境→布局→第一次碾压→局势反转",
        "total_words": 36000,
        "stages": [
            {"name": "确认处境", "description": "穿越/重生、弄清身份与时间点、盘算先发优势",
             "min_words": 3000, "max_words": 6000,
             "key_events": ["高能开局（穿越/重生）", "弄清身份处境", "盘点先知信息"],
             "foreshadow_opportunities": ["穿越/重生的原因存疑"]},
            {"name": "先发布局", "description": "抢在未来关键节点前埋下棋子、避开前世雷区",
             "min_words": 9000, "max_words": 15000,
             "key_events": ["提前获取关键资源", "拉拢关键人物", "避开前世踩过的坑"],
             "foreshadow_opportunities": ["蝴蝶效应引发的新变量"],
             "children": [
                 {"name": "提前埋子", "description": "在关键节点前布下棋子", "min_words": 3000, "max_words": 6000,
                  "key_events": ["占住资源位", "提前示好关键人"]},
                 {"name": "拉拢关键人物", "description": "收编前世可用的盟友", "min_words": 3000, "max_words": 6000,
                  "key_events": ["救下前世恩人", "结盟军需官"]},
                 {"name": "避开雷区", "description": "绕开前世踩过的坑", "min_words": 3000, "max_words": 3000,
                  "key_events": ["识破前世陷阱", "改变致命选择"]},
             ]},
            {"name": "第一次碾压", "description": "用先发优势正面碾压第一个前世仇人/竞争者",
             "min_words": 6000, "max_words": 12000,
             "key_events": ["打脸第一个敌人", "身份地位突变", "被多方关注"],
             "foreshadow_opportunities": ["更高层对手投来的目光"]},
            {"name": "局势反转", "description": "顺风局的暗涌：新对手出手、旧雷区爆炸",
             "min_words": 6000, "max_words": 9000,
             "key_events": ["新对手试探", "此前布局被反将一军", "亮出更深底牌"],
             "foreshadow_opportunities": ["幕后黑手的阴影"],
             "themes": [{"name": "复仇（Revenge）", "position": "结尾",
                         "how": "先发碾压与局势反转的高光时刻以复仇意志引爆"}]},
        ],
        "opening_patterns": ["plot_dating_011", "plot_dating_012"],
        "climax_patterns": ["plot_dating_001", "plot_dating_005"],
        "tags": ["穿越", "重生", "爽文", "快节奏"],
        "source": "创作积累", "created_at": "2026-08-27",
    },
    {
        "id": "arc_xuanhuan_01", "name": "玄幻·试炼扬名弧",
        "description": "入门后在一场试炼/赛事中快速扬名的一段弧：入门危机→初试锋芒→试炼夺魁",
        "total_words": 30000,
        "stages": [
            {"name": "入门危机", "description": "初入势力即遭打压/考验，证明资格",
             "min_words": 3000, "max_words": 9000,
             "key_events": ["被看轻/刁难", "第一次出手", "赢得入门资格"],
             "foreshadow_opportunities": ["考验背后有人在布局"]},
            {"name": "初试锋芒", "description": "在局部冲突中展露实力、攒下第一波声名",
             "min_words": 6000, "max_words": 12000,
             "key_events": ["越级战胜对手", "获得长辈/组织认可", "结交第一批盟友"],
             "foreshadow_opportunities": ["被更强的同辈盯上"]},
            {"name": "试炼夺魁", "description": "试炼/赛事中挫败劲敌、脱颖而出",
             "min_words": 9000, "max_words": 15000,
             "key_events": ["试炼开启", "与种子选手硬碰硬", "夺魁/达成目标"],
             "foreshadow_opportunities": ["试炼背后更大的图谋"],
             "themes": [{"name": "成长的代价（Cost of Growth）", "position": "结尾",
                         "how": "付出代价换取的胜利，在夺魁时刻点题成长"}],
             "children": [
                 {"name": "试炼开启", "description": "入场、立规则、初见强敌", "min_words": 3000, "max_words": 6000,
                  "key_events": ["抽签/分组", "种子选手亮相"]},
                 {"name": "硬碰强敌", "description": "与劲敌正面交锋", "min_words": 3000, "max_words": 6000,
                  "key_events": ["越级硬刚", "压箱底底牌"]},
             ]},
        ],
        "opening_patterns": ["plot_dating_012"],
        "climax_patterns": ["plot_dating_007", "plot_dating_010"],
        "tags": ["玄幻", "修仙", "升级", "爽文"],
        "source": "创作积累", "created_at": "2026-08-27",
    },
    {
        "id": "arc_dushi_01", "name": "都市·逆袭打脸弧",
        "description": "低谷中借金手指逆袭、当众打脸的反转爽感弧：低谷受辱→金手指初现→正面打脸→立足声名",
        "total_words": 30000,
        "stages": [
            {"name": "低谷受辱", "description": "展示最狼狈处境、被当众羞辱",
             "min_words": 3000, "max_words": 6000,
             "key_events": ["被退婚/被辞退/被看不起", "当众难堪", "绝境中触发金手指"],
             "foreshadow_opportunities": ["羞辱者背后的靠山"]},
            {"name": "金手指初现", "description": "第一次用金手指扳回局面、让人刮目相看",
             "min_words": 3000, "max_words": 9000,
             "key_events": ["首次施展能力", "小范围证明自己", "赢得初步尊重"],
             "foreshadow_opportunities": ["金手指的升级条件"]},
            {"name": "正面打脸", "description": "在公开场合碾压此前羞辱者、彻底翻盘",
             "min_words": 6000, "max_words": 12000,
             "key_events": ["约战/对赌/竞争", "当众反杀", "靠山出手又被反制"],
             "foreshadow_opportunities": ["更大的对手记恨上主角"],
             "children": [
                 {"name": "约战对赌", "description": "当众立约、把事闹大", "min_words": 3000, "max_words": 6000,
                  "key_events": ["立下赌约", "围观起哄"]},
                 {"name": "当众反杀", "description": "在众目睽睽下翻盘", "min_words": 3000, "max_words": 6000,
                  "key_events": ["绝境反转", "当众打脸"]},
             ]},
            {"name": "立足声名", "description": "逆袭后的余波：收获人脉、露出更大的舞台",
             "min_words": 6000, "max_words": 9000,
             "key_events": ["声名传开", "新势力抛来橄榄枝", "埋下下一段冲突"],
             "foreshadow_opportunities": ["幕后黑手浮现"]},
        ],
        "opening_patterns": ["plot_dating_001", "plot_dating_011"],
        "climax_patterns": ["plot_dating_001", "plot_dating_005"],
        "tags": ["都市", "逆袭", "爽文", "现代"],
        "source": "创作积累", "created_at": "2026-08-27",
    },
    {
        "id": "arc_xuanyi_01", "name": "悬疑·设局揭晓弧",
        "description": "一起离奇事件从入局到真相浮出的完整弧：异常入局→线索排查→设局反杀→真相浮现",
        "total_words": 36000,
        "stages": [
            {"name": "异常入局", "description": "主角被卷入一起明显不对的离奇事件",
             "min_words": 3000, "max_words": 9000,
             "key_events": ["目击/卷入异常事件", "发现第一个疑点", "确认自己被盯上"],
             "foreshadow_opportunities": ["事件与主角过往的隐秘关联"]},
            {"name": "线索排查", "description": "走访/调查，拼凑碎片、遭遇阻力",
             "min_words": 9000, "max_words": 15000,
             "key_events": ["收集线索", "关键证人/物证", "调查方向被误导"],
             "foreshadow_opportunities": ["每个线索都指向更大阴谋"],
             "children": [
                 {"name": "走访收集", "description": "逐点取证、拼图", "min_words": 3000, "max_words": 6000,
                  "key_events": ["目击者访谈", "现场勘验"]},
                 {"name": "方向被误导", "description": "假线索引偏调查", "min_words": 3000, "max_words": 6000,
                  "key_events": ["伪证出现", "追查落空"]},
             ]},
            {"name": "设局反杀", "description": "识破误导、反将一军、逼近核心",
             "min_words": 6000, "max_words": 12000,
             "key_events": ["识破谎言", "设局引蛇出洞", "当面揭穿伪证"],
             "foreshadow_opportunities": ["真正的幕后另有其人"]},
            {"name": "真相浮现", "description": "核心真相揭晓、事件收束（可留悬念）",
             "min_words": 6000, "max_words": 12000,
             "key_events": ["动机真相", "与真凶正面交锋", "事件落幕/新疑点"],
             "foreshadow_opportunities": ["更大的局等下一次揭晓"]},
        ],
        "opening_patterns": ["plot_dating_004"],
        "climax_patterns": ["plot_dating_004", "plot_dating_010"],
        "tags": ["悬疑", "推理", "反转", "阴谋"],
        "source": "创作积累", "created_at": "2026-08-27",
    },
    {
        "id": "arc_tianwen_01", "name": "言情·误会和解弧",
        "description": "从意外相识到关系确认的甜中带虐小弧：意外初遇→暧昧升温→误会波折→和解确认",
        "total_words": 30000,
        "stages": [
            {"name": "意外初遇", "description": "被迫/巧合的相遇，留下第一印象",
             "min_words": 3000, "max_words": 6000,
             "key_events": ["意外相遇", "一方先动心或双方嘴硬", "留下一个共同的小秘密"],
             "foreshadow_opportunities": ["未说出口的心结"]},
            {"name": "暧昧升温", "description": "日常互动里感情悄悄加深",
             "min_words": 6000, "max_words": 12000,
             "key_events": ["多次碰面/合作", "体贴细节", "第一个脸红/心动场景"],
             "foreshadow_opportunities": ["对方的过去痛点"]},
            {"name": "误会波折", "description": "小误会或外部压力让关系跌入冰点",
             "min_words": 6000, "max_words": 12000,
             "key_events": ["误会产生", "一方受伤/遇险", "第三方搅局"],
             "foreshadow_opportunities": ["误会的真正来源"],
             "children": [
                 {"name": "误会产生", "description": "一句话/一个误会引爆", "min_words": 3000, "max_words": 6000,
                  "key_events": ["被撞见暧昧", "旧事被翻出"]},
                 {"name": "第三方搅局", "description": "外人加剧误会", "min_words": 3000, "max_words": 3000,
                  "key_events": ["绿茶/情敌挑拨", "家人反对"]},
             ]},
            {"name": "和解确认", "description": "误会解开、关系正式确认/升级",
             "min_words": 3000, "max_words": 9000,
             "key_events": ["真相大白", "告白/和解", "关系升温定格"],
             "foreshadow_opportunities": ["下一段感情线伏笔"]},
        ],
        "opening_patterns": ["plot_dating_006"],
        "climax_patterns": ["plot_dating_006", "plot_dating_009"],
        "tags": ["言情", "甜文", "日常", "短篇"],
        "source": "创作积累", "created_at": "2026-08-27",
    },
    {
        "id": "arc_scifi_01", "name": "科幻·末日求生弧",
        "description": "灾变降临后从求存到重建秩序的一段弧：灾变降临→求存囤积→冲突突围→秩序重建",
        "total_words": 36000,
        "stages": [
            {"name": "灾变降临", "description": "秩序崩塌的瞬间，主角失去一切",
             "min_words": 3000, "max_words": 9000,
             "key_events": ["灾变爆发", "逃出生天", "确认幸存者身份"],
             "foreshadow_opportunities": ["灾变的真正源头成谜"]},
            {"name": "求存囤积", "description": "搜集物资、加固据点、为活下去积累底牌",
             "min_words": 9000, "max_words": 15000,
             "key_events": ["搜集物资", "加固据点", "与第一批幸存者结盟"],
             "foreshadow_opportunities": ["幸存者中混入异类"],
             "children": [
                 {"name": "搜集物资", "description": "搜刮补给、装备", "min_words": 3000, "max_words": 6000,
                  "key_events": ["超市/军械库搜刮", "抢到第一辆车"]},
                 {"name": "加固据点", "description": "把落脚点改造成堡垒", "min_words": 3000, "max_words": 6000,
                  "key_events": ["选址封堵", "囤粮储水"]},
             ]},
            {"name": "冲突突围", "description": "遭遇强敌/人性之恶，杀出重围",
             "min_words": 6000, "max_words": 12000,
             "key_events": ["被掠夺者围困", "背水一战", "付出代价换生存"],
             "foreshadow_opportunities": ["更深层的阴谋浮出"],
             "children": [
                 {"name": "被掠夺者围困", "description": "恶徒围攻据点", "min_words": 3000, "max_words": 3000,
                  "key_events": ["围城", "人质要挟"]},
                 {"name": "背水一战", "description": "绝境反击杀出血路", "min_words": 3000, "max_words": 6000,
                  "key_events": ["突破包围", "火并头目"],
                  "children": [
                      {"name": "绝境反击", "description": "绝处逢生的反杀", "min_words": 3000, "max_words": 3000,
                       "key_events": ["引爆弹药库", "斩首头目"]},
                  ]},
             ]},
            {"name": "秩序重建", "description": "短暂的喘息与新秩序的萌芽（可续接下一弧）",
             "min_words": 6000, "max_words": 12000,
             "key_events": ["重建小秩序", "收容更多幸存者", "灾变真相露出一角"],
             "foreshadow_opportunities": ["更大危机的信号"]},
        ],
        "opening_patterns": ["plot_dating_011"],
        "climax_patterns": ["plot_dating_007", "plot_dating_010"],
        "tags": ["科幻", "末世", "求生", "爽文"],
        "source": "创作积累", "created_at": "2026-08-27",
    },
]

BUILTIN_STRUCTURES: list = []
for _spec in _BUILTIN_SPECS:
    _rows = flatten_nested_tree(_spec, root_id=_spec["id"])
    BUILTIN_STRUCTURES.extend(ArcNode.from_dict(r) for r in _rows)
