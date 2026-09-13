"""规划阶段状态机（建书 canonical `plan_meta` 的权威定义）。

**为什么阶段不能靠"草稿内容完整度"派生**：内容完成度 ≠ 工作流完成度。用户自己先把
世界观填满，不代表"查库对镜"发生过；agent 上一轮做过对镜，下一轮也无法从内容自证。
所以 `phase` 是**显式持久化状态**，由服务端推进；清单（`build_checklist`）只回答
"东西填得怎么样"。

两条不变量：

1. **一个 event 至多触发一次确定转移**（转移表）。`save_build_draft` 不是"推进到下一
   阶段"的泛化动作——它只在当前 phase 的**规定路径**被写入时才触发那一行转移；
   `user_ack` 只在 `stop_A` 有效，其它 phase 调用一律 no-op（防双推进跳阶段）。
2. **`completed_phases` 与 `stale_phases` 是两回事**：前者"曾完成过"，只增、用于审计、
   永不回退；后者"当前需要重新确认"。**当前有效 = 两者之差**。

依赖图（改动使下游失效，逐项清除，不是一刀切清空）：

    planning_thesis → scaffold → executable_horizon → forecast_horizon → validate

`scaffold` 重做后 H0/H1H2 仍基于旧 scaffold，所以那时只能移除 `scaffold` 一项。
"""
from __future__ import annotations

PLAN_META_SCHEMA = 1

ENTRY_BUILD = "build"
ENTRY_REPLAN = "replan"

# 自动推进的 phase（内容阶段 + 校验）
P_THESIS = "planning_thesis"
P_MIRROR = "mirror"
P_SCAFFOLD = "scaffold"
P_H0 = "executable_horizon"
P_FORECAST = "forecast_horizon"
P_PROMISE = "promise_reconciliation"
P_VALIDATE = "validate"
# 等用户的两个 gate
P_STOP_A = "stop_A"
P_STOP_B = "stop_B"
P_PREVIEW_CONFIRM = "preview_confirm"
P_DONE = "done"

# 入口 chain = 该入口的 phase policy（写死，不由 agent 决定）。
# replan 没有 scaffold（世界观已在建造期定稿）也没有中途停点；mirror 只在 build 是
# required，故 replan 的链上不出现 `mirror`（查库对镜仍是内核的认知步骤，见 skill）。
CHAINS = {
    ENTRY_BUILD: [P_THESIS, P_MIRROR, P_SCAFFOLD, P_STOP_A,
                  P_H0, P_FORECAST, P_PROMISE, P_VALIDATE, P_STOP_B, P_DONE],
    ENTRY_REPLAN: [P_THESIS, P_H0, P_FORECAST, P_PROMISE, P_VALIDATE,
                   P_PREVIEW_CONFIRM, P_DONE],
}

# phase → 离开它所需的唯一事件
EVENT_FOR = {
    P_THESIS: "agent_save",
    P_MIRROR: "mirror_done",
    P_SCAFFOLD: "agent_save",
    P_H0: "agent_save",
    P_FORECAST: "agent_save",
    P_PROMISE: "agent_save",
    P_VALIDATE: "validation_pass",
    P_STOP_A: "user_ack",
    P_STOP_B: "user_submit",
    P_PREVIEW_CONFIRM: "user_confirm",
}

# ── 字段路径 → 内容级别 ──────────────────────────────────────────────────────
# 有序前缀表（长前缀必须在前）；entity-id 寻址的集合元素用 `[` 起头匹配。
_PATH_RULES = (
    ("world.world_building.core_conflict", P_THESIS),
    ("world.world_building.differentiation", P_THESIS),
    ("world.tone", P_THESIS),
    ("world.target_audience", P_THESIS),
    ("world.pov", P_THESIS),
    ("world.era_language", P_THESIS),
    ("world.world_building", P_SCAFFOLD),
    ("world.factions", P_SCAFFOLD),
    ("world", P_SCAFFOLD),
    ("characters", P_SCAFFOLD),
    ("storyline.promises", P_PROMISE),
    ("storyline.plots", P_H0),
    ("storyline.outlines", P_H0),
    ("storyline.threads", P_H0),
    ("storyline.themes", P_H0),
    ("storyline.planning.horizon.h1", P_FORECAST),
    ("storyline.planning.future_intents", P_FORECAST),
    ("storyline.planning", P_FORECAST),
    ("storyline", P_H0),
    ("planning.horizon.h1", P_FORECAST),      # replan 侧 planning_patch 的路径
    ("planning.future_intents", P_FORECAST),
)

