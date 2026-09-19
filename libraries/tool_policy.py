"""工具阶段门控（ToolPolicy）—— 按 storyline.phase 约束工具调用。

背景：dsh 会擅自调用任务未要求的工具（如 save_basic_info）破坏状态
（docs/agent-sidecar-spike-2026-08-20.md）。在 agent_tools 注册层做确定性门控：
工具要求的 phase 与书的当前 phase 不符即拒绝——纯规则、零 LLM，全 surface 一致
（内置 agent 与 MCP 面共享同一注册表，天然都受约束）。

phase 语义：storyline.phase ∈ config/outlines/plots/ready。
PHASE_GATES 未收录的工具 = 不门控（只读/导航/建书向导工具，空集安全）。
"""
import functools
import os
from typing import Optional

# 工具名 → 允许调用时的 storyline.phase 集合（未收录 = 不门控）
PHASE_GATES = {
    # 质量 / 诊断 —— 需大纲就绪（phase=ready）
    "chapter_quality_gate": {"ready"},
    # 薄工具（agent 生成后落盘；无 LLM）
    "save_outlines": {"config", "outlines", "plots", "ready"},
    "save_book_meta": {"ready"},
    "save_plot_draft": {"ready"},
    "save_chapter_text": {"ready"},
    "prepare_plot_run": {"ready"},
    "prepare_plot_revision": {"ready"},
    "save_plot_revision": {"ready"},
    "get_orchestration_state": {"ready"},
    "get_plot_review_context": {"ready"},
    "plot_quality_gate": {"ready"},
    "accept_plot_draft": {"ready"},
    # 编排收章 / Critic 判决写入（主 Agent 编排链路）
    "finalize_draft_chapter": {"ready"},
    "record_plot_review": {"ready"},
    # 章计划（运行时，不改故事线）
    "set_chapter_plan": {"ready"},
    # 写作风格取样 —— Plot Run 每段抽单篇样文
    "pick_plot_sample": {"ready"},
    # 建书规划 —— config 落基本盘；plots 草案期放行。
    # ready 期仅在 save_basic_info 内部走「受限人物修正」分支（只准 characters + 必带 revision），
    # 世界观/书名/POV 等在写作期由函数内硬拒，不放给 Agent。
    "save_basic_info": {"config", "outlines", "plots", "ready"},
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


def _gate_book_id(kwargs: dict) -> str:
    """门控要判哪本书的 phase。

    多数工具签名里有 book_id；但 Writer 面只有 `save_plot_draft(commit_token, …)`，
    书号由服务端经 `NOVEL_WRITE_BOOK_ID` 注入子进程。这里把 commit_token 解析成归属书，
    让写作提交也受 phase 门控（否则 phase 已回退/未就绪时仍能写盘）。
    """
    book_id = kwargs.get("book_id") or ""
    if book_id:
        return str(book_id)
    token = kwargs.get("commit_token") or ""
    if not token:
        return ""
    try:
        from libraries.plot_commit_tokens import resolve_book_id
        return resolve_book_id(str(token), os.environ.get("NOVEL_WRITE_BOOK_ID", ""))
    except Exception:
        return ""


def _wrap_phase_gate(fn):
    """把工具包上 phase 门控（未收录于 PHASE_GATES 的工具原样返回）。

    gate 先于书锁（_build_registry 里包在 _wrap_book_lock 外层）：phase 不对
    就不去等锁，减少无谓阻塞。注意 `_SELF_LOCKED_TOOLS`（如 save_plot_draft）自己
    在函数体内加锁，门控这里只解析书号、**不加锁**。
    """
    allowed = PHASE_GATES.get(fn.__name__)
    if not allowed:
        return fn

    @functools.wraps(fn)
    def wrapper(**kwargs):
        book_id = _gate_book_id(kwargs)
        phase = _resolve_phase(book_id)
        if phase is not None and phase not in allowed:
            raise RuntimeError(
                f"phase 门控拒绝：{fn.__name__} 要求 phase∈{sorted(allowed)}，"
                f"当前「{book_id}」phase={phase}。请先完成上一阶段（建书→大纲→写作）。")
        return fn(**kwargs)

    return wrapper
