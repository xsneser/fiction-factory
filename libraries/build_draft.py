"""建书 canonical 记录 — 一次建书的**唯一权威状态**（`storage/build_drafts/<sid>.json`）。

为什么要有这个文件（改前先读，这是踩过坑后的收口）：

1. **阶段权威不能挂在浏览器上**。此前阶段由两处表达：浏览器上报的
   `storage/build_status.json` 快照 + 任务文本里的自然语言。2026-09-10 实测：用户点
   「已挑选完毕」时前端**先**发 agent 任务、**后**才上报 `cur=3`，于是 `_build_fsm`
   读到 `cur=2` → 起了 `build-candidates` profile 的子 run（那个工具面没有
   set_world/set_outline）→ 模型整轮只能做「平台错误恢复」。
   现在 `step` 只由这里持有，且只由**服务端端点**（transition）原子推进：前端必须先
   拿到 transition 成功响应，才允许派发 agent。

2. **不要复用 `storage/build_sessions/<sid>.json`**。那是 `libraries/planning_state.py`
   的 planning 草稿，且 `attach_build_session` 在建书成功时会 `unlink()` 它
   （`libraries/build_flow.py` 顶部第 1 条已写明）——两者同文件会互相踩且随建书消失。

3. **与 `build_status.json` 的分工**（两个文件都要有，别合并）：
   - 本文件 = canonical：step / revision / selected_candidate / draft / 提交结果。
   - `build_status.json` = 浏览器 UI 现状快照（向导当前页、submit_error 原文），
     供 `get_build_status` 工具与 `drive_ui` 步门控使用，**不再作阶段判据**。

4. **`draft` 是提交源**（`/books/start` 命中 revision 时用它建书），浏览器表单只是它的
   UI 投影；手动模式（「跳过，手动设定」）没有 draft，提交回退表单 payload。

并发：三个写者（浏览器端点、MCP 薄工具、提交回写）分属不同进程，故用
`process_file_lock` 跨进程锁 + `write_json_atomic`，与 `libraries/crawl_progress.py` 同形。
"""
from __future__ import annotations

import os
import time

from core.json_store import process_file_lock, read_json, write_json_atomic
from libraries import build_phases, plan_diff

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DIR = os.path.join(_ROOT, "storage", "build_drafts")

# 保留的会话记录数（建书成功不删记录：它是审计线索，只做有界清理）
MAX_KEPT = 50

# 步号语义：1-2=候选阶段（build-candidates），3=内容构建（build）。
# 与 dsh_bridge._build_fsm 的 profile 映射一一对应（见 libraries/skill_profile.SKILL_PROFILE_MAP）。
STEP_TO_PROFILE = {1: "build-candidates", 2: "build-candidates", 3: "build"}

_DEFAULTS = {
    "session_id": "",
    "step": 1,                 # 权威步号（服务端 transition 推进）
    # **两个正交的版本轴**（2026-09-13）：
    #   revision          —— CAS 唯一依据，任何 canonical 写都 +1（内容写 + 流程元数据写）；
    #   content_revision  —— **只有** world/storyline/characters 的语义内容真变了才 +1。
    # 混用一个数的后果：用户点一下"确认本阶段"就把刚通过的校验判失效，小说一个字没变。
    "revision": 0,
    "content_revision": 0,
    "content_digest": "",      # 当前草稿的语义摘要（plan_diff.semantic_digest）
    # 规划阶段状态机（阶段/失效/锁定/对镜证据/校验回执），权威定义见 libraries/build_phases.py
    "plan_meta": None,
    "idea": "",
    "tags": [],
    "pen_name": "",
    # 候选卡列表（set_candidates 整体替换 / add_candidate 增量追加，服务端副本）
    "candidates": [],
    # 用户在步 2 选定的候选快照（服务端持久化，**不依赖浏览器 window 状态**）
    "selected_candidate": None,
    # 步 3 内容草稿：{world: {...}, storyline: {...}, characters: [...]}
    "draft": None,
    "created": False,
    "book_id": "",
    "submit_error": "",
    "updated_at": "",
}


class StaleRevision(Exception):
    """CAS 失败：调用方拿的 revision 已过期（在**锁内**判定，见 update()）。"""

    def __init__(self, current: int, expected: int):
        super().__init__(f"revision_conflict: 期望 {expected}，当前 {current}")
        self.current = int(current)
        self.expected = int(expected)


