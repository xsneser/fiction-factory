"""提取入库判断闸门 —— 把「什么能进四库」从 agent 自觉变成代码可执行判定。

设计文档《外部书目提取-顺序通读式提取.md》§10 定义了入库决策与去重协议，
但那是 agent 协议（靠 LLM 自觉 + query_* 自查），代码侧 ingest 没有闸门：
- ingest_library_assets / LibraryIngestor._add_* 只做 scout_{source}_{name} 精确 id 去重；
- 结构不完整（桥段无箭头骨架/无 slots、弧无 stages…）与机制级近似（同骨架换皮）照收，
  导致库里堆满不可复用 / 重复模板 —— 即「判断什么能进四库完全不准确」。

本模块把判断落成确定性规则（无 LLM）：
1. 结构完整性（deterministic）：桥段要有箭头骨架 structure + 变量槽 slots、弧要有 stages、
   笑点要有机制描述 pattern_description、角色要有 personality —— 缺则 decision=incomplete
   （不进库，reasons 说明缺什么，供 agent 补全或落书级档案）。
2. 库内近似去重（骨架优先 bigram）：候选 vs 现有库条目 + 本批已过闸候选。
   - 骨架/机制文本（structure/description/pattern_description/personality）bigram 高重合 → duplicate；
   - 骨架中等重合且名称高重合 → duplicate（近名 + 近骨架双确认，降误伤）；
   - 候选带 `_mechanism_key`（agent 给出的规范化机制键）时，与本批同键候选精确命中 → duplicate
     —— 这是「同一个机制/骨架最多收 1 个变体」的确定性通道（语义级去重交给 agent 给键）。
3. 可复用性自评（agent/LLM 提供，可选）：候选带 _book_specific=true 或 _reusable=false
   → decision=book_archive（书级专用，不进四库，可记入 extract_state.digest）。

decision 四值：four_lib / duplicate / incomplete / book_archive。
纯规则，无 LLM，不写库。入库侧闸门复用本模块（见 agent_tools.judge_extraction /
ingest_library_assets gate=True）。

已知边界（如实保留）：纯规则 bigram 无法可靠识别「同义换词」的近似（公布规则 vs 揭晓规则）。
该层判定靠 agent 在提炼时给 `_mechanism_key`（规范化骨架/机制键）落地；不给键时只拦截
「近名+近骨架」与「骨架近乎逐字相同」，宁放过不误杀。
"""
import re
from typing import Any, Iterable, Optional


# ═══════════════════════════════════════════════════════════
# 相似度（中文二元组，对齐 libraries/plot.py PlotLibrary._bigrams）
# ═══════════════════════════════════════════════════════════

def _bigrams(text: str) -> set[str]:
    t = re.sub(r"[\s，。、；：！？…《》【】()（）　→]+", "", text or "")
    return {t[i:i + 2] for i in range(len(t) - 1) if t[i:i + 2].strip()}


def text_overlap(a: str, b: str) -> float:
    """Jaccard 式二元组重合度 [0,1]；任一方空文本 = 0。"""
    sa, sb = _bigrams(a), _bigrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(len(sa), len(sb))


