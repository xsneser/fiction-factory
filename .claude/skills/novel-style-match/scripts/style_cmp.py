# -*- coding: utf-8 -*-
"""样段 vs 参考书某章 同维度风格对比。用法（仓库根）:
    python .../style_cmp.py <sample.txt> <folder> [ch=1]
读 storage/novels/<folder>/chapters/%04d.json 作参考。✓ 表示该维与参考对齐(±25%/0.5内)。只读。"""
import glob, io, json, re, statistics, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sample_path, FOLDER = sys.argv[1], sys.argv[2]
CH = int(sys.argv[3]) if len(sys.argv) > 3 else 1
sample = open(sample_path, encoding="utf-8").read()
orig = json.load(open(glob.glob(f"storage/novels/{FOLDER}/chapters/{CH:04d}.json")[0], encoding="utf-8"))["content"]

def cnt(s, p): return len(re.findall(p, s))
def paras(s): return [x.strip() for x in s.split("\n") if x.strip()]
def sents(s): return [x for x in re.split(r"(?<=[。！？…!?])", s.replace("\n", "")) if x.strip()]
def cn(s): return sum(1 for c in s if '一' <= c <= '鿿' or c in '，。！？；：、""\'\'「」（）…—～·')

_PRON_RE = re.compile(r"我们|她们|他们|它们|咱们|你们|他|她|它|我|你")
_QUOTE_RE = re.compile(r"[“\"][^”\"]*[”\"]")   # 对话双引:全角 “ ” 或 ASCII "
def _pron_counts(t):
    c = {}
    for m in _PRON_RE.finditer(t):
        c[m.group(0)] = c.get(m.group(0), 0) + 1
    return c
def _pn_rates(t):
    narr_t = _QUOTE_RE.sub("", t)
    diag_t = "".join(m.group(0) for m in _QUOTE_RE.finditer(t)).replace("“", "").replace("”", "").replace('"', "")
    full = sum(_pron_counts(t).values())
    narr = sum(_pron_counts(narr_t).values())
    diag = sum(_pron_counts(diag_t).values())
    return dict(full=full/max(cn(t), 1)*1000,
                narr=narr/max(cn(narr_t), 1)*1000,
                diag=diag/max(cn(diag_t), 1)*1000)

def analyze(t):
    PS, SS, n = paras(t), sents(t), max(cn(t), 1)
    pn = _pn_rates(t)
    L = [cn(x) for x in SS]; pp = [len(sents(x)) for x in PS]
    return dict(dot=cnt(t, r"。"), bang=cnt(t, r"！"), qm=cnt(t, r"？"), ell=cnt(t, r"…"),
                q1=cnt(t, r"“"), jiao=cnt(t, r"「"), paren=cnt(t, r"（"), dash=cnt(t, r"——"),
                st=len(SS), para=len(PS),
                mean=round(statistics.mean(L), 1) if L else 0, med=int(statistics.median(L)) if L else 0,
                p90=sorted(L)[int(len(L)*.9)] if L else 0,
                short6=round(sum(1 for l in L if l <= 6)/max(len(L), 1)*100),
                one_para=round(pp.count(1)/max(len(pp), 1)*100),
                s_per_1k=round(len(SS)/n*1000, 1) if n else 0,
                diag=round(sum(cn(m.group(0)) for m in _QUOTE_RE.finditer(t))/n*100) if n else 0,
                pn_full=round(pn["full"], 1), pn_narr=round(pn["narr"], 1), pn_diag=round(pn["diag"], 1),
                words={w: round(cnt(t, w)/n*1000, 1) for w in
                       ["缓缓","仿佛","似乎","忽然","居然","竟然","顿时","猛地","只见","然而","所以","于是","可是","但",
                        "难道","应该","看来","按理","换句话说","沉默","愣","抬眼","打量","皱眉","站起身","说道","问道","开口",
                        "心想","想","下意识","忍不住","顿了顿"] if cnt(t, w) > 0})

A, B = analyze(sample), analyze(orig)
def row(k, label=None):
    a, b = A.get(k), B.get(k)
    mark = " ✓" if isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a-b) <= max(0.5, abs(b)*0.25) else ""
    print(f"  {label or k:12s} 样段={a!s:>7} | 参考={b!s:>7}{mark}")
print(f"== 样段 vs {FOLDER} 第{CH}章（✓=对齐） ==")
print("标点(次数):")
for k, lab in [("dot","句号。"),("bang","叹号！"),("qm","问号？"),("ell","省略号…"),("q1","双引“"),("jiao","「"),("paren","（"),("dash","——")]:
    row(k, lab)
print("句/段形态:")
for k, lab in [("s_per_1k","句/千字"),("mean","句长均值"),("med","句长中位"),("p90","句长p90"),
               ("short6","≤6字%"),("one_para","单句段%"),("diag","对话%")]:
    row(k, lab)
print("人称代词(/千字):")
for k, lab in [("pn_full","全文"),("pn_narr","叙述层引号外"),("pn_diag","对话层引号内")]:
    row(k, lab)
print("语词密度(/千字):")
for w in sorted(set(A["words"]) | set(B["words"])):
    print(f"  {w:8s} 样段={A['words'].get(w,0):>6} | 参考={B['words'].get(w,0):>6}")
print("提示: 数字只定位差异；像不像由人读定稿。")
