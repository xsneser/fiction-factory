"""navigate 外部驱动桥 — JSON 意图队列。

外部 agent（MCP）调用 navigate 时，把跳转意图写入 `storage/nav_intent.json`；
Web 浏览器每 ~2.5s 轮询 `GET /api/agent/nav-intents` 消费（取后即清空）。
与内部 agent_loop 的 SSE 路径并存：内部直达 SSE（立即翻页），外部走本队列。

边界：
- intent 带 id，取即删（消费语义）
- TTL 30s：未消费自动过期（外部 agent 已转投它处时不残留脏意图）
- 写入用 write_json_atomic（原子替换，防半写）
"""
import json
import os
import threading
import time
import uuid

from core.json_store import read_json, write_json_atomic

_PATH = os.path.join("storage", "nav_intent.json")
_TTL_S = 30.0
_LOCK = threading.Lock()


def push_nav_intent(url: str, tab: str = "", params: dict = None) -> dict:
    """写入一条导航意图，返回 intent dict（供调用方记录/返回）。"""
    intent = {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "url": url,
        "tab": tab or "",
        "params": params or {},
    }
    with _LOCK:
        items = read_json(_PATH, []) or []
        now = time.time()
        items = [i for i in items if now - (i.get("ts") or 0) <= _TTL_S]
        items.append(intent)
        write_json_atomic(_PATH, items)
    return intent


def take_nav_intents() -> list:
    """取出全部未过期意图并清空队列（浏览器消费语义）。"""
    with _LOCK:
        items = read_json(_PATH, []) or []
        now = time.time()
        fresh = [i for i in items if now - (i.get("ts") or 0) <= _TTL_S]
        write_json_atomic(_PATH, [])
        return fresh