def _field(obj: Any, name: str, default: Any = "") -> Any:
    """dict 或 dataclass 都取得到字段。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ═══════════════════════════════════════════════════════════
# 判据定义
# ═══════════════════════════════════════════════════════════

# 各类型进四库的结构前提：字段名 → 缺它意味着什么
_REQUIRED = {
    "plot":      (["structure", "slots"],
                  "桥段需箭头流程骨架 structure + 至少一个变量槽 slots（否则不可迁移复用）"),
    "structure": (["stages"],
                  "弧模板需至少一个 stage（否则不是可复用弧模板）"),
    "gag":       (["pattern_description"],
                  "笑点需机制描述 pattern_description（否则是段子不是机制）"),
    "character": (["personality"],
                  "角色原型需 personality（否则无法归原型）"),
}

# 近似阈值（骨架 bigram 高重合 / 名称高重合+骨架中重合）
_THRESHOLDS = {
    "plot":      {"ske": 0.50, "name": 0.60, "ske_with_name": 0.35},
    "structure": {"ske": 0.45, "name": 0.60, "ske_with_name": 0.30},
    "gag":       {"ske": 0.50, "name": 0.60, "ske_with_name": 0.35},
    "character": {"ske": 0.50, "name": 0.60, "ske_with_name": 0.35},
}
# 至少重合的二元组个数（防短文本偶然撞车）
_MIN_INTERSECTION = 3


def _skeleton(kind: str, obj: Any) -> str:
    """骨架/机制文本：判断「同骨架」时用的主体字段。"""
    if kind == "plot":
        return str(_field(obj, "structure", "") or _field(obj, "template_structure", ""))
    if kind == "structure":
        return str(_field(obj, "description", ""))
    if kind == "gag":
        return str(_field(obj, "pattern_description", ""))
    if kind == "character":
        return str(_field(obj, "personality", ""))
    return str(_field(obj, "name", ""))


def _index_text(kind: str, obj: Any) -> str:
    """索引文本：骨架/机制描述 > 名称 > 分类/标签。"""
    return " ".join([_skeleton(kind, obj),
                     str(_field(obj, "name", "")),
                     str(_field(obj, "category", "")),
                     " ".join(str(x) for x in (_field(obj, "tags", []) or []))])


def _pool_entry(kind: str, ent: Any) -> dict:
    """把池内条目（dict / dataclass / str 索引文本）归一成 {name, key, ske, idx}。"""
    if isinstance(ent, str):
        return {"name": "", "key": "", "ske": "", "idx": ent}
    return {
        "name": str(_field(ent, "name", "")),
        "key": str(_field(ent, "_mechanism_key", "") or "").strip(),
        "ske": _skeleton(kind, ent),
        "idx": _index_text(kind, ent),
    }


# ═══════════════════════════════════════════════════════════
# 判定
# ═══════════════════════════════════════════════════════════

def judge_candidate(kind: str, item: Any,
                    library_entries: Iterable[Any] = (),
                    accepted: Optional[list] = None,
                    thresholds: Optional[dict] = None) -> dict:
    """判定单条候选。

    kind: plot | structure | gag | character
    item: 候选 dict（可带自评字段 _book_specific / _reusable，规范化键 _mechanism_key）
    library_entries: 现有库条目（dict 或 dataclass）
    accepted: 本批已判 four_lib 的候选（归一 dict，见 _pool_entry）

    返回 {name, decision, reasons, overlap_with, score}，
    decision ∈ four_lib / duplicate / incomplete / book_archive。
    """
    name = str(_field(item, "name", "") or "").strip()
    thr = (thresholds or _THRESHOLDS).get(kind, _THRESHOLDS["plot"])

    # ① 可复用性自评（agent/LLM 提供，最高优先）
    if _field(item, "_book_specific", False):
        return {"name": name, "decision": "book_archive",
                "reasons": ["自评书级专用（绑定书名/角色/规则），不进四库，可记 extract_state"],
                "overlap_with": "", "score": 0.0}
    if _field(item, "_reusable", True) is False:
        return {"name": name, "decision": "book_archive",
                "reasons": ["自评不可跨书复用"], "overlap_with": "", "score": 0.0}

    # ② 结构完整性
    req_fields, why = _REQUIRED[kind]
    missing = []
    for f in req_fields:
        v = _field(item, f)
        if isinstance(v, list):
            if not v:
                missing.append(f)
        elif not str(v or "").strip():
            missing.append(f)
    if missing:
        return {"name": name, "decision": "incomplete",
                "reasons": [why, f"缺失字段: {', '.join(missing)}"],
                "overlap_with": "", "score": 0.0}

    # ③ 库内近似（现有库 + 本批已过闸候选）
    cur = _pool_entry(kind, item)
    if not cur["idx"].strip():
        return {"name": name, "decision": "incomplete",
                "reasons": ["索引文本为空，无法判断可复用性"], "overlap_with": "", "score": 0.0}
    mech_key = cur["key"]
    best_name, best_score = "", 0.0
    pool = [_pool_entry(kind, e) for e in (library_entries or [])] + list(accepted or [])
    for ent in pool:
        if not (ent["idx"] or "").strip():
            continue
        # 机制键精确命中（agent 规范化键，语义级同骨架）
        if mech_key and ent["key"] and mech_key == ent["key"]:
            best_name, best_score = ent["name"] or "(批内同键)", 1.0
            break
        ske_ov = text_overlap(cur["ske"], ent["ske"]) if cur["ske"] and ent["ske"] else 0.0
        name_ov = text_overlap(cur["name"], ent["name"]) if cur["name"] and ent["name"] else 0.0
        inter = len(_bigrams(cur["idx"]) & _bigrams(ent["idx"]))
        hit = (ske_ov >= thr["ske"] or (ske_ov >= thr["ske_with_name"] and name_ov >= thr["name"]))
        if hit and inter >= _MIN_INTERSECTION and ske_ov > best_score:
            best_name, best_score = ent["name"] or "(近似)", ske_ov
    if best_name:
        return {"name": name, "decision": "duplicate",
                "reasons": [f"库内已有高度近似（骨架重合度 {best_score:.0%}），跳过或差异化改名"],
                "overlap_with": best_name, "score": round(best_score, 3)}

    return {"name": name, "decision": "four_lib",
            "reasons": ["通过结构完整性与库内比对，可进四库"],
            "overlap_with": "", "score": 0.0}


def judge_batch(kind: str, candidates: Iterable[Any],
                library_entries: Iterable[Any] = (),
                thresholds: Optional[dict] = None) -> list[dict]:
    """批量判定：批内已过闸候选进入后续候选的近似比对池（同骨架只收 1 个变体）。

    返回与 candidates 顺序对齐的 decision 列表。
    """
    accepted_pool: list[dict] = []
    out: list[dict] = []
    for item in candidates:
        d = judge_candidate(kind, item, library_entries,
                            accepted=accepted_pool, thresholds=thresholds)
        if d["decision"] == "four_lib":
            accepted_pool.append(_pool_entry(kind, item))
        out.append(d)
    return out


def judge_all(plots: Optional[list] = None, structures: Optional[list] = None,
              gags: Optional[list] = None, characters: Optional[list] = None,
              plot_lib: Any = None, struct_lib: Any = None,
              gag_lib: Any = None, char_lib: Any = None,
              thresholds: Optional[dict] = None) -> dict:
    """四类整体判定（agent 预检 / 入库闸门共用）。

    plot_lib/struct_lib/gag_lib/char_lib 传库实例（取 .templates/.patterns/.archetypes）；
    不传则只做结构完整性与批内去重（不比对现有库）。

    返回 {plots:[decision...], structures:[...], gags:[...], characters:[...]}，
    各列表与传入候选顺序对齐。
    """
    result: dict = {}
    if plots is not None:
        entries = getattr(plot_lib, "templates", []) if plot_lib else []
        result["plots"] = judge_batch("plot", plots, entries, thresholds)
    if structures is not None:
        # 情节弧库扁平存储：structure 判定/去重单元 = 一棵根弧（根 + 子弧）。候选须已按根
        # 归一成树 dict（libraries.structure.normalize_structures / rows_to_tree_dicts），
        # 池 = 库内根弧（不拿子弧当独立模板比对）。
        if struct_lib is not None and hasattr(struct_lib, "roots"):
            entries = struct_lib.roots()
        else:
            entries = getattr(struct_lib, "templates", []) if struct_lib else []
        result["structures"] = judge_batch("structure", structures, entries, thresholds)
    if gags is not None:
        entries = getattr(gag_lib, "patterns", []) if gag_lib else []
        result["gags"] = judge_batch("gag", gags, entries, thresholds)
    if characters is not None:
        entries = getattr(char_lib, "archetypes", []) if char_lib else []
        result["characters"] = judge_batch("character", characters, entries, thresholds)
    return result


def split_by_decision(candidates: list, decisions: list[dict]) -> tuple[list, dict]:
    """按判定结果把候选拆成 [进四库候选, {decision: [丢弃明细...]}]。

    decisions 与 candidates 顺序对齐。丢弃明细 = {name, reasons, overlap_with}。
    """
    keep: list = []
    dropped: dict = {}
    for item, d in zip(candidates, decisions):
        if d["decision"] == "four_lib":
            keep.append(item)
        else:
            dropped.setdefault(d["decision"], []).append({
                "name": d.get("name", ""), "reasons": d.get("reasons", []),
                "overlap_with": d.get("overlap_with", ""),
            })
    return keep, dropped
