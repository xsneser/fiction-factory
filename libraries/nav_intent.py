"""外部驱动桥 — JSON 意图队列（导航 + 建书向导 UI 命令）。

外部 agent（MCP）调用 navigate / drive_ui 时，把跳转/UI 命令意图写入
`storage/nav_intent.json`；Web 浏览器每 ~2.5s 轮询 `GET /api/agent/nav-intents` 消费（取后即清空）。
（内置 agent 已删，此队列是 navigate/drive_ui 的唯一消费通道。）

边界：
- intent 带 id，取即删（消费语义）
- TTL 30s：未消费自动过期（外部 agent 已转投它处时不残留脏意图）
- 写入用 write_json_atomic（原子替换，防半写）
- kind 区分：navigate（切页）/ ui_command（建书向导填表/点下一步，由 start_book.html 的 onnecommand 执行）
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


def _push(intent: dict) -> dict:
    """写入一条意图（TTL 过滤 + 原子追加），返回 intent dict。"""
    with _LOCK:
        items = read_json(_PATH, []) or []
        now = time.time()
        items = [i for i in items if now - (i.get("ts") or 0) <= _TTL_S]
        items.append(intent)
        write_json_atomic(_PATH, items)
    return intent


def push_nav_intent(url: str, tab: str = "", params: dict = None) -> dict:
    """写入一条导航意图（kind=navigate），返回 intent dict。"""
    return _push({
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "url": url,
        "tab": tab or "",
        "params": params or {},
        "kind": "navigate",
    })


def push_ui_command(cmd: str, args: dict = None) -> dict:
    """写入一条 UI 命令意图（kind=ui_command，建书向导桥）。

    cmd ∈ drive_ui 白名单（agent_tools._WIZARD_CMDS）；args 传给浏览器
    start_book.html 的 window.onnecommand 执行。
    """
    return _push({
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "url": "",
        "tab": "",
        "kind": "ui_command",
        "cmd": cmd,
        "args": args or {},
    })


def take_nav_intents() -> list:
    """取出全部未过期意图并清空队列（浏览器消费语义）。"""
    with _LOCK:
        items = read_json(_PATH, []) or []
        now = time.time()
        fresh = [i for i in items if now - (i.get("ts") or 0) <= _TTL_S]
        write_json_atomic(_PATH, [])
        return fresh
