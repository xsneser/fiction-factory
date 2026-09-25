"""Unit tests for the local service health/restart boundary."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask  # noqa: E402

import ui.web_blueprints.system as system  # noqa: E402
from tools import restart_service  # noqa: E402


def _client():
    app = Flask(__name__)
    app.register_blueprint(system.bp)
    return app.test_client()


def test_health_identity_and_no_store():
    response = _client().get("/api/system/health")
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["boot_id"] == system._BOOT_ID
    assert "no-store" in response.headers["Cache-Control"]


def test_restart_requires_local_json_action_header():
    client = _client()
    assert client.post("/api/system/restart", json={}).status_code == 400
    assert client.post(
        "/api/system/restart",
        json={},
        headers={"X-NovelEngine-Action": "restart"},
        environ_overrides={"REMOTE_ADDR": "192.0.2.10"},
    ).status_code == 403


def test_restart_stops_agent_after_helper_is_detached():
    original_started = system._RESTART_STARTED
    system._RESTART_STARTED = False
    events = []

    class _ImmediateThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    try:
        with patch.object(system, "_start_restart_helper", side_effect=lambda: events.append("helper")), \
             patch.object(system, "interrupt_current_task", side_effect=lambda: events.append("agent")), \
             patch.object(system.os, "_exit", side_effect=lambda code: events.append(("exit", code))), \
             patch.object(system, "_RESTART_DELAY", 0), \
             patch.object(system.threading, "Thread", _ImmediateThread):
            response = _client().post(
                "/api/system/restart",
                json={},
                headers={"X-NovelEngine-Action": "restart"},
            )
        assert response.status_code == 202
        assert response.get_json()["restarting"] is True
        assert events == ["helper", "agent", ("exit", 0)]
    finally:
        system._RESTART_STARTED = original_started


def test_restart_is_idempotent():
    original_started = system._RESTART_STARTED
    system._RESTART_STARTED = True
    try:
        response = _client().post(
            "/api/system/restart",
            json={},
            headers={"X-NovelEngine-Action": "restart"},
        )
        assert response.status_code == 409
        assert response.get_json()["error"] == "already_restarting"
    finally:
        system._RESTART_STARTED = original_started


def test_helper_waits_for_ports_before_launching(tmp_path):
    with patch.object(restart_service, "wait_for_exit_and_ports", return_value=True) as wait, \
         patch.object(restart_service, "launch_service", return_value=Mock()) as launch:
        assert restart_service.restart_service(123, tmp_path, 58080, 58082, 1) == 0
    wait.assert_called_once_with(123, (58080, 58082), 1, root=tmp_path)
    launch.assert_called_once_with(tmp_path)


def test_helper_does_not_launch_when_shutdown_times_out(tmp_path):
    with patch.object(restart_service, "wait_for_exit_and_ports", return_value=False), \
         patch.object(restart_service, "launch_service") as launch:
        assert restart_service.restart_service(123, tmp_path, 58080, 58082, 1) == 2
    launch.assert_not_called()


def test_pid_alive_current_and_invalid():
    import os
    # Current process must be alive
    assert restart_service._pid_alive(os.getpid()) is True
    # Non-positive PID is not alive
    assert restart_service._pid_alive(0) is False
    assert restart_service._pid_alive(-1) is False
    # Huge PID that doesn't exist should be False, not raise
    assert restart_service._pid_alive(4194304) is False


def test_pid_alive_handles_exceptions_gracefully():
    with patch.object(restart_service, "_pid_alive_win32", side_effect=SystemError("mock crash")):
        with patch.object(restart_service.os, "name", "nt"):
            assert restart_service._pid_alive(12345) is False


def test_launch_service_windows_command(tmp_path):
    import subprocess
    launcher = tmp_path / "launch.bat"
    launcher.write_text("@echo off\n", encoding="utf-8")

    with patch.object(restart_service.subprocess, "Popen", return_value=Mock()) as popen:
        with patch.object(restart_service.os, "name", "nt"):
            restart_service.launch_service(tmp_path)
    popen.assert_called_once()
    args, kwargs = popen.call_args
    cmd = args[0]
    assert cmd == ["cmd.exe", "/c", str(launcher), "--no-browser"]
    assert kwargs.get("shell") is not True
    assert kwargs.get("creationflags") == subprocess.CREATE_NEW_CONSOLE
    assert kwargs.get("env", {}).get("NE_SKIP_BROWSER") == "1"


if __name__ == "__main__":
    import tempfile
    test_health_identity_and_no_store()
    test_restart_requires_local_json_action_header()
    test_restart_stops_agent_after_helper_is_detached()
    test_restart_is_idempotent()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        test_helper_waits_for_ports_before_launching(p)
        test_helper_does_not_launch_when_shutdown_times_out(p)
        test_launch_service_windows_command(p)
    test_pid_alive_current_and_invalid()
    test_pid_alive_handles_exceptions_gracefully()
    print("ALL TESTS PASSED in test_system_restart.py")

