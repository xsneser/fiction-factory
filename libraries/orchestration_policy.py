"""编排授权策略 —— 「此刻哪些动作合法」的**唯一服务端真源**（不变量 I2）。

设计边界（贯穿整个主 Agent 编排）：**LLM 选择下一动作，服务端决定这个动作此刻是否合法。**
Root Agent 的自由只存在于「多个 allowed 动作之间怎么选」；本模块只回答「能不能」，不回答
「该不该」——后者是 `recommend_action`（纯建议，永远不能当守卫用）。

为什么必须只有一个真源：`get_orchestration_state.advisory.decision_options` 会把这些
allowed/reasons 交给模型看，而 `prepare_plot_run` / `finalize_draft_chapter` /
`accept_plot_draft` / `commit_replan_preview` 又各自要真正拒绝非法动作。若两处各写一套判断，
迟早漂移成「界面说能做、调用却被拒」或更糟的「界面说不能做、实际放行」。所以：

- `compute_orchestration_permissions(facts)` 同时喂给「告知」与「守卫」两条路径；
- 每个 mutation 工具调 `require_permission(action, facts)`，拒绝用**同一套 reason 码**；
- `tools/test_orchestration_permissions.py` 钉住「advisory 说 false → 同条件的工具调用必被拒」。

纯函数、零副作用、零磁盘写：facts 由调用方（agent_tools）读盘采集后传入。
"""
from __future__ import annotations

# 编排动作全集。工具 → 动作的映射见 agent_tools 各 mutation 工具的守卫调用。
ORCHESTRATION_ACTIONS = (
    "write_next_plot", "review_plot", "record_review", "accept_plot",
    "revise_plot", "finalize_chapter", "plan_chapter", "replan",
    "generate_replan", "commit_replan", "delegate_planner",
)


def draft_plot_ids(draft: dict | None) -> set[str]:
    """当前草稿里已写入的情节段 id 集合（draft 段落键兼容 bridges/plots）。"""
    d = draft or {}
    segs = d.get("plots")
    if segs is None:
        segs = d.get("bridges")
    return {str(s.get("plot_id")) for s in (segs or []) if (s or {}).get("plot_id")}


def ordered_plots(tl) -> list:
    """按「弧顺序 → 阶段 → 次序 → 原列表序」排情节段。

    与 outline_agent/storyline_writer 的既有排序口径一致（它们都用
    `大纲位置→stage_index→order`），只多一个「原列表序」兜底，保证遗留数据
    （order 全为 0）仍按写入顺序稳定可复现。写作顺序、horizon、cast 预测共用它。
    """
    outline_pos = {getattr(o, "id", ""): i for i, o in enumerate(getattr(tl, "outlines", None) or [])}
    plots = list(getattr(tl, "plots", None) or [])
    index = {id(p): i for i, p in enumerate(plots)}
    return sorted(plots, key=lambda p: (
        outline_pos.get(getattr(p, "outline_id", ""), 9999),
        int(getattr(p, "stage_index", 0) or 0),
        int(getattr(p, "order", 0) or 0),
        index.get(id(p), 0)))


def next_plot(tl, draft):
    """下一个待写情节段（written_chapter==0 且不在当前草稿内）——draft-aware，防章中途重复返回同一首。

    顺序取 `ordered_plots`（弧→阶段→次序），不再直接用 JSON 里的 `tl.plots` 列表序——
    续规划 append 后列表序未必等于叙事序。
    """
    indraft = draft_plot_ids(draft)
    for p in ordered_plots(tl):
        if getattr(p, "written_chapter", 0) or 0:
            continue
        if p.id in indraft:
            continue
        return p
    return None


def planned_words_of(plot) -> int | None:
    """情节段目标字数（`storyline_writer.planned_words` 的宽容包装；None=无情节段）。"""
    if plot is None:
        return None
    from libraries.storyline_writer import planned_words
    try:
        return int(planned_words(plot) or 0)
    except Exception:  # noqa: BLE001 — 取不到就当 0，不阻断门禁
        return 0


