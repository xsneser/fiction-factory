"""规划待填清单（纯规则，零 LLM、零 IO）。

**checklist 只回答"东西填得怎么样"，phase 回答"流程走到哪"**——两者不能合并。
用户自己先把世界观填满，不代表"查库对镜"发生过；反过来 agent 上一轮做过对镜，
下一轮也无法从内容自证。所以阶段游标在 `build_phases`（显式持久化），这里只管
readiness。

三个消费面共用同一套 item 结构 / 状态机 / 判据函数，但**不共用 item 清单**：

- **Common**（建书与续写都有）：H0 可执行度 / H1 承接 / H2 仍有效 / 伏笔合理性 / 一致性；
- **Build 专属**：premise / 世界维度 / 势力 / 人物 / 人物语音 / 弧设计意图；
- **Replan 专属**：diagnosis / changed assumptions / 越界检查。

硬门禁只有三项，且**每一项的 `missing` 都与 `validate_build` 今天已有的 issue 一一对应**
（世界观为空 / 弧或情节段为空 / 没有主角）。这是刻意的：门禁是现有 `issues` 的**真子集**，
`passed` 语义一字不改，因此只能断言单向蕴含 `gate fail ⇒ validation fail`——
"门禁过、整体不过"（还有非门禁 issue）是正常情形。"核心矛盾太短""语音没给全"这类
一律是 `thin` 软提醒，不阻断。

`validators_for_paths()` 是**校验强度的唯一入口**：按被写字段路径取 validator 集合
（含前置闭包），不是线性的"最大档"——例如只改 `plots[*].foreshadow` 需要的是
{H0 结构, 伏笔结构}，而不是"只跑伏笔档"。agent 无参数可降级。
"""
from __future__ import annotations

from libraries import build_phases as bp
from libraries.storyline import voice_keys_with_content

# 世界观必须成形的维度（world_building 内）
WORLD_DIMS = ("world_summary", "era", "power_system", "geography",
              "culture", "history", "social_structure", "rules")
CORE_CONFLICT_MIN = 12          # 一句话核心矛盾的最短长度（软阈值）
_THIN_VOICE_MIN = 2             # speech_profile 里"有实质内容"的键数下限

MISSING, THIN, OK, VALIDATED = "missing", "thin", "ok", "validated"
_RANK = {MISSING: 0, THIN: 1, OK: 2, VALIDATED: 3}

GROUP_BUILD, GROUP_REPLAN, GROUP_COMMON = "build", "replan", "common"


def _item(iid, title, group, phase, status, *, gate=False, detail="", hint=""):
    return {"id": iid, "title": title, "group": group, "phase": phase,
            "status": status, "gate": bool(gate), "detail": detail, "hint": hint}


def _txt(v) -> str:
    return str(v or "").strip()


def _wb_of(draft) -> dict:
    world = (draft or {}).get("world")
    if not isinstance(world, dict):
        return {}
    wb = world.get("world_building")
    if isinstance(wb, dict):
        return wb
    # 容忍裸 world_building 本体
    return {k: v for k, v in world.items()
            if k not in ("tone", "target_audience", "pov", "era_language")}


def _sl_of(draft) -> dict:
    sl = (draft or {}).get("storyline")
    return sl if isinstance(sl, dict) else {}


def _planning_of(draft) -> dict:
    pl = _sl_of(draft).get("planning")
    return pl if isinstance(pl, dict) else {}


def _future_intents_of(draft) -> list:
    """H2 的建书期 staging 位置（见 docs：建书时 planning_state 还不存在）。"""
    for src in (_planning_of(draft).get("future_intents"), (draft or {}).get("future_intents")):
        if isinstance(src, list):
            return src
    return []


def _h1_of(draft) -> list:
    hz = _planning_of(draft).get("horizon")
    if isinstance(hz, dict) and isinstance(hz.get("h1"), list):
        return hz["h1"]
    return []


