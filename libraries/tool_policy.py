"""工具阶段门控（ToolPolicy）—— 按 storyline.phase 约束工具调用。

背景：dsh 会擅自调用任务未要求的工具（如 save_basic_info）破坏状态
（docs/agent-sidecar-spike-2026-08-20.md）。在 agent_tools 注册层做确定性门控：
工具要求的 phase 与书的当前 phase 不符即拒绝——纯规则、零 LLM，全 surface 一致
（内置 agent 与 MCP 面共享同一注册表，天然都受约束）。

phase 语义：storyline.phase ∈ config/outlines/plots/ready。
PHASE_GATES 未收录的工具 = 不门控（只读/导航/建书向导工具，空集安全）。
"""
import functools
from typing import Optional

# 工具名 → 允许调用时的 storyline.phase 集合（未收录 = 不门控）
PHASE_GATES = {
    # 写作 / 元数据 / 质量 —— 需大纲就绪（phase=ready）
    "write_next_bridge": {"ready"},
    "write_chapter": {"ready"},
    "generate_book_meta": {"ready"},
    "tag_punch_points": {"ready"},
    "diagnose_retention": {"ready"},
    "diagnose_promises": {"ready"},
    # 大纲链 —— config/outlines/plots 阶段推进用
    "generate_full_outline": {"config", "outlines", "plots"},
    "generate_outlines": {"config", "outlines"},
    "confirm_outlines": {"config", "outlines"},
    "extend_outline": {"plots", "ready"},
    "fill_plots": {"outlines"},
    "fill_gags": {"plots"},
    "outline_agent": {"outlines", "plots"},
    # 建书规划 —— 仅 config 阶段（未建大纲前）
    "save_basic_info": {"config"},
    "generate_world": {"config"},
    "world_candidates": {"config"},
    "generate_characters": {"config"},
    "confirm_world": {"config"},
    "generate_core_conflict": {"config"},
    "generate_factions": {"config"},
    "generate_rest_world": {"config"},
    # 上架 —— 需已有正文
    "publish_check": {"ready"},
    "publish_book": {"ready"},
    "mark_finished": {"ready"},
    "export_book": {"ready"},
}


def _resolve_phase(book_id: Optional[str]) -> Optional[str]:
    """读书的 storyline.phase；无 book_id / 无故事线 / 读取失败返回 None（不门控）。

    延迟 import agent_tools（避免 tool_policy ↔ agent_tools 循环导入：
    agent_tools 在模块加载时 import tool_policy，而这里只在调用时 import agent_tools）。
    """
    if not book_id:
        return None
    try:
        from agent_tools import load_tl
        tl = load_tl(book_id)
        return getattr(tl, "phase", None)
    except Exception:
        return None


def _wrap_phase_gate(fn):
    """把工具包上 phase 门控（未收录于 PHASE_GATES 的工具原样返回）。

    gate 先于书锁（_build_registry 里包在 _wrap_book_lock 外层）：phase 不对
    就不去等锁，减少无谓阻塞。
    """
    allowed = PHASE_GATES.get(fn.__name__)
    if not allowed:
        return fn

    @functools.wraps(fn)
    def wrapper(**kwargs):
        book_id = kwargs.get("book_id") or ""
        phase = _resolve_phase(book_id)
        if phase is not None and phase not in allowed:
            raise RuntimeError(
                f"phase 门控拒绝：{fn.__name__} 要求 phase∈{sorted(allowed)}，"
                f"当前「{book_id}」phase={phase}。请先完成上一阶段（建书→大纲→写作）。")
        return fn(**kwargs)

    return wrapper