# 世界观里属于**表单顶层**而非 world_building 本体的键（start_book.html 的 set_world
# 处理器、dashboard 的提交读取都是这么拆的）
_WORLD_TOP_KEYS = ("tone", "target_audience", "pov", "era_language")


def normalize_world(world) -> dict | None:
    """把 `draft.world` 归一到**所有消费端都认**的形状（幂等）。

    规范形状 = `{"world_building": {...本体...}, tone?, target_audience?, pov?, era_language?}`。

    为什么非归一不可（2026-09-13 事故）：agent 常把 world_building 本体直接摊在 world 顶层
    （core_conflict/factions/geography 就在 world 顶上）。此前只有校验端容忍这种裸本体，
    另外两个消费端都只认带外层键的形状——于是"校验通过、投影报缺少 world_building、
    成书时世界观被丢掉"。归一到这一个函数，读（load）写（update）两侧都过一遍，
    老记录因此无需迁移。
    """
    if not isinstance(world, dict) or not world:
        return None
    if isinstance(world.get("world_building"), dict):
        return world
    out = {"world_building": {k: v for k, v in world.items() if k not in _WORLD_TOP_KEYS}}
    for k in _WORLD_TOP_KEYS:
        if world.get(k):
            out[k] = world[k]
    return out


def normalize_draft(draft) -> dict | None:
    """归一化整份步 3 草稿（world 包一层；storyline/characters 形状已是最终形态）。"""
    if not isinstance(draft, dict):
        return None
    out = dict(draft)
    out["world"] = normalize_world(draft.get("world"))
    if not isinstance(out.get("storyline"), dict):
        out["storyline"] = None
    if not isinstance(out.get("characters"), list):
        out["characters"] = None
    return out


def _safe_sid(session_id: str) -> str:
    safe = "".join(c for c in str(session_id or "") if c.isalnum() or c in "-_")
    if not safe:
        raise ValueError("build_session_id 不能为空")
    return safe


def path_for(session_id: str) -> str:
    return os.path.join(_DIR, f"{_safe_sid(session_id)}.json")


def _coerce(data: dict) -> dict:
    """补默认 + 清洗（与 build_status 同形：别把 _DEFAULTS 里的可变对象递出去）。"""
    out = dict(_DEFAULTS)
    for k, v in (data or {}).items():
        if k in _DEFAULTS:
            out[k] = v
    out["session_id"] = str(out.get("session_id") or "")
    out["step"] = int(out.get("step") or 1)
    out["revision"] = int(out.get("revision") or 0)
    out["content_revision"] = int(out.get("content_revision") or 0)
    out["content_digest"] = str(out.get("content_digest") or "")
    out["plan_meta"] = build_phases.coerce_meta(out.get("plan_meta"))
    out["tags"] = [t.strip() for t in (out.get("tags") or []) if isinstance(t, str) and t.strip()]
    out["pen_name"] = str(out.get("pen_name") or "")
    out["created"] = bool(out.get("book_id"))
    out["submit_error"] = str(out.get("submit_error") or "")
    out["candidates"] = [c for c in (out.get("candidates") or []) if isinstance(c, dict)]
    if not isinstance(out.get("selected_candidate"), dict):
        out["selected_candidate"] = None
    # 读写两侧都过归一：老记录（裸 world）读出来就是规范形状，无需迁移脚本
    out["draft"] = normalize_draft(out.get("draft"))
    return out


def load(session_id: str) -> dict:
    """读 canonical 记录；无记录返回默认（step=1、revision=0）。重复读不消费。"""
    if not session_id:
        return dict(_DEFAULTS)
    try:
        raw = read_json(path_for(session_id), {}) or {}
    except Exception:  # noqa: BLE001 —— 坏文件不该让建书流程崩，按无记录处理
        raw = {}
    return _coerce(raw)


def exists(session_id: str) -> bool:
    return bool(session_id) and os.path.exists(path_for(session_id))


def _prune(keep_name: str) -> None:
    try:
        files = sorted((os.path.join(_DIR, f) for f in os.listdir(_DIR) if f.endswith(".json")),
                       key=os.path.getmtime, reverse=True)
    except OSError:
        return
    for path in files[MAX_KEPT:]:
        if os.path.basename(path) == keep_name:
            continue
        try:
            os.remove(path)
        except OSError:
            pass