# ── Build 专属项 ────────────────────────────────────────────────────────────

def _premise_item(draft) -> dict:
    wb = _wb_of(draft)
    # gate 的 missing 判据 = "世界观段为空"，与 validate_build 的 world 为空 issue 一一对应
    if not wb:
        return _item("premise", "核心矛盾与差异化命题", GROUP_BUILD, bp.P_THESIS, MISSING,
                     gate=True, detail="世界观还没开始写（core_conflict 缺失）",
                     hint="先立核心矛盾：一句话最核心的写进 core_conflict，完整论述写 differentiation")
    cc, diff = _txt(wb.get("core_conflict")), _txt(wb.get("differentiation"))
    if len(cc) >= CORE_CONFLICT_MIN and diff:
        return _item("premise", "核心矛盾与差异化命题", GROUP_BUILD, bp.P_THESIS, OK,
                     gate=True, detail="core_conflict + differentiation 已成形")
    lacks = []
    if len(cc) < CORE_CONFLICT_MIN:
        lacks.append(f"core_conflict 只有 {len(cc)} 字（建议 ≥{CORE_CONFLICT_MIN}）")
    if not diff:
        lacks.append("缺 differentiation（与同类的完整差异论述）")
    return _item("premise", "核心矛盾与差异化命题", GROUP_BUILD, bp.P_THESIS, THIN,
                 gate=True, detail="；".join(lacks),
                 hint="命题没立稳，后面的骨架与规划都是它的推论")


def _world_dims_item(draft) -> dict:
    wb = _wb_of(draft)
    filled = [k for k in WORLD_DIMS if _txt(wb.get(k))]
    n = len(filled)
    status = OK if n >= 6 else (THIN if n >= 3 else MISSING)
    missing = [k for k in WORLD_DIMS if k not in filled]
    return _item("world_dims", "世界观维度", GROUP_BUILD, bp.P_SCAFFOLD, status,
                 detail=f"{n}/{len(WORLD_DIMS)} 维已填" + (f"，缺 {', '.join(missing)}" if missing else ""))


def _factions_item(draft) -> dict:
    wb, chars = _wb_of(draft), (draft or {}).get("characters") or []
    factions = wb.get("factions") or []
    if not isinstance(factions, list) or not factions:
        return _item("factions", "势力", GROUP_BUILD, bp.P_SCAFFOLD, THIN,
                     detail="还没有势力", hint="每个势力至少要有 1 个对应人物")
    names = {_txt(f.get("name")) for f in factions if isinstance(f, dict)}
    owned = {_txt(c.get("faction")) for c in chars if isinstance(c, dict)}
    orphan = sorted(n for n in names if n and n not in owned)
    if orphan:
        return _item("factions", "势力", GROUP_BUILD, bp.P_SCAFFOLD, THIN,
                     detail=f"{len(factions)} 个势力，其中 {', '.join(orphan)} 还没有人物")
    return _item("factions", "势力", GROUP_BUILD, bp.P_SCAFFOLD, OK,
                 detail=f"{len(factions)} 个势力均有人物")


def _cast_item(draft) -> dict:
    chars = (draft or {}).get("characters") or []
    named = [c for c in chars if isinstance(c, dict) and _txt(c.get("name"))]
    leads = [c for c in named if int(c.get("importance") or 0) == 1]
    # gate 的 missing 判据 = "没有人物 / 没有主角"，与 _character_issues 的 issue 对应
    if not named or not leads:
        detail = "还没有人物" if not named else "没有主角（importance=1）"
        return _item("cast", "主角与主要人物", GROUP_BUILD, bp.P_SCAFFOLD, MISSING,
                     gate=True, detail=detail, hint="至少要有一个主角，且每个势力至少一人")
    bad = [c for c in named if _txt(c.get("role")) not in ("主角", "配角", "反派", "其他")]
    if bad:
        return _item("cast", "主角与主要人物", GROUP_BUILD, bp.P_SCAFFOLD, THIN, gate=True,
                     detail=f"{len(bad)} 人 role 不合法（只能 主角/配角/反派/其他）")
    return _item("cast", "主角与主要人物", GROUP_BUILD, bp.P_SCAFFOLD, OK, gate=True,
                 detail=f"{len(named)} 人，含 {len(leads)} 个主角")


