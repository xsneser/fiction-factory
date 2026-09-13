"""Web UI 共享上下文 — 全局服务/LLM 客户端/引擎缓存/故事线统一存取。

自 ui/web_ui.py 拆分：所有蓝图模块 `from .ctx import *` 共享同一份状态。
"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Response, stream_with_context

from libraries.plot import PlotLibrary
from libraries.structure import StructureLibrary
from libraries.gag import GagLibrary
from libraries.character import CharacterLibrary
from libraries.style_rules import StyleRuleLibrary
from libraries.profiles import ProfileManager
from libraries.book_manager import BookManager
from libraries.cost_tracker import CostTracker
from libraries.de_ai import DeAIEngine
from libraries.character_state import CharacterStateMachine
from libraries.reviewer import ContentReviewer
from libraries.engine import NovelEngine, BookMode, Op, Instruction
from core.api_config import load_api_config, is_api_configured
from core.llm_client import LLMClient
from core.models import APIConfig
from core.json_store import read_json, write_json_atomic
from core.safe_paths import ensure_child_path, parse_int
from libraries.storyline import (
    BookStoryline, save_storyline, load_storyline, StorylineBuilder,
    get_mc, get_characters, relation_to_mc, normalize_basic_info,
)

log = logging.getLogger("web_ctx")

# ─── 全局服务 ───
plot_lib = PlotLibrary()
struct_lib = StructureLibrary()
gag_lib = GagLibrary()
char_lib = CharacterLibrary()
style_rules = StyleRuleLibrary()   # 风格规则库（禁句式 + 去AI词表，可编辑）
profiles = ProfileManager("profiles")
book_mgr = BookManager("books")

# ─── LLM 客户端 ───
_llm_client = None

def get_llm():
    """共享 LLM 客户端（按 api.json 构造；未配置好则返回 None）。

    配置一律经 core.api_config 读取，不要在这里手写字段 —— 手写必然漏字段。
    """
    global _llm_client
    if _llm_client is not None:
        return _llm_client
    api_cfg = load_api_config()
    if not is_api_configured(api_cfg):
        return None
    _llm_client = LLMClient(api_cfg)
    return _llm_client


def invalidate_llm():
    """清除缓存的 LLM 客户端与引擎实例：设置保存后调用，使下一次请求按新配置重建。

    必须在本模块内改全局（from .ctx import * 只会拷贝引用，外部赋值清不掉缓存）。
    `_engines` 里的 NovelEngine 持有构造时注入的旧 client（DeAIEngine/ContentReviewer 同理），
    只清 `_llm_client` 会让已缓存的那本书继续用旧地址/旧 key —— 这正是"改了设置不生效"的来源。
    """
    global _llm_client
    _llm_client = None
    _engines.clear()


def sse_stream_response(gen):
    """包装 SSE 流式响应（统一 headers，避免各端点重复）"""
    return Response(
        stream_with_context(gen),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


# ─── 引擎实例缓存 ───
_engines: dict[str, NovelEngine] = {}
_storylines: dict[str, dict] = {}  # 故事线配置缓存
_storylines_mtime: dict[str, int] = {}  # 缓存对应的 storyline.json st_mtime_ns（跨进程失效）
_storyline_lock = threading.Lock()  # 保护故事线缓存读写（Flask 多线程）

# ─── 故事线统一存取：每本书的故事线都在书目录内 books/<id>/storyline.json ───

def _storyline_filepath(storyline_id: str) -> str:
    """故事线统一存在书目录内。"""
    return f"books/{storyline_id}/storyline.json"


def _resolve_storyline(storyline_id):
    """从内存缓存或磁盘加载 BookStoryline（缓存按文件 st_mtime_ns 失效）。

    外部 agent（MCP，独立进程）会直接写 storyline.json；若本进程缓存不带 mtime 失效，
    Web 会一直读到陈旧故事线（R3 修订 6：用纳秒 mtime，Windows 下快速连续写入也可靠）。
    """
    import os as _os
    path = _storyline_filepath(storyline_id)
    try:
        stat = _os.stat(path).st_mtime_ns if _os.path.exists(path) else None
    except OSError:
        stat = None
    with _storyline_lock:
        sl = _storylines.get(storyline_id)
        cached_mtime = _storylines_mtime.get(storyline_id)
        if sl is None or stat is None or cached_mtime != stat:
            sl = load_storyline(path)
            if sl:
                _storylines[storyline_id] = sl
                if stat is not None:
                    _storylines_mtime[storyline_id] = stat
            else:
                _storylines.pop(storyline_id, None)
                _storylines_mtime.pop(storyline_id, None)
    return sl


def _save_storyline(sl, storyline_id):
    """写入内存缓存并落盘；结构写入自动推进乐观并发 revision。

    UI 直写路径也必须与 MCP/dsh 侧互斥：加书锁 + 在锁内重新读盘抬 revision（避免两处
    「读旧版本→各写一份→互相覆盖」）。mtime 在**写盘之后**记录，否则记的是写前时间戳，
    下一次读必然 cache miss。
    """
    from libraries.book_lock import BookBusyError, BookLock
    path = _storyline_filepath(storyline_id)
    book_id = str(storyline_id or "")
    lock = BookLock(book_id) if book_id.startswith("book_") else None
    if lock is not None and not lock.acquire(timeout=30.0, purpose="_save_storyline"):
        raise BookBusyError(f"另一进程正在操作这本书，请稍后再试：{book_id}")
    try:
        try:
            disk = load_storyline(path)
            disk_revision = int(getattr(disk, "storyline_revision", 0) or 0) if disk else -1
            if int(getattr(sl, "storyline_revision", 0) or 0) <= disk_revision:
                sl.storyline_revision = disk_revision + 1
        except Exception as e:  # noqa: BLE001
            log.warning("_save_storyline 读盘抬版本失败（按传入版本写入）%s: %s", storyline_id, e)
        save_storyline(sl, path)
        with _storyline_lock:
            _storylines[storyline_id] = sl
            try:
                import os as _os
                _storylines_mtime[storyline_id] = _os.stat(path).st_mtime_ns   # 写盘后记录
            except OSError:
                _storylines_mtime.pop(storyline_id, None)
    finally:
        if lock is not None:
            lock.release()


def _max_id_suffix(ids) -> int:
    """取 id 列表中最大数字后缀，用于 seed StorylineBuilder 计数器防碰撞。"""
    import re as _re
    max_n = 0
    for i in ids:
        m = _re.search(r"_(\d+)$", i or "")
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n


def _seed_builder_counter(builder, ids) -> None:
    """把 StorylineBuilder._counter 抬到现有 id 最大后缀之上，避免 outline_0001/plot_0001 碰撞。"""
    builder._counter = _max_id_suffix(ids)


__all__ = [
    "log",
    "plot_lib", "struct_lib", "gag_lib", "char_lib", "style_rules", "profiles", "book_mgr",
    "get_llm", "invalidate_llm", "sse_stream_response",
    "_engines", "_storylines", "_storyline_lock",
    "_storyline_filepath", "_resolve_storyline", "_save_storyline",
    "_max_id_suffix", "_seed_builder_counter",
    "NovelEngine", "BookMode", "Op", "Instruction",
    "CostTracker", "DeAIEngine", "CharacterStateMachine", "ContentReviewer",
    "LLMClient", "APIConfig",
    "read_json", "write_json_atomic", "ensure_child_path", "parse_int",
    "BookStoryline", "save_storyline", "load_storyline", "StorylineBuilder",
    "get_mc", "get_characters", "relation_to_mc", "normalize_basic_info",
]
