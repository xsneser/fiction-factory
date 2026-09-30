# -*- coding: utf-8 -*-
"""章节字数区间门禁（预测式）验收。纯规则、无 LLM、不落盘。

要保护的不变量：
1. 区间由 target 比例推导（3000 → 2700/3300/3600，落盘下限 0.6→1800）；
2. **不是**「到 target 就换章」——2000+1500 仍能自然成章；
3. 预测下一个完整情节段会冲破 hard_max → 现在就收章；
4. `chapter_break_after` 是 Planner 表达语义、Server 决定断章；
5. legacy 超长情节段不会把门禁逼进「不能继续写也不能收章」的死锁。
"""
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from libraries.write_flow import chapter_status, next_action  # noqa: E402


def tl(plots=("p_next",)):
    return SimpleNamespace(
        words_per_chapter=3000,
        plots=[SimpleNamespace(id=i, written_chapter=0) for i in plots])


def draft(words, bridges=1):
    return {"chapter_num": 1, "words": words,
            "bridges": [{"plot_id": f"done{i}", "text": "x" * 100} for i in range(bridges)]}


def status(words, next_words, break_after="allowed", *, bridges=1, plots=("p_next",),
           needs_replan=False):
    return chapter_status("book_t", tl(plots), draft(words, bridges),
                          needs_replan=needs_replan,
                          next_plot_planned_words=next_words,
                          next_plot_break_after=break_after)


