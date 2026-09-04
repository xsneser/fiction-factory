#!/usr/bin/env python3
"""extract_judge 判断闸门自测（隔离：临时库，不污染 libraries/data/*.jsonl）"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from libraries.extract_judge import (
    judge_candidate, judge_batch, judge_all, split_by_decision, text_overlap,
)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name} {detail}")


print("═══ extract_judge 单元自测 ═══")

# ── 1. 相似度 ──
check("text_overlap 相同文本=1", abs(text_overlap("规则揭晓 人人自危", "规则揭晓 人人自危") - 1.0) < 1e-6)
check("text_overlap 无关文本<0.3",
      text_overlap("退婚打脸 家族大会", "拍卖会捡漏 灵药") < 0.3)

# ── 2. 四条判定路径 ──
plot_ok = {"name": "规则揭晓·人人自危", "category": "冲突", "sub_category": "规则",
           "structure": "[揭晓规则]→[人人自危]→[主角冷眼旁观]",
           "slots": [{"name": "惩罚", "options": ["杀人", "逐出", "扣分"]}]}
d = judge_candidate("plot", plot_ok, [])
check("桥段完整无近似 → four_lib", d["decision"] == "four_lib", str(d["decision"]))

# 结构不完整（无 slots）→ incomplete
plot_bad = {"name": "规则揭晓", "category": "冲突", "structure": "[揭晓规则]→[人人自危]"}
d = judge_candidate("plot", plot_bad, [])
check("桥段缺 slots → incomplete", d["decision"] == "incomplete",
      f"reasons={d['reasons']}")

# 真实污染场景：同名骨架逐字相同、只改名称 → duplicate（骨架高重合）
plot_dup = {"name": "规则揭晓·众人自危（第二副本）", "category": "冲突",
            "structure": "[揭晓规则]→[人人自危]→[主角冷眼旁观]",
            "slots": [{"name": "惩罚", "options": ["杀人", "逐出", "扣分"]}]}
d = judge_candidate("plot", plot_dup, [plot_ok])
check("同骨架换名 → duplicate", d["decision"] == "duplicate",
      f"overlap_with={d.get('overlap_with')}")

# 同义换词但给了规范化机制键 → duplicate（语义级通道）
plot_syn = {"name": "规则公布·群体恐慌", "category": "冲突",
            "structure": "[公布规则]→[群体恐慌]→[主角冷静]",
            "slots": [{"name": "惩罚", "options": ["杀人"]}],
            "_mechanism_key": "rule_reveal_panic"}
plot_syn2 = {"name": "规则揭晓·人人自危", "category": "冲突",
             "structure": "[揭晓规则]→[人人自危]→[主角冷眼旁观]",
             "slots": [{"name": "惩罚", "options": ["杀人"]}],
             "_mechanism_key": "rule_reveal_panic"}
rows_syn = judge_batch("plot", [plot_syn2, plot_syn], [])
check("同机制键（同义换词）→ 第二个 duplicate",
      rows_syn[0]["decision"] == "four_lib" and rows_syn[1]["decision"] == "duplicate",
      str([r["decision"] for r in rows_syn]))

# 自评书级专用 → book_archive
plot_book = {**plot_ok, "_book_specific": True}
d = judge_candidate("plot", plot_book, [])
check("自评书级专用 → book_archive", d["decision"] == "book_archive")

# ── 3. 批内去重（同骨架第二个被判 duplicate）──
rows = judge_batch("plot", [plot_ok, plot_dup], [])
check("批内同骨架第二个 → duplicate",
      rows[0]["decision"] == "four_lib" and rows[1]["decision"] == "duplicate",
      str([r["decision"] for r in rows]))

# ── 4. split_by_decision ──
keep, dropped = split_by_decision([plot_ok, plot_dup], rows)
check("split 拆出 1 进库 / 1 丢弃",
      len(keep) == 1 and "duplicate" in dropped,
      f"keep={len(keep)} dropped={list(dropped)}")

# ── 5. 四类各自判据 ──
check("structure 缺 description → incomplete",
      judge_candidate("structure", {"name": "孤例弧"}, [])["decision"] == "incomplete")
s_ok = {"name": "规则副本通关弧",
        "description": "入场→探清规则→破局登顶的一整段弧（可复用内容主体）",
        "min_words": 12000, "max_words": 24000}
check("structure 有 description → four_lib",
      judge_candidate("structure", s_ok, [])["decision"] == "four_lib")
check("gag 缺 pattern_description → incomplete",
      judge_candidate("gag", {"name": "某段子"}, [])["decision"] == "incomplete")
check("character 缺 personality → incomplete",
      judge_candidate("character", {"name": "某人物"}, [])["decision"] == "incomplete")
check("character 自评不可复用 → book_archive",
      judge_candidate("character", {"name": "某人物", "personality": "x",
                                    "_reusable": False}, [])["decision"] == "book_archive")

# ── 6. agent_tools 集成：judge_extraction 已注册、ingest 闸门生效（隔离临时库）──
import agent_tools
names = [e["name"] for e in agent_tools.TOOL_REGISTRY]
check("judge_extraction 已注册进工具表", "judge_extraction" in names)
check("ingest_library_assets 在工具表", "ingest_library_assets" in names)

from libraries.plot import PlotLibrary
PlotLibrary._instance = None  # 破单例，让 data_dir 真正生效（否则返回真实库实例）
tmp = tempfile.mkdtemp()
tl = PlotLibrary(data_dir=tmp)
orig = agent_tools.plot_lib
agent_tools.plot_lib = tl
try:
    before = len(tl.templates)
    r = agent_tools.ingest_library_assets(
        plots=[plot_ok], source="test", gate=True)
    after = len(tl.templates)
    check("闸门放行完整桥段并入库(临时库 +1)",
          after == before + 1 and r.get("plots") == 1,
          f"before={before} after={after} stats={r.get('plots')}")

    before2 = len(tl.templates)
    r2 = agent_tools.ingest_library_assets(
        plots=[plot_bad, plot_dup], source="test", gate=True)
    after2 = len(tl.templates)
    judge = r2.get("judge", {})
    check("闸门拦截不完整/近似候选(不入库)",
          after2 == before2 and r2.get("plots") == 0
          and len(judge.get("incomplete", [])) == 1
          and len(judge.get("duplicate", [])) == 1,
          f"after2={after2} judge={ {k: len(v) for k, v in judge.items() if isinstance(v, list)} }")
finally:
    agent_tools.plot_lib = orig

# ── 7. structure 平级独立弧闸门 + 逐条落盘（隔离临时库，不污染真实库）──
from libraries.structure import StructureLibrary
StructureLibrary._instance = None
stmp = tempfile.mkdtemp()
sl = StructureLibrary(data_dir=stmp)
orig_sl = agent_tools.struct_lib
agent_tools.struct_lib = sl
try:
    arcs = [
        {"name": "复仇·夺嫡清算弧", "description": "被夺权者蛰伏反杀、当众清算的一整段弧",
         "min_words": 20000, "max_words": 30000, "tags": ["复仇", "爽文"]},
        {"name": "蛰伏攒底牌", "description": "示弱潜伏、暗中串联旧部", "min_words": 3000, "max_words": 6000,
         "tags": ["复仇"]},
    ]
    j = agent_tools.judge_extraction(structures=arcs)
    check("judge_extraction 平级逐弧判 four_lib",
          len(j["structures"]) == 2 and all(x["decision"] == "four_lib" for x in j["structures"]),
          str(j["structures"]))
    before3 = len(sl.templates)
    rr = agent_tools.ingest_library_assets(structures=arcs, source="fanqie", gate=True)
    after3 = len(sl.templates)
    check("平级 gate=True 落 2 弧", after3 == before3 + 2 and rr.get("structures") == 2,
          f"before={before3} after={after3} stats={rr.get('structures')}")
    added = [t for t in sl.templates if t.id.startswith("scout_fanqie_")]
    check("每条独立弧落 1 行(id规范, 无parent, 自带tags)",
          len(added) == 2 and all("parent_arc_id" not in t.to_dict() for t in added)
          and all(t.tags for t in added), str([(t.id, t.tags) for t in added]))
    before4 = len(sl.templates)
    agent_tools.ingest_library_assets(structures=arcs, source="fanqie", gate=True)
    check("重复逐条跳过", len(sl.templates) == before4)
finally:
    agent_tools.struct_lib = orig_sl

print("\n" + "=" * 50)
print(f"extract_judge 自测: {len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项:", "、".join(FAIL))
    sys.exit(1)
print("✅ 判断闸门验证通过")
