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
from core.safe_paths import ensure_child_path, parse_int
from libraries.storyline import (
    BookStoryline, save_storyline, load_storyline, StorylineBuilder,
    get_mc, get_characters, relation_to_mc, normalize_basic_info,
)

# ─── 全局服务 ───
plot_lib = PlotLibrary()
struct_lib = StructureLibrary()
gag_lib = GagLibrary()
char_lib = CharacterLibrary()
profiles = ProfileManager("profiles")
book_mgr = BookManager("books")

# ─── LLM 客户端 ───
_llm_client = None

def get_llm():
    global _llm_client
    if _llm_client is not None:
        return _llm_client
    api_path = os.path.join(_REPO_ROOT, "api.json")
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


def invalidate_llm():
    """清除缓存的 LLM 客户端：设置保存后调用，使下一次 get_llm() 按新配置重建。

    必须在本模块内改全局（from .ctx import * 只会拷贝引用，外部赋值清不掉缓存）。
    """
    global _llm_client
    _llm_client = None


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
_storyline_lock = threading.Lock()  # 保护故事线缓存读写（Flask 多线程）

# ─── 故事线统一存取：每本书的故事线都在书目录内 books/<id>/storyline.json ───

def _storyline_filepath(storyline_id: str) -> str:
    """故事线统一存在书目录内。"""
    return f"books/{storyline_id}/storyline.json"


def _resolve_storyline(storyline_id):
    """从内存缓存或磁盘加载 BookStoryline。"""
    with _storyline_lock:
        sl = _storylines.get(storyline_id)
        if sl is None:
            sl = load_storyline(_storyline_filepath(storyline_id))
            if sl:
                _storylines[storyline_id] = sl
    return sl


def _save_storyline(sl, storyline_id):
    """写入内存缓存并落盘。"""
    with _storyline_lock:
        _storylines[storyline_id] = sl
    save_storyline(sl, _storyline_filepath(storyline_id))


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
    "plot_lib", "struct_lib", "gag_lib", "char_lib", "profiles", "book_mgr",
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