def main():
    # ── 1. 区间推导 ──
    s = status(1000, 600)
    assert (s["soft_min_words"], s["soft_max_words"], s["hard_max_words"], s["commit_floor"]) \
        == (2700, 3300, 3600, 1800), s
    assert s["target_words"] == 3000
    print("[1] 区间 2700/3300/3600（下限 1800）OK")

    # ── 2. 不是「到 target 就换章」──
    s = status(2000, 1500)
    assert not s["chapter_ready"] and s["reason"] == "need_more_words", s
    assert s["predicted_words_after_next_plot"] == 3500 < s["hard_max_words"] + 1
    assert next_action(s) == "PREPARING_PLOT"
    print("[2] 2000+1500=3500 继续写（不机械卡 3300）OK")

    # ── 3. 到达 soft_max：allowed/preferred 收章，avoid 继续 ──
    for ba in ("allowed", "preferred"):
        s = status(3350, 500, ba)
        assert s["chapter_ready"] and s["reason"] == "soft_max_reached", (ba, s)
        assert next_action(s) == "COMMITTING_CHAPTER"
    # avoid 只在「预测不越硬上限」时才允许继续（3350+200=3550 ≤ 3600）；
    # 预测越 hard_max 时照样收——avoid 不能无限突破硬上限。
    s = status(3350, 200, "avoid")
    assert not s["chapter_ready"] and s["reason"] == "need_more_words", s
    s = status(3350, 500, "avoid")
    assert s["chapter_ready"] and s["reason"] == "next_plot_would_exceed_hard_max", s
    # 未到 soft_max（3100）且预测不越界（3100+500=3600 恰好等于 hard_max）→ 继续
    s = status(3100, 500, "allowed")
    assert not s["chapter_ready"], s
    print("[3] soft_max + allowed/preferred 收章、avoid 继续 OK")

    # ── 4. 硬上限：无条件收章（不看 break_after）──
    for ba in ("allowed", "preferred", "avoid"):
        s = status(3600, 500, ba)
        assert s["chapter_ready"] and s["reason"] == "hard_max_reached", (ba, s)
    print("[4] hard_max 无条件收章 OK")

    # ── 5. 预测式：再塞一个完整情节段会冲破 hard_max → 现在收 ──
    s = status(2500, 1200)
    assert s["chapter_ready"] and s["reason"] == "next_plot_would_exceed_hard_max", s
    assert s["forced_budget_boundary"] is True and s["chapter_ready"] is True
    assert s["predicted_words_after_next_plot"] == 3700
    # 严格大于才是「超」：2500+1100=3600 恰好等于 hard_max → 继续
    s = status(2500, 1100)
    assert not s["chapter_ready"], s
    print("[5] 预测冲破 hard_max 收章 OK")

    # ── 6. 预测跨 soft_max 且 break_after=preferred → 收章 ──
    s = status(2800, 700, "preferred")
    assert s["chapter_ready"] and s["reason"] == "predicted_crosses_soft_max", s
    s = status(2800, 700, "allowed")
    assert not s["chapter_ready"], "allowed 不因预测跨 soft_max 收章"
    print("[6] 预测跨 soft_max（preferred）收章 OK")

    # ── 7. 死锁守卫：legacy 超长情节段 + 章内字数低于落盘下限 ──
    # 预测 1500+2200=3700 > hard_max，但 1500 < commit_floor(1800) —— 收章会被
    # save_chapter_text 拒（0.6×3000）。必须**继续写**，不能进死锁。
    s = status(1500, 2200)
    assert not s["chapter_ready"], s
    assert s["reason"] == "need_more_words" and next_action(s) == "PREPARING_PLOT", s
    # 越过下限后同一条就正常收
    s = status(1900, 2200)
    assert s["chapter_ready"] and s["reason"] == "next_plot_would_exceed_hard_max", s
    print("[7] 死锁守卫（低于 commit_floor 不强收）OK")

    # ── 8. 情节段耗尽：过 soft_min 自然收束，未过则续规划/失败 ──
    s = status(2750, None, plots=())
    assert s["chapter_ready"] and s["reason"] == "plot_exhausted_at_soft_min", s
    s = status(2000, None, plots=(), needs_replan=True)
    assert not s["chapter_ready"] and s["reason"] == "plot_exhausted_needs_replan"
    assert next_action(s) == "REPLANNING"
    s = status(2000, None, plots=(), needs_replan=False)
    assert next_action(s) == "FAILED"
    print("[8] 情节段耗尽分支 OK")

    # ── 9. 空草稿 / 0 与 None 的语义区分 ──
    s = chapter_status("book_t", tl(), {"chapter_num": 1, "words": 0, "bridges": []},
                       needs_replan=False, next_plot_planned_words=600)
    assert not s["has_legal_closure"] and not s["chapter_ready"], s
    # next=0 是「有一个目标字数为 0 的情节段」，**不是**「没有下一个」
    s = status(3350, 0)
    assert s["has_next_committed_plot"] and s["next_plot_planned_words"] == 0, s
    assert s["chapter_ready"] and s["reason"] == "soft_max_reached", s
    s = status(1000, 0)
    assert not s["chapter_ready"], s
    # None = 没有待写情节段（走 plot_exhausted 分支），与 0 必须可区分
    s_none = status(2000, None, plots=())
    assert s_none["next_plot_planned_words"] is None, s_none
    assert s_none["reason"].startswith("plot_exhausted"), s_none
    # 矛盾输入（说没有下一个、但故事线里还有）不得误判成 ready —— 安全侧是「继续写」
    s_odd = status(2000, None)
    assert not s_odd["chapter_ready"], s_odd
    print("[9] 无收束点不报 ready / 0 与 None 语义区分 OK")

    # ── 10. 非法 break_after 回落 allowed（不因脏数据卡死）──
    s = status(3350, 500, "BOGUS")
    assert s["next_plot_break_after"] == "allowed" and s["chapter_ready"], s
    print("[10] 非法 break_after 回落 OK")

    # ── 11. 模拟 FSM 走完一章：确认收章位置与落点字数 ──
    # 复用「章内决策」逻辑（chapter_status + next_action），不调 LLM、不落盘。
    # 口径：`plan` = 该章依次要写的情节段目标字数；末尾追加一个「下一章首段」哨兵，
    # 让每一步的「下一段预算」都有定义（这正是 FSM 每轮的真实输入）。
    SENTINEL = 1200   # 下一章首段（典型新情节段上限）

    def simulate(plans, *, break_afters=None):
        """返回 (本章收章时写掉的段数, 本章字数)。"""
        seq = list(plans) + [SENTINEL]
        ba = list(break_afters or []) + ["allowed"] * (len(seq) - len(break_afters or []))
        written, used = 0, 0
        for i, w in enumerate(seq):
            # 第 i 步是「要不要写 seq[i]」：预测 = 当前已写 + **这一段**的目标字数，
            # break_after 也取这一段的。取 seq[i+1] 是经典差一错（会把下一段的预算
            # 算进这一次的决策里）。
            s = chapter_status("book_t", tl(), draft(written, bridges=max(1, used)),
                               needs_replan=False, next_plot_planned_words=seq[i],
                               next_plot_break_after=ba[i])
            if next_action(s) == "COMMITTING_CHAPTER":
                break
            written += w
            used += 1
        return used, written

    # 新粒度：一章 4~6 个情节段（500~700 字/段）→ 收章字数落在软区间内
    for plan in ([700, 700, 700, 700, 700], [600, 600, 600, 600, 600, 600],
                 [500, 650, 700, 600, 650, 550]):
        n, w = simulate(plan)
        assert 4 <= n <= 6, (plan, n, w)                  # 不再是「一章两三段」
        assert 2700 <= w <= 3600, (plan, n, w)            # soft_min ~ hard_max
    # break_after 真的影响落点：同一组情节段，标 preferred 会更早收章
    n_a, w_a = simulate([700] * 5, break_afters=["allowed"] * 5)
    n_p, w_p = simulate([700] * 5, break_afters=["preferred"] * 5)
    assert w_p <= w_a, (w_a, w_p)
    # 旧粒度（一条 2200 字的长情节段）：**情节段不可切分**，所以章只能在段边界收。
    # 收在 2200（一条）——低于 soft_min 2700，但 ≥ 落盘下限 1800 且比旧行为（两条 4400）
    # 更贴近 target 3000。这是「不迁移旧数据」下情节段原子性的必然结果，不是缺陷。
    n, w = simulate([2200, 2200])
    assert n == 1 and w == 2200, (n, w)
    print("[11] FSM 模拟：新粒度 4~6 段落在区间内；旧粒度按段边界收（2200）OK")

    print()
    print("章节字数门禁：全部通过")


if __name__ == "__main__":
    main()
