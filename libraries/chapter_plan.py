"""运行时章计划 —— 「这一章用哪些情节段、各段写多长、为什么在这里断章」。

三层关系（读代码前先建立这个心智模型）：

    规划层 Plot（storyline，长期承诺，revision 管辖）
        │  本章如何消费连续的一段 Plot
        ▼
    运行时章计划（本模块，**旁路运行态**，不改 storyline、不 bump revision）
        │  一次具体写作事务
        ▼
    Prepared Plot Run（prepare_plot_run 的快照 + commit_token）

它替代的是旧口径「按字数阈值机械断章」：`write_flow.chapter_status` 只能回答「预算上还能不能
再塞一段」，回答不了「这里是不是一个自然的断章点」。章计划把**选段与断章**变成主 Agent 的显式
决策，服务端只校验硬边界（连续前缀、字数带、越界、陈旧）。

**为什么带「相对漂移」约束**：允许覆写段落目标字数是为了解决「3 段不够、4 段又超」的篇幅微调，
不是为了把 Plot 容量二次改写。没有 `0.7×~1.5×` 这一层，`planned=300 → assigned=1200` 也会被放行，
于是「拉长一段来凑章」重新变成可能——那正是最初要修的问题。

存哪：写进 `write_flow` 记录（flow 已按章创建、有原子写、有生命周期）。但**计划本身不占写租约**
——`set_chapter_plan` 建的是 `phase=PLANNED` 的记录，不 `acquire_lease`；首次 prepare/commit 才取租约。
否则「只做了规划但 agent 崩了」会留下一个长期锁住这本书的 flow（见不变量 I7）。
"""
from __future__ import annotations

import hashlib
import json
import time

from libraries import orchestration_policy as OP

# 覆写目标字数的**相对漂移**上下界（不变量 I5）
DRIFT_MIN = 0.7
DRIFT_MAX = 1.5
# 断章原因码。`avoid` 断点只有在「服务端认可的强制理由」下才允许作为末段。
BREAK_REASONS = ("natural_closure", "preferred_break", "budget_boundary",
                 "plot_exhaustion", "forced_legacy_atomic")
FORCED_BREAK_REASONS = ("budget_boundary", "plot_exhaustion", "forced_legacy_atomic")


