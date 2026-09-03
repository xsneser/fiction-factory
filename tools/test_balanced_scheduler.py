# -*- coding: utf-8 -*-
"""离线无网 dry-run：验证多源下载「均衡 + 自愈」负责源分派。

import 真实 book_fetch 的 _choose_prefer/_sched_on_done/_sched_stats/_parse_chapter_num，
用确定性 stub 替代网络 _dl_one（仿 prefer→source_order 兜底 + 每源 has/fail/lat 画像），
复刻有界窗口 coordinator 循环，断言：
  - 每章恰提交一次且按番茄目录原序
  - 全健康多源 → 负责次数/实际提供接近均匀
  - 病源持续失败 → 被降权（assigned 显著低于均分），其余源兜底、章节不丢不抛
  - 病源在"仅它覆盖"的区间恢复提供服务 → 窗口内 miss 清零 → 重新获得指派（自愈）
  - 退化：单源全失败 / 无章号的番外 → 不卡、不重、不丢

直接 python tools/test_balanced_scheduler.py 运行，不联网。
"""
import os
import random
import sys
import time
import concurrent.futures as cfd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from collections import Counter

from plugins.book_fetch import (
    _choose_prefer, _sched_on_done, _sched_stats,
    _DL_MISS_WINDOW, _DL_MISS_PENALTY, _DL_MISS_DOWN,
)
from plugins.webnovel_scraper import _parse_chapter_num


# ── 构造：章节 / 源画像 ────────────────────────────────────────────

def make_chapters(nums):
    """nums: list[int]，None 表示无「第N章」号（番外/序章）。index 从 1 递增。"""
    out = []
    for i, n in enumerate(nums, 1):
        out.append({"index": i, "title": (f"第{n}章 标题{i}" if n is not None
                                          else f"番外{i}")})
    return out


def make_srcs(spec, all_nums):
    """spec: {site: {"has": None=含全部 or set[int], "fail": set[int], "lat":ms, "score":int}}
    返回按 score 降序的源列表（项含 site/wmap）与 profile。"""
    srcs, prof = [], {}
    for site, c in spec.items():
        has = all_nums if c.get("has") is None else set(c["has"])
        srcs.append({"site": site, "wmap": {n: {"num": n} for n in has}})
        prof[site] = {"has": has, "fail": set(c.get("fail") or ()),
                      "lat": c.get("lat", 3)}
    srcs.sort(key=lambda s: spec[s["site"]].get("score", 0), reverse=True)
    return srcs, prof


# ── 有界窗口 coordinator 复刻（与 book_fetch._save_merged 下载段同构） ──

def run(chapter_nums, spec, workers=6, seed=1):
    chapters = make_chapters(chapter_nums)
    all_nums = {n for n in chapter_nums if n is not None}
    srcs, prof = make_srcs(spec, all_nums)
    order = [s["site"] for s in srcs]
    dl_workers = max(1, min(workers, len(chapters)))
    ex = cfd.ThreadPoolExecutor(max_workers=dl_workers)
    dl_st = _sched_stats(srcs)
    jobs = list(enumerate(chapters))
    inflight = {}              # Future -> (ch, pref_site|None)
    submitted = []             # 原序已提交的 ch index
    done_log = []              # (index, fnum, ok, used, pref_site)
    _next = 0

    def _dl_one_stub(index, ch, prefer):
        """仿 _dl_one：prefer 有则该章试 prefer，否则按 order 兜底到第一个能取的源；
        某源 fail（含该章号）→ 视作下载空/失败继续往下。耗时 = 负责尝试源的 lat + 种子抖动。"""
        fnum = _parse_chapter_num(ch["title"])
        used = None
        if fnum is not None:
            pref_first = prefer if (prefer and fnum in prefer["wmap"]) else None
            tries = ([prefer] if pref_first else []) + [s for s in srcs if s is not pref_first]
            for s in tries:
                if fnum not in s["wmap"]:
                    continue
                site = s["site"]
                if fnum in prof[site]["fail"]:
                    continue
                used = site
                break
        lat = prof[used]["lat"] if used else 1
        rnd = random.Random(f"{seed}:{index}:{used}")
        time.sleep((lat + rnd.random() * 2) / 1000.0)
        return used is not None, used, fnum

    def _fill(limit):
        nonlocal _next
        added = 0
        while _next < len(jobs) and added < limit:
            _i, ch = jobs[_next]
            _next += 1
            submitted.append(ch["index"])
            pref = None
            if srcs:
                fnum = _parse_chapter_num(ch["title"])
                if fnum is not None:
                    avail = [s for s in srcs if fnum in s["wmap"]]
                    pref = _choose_prefer(avail, dl_st)
            if pref is not None:
                dl_st[pref["site"]]["load"] += 1
                dl_st[pref["site"]]["assigned"] += 1
            inflight[ex.submit(_dl_one_stub, int(ch["index"]), ch, pref)] = \
                (ch, pref["site"] if pref else None)
            added += 1

    try:
        _fill(dl_workers)
        while inflight:
            _done, _ = cfd.wait(list(inflight), return_when=cfd.FIRST_COMPLETED)
            for fut in _done:
                ch, pref_site = inflight.pop(fut)
                try:
                    ok, used, fnum = fut.result()
                except Exception as e:  # 不应发生；兜底记失败
                    print("WORKER_EXC", ch, e)
                    ok, used, fnum = False, None, None
                _sched_on_done(dl_st, pref_site, ok, used)
                done_log.append((ch["index"], fnum, ok, used, pref_site))
                _fill(1)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    assigned = Counter(s["site"] for s in srcs
                       for _ in range(dl_st[s["site"]]["assigned"]))
    served = Counter(u for (_, _, ok, u, _) in done_log if ok and u)
    return {"submitted": submitted, "done_log": done_log,
            "assigned": assigned, "served": served, "srcs": srcs,
            "dl_st": dl_st, "order": order}


