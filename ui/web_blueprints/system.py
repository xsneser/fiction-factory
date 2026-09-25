"""Local service lifecycle endpoints used by the global UI controls."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request

from libraries.dsh_bridge import interrupt_current_task
from libraries.token_proxy import PROXY_PORT


bp = Blueprint("system", __name__)
_ROOT = Path(__file__).resolve().parents[2]
_BOOT_ID = uuid.uuid4().hex
_RESTART_DELAY = 1.0
_RESTART_LOCK = threading.Lock()
_RESTART_STARTED = False


def _exit_after_response() -> None:
    """Stop Agent descendants after the restart response has been sent."""
    time.sleep(_RESTART_DELAY)
    try:
        interrupt_current_task()
    finally:
        os._exit(0)  # noqa: WPS437 - intentional process handoff to the detached helper


def _start_restart_helper() -> None:
    helper = _ROOT / "tools" / "restart_service.py"
    if not helper.is_file():
        raise FileNotFoundError(str(helper))

    storage_dir = _ROOT / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    log_file_path = storage_dir / "restart_service.log"
    log_fp = open(log_file_path, "a", encoding="utf-8")

    env = os.environ.copy()
    env["NE_SKIP_BROWSER"] = "1"
    command = [
        sys.executable, str(helper),
        "--pid", str(os.getpid()),
        "--root", str(_ROOT),
        "--web-port", "58080",
        "--proxy-port", str(PROXY_PORT),
    ]
    kwargs = {
        "cwd": str(_ROOT),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": log_fp,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)
    finally:
        log_fp.close()


@bp.route("/api/system/health", methods=["GET"])
def system_health():
    """Return a process identity so the browser can detect a completed restart."""
    response = jsonify({"ok": True, "boot_id": _BOOT_ID})
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


def _local_request() -> bool:
    """Keep the process-control endpoint local even if the app binds broadly."""
    return request.remote_addr in (None, "127.0.0.1", "::1", "localhost")


@bp.route("/api/system/restart", methods=["POST"])
def system_restart():
    """Restart the local backend and the current dsh/MCP process tree."""
    global _RESTART_STARTED
    if not _local_request():
        return jsonify({"ok": False, "error": "local_request_required"}), 403
    if not request.is_json or request.headers.get("X-NovelEngine-Action") != "restart":
        return jsonify({"ok": False, "error": "restart_header_required"}), 400
    if os.environ.get("NOVEL_DEBUG") == "1":
        return jsonify({
            "ok": False,
            "error": "debug_mode_restart_unsupported",
            "message": "请先关闭 NOVEL_DEBUG=1，再执行服务重启。",
        }), 409

    with _RESTART_LOCK:
        if _RESTART_STARTED:
            return jsonify({
                "ok": False,
                "error": "already_restarting",
                "boot_id": _BOOT_ID,
            }), 409
        try:
            _start_restart_helper()
        except Exception as exc:  # noqa: BLE001 - return a bounded local diagnostic
            return jsonify({"ok": False, "error": str(exc)[:300]}), 500
        _RESTART_STARTED = True

    threading.Thread(target=_exit_after_response, daemon=True, name="ne-service-restart").start()
    return jsonify({
        "ok": True,
        "restarting": True,
        "boot_id": _BOOT_ID,
    }), 202
