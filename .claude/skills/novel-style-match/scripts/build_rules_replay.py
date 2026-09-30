# -*- coding: utf-8 -*-
"""把「笔名风格规则规格 JSON」一键写入 libraries/data/style_rules.jsonl（保留其他 profile 原行）。
用途：并行窗口还原文件 / 会话翻页丢规格后的重放；或整库重建。
用法（仓库根）: python .../build_rules_replay.py <spec.json>
spec.json 形如:
  [
    {"kind":"prefer","profile_id":"profile_001","pattern":"…","desc":"…","severity":"warning","replacements":[],"enabled":true},
    {"kind":"ban","profile_id":"profile_001","pattern":"然而","desc":"…","replacements":["可","但"]},
    ...
  ]
可省略 desc/severity(默认 warning)/replacements(默认 [])/enabled(默认 true)。
同 profile_id 的旧行会被 spec 整体替换；未出现在 spec 的 profile 行原样保留。
要造 spec：agent 可直接从 add_style_rule 的返回 dict 累积，或读当前 style_rules.jsonl 过滤出该 profile 后人工改。"""
import io, json, sys
from collections import Counter
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PATH = "libraries/data/style_rules.jsonl"
if len(sys.argv) < 2:
    print("用法: build_rules_replay.py <spec.json>"); sys.exit(1)
spec = json.load(open(sys.argv[1], encoding="utf-8"))
if not isinstance(spec, list) or not spec:
    print("spec 须为非空 JSON 数组"); sys.exit(1)

# 1) 保留未在 spec 出现的 profile 的现存行
spec_profiles = {r.get("profile_id") for r in spec}
kept = []
for line in open(PATH, encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    r = json.loads(line)
    if r.get("profile_id") not in spec_profiles:
        kept.append(r)

# 2) 规范化 spec 行（补默认值 / 赋 id）
rows = list(kept)
for i, r in enumerate(spec):
    kind = r["kind"]
    base = {
        "id": r.get("id") or f"{'prefer' if kind == 'prefer' else 'ban'}_{i + 1}",
        "kind": kind,
        "profile_id": r["profile_id"],
        "pattern": r["pattern"],
        "desc": r.get("desc", ""),
        "severity": r.get("severity", "warning"),
        "replacements": r.get("replacements") or [],
        "enabled": r.get("enabled", True),
    }
    rows.append(base)

with open(PATH, "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")

print(f"replay OK: 总行 {len(rows)}")
for pid in sorted(spec_profiles):
    c = Counter(x["kind"] for x in rows if x["profile_id"] == pid)
    print(f"  {pid}: {dict(c)}")
print("提示: 运行中的 58080/长驻 MCP 进程缓存旧库，dsh 新任务（新进程）会读到新文件。勿跑 test_all（会重写此文件）。")