# ── 断言工具 ───────────────────────────────────────────────────────

CHECKS = []
def chk(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(("  PASS " if cond else "  FAIL ") + name + (f"   {detail}" if detail else ""))


def assert_exact_once_and_order(res, n):
    sub = res["submitted"]
    chk("每章恰提交一次且按原序", len(sub) == n and sub == list(range(1, n + 1)),
        f"len={len(sub)} first={sub[:3]} last={sub[-3:]}")


def main():
    N = 300

    # ── A) 全健康三源：各源含全部章、同速 → 负责/实际提供接近均匀 ──
    print("\n[A] 全健康三源（各含全部章，同速）均衡")
    res = run(list(range(1, N + 1)),
              {"A": {"lat": 3, "score": 3},
               "B": {"lat": 3, "score": 2},
               "C": {"lat": 3, "score": 1}}, workers=6, seed=11)
    assert_exact_once_and_order(res, N)
    ok_all = all(ok for (_, _, ok, _, _) in res["done_log"])
    chk("全部章节成功(ok)", ok_all, f"ok={sum(ok for _,_,ok,_,_ in res['done_log'])}")
    asg = res["assigned"]; svd = res["served"]
    chk("assigned 均匀(差≤15)", max(asg.values()) - min(asg.values()) <= 15,
        f"assigned={dict(asg)}")
    chk("served 均匀(每源≥85 且≤115)", all(85 <= svd[s] <= 115 for s in asg),
        f"served={dict(svd)}")

    # ── B) 病源持续失败 → 降权；A/C 兜底，章节不丢 ──
    print("\n[B] 病源 B 全失败（A/C 含全部章）→ 降权自愈于 A/C")
    res = run(list(range(1, N + 1)),
              {"A": {"lat": 3, "score": 3},
               "B": {"lat": 3, "score": 2, "fail": set(range(1, N + 1))},
               "C": {"lat": 3, "score": 1}}, workers=6, seed=22)
    assert_exact_once_and_order(res, N)
    ok_all = all(ok for (_, _, ok, _, _) in res["done_log"])
    chk("全部章节成功(ok)", ok_all)
    asg = res["assigned"]
    n_b = asg.get("B", 0)
    chk("病源 B 被降权(assigned<0.12*N)", n_b < 0.12 * N, f"B assigned={n_b}")
    chk("病源 B 仍先被探测过(assigned>0)", n_b > 0)
    chk("A/C 承担大部分", asg.get("A", 0) + asg.get("C", 0) > 0.85 * N,
        f"A+C={asg.get('A',0)+asg.get('C',0)}")

    # ── C) 自愈：B 先失败被降权，后在"仅 B 覆盖"区间恢复提供服务 → 重新获指派 ──
    print("\n[C] B 前 60 章失败→降权；101-140 仅 B 覆盖→B 兜底恢复→重获指派")
    nums = list(range(1, N + 1))
    spec = {"A": {"lat": 3, "score": 3, "has": [n for n in nums if n < 101 or n > 140]},
            "B": {"lat": 3, "score": 2, "fail": set(range(1, 61))},
            "C": {"lat": 3, "score": 1, "has": [n for n in nums if n < 101 or n > 140]}}
    res = run(nums, spec, workers=6, seed=33)
    assert_exact_once_and_order(res, N)
    ok_all = all(ok for (_, _, ok, _, _) in res["done_log"])
    chk("全部章节成功(ok，含仅B覆盖的101-140)", ok_all,
        f"failed={[i for i,_,ok,_,_ in res['done_log'] if not ok][:5]}")
    # 101-140 只能由 B 提供
    b_served_isolated = sum(1 for (i, _, ok, u, _) in res["done_log"]
                            if 101 <= i <= 140 and ok and u == "B")
    chk("仅B覆盖区间由B提供服务", b_served_isolated == 40, f"B served 101-140={b_served_isolated}")
    # 恢复后（B 窗内 miss 清零）在 181-300 的全覆盖区重新获得指派
    tail_b = sum(1 for (i, _, _, _, p) in res["done_log"] if 181 <= i <= 300 and p == "B")
    chk("B 自愈后重获指派(tail assigned>0)", tail_b > 0, f"B tail assigned={tail_b}")

    # ── D) 退化：单源全失败 / 带无章号番外 → 不卡不重不丢 ──
    print("\n[D] 退化：单源全失败 + 2 个番外(无章号)")
    nums = [None, None] + list(range(1, 61))
    res = run(nums,
              {"A": {"lat": 2, "score": 1, "fail": set(range(1, 61))}},
              workers=4, seed=44)
    sub = res["submitted"]
    chk("每章(含番外)恰提交一次", len(sub) == len(nums) and sub == list(range(1, len(nums) + 1)),
        f"len={len(sub)}")
    # 番外无章号 → 镜像取不到(ok False 占位)但流程不崩；正文章全失败 → 占位，不抛
    chk("流程完成、无异常", len(res["done_log"]) == len(nums))

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