def _voice_item(draft) -> dict:
    chars = [c for c in ((draft or {}).get("characters") or [])
             if isinstance(c, dict) and _txt(c.get("name")) and _txt(c.get("role")) != "其他"]
    if not chars:
        return _item("voice", "人物语音规律", GROUP_BUILD, bp.P_SCAFFOLD, THIN,
                     detail="还没有具名人物")
    ready = [c for c in chars
             if len(voice_keys_with_content(c.get("speech_profile"))) >= _THIN_VOICE_MIN]
    ratio = len(ready) / len(chars)
    status = OK if ratio >= 1 else (THIN if ratio >= 0.5 else MISSING)
    return _item("voice", "人物语音规律", GROUP_BUILD, bp.P_SCAFFOLD, status,
                 detail=f"{len(ready)}/{len(chars)} 人给了 ≥{_THIN_VOICE_MIN} 项语音规律",
                 hint="给「生成规律」（句长节奏/判断逻辑/情绪如何改变说话）而不是台词")


def _arc_intent_item(draft) -> dict:
    outs = [o for o in (_sl_of(draft).get("outlines") or []) if isinstance(o, dict)]
    if not outs:
        return _item("arc_intent", "弧的设计意图", GROUP_BUILD, bp.P_H0, THIN, detail="还没有弧")
    ready = 0
    for o in outs:
        di = o.get("design_intent") if isinstance(o.get("design_intent"), dict) else {}
        if _txt(di.get("goal")) and _txt(di.get("deviation")):
            ready += 1
    status = OK if ready == len(outs) else (THIN if ready else MISSING)
    return _item("arc_intent", "弧的设计意图", GROUP_BUILD, bp.P_H0, status,
                 detail=f"{ready}/{len(outs)} 条弧写了 design_intent{{goal, deviation}}",
                 hint="写清本弧目标 + 偏离库模板的地方；notes 是自由备注，不做格式要求")


# ── Common 项（建书与续写共用判据）──────────────────────────────────────────

def _h0_item(*, outlines, plots, problems, validation_ran=False) -> dict:
    outs = [o for o in (outlines or []) if isinstance(o, dict)]
    pls = [p for p in (plots or []) if isinstance(p, dict)]
    if not outs or not pls:
        return _item("h0", "近期规划可执行度", GROUP_COMMON, bp.P_H0, MISSING, gate=True,
                     detail="缺顶层弧或情节段", hint="正式弧 + 情节段至少要各有一条")
    # 只有**真跑过**结构校验（validate_storyline）且无问题时才算 validated；
    # 否则最多是 ok——不跑校验却自称已校验会误导下一步决策。
    problems = list(problems or [])
    st = VALIDATED if (validation_ran and not problems) else OK
    return _item("h0", "近期规划可执行度", GROUP_COMMON, bp.P_H0, st, gate=True,
                 detail=(f"{len(outs)} 条弧 / {len(pls)} 个情节段"
                         + (f"；{len(problems)} 处结构问题" if problems
                            else ("；结构校验通过" if validation_ran else "；尚未跑结构校验"))),
                 hint="；".join(problems[:3]) if problems else "")


def _h1_item(h1) -> dict:
    rows = [x for x in (h1 or []) if isinstance(x, dict)]
    if not rows:
        return _item("h1", "远期方向 H1", GROUP_COMMON, bp.P_FORECAST, THIN,
                     detail="还没有近期方向", hint="下一小段往哪走、承接当前的什么")
    ok = all(_txt(x.get("title") or x.get("arc_intent")) for x in rows)
    return _item("h1", "远期方向 H1", GROUP_COMMON, bp.P_FORECAST, OK if ok else THIN,
                 detail=f"{len(rows)} 条方向")