def _deny(reasons: list[str]) -> dict:
    return {"allowed": not reasons, "reasons": reasons}


def _plot_id_of(value) -> str:
    """取情节段 id：facts 里是**视图 dict**，而纯函数调用方可能直接传 PlotSlot。两种都吃。"""
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return str(getattr(value, "id", "") or "")


def _plan_missing(facts: dict) -> list[str]:
    """章计划（阶段二）**存在性**问题；未启用计划时恒为空。"""
    if not facts.get("plan_required"):
        return []
    plan = facts.get("plan")
    if not isinstance(plan, dict) or not plan:
        return ["CHAPTER_PLAN_REQUIRED_MISSING"]
    if not plan.get("valid", True):
        return ["CHAPTER_PLAN_INVALID"]
    return []


def compute_orchestration_permissions(facts: dict) -> dict:
    """事实 → 每个编排动作的 {allowed, reasons}。**本函数的输出就是授权真源。**"""
    pending = str(facts.get("pending_review_plot") or "")
    last = facts.get("last_bridge") or None
    nxt = facts.get("next_plot") or None
    status = facts.get("chapter_status") or {}
    boundary = facts.get("boundary") or {}
    receipt = facts.get("receipt") or None
    budget = facts.get("budget") or {}
    gate_on = bool(facts.get("review_gate"))
    plan = facts.get("plan") if isinstance(facts.get("plan"), dict) else None
    plan_problems = _plan_missing(facts)

    # ── 写下一段 ──
    write_reasons = []
    if pending:
        write_reasons.append("PLOT_REVIEW_PENDING")
    if nxt is None:
        write_reasons.append("NO_COMMITTED_PLOT")
    if int(budget.get("actions_used") or 0) >= int(budget.get("actions_max") or 0):
        write_reasons.append("ORCHESTRATION_BUDGET_EXHAUSTED")
    floor = int(status.get("commit_floor") or 0)
    supply_words = int(facts.get("available_committed_planned_words", 0) or 0)
    is_terminal = bool(facts.get("is_terminal_chapter", False))
    if not facts.get("draft_has_bridges") and boundary.get("needs_replan") and floor > 0 and supply_words < floor and not is_terminal:
        write_reasons.append("OPENING_COMMITTED_SUPPLY_BELOW_FLOOR")
    if plan_problems:
        write_reasons.extend(plan_problems)
    elif plan and nxt is not None:
        # 计划存在时，下一个可写段必须是计划里的下一段——不许自动越过计划去写别的。
        planned_next = str(plan.get("next_planned_plot_id") or "")
        if planned_next and _plot_id_of(nxt) != planned_next:
            write_reasons.append("CHAPTER_PLAN_NOT_NEXT")

    # ── 评审 / 记录判决 ──
    # 体检报告的 digest 是「当前正文已被报告覆盖」的唯一证据，取末段 bridge 上那份。
    gate_digest = str((last or {}).get("gate_digest") or "")
    review_reasons = [] if last else ["NO_DRAFT_PLOT"]
    record_reasons = list(review_reasons)
    if last and not gate_digest:
        record_reasons.append("GATE_REPORT_MISSING")

    # ── 接受 ──
    accept_reasons = []
    if not pending:
        accept_reasons.append("NO_PENDING_REVIEW")
    if not last:
        accept_reasons.append("NO_DRAFT_PLOT")
    elif not gate_digest:
        accept_reasons.append("GATE_REPORT_MISSING")
    if int((last or {}).get("blocking_hard_issue_count") or 0) > 0:
        accept_reasons.append("BLOCKING_HARD_ISSUES")
    if gate_on:
        # 不变量 I1：判决必须来自服务端签发的 receipt，root 不能自带 verdict。
        if not receipt:
            accept_reasons.append("REVIEW_RECEIPT_REQUIRED")
        else:
            if not receipt.get("valid"):
                accept_reasons.append("REVIEW_RECEIPT_INVALID")
            if str(receipt.get("verdict") or "") != "accept":
                accept_reasons.append("REVIEW_VERDICT_NOT_ACCEPT")

    # ── 改稿 ──
    revise_reasons = []
    if not last:
        revise_reasons.append("NO_DRAFT_PLOT")
    if int(budget.get("revise_used") or 0) >= int(budget.get("revise_max") or 0):
        revise_reasons.append("REVISE_BUDGET_EXHAUSTED")

    # ── 收章 ──
    finalize_reasons = []
    if not facts.get("draft_has_bridges"):
        finalize_reasons.append("NO_DRAFT_PLOT")
    if pending:
        finalize_reasons.append("PLOT_REVIEW_PENDING")
    if facts.get("draft_has_bridges") and not facts.get("all_draft_plots_exist", True):
        finalize_reasons.append("DRAFT_PLOT_MISSING")
    floor = int(status.get("commit_floor") or 0)
    if floor and int(status.get("written_words") or 0) < floor:
        finalize_reasons.append("CHAPTER_BELOW_COMMIT_FLOOR")
    finalize_reasons.extend(plan_problems)
    if plan and str(plan.get("state") or "active") != "complete":
        # 计划里还有没写的段落却要收章：要求主 Agent 先把计划**显式改小**
        # （set_chapter_plan 少选一段），把「在这断章」变成一个被记录的决定，
        # 而不是让它用收章悄悄绕过自己刚立下的计划。
        finalize_reasons.append("CHAPTER_PLAN_INCOMPLETE")

    # ── 分章计划（阶段二；未启用时永远 false，工具面也不暴露） ──
    plan_chapter_reasons = [] if facts.get("plan_enabled") else ["CHAPTER_PLAN_DISABLED"]

    # ── 续规划草案生成（委派 Planner） ──
    generate_replan_reasons = []
    if pending:
        generate_replan_reasons.append("PLOT_REVIEW_PENDING")
    if facts.get("draft_has_bridges"):
        generate_replan_reasons.append("UNACCEPTED_DRAFT_PRESENT")
    if int(budget.get("replan_used") or 0) >= int(budget.get("replan_max") or 0):
        generate_replan_reasons.append("REPLAN_BUDGET_EXHAUSTED")

    # ── 续规划提交 ──
    # 不变量 I4：存在未收章的草稿时一律禁止提交 replan（会把当前草稿变成孤儿）。
    # **只在编排面生效**：legacy FSM 在「情节段耗尽但章未满」时正是带草稿提交的，
    # 所以这条守卫挂在 agent_tools 的编排包装上，不写进共享的 replan_service。
    commit_replan_reasons = list(generate_replan_reasons)
    # 预览侧就绪度：让 advisory 提前说出「还差什么」，而不是等调用被服务端拒。
    preview = facts.get("preview") or {}
    if not commit_replan_reasons:
        if not preview.get("exists"):
            commit_replan_reasons.append("REPLAN_PREVIEW_MISSING")
        elif not preview.get("validation_passed"):
            commit_replan_reasons.append("REPLAN_PREVIEW_INVALID")
        elif int(preview.get("expected_revision") or -1) != int(facts.get("storyline_revision") or 0):
            commit_replan_reasons.append("REPLAN_PREVIEW_STALE")

    replan_reasons = commit_replan_reasons
    delegate_planner_reasons = generate_replan_reasons

    return {
        "write_next_plot": _deny(write_reasons),
        "review_plot": _deny(review_reasons),
        "record_review": _deny(record_reasons),
        "accept_plot": _deny(accept_reasons),
        "revise_plot": _deny(revise_reasons),
        "finalize_chapter": _deny(finalize_reasons),
        "plan_chapter": _deny(plan_chapter_reasons),
        "replan": _deny(replan_reasons),
        "commit_replan": _deny(commit_replan_reasons),
        "generate_replan": _deny(generate_replan_reasons),
        "delegate_planner": _deny(delegate_planner_reasons),
    }