# "本 phase 该写什么"= 归属哪个**内容级别**（`agent_save` 命中才算推进）。
#
# 用**级别**而不是字段前缀表：前缀表会踩"粗前缀吞细路径"的坑——曾用
# `("world", "characters")` 当骨架的目标，于是改 `world.world_building.core_conflict`
# （命题级）也匹配 `world.`，一次顺手的命题修改会把"重做骨架"判成已完成。
# 归属判定只有一个真源：`phase_for_path()`。
_PHASE_TARGET_LEVEL = {
    P_THESIS: P_THESIS,
    P_MIRROR: P_SCAFFOLD,      # 对镜的结论落进骨架
    P_SCAFFOLD: P_SCAFFOLD,
    P_H0: P_H0,
    P_FORECAST: P_FORECAST,
    P_PROMISE: P_PROMISE,
}

# 改了某个级别的内容 → 哪些下游阶段失效
STALE_DOWNSTREAM = {
    P_THESIS: (P_SCAFFOLD, P_H0, P_FORECAST, P_VALIDATE),
    P_SCAFFOLD: (P_H0, P_FORECAST, P_VALIDATE),
    P_H0: (P_FORECAST, P_VALIDATE),
    P_FORECAST: (P_VALIDATE,),
    P_PROMISE: (P_VALIDATE,),
}


def chain_for(entry: str) -> list:
    return list(CHAINS.get(str(entry or ENTRY_BUILD)) or CHAINS[ENTRY_BUILD])


def phase_for_path(path: str) -> str:
    """字段路径 → 内容级别（`""` = 与本状态机无关，例如纯流程字段）。"""
    p = str(path or "").strip()
    if not p:
        return ""
    # plots 里的 foreshadow 是伏笔（promise 级），其余字段是 H0 结构
    if p.startswith("storyline.plots[") and ".foreshadow" in p:
        return P_PROMISE
    for prefix, level in _PATH_RULES:
        if p == prefix or p.startswith(prefix + ".") or p.startswith(prefix + "["):
            return level
    return ""


def stale_for_paths(paths, *, entry: str = ENTRY_BUILD) -> list:
    """改动的字段路径 → 需要重新确认的阶段（按 chain 过滤，保持链上顺序）。"""
    hit = set()
    for p in (paths or []):
        level = phase_for_path(p)
        if level:
            hit.update(STALE_DOWNSTREAM.get(level) or ())
    chain = chain_for(entry)
    return [p for p in chain if p in hit]


# ── plan_meta 形状 ──────────────────────────────────────────────────────────

def default_meta(entry: str = ENTRY_BUILD) -> dict:
    entry = str(entry or ENTRY_BUILD)
    chain = chain_for(entry)
    return {
        "schema_version": PLAN_META_SCHEMA,
        "entry": entry,
        "phase": chain[0],
        "phase_revision": 0,
        "completed_phases": [],
        "stale_phases": [],
        "locked_fields": [],
        "mirror_evidence": {"arc_query": False, "plot_query": False, "run_id": "", "at": ""},
        "reference_takeaways": [],
        "phase_ack": {},
        "validated": {"passed": False, "content_revision": -1, "content_digest": "", "at": ""},
    }


def coerce_meta(raw, *, entry: str = "") -> dict:
    """补默认 + 清洗（缺键补默认，**不做迁移**；未知键丢弃）。"""
    base = default_meta(entry or (raw or {}).get("entry") or ENTRY_BUILD)
    if not isinstance(raw, dict):
        return base
    out = dict(base)
    for k, v in raw.items():
        if k not in base:
            continue
        out[k] = v
    out["entry"] = str(out.get("entry") or ENTRY_BUILD)
    if out["entry"] not in CHAINS:
        out["entry"] = ENTRY_BUILD
    chain = chain_for(out["entry"])
    out["schema_version"] = PLAN_META_SCHEMA
    out["phase"] = str(out.get("phase") or chain[0])
    if out["phase"] not in chain:
        out["phase"] = chain[0]
    out["phase_revision"] = int(out.get("phase_revision") or 0)
    out["completed_phases"] = _uniq([str(p) for p in (out.get("completed_phases") or [])
                                     if isinstance(p, str)])
    out["stale_phases"] = _uniq([str(p) for p in (out.get("stale_phases") or [])
                                 if isinstance(p, str)])
    out["locked_fields"] = [str(p) for p in (out.get("locked_fields") or []) if isinstance(p, str)]
    if not isinstance(out.get("mirror_evidence"), dict):
        out["mirror_evidence"] = dict(base["mirror_evidence"])
    if not isinstance(out.get("validated"), dict):
        out["validated"] = dict(base["validated"])
    if not isinstance(out.get("phase_ack"), dict):
        out["phase_ack"] = {}
    if not isinstance(out.get("reference_takeaways"), list):
        out["reference_takeaways"] = []
    return out


def _uniq(seq) -> list:
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