def _h2_item(intents) -> dict:
    rows = [x for x in (intents or []) if isinstance(x, dict)]
    if not rows:
        return _item("h2", "远期意图 H2", GROUP_COMMON, bp.P_FORECAST, THIN,
                     detail="还没有远期意图", hint="主线升级 / 角色成长 / 未兑现线索")
    bad = [x for x in rows if not _txt(x.get("intent") or x.get("title"))]
    return _item("h2", "远期意图 H2", GROUP_COMMON, bp.P_FORECAST, OK if not bad else THIN,
                 detail=f"{len(rows)} 条远期意图" + (f"，{len(bad)} 条缺 intent" if bad else ""))


def _far_foreshadow_item(intents) -> dict:
    n = sum(len(x.get("foreshadow") or []) for x in (intents or []) if isinstance(x, dict))
    if n:
        return _item("far_foreshadow", "远期伏笔", GROUP_COMMON, bp.P_FORECAST, OK,
                     detail=f"{n} 条拟埋/拟收伏笔")
    return _item("far_foreshadow", "远期伏笔", GROUP_COMMON, bp.P_FORECAST, THIN,
                 detail="远期还没有伏笔", hint="远期意图里写 foreshadow{kind, desc, setup_at, payoff_at}")


def _near_foreshadow_item(*, plots, promises) -> dict:
    pls = [p for p in (plots or []) if isinstance(p, dict)]
    setup_ids = set()
    for p in pls:
        for r in (p.get("resolves_promise_ids") or []):
            setup_ids.add(_txt(r))
        for f in (p.get("foreshadow") or []):
            if isinstance(f, dict) and f.get("kind") == "payoff" and _txt(f.get("promise_id")):
                setup_ids.add(_txt(f["promise_id"]))
        if _txt(p.get("resolves_plot_id")):
            setup_ids.add(_txt(p["resolves_plot_id"]))
    buried = [p for p in pls if any(isinstance(f, dict) and f.get("kind") == "setup"
                                    for f in (p.get("foreshadow") or []))]
    ledger = {_txt(q.get("id")) for q in (promises or []) if isinstance(q, dict)}
    covered = {_txt(q.get("setup_plot_id")) for q in (promises or []) if isinstance(q, dict)}
    if not buried and not ledger:
        return _item("near_foreshadow", "近期伏笔", GROUP_COMMON, bp.P_PROMISE, THIN,
                     detail="近期还没有登记伏笔",
                     hint="设局段写 foreshadow{kind:'setup'}，收局段用 promise_id 指回要兑现的那一条")
    orphan = [p.get("id") for p in buried if _txt(p.get("id")) not in covered]
    unresolved = sorted(s for s in setup_ids if s and s not in ledger)
    detail = f"{len(buried)} 处设局 / {len(ledger)} 条台账"
    if orphan:
        detail += f"；{len(orphan)} 处设局没有收口"
    if unresolved:
        detail += f"；{len(unresolved)} 个兑现点找不到对应的设局"
    return _item("near_foreshadow", "近期伏笔", GROUP_COMMON, bp.P_PROMISE,
                 OK if not (orphan or unresolved) else THIN, detail=detail)


def _consistency_item(*, stale_phases, validated) -> dict:
    v = validated if isinstance(validated, dict) else {}
    stale = [s for s in (stale_phases or [])]
    if stale:
        return _item("consistency", "一致性回打", GROUP_COMMON, bp.P_VALIDATE, THIN,
                     detail=f"{len(stale)} 个阶段因上游变化需重新检查：{', '.join(stale)}",
                     hint="从最靠前的失效阶段起重做，不要假装下游仍然有效")
    if v.get("passed"):
        return _item("consistency", "一致性回打", GROUP_COMMON, bp.P_VALIDATE, VALIDATED,
                     detail="校验通过")
    return _item("consistency", "一致性回打", GROUP_COMMON, bp.P_VALIDATE, MISSING,
                 detail="还没有跑通过一次完整校验")