def plan_digest(plan: dict | None) -> str:
    """章计划的语义摘要：进 prepared 快照 / 版本向量 / 收章 CAS 的同一个值。"""
    p = plan or {}
    return hashlib.sha256(json.dumps({
        "chapter_num": int(p.get("chapter_num") or 0),
        "plot_ids": [str(x) for x in (p.get("plot_ids") or [])],
        "target_words": int(p.get("target_words") or 0),
        "break_reason": (p.get("break_reason") or {}).get("code", ""),
        "plot_word_targets": sorted(
            [{"plot_id": str(t.get("plot_id") or ""), "target_words": int(t.get("target_words") or 0)}
             for t in (p.get("plot_word_targets") or [])],
            key=lambda x: x["plot_id"]),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:32]


def _band_for(plot) -> tuple[int, int] | None:
    """情节段类型的建议字数区间（`storyline.PLOT_WORD_BANDS`）；未知类型 → None。"""
    from libraries.storyline import PLOT_WORD_BANDS
    return PLOT_WORD_BANDS.get(str(getattr(plot, "category", "") or ""))


def allowed_assigned_range(plot, planned: int) -> tuple[int, int]:
    """覆写目标字数的**合法区间**（不变量 I5 的三条同时成立）。

    类型带宽 ≠ 漂移带时取交集；交集为空（存量 Plot 的 planned 本身就在带外）则退化为漂移带
    ——**不制造无解的硬要求**。上限永远受 `PLOT_HARD_MAX` 约束。
    """
    from libraries.storyline import PLOT_HARD_MAX
    if planned <= 0:
        planned = 0
    lo = int(planned * DRIFT_MIN) if planned else 0
    hi = int(planned * DRIFT_MAX) if planned else PLOT_HARD_MAX
    band = _band_for(plot)
    if band:
        lo, hi = max(lo, band[0]), min(hi, band[1])
        if lo > hi:                      # 类型带与漂移带无交集 → 只守漂移带
            lo, hi = int(planned * DRIFT_MIN) if planned else band[0], (int(planned * DRIFT_MAX) if planned else band[1])
    return max(0, lo), min(PLOT_HARD_MAX, max(hi, 0))


def resolve_effective_budgets(plan: dict | None, tl, plots: list) -> dict:
    """每个情节段本次的**实际写作目标** {plot_id: {planned, assigned, source, band}}。

    没有章计划（或该段没有覆写）时 `assigned == planned`、`source == "storyline"`。
    """
    overrides = {}
    for row in ((plan or {}).get("plot_word_targets") or []):
        if isinstance(row, dict) and row.get("plot_id"):
            overrides[str(row["plot_id"])] = int(row.get("target_words") or 0)
    out = {}
    for plot in plots:
        pid = str(getattr(plot, "id", ""))
        planned = int(OP.planned_words_of(plot) or 0)
        assigned = overrides.get(pid) or planned
        out[pid] = {"planned": planned, "assigned": assigned,
                    "source": "chapter_plan" if pid in overrides else "storyline",
                    "band": list(_band_for(plot) or []), "plot_name": str(getattr(plot, "name", "") or "")}
    return out


def validate_chapter_plan(plan: dict, tl, draft: dict | None, chapter_num: int,
                          pending_review_plot: str = "") -> dict:
    """章计划的服务端硬校验。返回 {ok, problems[码], messages[], effective{}, digest}。

    规则清单（每条都对应一种「用章计划偷改故事线」或「用章计划凑字数」的绕过手法）：
      R2 章号必须是当前草稿章或下一章；
      R3 plot_ids 非空、不重复、全部存在；
      R4 已写入草稿的情节段必须是 plan 的**严格前缀**（不能改换已写段的身份）；
      R5 未来情节段必须是 `ordered_plots` 从 `next_plot` 起的**连续前缀**（不许跳序）；
      R6 存在未接受草稿时不许改计划（评审门禁的意义就在于改动必须复评）；
      R7 target_words 落在 [commit_floor, hard_max]；
      R8 选中各段的实际目标字数之和 ≥ commit_floor；
      R9 末段是 `chapter_break_after=avoid` 时必须给强制理由（否则不许在此断章）；
      R10 覆写受类型带 + `≤1200` + 相对漂移三重约束（见 `allowed_assigned_range`）；
      R11 计划总量不得越过 hard_max（除非计划已全部落稿——legacy 超长原子段不能造成死锁）。
    """
    from libraries.write_flow import _chapter_bands
    problems, messages = [], []
    plan = plan or {}
    draft = draft or {}

    def bad(code: str, msg: str) -> None:
        problems.append(code)
        messages.append(msg)

    # R2 章号
    if int(plan.get("chapter_num") or 0) != int(chapter_num or 0):
        bad("CHAPTER_PLAN_WRONG_CHAPTER",
            f"计划章号 {plan.get('chapter_num')} 不是当前章 {chapter_num}")

    plot_ids = [str(x) for x in (plan.get("plot_ids") or [])]
    by_id = {str(getattr(p, "id", "")): p for p in (getattr(tl, "plots", None) or [])}
    # R3 基本形状
    if not plot_ids:
        bad("CHAPTER_PLAN_EMPTY", "plot_ids 不能为空")
    if len(set(plot_ids)) != len(plot_ids):
        bad("CHAPTER_PLAN_DUPLICATE", "plot_ids 存在重复")
    unknown = [x for x in plot_ids if x not in by_id]
    if unknown:
        bad("CHAPTER_PLAN_UNKNOWN_PLOT", f"plot_ids 含不存在的情节段：{'、'.join(unknown)}")

    drafted = [str(b.get("plot_id") or "") for b in (draft.get("bridges") or [])]
    if drafted:
        # R4 已写段落必须是严格前缀
        if plot_ids[:len(drafted)] != drafted:
            bad("CHAPTER_PLAN_DRAFT_NOT_PREFIX",
                f"已写入草稿的情节段 {drafted} 必须是计划的前缀，当前计划 {plot_ids[:len(drafted)]}")
    # R5 未来段落必须是 ordered_plots 从 next_plot 起的连续前缀
    ordered_ids = [str(getattr(p, "id", "")) for p in OP.ordered_plots(tl)]
    nxt = OP.next_plot(tl, draft)
    future = [x for x in plot_ids if x not in drafted]
    if future:
        if nxt is None:
            bad("CHAPTER_PLAN_NO_COMMITTED_PLOT", "已无可写的已承诺情节段，却计划了未来段落")
        else:
            start = ordered_ids.index(str(nxt.id)) if str(nxt.id) in ordered_ids else -1
            expect = ordered_ids[start:start + len(future)] if start >= 0 else []
            if expect != future:
                bad("CHAPTER_PLAN_NOT_CONTIGUOUS",
                    f"未来段落必须是叙事顺序上的连续前缀；期望 {expect}，实际 {future}")
    # R6 未接受的草稿
    if pending_review_plot:
        bad("PLOT_REVIEW_PENDING", f"情节段 {pending_review_plot} 尚未评审接受，不能改章计划")

    bands = _chapter_bands(int(getattr(tl, "words_per_chapter", 0) or 3000))
    target = int(plan.get("target_words") or 0)
    # R7 目标字数带
    if target < bands["commit_floor"]:
        bad("CHAPTER_PLAN_BELOW_FLOOR", f"目标字数 {target} 低于落盘下限 {bands['commit_floor']}")
    if target > bands["hard_max"]:
        bad("CHAPTER_PLAN_OVER_HARD_MAX", f"目标字数 {target} 超过硬上限 {bands['hard_max']}")

    # R10 覆写三重约束
    effective = resolve_effective_budgets(plan, tl, [by_id[x] for x in plot_ids if x in by_id])
    for pid, row in effective.items():
        if row["source"] != "chapter_plan":
            continue
        plot = by_id[pid]
        lo, hi = allowed_assigned_range(plot, row["planned"])
        if not (lo <= row["assigned"] <= hi):
            bad("CHAPTER_PLAN_OVERRIDE_OUT_OF_RANGE",
                f"情节段「{row['plot_name'] or pid}」的覆写目标 {row['assigned']} 越界："
                f"必须在 {lo}~{hi} 之间（类型带 {row['band'] or '未分类'}、"
                f"原计划 {row['planned']}、硬上限 1200）")
    # R8 总量下限
    total_assigned = sum(r["assigned"] for r in effective.values())
    if plot_ids and total_assigned < bands["commit_floor"]:
        bad("CHAPTER_PLAN_TOTAL_BELOW_FLOOR",
            f"选中各段目标字数合计 {total_assigned} 低于落盘下限 {bands['commit_floor']}；"
            "请多选一段或把某段的目标调高（受覆写区间约束）")
    # R11 总量上限（已全部落稿时豁免：legacy 超长原子段不能变成死锁）
    if future and total_assigned > bands["hard_max"]:
        bad("CHAPTER_PLAN_TOTAL_OVER_HARD_MAX",
            f"计划总量 {total_assigned} 超过硬上限 {bands['hard_max']}；"
            "请少选一段（情节段是不可切分的提交单元，超出的部分不能靠截断解决）")
    # R9 末段 avoid
    if plot_ids and plot_ids[-1] in by_id:
        last_plot = by_id[plot_ids[-1]]
        code = str((plan.get("break_reason") or {}).get("code") or "")
        if str(getattr(last_plot, "chapter_break_after", "allowed") or "allowed") == "avoid":
            if code not in FORCED_BREAK_REASONS:
                bad("CHAPTER_PLAN_BREAK_AVOID",
                    f"末段标记为 chapter_break_after=avoid，必须有强制理由"
                    f"（{'/'.join(FORCED_BREAK_REASONS)}）才允许在此断章")
        if code and code not in BREAK_REASONS:
            bad("CHAPTER_PLAN_BREAK_REASON_UNKNOWN", f"未知断章原因码：{code}")

    return {"ok": not problems, "problems": problems, "messages": messages,
            "effective": effective, "total_assigned": total_assigned,
            "target_words": target, "bands": bands,
            "digest": plan_digest(plan) if not problems else ""}


def chapter_plan_view(plan: dict | None, draft: dict | None) -> dict | None:
    """给 `get_orchestration_state` 的只读视图：计划进度 + 下一个该写的段 + 摘要。"""
    if not isinstance(plan, dict) or not plan:
        return None
    plot_ids = [str(x) for x in (plan.get("plot_ids") or [])]
    drafted = {str(b.get("plot_id") or "") for b in ((draft or {}).get("bridges") or [])}
    done = [x for x in plot_ids if x in drafted]
    rest = [x for x in plot_ids if x not in drafted]
    return {
        "exists": True,
        "chapter_num": int(plan.get("chapter_num") or 0),
        "plot_ids": plot_ids,
        "completed_plot_ids": done,
        "next_planned_plot_id": rest[0] if rest else "",
        "target_words": int(plan.get("target_words") or 0),
        "break_reason": plan.get("break_reason") or {},
        "plot_word_targets": plan.get("plot_word_targets") or [],
        "plan_digest": plan_digest(plan),
        "state": "active" if rest else "complete",
        "valid": True,
        "updated_at": float(plan.get("updated_at") or 0),
    }


def mark_updated(plan: dict) -> dict:
    out = dict(plan or {})
    out["updated_at"] = time.time()
    return out


# ── 持久化：计划住在未结束的 write_flow 记录里（不占写租约，见模块 docstring）──

def load_plan(book_id: str, chapter_num: int = 0) -> dict | None:
    """读当前活跃章计划。

    `chapter_num` 给出时，计划章号不匹配即视为**没有计划**（上一章的残留不该被当成有效计划）；
    同时 flow 必须未被 resolve_flow 过滤掉（DONE / 超时的 PLANNED 都读不到）。
    """
    from libraries.write_flow import load_flow, resolve_flow
    fid = resolve_flow(book_id)
    if not fid:
        return None
    flow = load_flow(book_id, fid) or {}
    plan = flow.get("chapter_plan")
    if not isinstance(plan, dict) or not plan:
        return None
    if chapter_num and int(plan.get("chapter_num") or 0) != int(chapter_num or 0):
        return None
    return plan


def save_plan(book_id: str, chapter_num: int, plan: dict) -> dict:
    """把计划写进一个 `PLANNED` flow 记录并返回 (plan, flow_id)。

    **刻意不取写租约**（不变量 I7）：计划只是运行态意图，`prepare_plot_run` / 首次 Plot 提交
    才真正取租约。这样「只规划了但 agent 崩了」不会长期锁书。
    旧的 PLANNED flow（同类、未取租约）会被标记 superseded，避免计划分叉。
    """
    from libraries.write_flow import (load_flow, save_flow, start_planned_flow,
                                      supersede_planned_flows, adopt_flow)
    stored = mark_updated(plan)
    fid = ""
    # 优先复用「本章已存在、且已持有租约」的 flow（写作中改计划）
    from libraries.write_flow import active_flow_id
    owner = active_flow_id(book_id)
    if owner:
        flow = load_flow(book_id, owner) or {}
        if int(flow.get("chapter_num") or 0) == int(chapter_num or 0):
            fid = owner
            flow["chapter_plan"] = stored
            save_flow(book_id, flow)
    if not fid:
        flow = start_planned_flow(book_id, chapter_num)
        fid = flow["flow_id"]
        flow["chapter_plan"] = stored
        save_flow(book_id, flow)
    supersede_planned_flows(book_id, keep=fid)
    return {"plan": stored, "flow_id": fid}


def consume_plan(book_id: str, flow_id: str = "") -> None:
    """收章成功后清掉计划（章计划是**章级**运行态，不该跨章复用）。"""
    from libraries.write_flow import active_flow_id, load_flow, save_flow
    fid = flow_id or active_flow_id(book_id)
    if not fid:
        return
    flow = load_flow(book_id, fid)
    if not flow:
        return
    flow.pop("chapter_plan", None)
    save_flow(book_id, flow)


__all__ = ["plan_digest", "allowed_assigned_range", "resolve_effective_budgets",
           "validate_chapter_plan", "chapter_plan_view", "load_plan", "save_plan",
           "consume_plan", "BREAK_REASONS", "FORCED_BREAK_REASONS"]
