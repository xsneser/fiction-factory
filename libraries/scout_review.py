"""侦察/提取候选快照 — 跨进程共享状态文件。

agent 调 drive_ui(set_review) 把五库候选呈现到 /extract 审查区时，除实时 SSE 外
还落一份持久快照 storage/review_pending.json：页面不在场 / SSE 渲染失败时，
/extract 页轮询 `GET /api/scout/pending-review` 可恢复。镜像 build_status.json 的
「非消费快照」协调模式（重复读不消费；用户确认入库后由 /api/scout/ingest 清空）。
"""
import os
import time

from core.json_store import read_json, write_json_atomic

_PATH = os.path.join("storage", "review_pending.json")


def write_pending_review(data: dict) -> dict:
    """写一份候选审查快照（注入 ts，原子替换）。data 为 set_review 的 args。"""
    payload = dict(data or {})
    payload["ts"] = time.time()
    write_json_atomic(_PATH, payload)
    return payload


def read_pending_review() -> dict:
    """读当前候选快照（无记录返回空 dict）。"""
    return read_json(_PATH, {}) or {}


def clear_pending_review() -> None:
    """清空候选快照（候选已被消费/弃用）。"""
    try:
        if os.path.exists(_PATH):
            os.remove(_PATH)
    except OSError:
        pass