# ── 组装 ────────────────────────────────────────────────────────────────────

def build_checklist(draft, *, stale_phases=None, validated=None, storyline_problems=None,
                    validation_ran=False) -> dict:
    """建书侧清单。`storyline_problems` 由调用方（validate_build）传入结构校验结论。"""
    d = draft if isinstance(draft, dict) else {}
    sl = _sl_of(d)
    items = [
        _premise_item(d),
        _world_dims_item(d),
        _factions_item(d),
        _cast_item(d),
        _voice_item(d),
        _arc_intent_item(d),
        _h0_item(outlines=sl.get("outlines"), plots=sl.get("plots"),
                 problems=storyline_problems, validation_ran=validation_ran),
        _h1_item(_h1_of(d)),
        _h2_item(_future_intents_of(d)),
        _far_foreshadow_item(_future_intents_of(d)),
        _near_foreshadow_item(plots=sl.get("plots"), promises=sl.get("promises")),
        _consistency_item(stale_phases=stale_phases, validated=validated),
    ]
    return _summarize(items, entry=bp.ENTRY_BUILD)


def replan_checklist(tl, state, *, diagnosis=None, stale_phases=None,
                     storyline_problems=None, validation_ran=False) -> dict:
    """续写侧清单。**不重新设计世界观**——没有 premise/world/factions/cast 项。"""
    st = state if isinstance(state, dict) else {}
    plots = [p.to_dict() if hasattr(p, "to_dict") else p
             for p in (getattr(tl, "plots", None) or [])]
    outlines = [o.to_dict() if hasattr(o, "to_dict") else o
                for o in (getattr(tl, "outlines", None) or [])]
    promises = list(getattr(tl, "promises", None) or [])
    horizon = st.get("horizon") if isinstance(st.get("horizon"), dict) else {}
    intents = st.get("future_intents") or []
    diag = diagnosis if isinstance(diagnosis, dict) else {}

    keys = ("current_pressure", "main_tension", "reader_question", "urgent_problem")
    filled = [k for k in keys if _txt(diag.get(k))]
    diag_status = OK if len(filled) == len(keys) else (THIN if filled else MISSING)

    items = [
        _item("diagnosis", "当前诊断", GROUP_REPLAN, bp.P_THESIS, diag_status,
              detail=f"{len(filled)}/{len(keys)} 键已给" if filled else "还没有诊断",
              hint="四个键：当前压力 / 主要矛盾 / 读者问题 / 最紧迫的问题"),
        _item("changed_assumptions", "既有预测复核", GROUP_REPLAN, bp.P_THESIS, THIN,
              detail="续规划时对照 facts 复核旧假设", hint="事实变了就废弃对应预测，不要硬续"),
        _h0_item(outlines=outlines, plots=plots, problems=storyline_problems,
                 validation_ran=validation_ran),
        _h1_item(horizon.get("h1")),
        _h2_item(intents),
        _far_foreshadow_item(intents),
        _near_foreshadow_item(plots=plots, promises=promises),
        _consistency_item(stale_phases=stale_phases, validated=st.get("validated")),
    ]
    return _summarize(items, entry=bp.ENTRY_REPLAN)


def _summarize(items, *, entry) -> dict:
    counts = {k: 0 for k in (MISSING, THIN, OK, VALIDATED)}
    for it in items:
        counts[it["status"]] += 1
    blocking = [it["id"] for it in items if it["gate"] and it["status"] == MISSING]
    gates = {"passed": not blocking, "blocking": blocking,
             "items": [it["id"] for it in items if it["gate"]]}
    pending = [it["id"] for it in items if it["status"] in (MISSING, THIN)]
    return {
        "ok": True, "entry": entry, "items": items, "counts": counts, "gates": gates,
        "pending": pending,
        "summary": _summary_text(counts, gates, pending),
    }