# ── 派生视图（不落盘）────────────────────────────────────────────────────────

def effective_completed(meta: dict) -> list:
    """当前**仍然有效**的已完阶段 = completed − stale。"""
    m = coerce_meta(meta)
    stale = set(m["stale_phases"])
    return [p for p in m["completed_phases"] if p not in stale]


def is_phase_effective(meta: dict, phase: str) -> bool:
    return str(phase) in effective_completed(meta)


def next_phase(meta: dict) -> str:
    chain = chain_for(coerce_meta(meta)["entry"])
    cur = coerce_meta(meta)["phase"]
    if cur not in chain:
        return chain[0]
    i = chain.index(cur)
    return chain[i + 1] if i + 1 < len(chain) else ""


# ── 转移 ────────────────────────────────────────────────────────────────────

def _hits_phase_targets(paths, phase: str) -> bool:
    """本次写入的字段路径里，有没有"属于当前 phase 该写的那一级"的。"""
    level = _PHASE_TARGET_LEVEL.get(str(phase or ""))
    if not level:
        return False
    return any(phase_for_path(p) == level for p in (paths or []))


def advance(meta: dict, event: str, *, wrote_paths=None,
            mirror_evidence=None) -> dict | None:
    """按转移表推进；**不满足即返回 None（no-op）**，绝不猜测。

    - `agent_save`：仅当 phase 的目标路径被写入时推进；
    - `mirror_done`：仅当服务器观测到两条查库都发生过时推进；
    - `user_ack`：仅 `stop_A` 有效；`user_submit` / `user_confirm` 各自只对末位 gate 有效。
    """
    m = coerce_meta(meta)
    chain = chain_for(m["entry"])
    phase = m["phase"]
    if phase not in chain:
        return None
    i = chain.index(phase)
    if i + 1 >= len(chain):
        return None
    if str(event or "") != EVENT_FOR.get(phase):
        return None
    if event == "agent_save" and not _hits_phase_targets(wrote_paths, phase):
        return None
    if event == "mirror_done":
        ev = mirror_evidence or m.get("mirror_evidence") or {}
        if not (ev.get("arc_query") and ev.get("plot_query")):
            return None
    out = dict(m)
    out["phase"] = chain[i + 1]
    out["completed_phases"] = _uniq(list(m["completed_phases"]) + [phase])
    # 逐项清除：只移除**本阶段**，下游仍基于旧结论，必须继续留在 stale 里
    out["stale_phases"] = [p for p in m["stale_phases"] if p != phase]
    out["phase_revision"] = int(m["phase_revision"]) + 1
    if event in ("user_ack", "user_submit", "user_confirm"):
        out["phase_ack"] = {"phase": phase, "at": _now(), "revision": None}
    return out


def mark_stale(meta: dict, phases) -> dict:
    """把阶段加入失效集（只增不减；清除由 `advance` 逐项做）。

    **只对"做过的阶段"生效**：`completed_phases` 之外的阶段还没有内容，谈不上失效。
    否则一本刚立完命题的新书会立刻显示"还有 4 个阶段要重新检查"——那是噪音，
    不是状态。
    """
    m = coerce_meta(meta)
    chain = chain_for(m["entry"])
    done = set(m["completed_phases"])
    add = [str(p) for p in (phases or []) if str(p) in set(chain) and str(p) in done]
    merged = [p for p in chain if p in set(m["stale_phases"]) | set(add)]
    if merged == m["stale_phases"]:
        return m
    out = dict(m)
    out["stale_phases"] = merged
    # 失效意味着"回到最早的失效阶段重做"——游标跟着回退，否则 agent 重做时落盘
    # 推进不了（phase 还停在更靠后的位置，甚至停在 stop_B）。`completed_phases`
    # 不回退：它是审计，仍如实记录"曾经做过"。
    if merged:
        idx = chain.index(merged[0])
        cur = chain.index(m["phase"]) if m["phase"] in chain else 0
        if idx < cur:
            out["phase"] = chain[idx]
    out["phase_revision"] = int(m["phase_revision"]) + 1
    return out


def record_mirror_evidence(meta: dict, *, arc_query=None, plot_query=None, run_id: str = "") -> dict:
    """由 runtime 按**实际发生的 tool call** 记录对镜证据（不让 agent 自报）。"""
    m = coerce_meta(meta)
    ev = dict(m.get("mirror_evidence") or {})
    if arc_query is not None:
        ev["arc_query"] = bool(arc_query)
    if plot_query is not None:
        ev["plot_query"] = bool(plot_query)
    if run_id:
        ev["run_id"] = str(run_id)
    ev["at"] = _now()
    out = dict(m)
    out["mirror_evidence"] = ev
    return out


def _now() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")
