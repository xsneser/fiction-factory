"""Web UI 共享上下文 — 全局服务/LLM 客户端/引擎缓存/时间线统一存取。

自 ui/web_ui.py 拆分：所有蓝图模块 `from .ctx import *` 共享同一份状态。
"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Response, stream_with_context

from libraries.plot import PlotLibrary
from libraries.structure import StructureLibrary
from libraries.gag import GagLibrary
from libraries.theme import ThemeLibrary
from libraries.profiles import ProfileManager
from libraries.book_manager import BookManager
from libraries.cost_tracker import CostTracker
from libraries.de_ai import DeAIEngine
from libraries.character_state import CharacterStateMachine
from libraries.reviewer import ContentReviewer
from libraries.engine import NovelEngine, BookMode, Op, Instruction
from core.llm_client import LLMClient
from core.models import APIConfig
from core.json_store import read_json, write_json_atomic
from core.safe_paths import ensure_child_path, is_safe_timeline_id, parse_int
from libraries.timeline import (
    BookTimeline, save_timeline, load_timeline, TimelineBuilder,
)

# ─── 全局服务 ───
plot_lib = PlotLibrary()
struct_lib = StructureLibrary()
gag_lib = GagLibrary()
theme_lib = ThemeLibrary()
profiles = ProfileManager("profiles")
book_mgr = BookManager("books")

# ─── LLM 客户端 ───
_llm_client = None

def get_llm():
    global _llm_client
    if _llm_client is not None:
        return _llm_client
    api_path = "api.json"
    if os.path.exists(api_path):
        cfg = read_json(api_path, {})
        api_cfg = APIConfig(
            api_key=cfg.get("api_key",""),
            base_url=cfg.get("base_url","https://api.deepseek.com"),
            model=cfg.get("model","deepseek-chat"),
            http_timeout_seconds=cfg.get("http_timeout_seconds",300),
            # verify_ssl 跟随 api.json：默认开启；旧证书环境可显式设为 false
            verify_ssl=cfg.get("verify_ssl", True),
        )
        _llm_client = LLMClient(api_cfg)
        return _llm_client
    return None


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
_timelines: dict[str, dict] = {}  # 时间线配置缓存
_timeline_lock = threading.Lock()  # 保护时间线缓存读写（Flask 多线程）

# ─── 时间线统一存取：草稿(tl_*) 与正式书(book_*) 两套 id 分派 ───

def _timeline_filepath(timeline_id: str) -> str:
    """按 id 前缀把时间线分派到磁盘路径：正式书在书目录内，草稿在 books/timelines/。"""
    if timeline_id.startswith("book_"):
        return f"books/{timeline_id}/timeline.json"
    return f"books/timelines/{timeline_id}.json"


def _resolve_timeline(timeline_id):
    """从内存缓存或磁盘加载 BookTimeline，兼容 tl_* 与 book_*。"""
    with _timeline_lock:
        tl = _timelines.get(timeline_id)
        if tl is None:
            tl = load_timeline(_timeline_filepath(timeline_id))
            if tl:
                _timelines[timeline_id] = tl
    return tl


def _save_timeline(tl, timeline_id):
    """写入内存缓存并落盘。"""
    with _timeline_lock:
        _timelines[timeline_id] = tl
    save_timeline(tl, _timeline_filepath(timeline_id))


def _max_id_suffix(ids) -> int:
    """取 id 列表中最大数字后缀，用于 seed TimelineBuilder 计数器防碰撞。"""
    import re as _re
    max_n = 0
    for i in ids:
        m = _re.search(r"_(\d+)$", i or "")
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n


def _seed_builder_counter(builder, ids) -> None:
    """把 TimelineBuilder._counter 抬到现有 id 最大后缀之上，避免 outline_0001/plot_0001 碰撞。"""
    builder._counter = _max_id_suffix(ids)


__all__ = [
    "plot_lib", "struct_lib", "gag_lib", "theme_lib", "profiles", "book_mgr",
    "get_llm", "sse_stream_response",
    "_engines", "_timelines", "_timeline_lock",
    "_timeline_filepath", "_resolve_timeline", "_save_timeline",
    "_max_id_suffix", "_seed_builder_counter",
    "NovelEngine", "BookMode", "Op", "Instruction",
    "CostTracker", "DeAIEngine", "CharacterStateMachine", "ContentReviewer",
    "LLMClient", "APIConfig",
    "read_json", "write_json_atomic", "ensure_child_path", "is_safe_timeline_id", "parse_int",
    "BookTimeline", "save_timeline", "load_timeline", "TimelineBuilder",
]
