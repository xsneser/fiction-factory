# -*- coding: utf-8 -*-
"""量化参考书风格标记。用法（仓库根目录跑）:
    python .claude/skills/novel-style-match/scripts/style_scan.py <folder> [起章] [止章]
读 storage/novels/<folder>/chapters/*.json。默认取样 前 20 章 + 第 300/800 起各 20 章（存在才取）。
输出: 引号/括号体系、句末标点、句长分布、单句段占比、对话占比、语词密度(>0 才列)。只读。"""
import glob, io, json, os, re, statistics, sys
from collections import Counter
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FOLDER = sys.argv[1] if len(sys.argv) > 1 else ""
START = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else None
END = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else None
if not FOLDER:
    print("用法: style_scan.py <folder> [起章] [止章]  # folder 如 十日终焉 (storage/novels/<folder>/chapters)"); sys.exit(1)

def cn(s):  # 汉字 + 常见中文标点
    return sum(1 for c in s if '一' <= c <= '鿿' or c in '，。！？；：、""\'\'「」（）…—～·')

def paras(s): return [p.strip() for p in s.split("\n") if p.strip()]
def sents(s): return [p for p in re.split(r"(?<=[。！？…!?])", s.replace("\n", "")) if p.strip()]
def cnt(s, pat): return len(re.findall(pat, s))

# 人称代词密度(分层)。单字 他/她/我 会套在 他们/我们 里 → 最长优先一次匹配,防重计。
_PRON_RE = re.compile(r"我们|她们|他们|它们|咱们|你们|他|她|它|我|你")
def _pron_count(t):
    c = {}
    for m in _PRON_RE.finditer(t):
        c[m.group(0)] = c.get(m.group(0), 0) + 1
    return c
_QUOTE_RE = re.compile(r"[“\"][^”\"]*[”\"]")   # 对话双引:全角 “ ” 或 ASCII "
def _quote_content(t):   # 引号内对话层文本(按对话双引整体切出)
    return "".join(m.group(0) for m in _QUOTE_RE.finditer(t))
def _pron_rates(t):
    """返回 (全文/千字, 叙述层/千字叙述字, 叙述层他她它/千字, 对话层/千字对话字)。
    各层以自身汉字为分母——引号外叙述代词密度与对话占比无关,跨文本可比。"""
    pd = _pron_count(t)
    narr_t = _QUOTE_RE.sub("", t)
    diag_t = _quote_content(t).replace("“", "").replace("”", "").replace('"', "")
    narr_pn = sum(_pron_count(narr_t).values())
    narr_sing = sum(v for w, v in _pron_count(narr_t).items() if w in ("他", "她", "它"))
    diag_pn = sum(_pron_count(diag_t).values())
    return (sum(pd.values())/cn(t)*1000, narr_pn/max(cn(narr_t), 1)*1000,
            narr_sing/max(cn(narr_t), 1)*1000, diag_pn/max(cn(diag_t), 1)*1000)

def pick(fs):
    n = [int(os.path.basename(f)[:4]) for f in fs]
    want = set()
    for base in (0, 300, 800):          # 0=前 20 章
        for k in range(base + 1, base + 21):
            if k in n: want.add(n.index(k))
    if START and END:
        want = {i for i, x in enumerate(n) if START <= x <= END}
    return [fs[i] for i in sorted(want)]

files = pick(glob.glob(f"storage/novels/{FOLDER}/chapters/*.json"))
text = "\n".join(json.load(open(f, encoding="utf-8")).get("content", "") for f in files)
PS, SS, n = paras(text), sents(text), max(cn(text), 1)
lens = [cn(s) for s in SS]; pp = [len(sents(p)) for p in PS]

print(f"== {FOLDER}: 取样 {len(files)} 章 | 段 {len(PS)} | 句 {len(SS)} | 汉字等 {n} ==")
print("  引号/括号体系:")
for name, pat in [("双引号“", r"“"), ("直角引号「", r"「"), ("圆括号（", r"（"), ("书名号《", r"《")]:
    print(f"    {name}: {cnt(text, pat)}")
print("  句末标点:")
for name in ["。", "！", "？", "…", "；", "："]:
    print(f"    {name}: {cnt(text, re.escape(name))}")
if lens:
    print(f"  句长(汉字) 均值 {statistics.mean(lens):.1f} 中位 {statistics.median(lens):.0f} "
          f"p90 {sorted(lens)[int(len(lens)*.9)]} | <=6字 {sum(1 for l in lens if l <= 6)/len(lens)*100:.0f}%")
if pp:
    print(f"  单句段占比 {pp.count(1)/len(pp)*100:.0f}% | 每千字句数 {len(SS)/n*1000:.1f}")
print(f"  对话占比(引号内汉字) {sum(cn(m.group(0)) for m in re.finditer(r'“[^”]*”', text))/n*100:.0f}%")

WORDS = ["缓缓", "仿佛", "似乎", "忽然", "居然", "竟然", "顿时", "猛地", "只见", "但见", "微微一笑",
         "心中一动", "眼中闪过一丝", "不禁", "不由得", "与此同时", "就在这时", "转眼间", "然而", "因此",
         "总之", "所以", "于是", "但是", "可是", "但", "难道", "应该", "看来", "按理", "换句话说",
         "沉默", "愣", "打量", "抬眼", "皱眉", "站起身", "说道", "问道", "开口", "心想", "想",
         "下意识", "忍不住", "卧槽", "淦", "牛逼", "他娘"]
print("  语词密度 (/千字, >0 才列):")
for w in WORDS:
    c = cnt(text, w)
    if c:
        print(f"    {w}: {c} 次 = {c/n*1000:.1f}/千字")
pn_full, pn_narr, pn_narr_it, pn_diag = _pron_rates(text)
print("  人称代词密度(/千字,各层以自身汉字为分母):")
print(f"    全文 {pn_full:.1f} | 叙述层 {pn_narr:.1f} (他/她/它 {pn_narr_it:.1f}) | 对话层 {pn_diag:.1f}")
# 高频 6-14 字片段（找抓取页脚/噪声顺带看）
big = Counter(x.strip() for x in re.findall(r"[^，。！？\n]{6,14}", text.replace("“", "").replace("”", "")))
print("  高频 6-14 字片段(疑似噪声/母题):", big.most_common(6))
