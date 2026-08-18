"""设置 + 状态任务 + 调试 — 蓝图（自 ui/web_ui.py 按域拆分）。"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, Response, stream_with_context
from .ctx import *

bp = Blueprint("settings", __name__)

@bp.route("/api/status/tasks")
def status_tasks():
    """返回当前运行中的任务列表（供右侧状态栏轮询）"""
    from plugins import task_manager
    tasks = task_manager.get_tasks()
    return jsonify(tasks)


@bp.route("/api/status/tasks/close", methods=["POST"])
def status_tasks_close():
    """关闭/删除指定任务（运行中的任务自动取消，已完成/失败的直接移除）"""
    from plugins import task_manager
    data = request.get_json()
    task_id = data.get("id", "")
    if task_id:
        # 如果还在运行则先取消
        task_manager.cancel(task_id)
        # 从列表移除
        task_manager.remove(task_id)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "missing id"})


def _load_api_config() -> dict:
    """读取 api.json（不存在返回空 dict）"""
    api_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api.json")
    if os.path.exists(api_path):
        return read_json(api_path, {})
    return {}


def _mask_api_key(key: str) -> str:
    """掩码 API key：保留前 8 位，其余用 **** 代替（含 **** 即视为"未修改"）"""
    if not key:
        return ""
    return key[:8] + "****"


@bp.route("/settings")
def settings_page():
    """设置页面 — 纯静态渲染，不发起 API 请求"""
    cfg = _load_api_config()

    # 不调用 LLM API，只检查本地配置是否存在（瞬间完成）
    api_configured = bool(cfg.get("api_key") and cfg.get("base_url"))

    return render_template("settings.html",
        config={
            # 只回传掩码，明文 key 不进入 HTML（防止源码泄露）
            "api_key_masked": _mask_api_key(cfg.get("api_key", "")),
            "base_url": cfg.get("base_url", "https://api.deepseek.com"),
            "model": cfg.get("model", "deepseek-chat"),
            "http_timeout_seconds": cfg.get("http_timeout_seconds", 300),
            "context_budget_tokens": cfg.get("context_budget_tokens", 300000),
            "url_strict": cfg.get("url_strict", False),
        },
        llm_ok=api_configured,
    )


@bp.route("/api/settings/save", methods=["POST"])
def settings_save():
    """保存设置"""
    data = request.json or {}
    api_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api.json")

    # 读取当前配置，只覆盖传入的字段
    cfg = _load_api_config()

    # API Key 掩码值（含 ****）表示未修改，保留已保存的原 key
    new_key = data.get("api_key", "")
    if new_key and "****" in new_key:
        data.pop("api_key", None)

    for key in ("api_key", "base_url", "model", "url_strict",
                "http_timeout_seconds", "context_budget_tokens"):
        if key in data:
            cfg[key] = data[key]

    cfg["http_timeout_seconds"] = parse_int(
        cfg.get("http_timeout_seconds"), 300, min_value=5, max_value=1800)
    cfg["context_budget_tokens"] = parse_int(
        cfg.get("context_budget_tokens"), 300000, min_value=8000, max_value=2000000)

    # 校验
    if not cfg.get("api_key"):
        return jsonify({"ok": False, "error": "API Key 不能为空"}), 400
    if not cfg.get("base_url"):
        return jsonify({"ok": False, "error": "API 地址不能为空"}), 400

    try:
        write_json_atomic(api_path, cfg)
    except Exception as e:
        return jsonify({"ok": False, "error": f"写入失败: {e}"}), 500

    # 清除缓存的 LLM 客户端
    global _llm_client
    _llm_client = None

    return jsonify({"ok": True, "message": "设置已保存"})


@bp.route("/api/settings/test", methods=["POST"])
def settings_test():
    """测试 LLM 连接"""
    data = request.json or {}

    from core.llm_client import LLMClient
    from core.models import APIConfig

    # 前端传回掩码/空值 → 用当前已保存的 key 测试（避免 key 进入浏览器后回传）
    api_key = data.get("api_key", "")
    if not api_key or "****" in api_key:
        api_key = _load_api_config().get("api_key", "")

    api_cfg = APIConfig(
        api_key=api_key,
        base_url=data.get("base_url", "https://api.deepseek.com"),
        model=data.get("model", "deepseek-chat"),
        http_timeout_seconds=10,
    )

    try:
        client = LLMClient(api_cfg)
        result = client.test_connection()
        if result.get("success"):
            return jsonify({"ok": True, "model": api_cfg.model,
                            "response": result.get("sample", "")[:50]})
        else:
            return jsonify({"ok": False, "error": result.get("error", "连接失败")}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@bp.route("/api/debug/search-test")
def debug_search_test():
    """调试：测试搜索"""
    from plugins.fanqie_scout import FanqieCrawler
    title = request.args.get("q", "这个游戏不对劲，我挖矿成神！")
    try:
        c = FanqieCrawler()
        # 检查文件修改时间
        import os
        mtime = os.path.getmtime(os.path.join(os.path.dirname(__file__), "..", "plugins", "fanqie_scout.py"))
        from datetime import datetime
        mtime_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        
        novel = c.search_novel(title)
        if novel:
            return jsonify({
                "ok": True,
                "title": novel.title,
                "author": novel.author,
                "book_id": novel.book_id,
                "chapters": novel.chapter_count,
                "file_time": mtime_str,
            })
        return jsonify({"ok": False, "message": "not found", "file_time": mtime_str})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "traceback": traceback.format_exc()})