def _summary_text(counts, gates, pending) -> str:
    parts = [f"待填 {len(pending)} 项"]
    if gates["blocking"]:
        parts.append(f"硬门禁未过 {len(gates['blocking'])} 项（{', '.join(gates['blocking'])}）")
    elif gates["items"]:
        parts.append("硬门禁已过")
    if counts[THIN]:
        parts.append(f"另有 {counts[THIN]} 项偏薄")
    if counts[MISSING] == 0 and counts[THIN] == 0:
        return "清单已全绿"
    return " · ".join(parts)


def derive_phase(checklist) -> str:
    """兜底：仅当 canonical 没有 `plan_meta`（老记录/异常恢复）时用来推一个起点。

    常规权威是 `plan_meta.phase`——不能靠内容完整度反推流程走到哪。
    """
    for it in (checklist or {}).get("items") or []:
        if it.get("gate") and it.get("status") == MISSING:
            return it.get("phase") or bp.P_THESIS
    return bp.P_H0


# ── 校验强度：按被写字段路径取 validator 集合（含前置闭包）──────────────────

V_THESIS = "thesis"
V_WORLD = "world"
V_CHARACTERS = "characters"
V_H0 = "h0"
V_FORECAST = "forecast"
V_PROMISE = "promise"

# 每个**内容级别**需要跑的 validator 闭包（不是"最高一档"）：
# 改 plots[*].foreshadow 既要 H0 基础结构合法，也要伏笔结构合法。
_LEVEL_VALIDATORS = {
    bp.P_THESIS: {V_THESIS},
    bp.P_SCAFFOLD: {V_WORLD, V_CHARACTERS},
    bp.P_H0: {V_H0},
    bp.P_FORECAST: {V_H0, V_FORECAST},
    bp.P_PROMISE: {V_H0, V_PROMISE},
}


def validators_for_paths(paths, *, phase: str = "") -> set:
    """被写字段路径 → 需要跑的 validator 集合（前置闭包）。

    例：`storyline.plots[id=pl1].foreshadow` → `{h0, promise}`（而非"只跑伏笔档"）。
    路径为空时退化为按 phase 取默认集——agent 无法通过参数降级校验强度。

    **单一真源**：归属判定复用 `build_phases.phase_for_path()`，不另建一套前缀表
    （并行前缀表会踩"粗前缀吞掉细路径"的坑：`world` 曾把 `core_conflict` 从 thesis
    级别误判成 scaffold，于是立命题阶段又开始要 geography/factions）。
    """
    out = set()
    for p in (paths or []):
        s = str(p or "")
        level = bp.phase_for_path(s)
        if level in _LEVEL_VALIDATORS:
            out |= _LEVEL_VALIDATORS[level]
        elif s.startswith("storyline"):
            out |= {V_H0}          # storyline 下未知字段：至少保证 H0 结构
    if not out:
        out = default_validators_for_phase(phase)
    return out


_PHASE_DEFAULT_VALIDATORS = {
    bp.P_THESIS: {V_THESIS},
    bp.P_SCAFFOLD: {V_WORLD, V_CHARACTERS},
    bp.P_H0: {V_H0},
    bp.P_FORECAST: {V_H0, V_FORECAST},
    bp.P_PROMISE: {V_H0, V_PROMISE},
    bp.P_VALIDATE: {V_THESIS, V_WORLD, V_CHARACTERS, V_H0, V_FORECAST, V_PROMISE},
}


def default_validators_for_phase(phase: str) -> set:
    return set(_PHASE_DEFAULT_VALIDATORS.get(str(phase or ""), ()))