def recommend_action(facts: dict, permissions: dict) -> dict:
    """纯建议（**不是守卫**）：按优先级给出「下一步更像该做什么」+ reason 码。"""
    pending = str(facts.get("pending_review_plot") or "")
    receipt = facts.get("receipt") or {}
    status = facts.get("chapter_status") or {}
    boundary = facts.get("boundary") or {}
    if pending:
        if permissions["record_review"]["allowed"] and "GATE_REPORT_MISSING" in permissions["record_review"]["reasons"]:
            return {"action": "REVIEW_PLOT", "reason_codes": ["GATE_REPORT_MISSING"]}
        if str(receipt.get("verdict") or "") == "revise_text" and permissions["revise_plot"]["allowed"]:
            return {"action": "REVISE_PLOT", "reason_codes": ["CRITIC_REQUESTED_REVISION"]}
        if not receipt.get("valid"):
            return {"action": "REVIEW_PLOT", "reason_codes": ["REVIEW_RECEIPT_REQUIRED"]}
        return {"action": "ACCEPT_PLOT", "reason_codes": ["PLOT_REVIEW_PENDING"]}
    can_write = permissions["write_next_plot"]["allowed"]
    can_finalize = permissions["finalize_chapter"]["allowed"]
    if "OPENING_COMMITTED_SUPPLY_BELOW_FLOOR" in permissions["write_next_plot"]["reasons"]:
        if permissions.get("commit_replan", {}).get("allowed"):
            return {"action": "COMMIT_REPLAN", "reason_codes": ["OPENING_COMMITTED_SUPPLY_BELOW_FLOOR"]}
        if permissions.get("delegate_planner", {}).get("allowed"):
            return {"action": "DELEGATE_PLANNER", "reason_codes": ["OPENING_COMMITTED_SUPPLY_BELOW_FLOOR"]}
    if status.get("chapter_ready") and can_finalize:
        return {"action": "FINALIZE_CHAPTER",
                "reason_codes": [str(status.get("reason") or "chapter_ready")]}
    if can_write:
        return {"action": "WRITE_NEXT_PLOT", "reason_codes": ["PLOT_AVAILABLE"]}
    if can_finalize:
        return {"action": "FINALIZE_CHAPTER",
                "reason_codes": [str(status.get("reason") or "no_next_plot")]}
    if permissions.get("commit_replan", {}).get("allowed") and boundary.get("needs_replan"):
        return {"action": "COMMIT_REPLAN", "reason_codes": list(boundary.get("reason_codes") or [])}
    if permissions.get("delegate_planner", {}).get("allowed") and boundary.get("needs_replan"):
        return {"action": "DELEGATE_PLANNER", "reason_codes": list(boundary.get("reason_codes") or [])}
    return {"action": "STOP",
            "reason_codes": sorted(set(permissions["write_next_plot"]["reasons"]
                                       + permissions["finalize_chapter"]["reasons"]
                                       + permissions["replan"]["reasons"]))}


def require_permission(action: str, facts: dict) -> dict:
    """mutation 工具的统一守卫：不合法就抛，reason 码与 advisory 完全同源（I2）。"""
    if action not in ORCHESTRATION_ACTIONS:
        raise ValueError(f"未知编排动作: {action}")
    verdict = compute_orchestration_permissions(facts)[action]
    if not verdict["allowed"]:
        raise RuntimeError(
            f"编排动作被拒（{action}）：{'、'.join(verdict['reasons'])}。"
            "请先调用 get_orchestration_state 读取最新状态与 decision_options，不要重试同一动作。")
    return verdict