def _bump_content_revision(cur: dict) -> None:
    """语义内容真变了才 +1（digest 不覆盖 revision/plan_meta/时间戳等元数据）。"""
    digest = plan_diff.semantic_digest(cur.get("draft"))
    if digest != str(cur.get("content_digest") or ""):
        cur["content_digest"] = digest
        cur["content_revision"] = int(cur.get("content_revision") or 0) + 1


def update(session_id: str, *, bump: bool = True, expected_revision: int | None = None,
           on_meta=None, **fields) -> dict:
    """合并写 canonical 记录（原子）。`bump=False` 时不动 revision。

    只接受 `_DEFAULTS` 内的键，其余静默丢弃——避免调用方把 UI 专用字段混进来。

    `expected_revision`：**在同一临界区内**做 CAS 比对（此前 `save_build_draft` 先
    `load` 再 `update`，guard 与写入不在同一临界区，并发写会静默丢版本）；不匹配抛
    `StaleRevision`，调用方转成既有的 `revision_conflict` 返回体。

    `on_meta`：可选的 `plan_meta -> plan_meta|None` 变换，在锁内应用——阶段转移与内容
    写入因此是一次原子写，不会出现"内容落了、阶段没推"的中间态。
    """
    path = path_for(session_id)
    os.makedirs(_DIR, exist_ok=True)
    with process_file_lock(path):
        cur = _coerce(read_json(path, {}) or {})
        if expected_revision is not None and int(cur.get("revision") or 0) != int(expected_revision):
            raise StaleRevision(int(cur.get("revision") or 0), int(expected_revision))
        if not cur.get("session_id"):
            cur["session_id"] = _safe_sid(session_id)
        for k, v in fields.items():
            if k in _DEFAULTS and k not in ("session_id", "revision"):
                cur[k] = v
        if "draft" in fields or on_meta is not None:
            # canonical 只存规范形状（裸 world 本体在读写两侧都归一）
            cur["draft"] = normalize_draft(cur.get("draft"))
        if on_meta is not None:
            cur["plan_meta"] = build_phases.coerce_meta(on_meta(cur.get("plan_meta")))
        _bump_content_revision(cur)
        if bump:
            cur["revision"] = int(cur.get("revision") or 0) + 1
        cur["created"] = bool(cur.get("book_id"))
        cur["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        write_json_atomic(path, cur)
    _prune(os.path.basename(path))
    return cur


def transition(session_id: str, *, step: int | None = None, **fields) -> dict:
    """阶段转场（**原子**）：一次写 step + 候选/表单事实，revision +1。

    前端必须拿到本函数成功返回后才允许派发 agent 任务——这正是 2026-09-10 那次
    「先发任务后上报状态」事故的修法：状态没落服务端就不启动依赖它的模型。
    """
    if step is not None:
        step = int(step)
        if step not in STEP_TO_PROFILE:
            raise ValueError(f"未知建书步号: {step}")
        fields["step"] = step
    return update(session_id, **fields)


def mark_submitted(session_id: str, book_id: str = "", error: str = "") -> dict:
    """提交结果回写（`/books/start` 建成功后调用；失败则记 submit_error）。"""
    if book_id:
        return update(session_id, book_id=book_id, submit_error="")
    return update(session_id, submit_error=error or "未知错误")


def profile_for_step(step: int) -> str:
    """步号 → 该步应有的 MCP profile（spawn 不变量的判据之一）。"""
    return STEP_TO_PROFILE.get(int(step or 1), "build-candidates")


def save_candidates(session_id: str, candidates: list, *, append: bool = False) -> dict:
    """候选卡落服务端（`set_candidates` 整体替换 / `add_candidate` 增量追加）。

    为什么服务端也要存：候选此前只活在浏览器 `window.__CANDIDATES__` 里，于是
    「用户选了什么 / 有哪些备选」无法服务端读回，只能靠向导把整段聊天转发给模型
    （本次约 4.2 万 token 的来源）。存下来之后 `/api/build/pick(idx)` 与
    `get_build_context` 的恢复链都不再依赖浏览器。
    """
    cands = [c for c in (candidates or []) if isinstance(c, dict) and c.get("title")]
    if append:
        cands = (load(session_id).get("candidates") or []) + cands
    return update(session_id, candidates=cands)

