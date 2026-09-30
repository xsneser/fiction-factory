"""Detached helper used by the local Web UI restart action.

The Flask process cannot safely replace itself while it is serving the restart
request.  This helper survives that process, waits for it to exit, then starts
the normal launcher in a fresh, visible console window.
"""
from __future__ import annotations

import argparse
import datetime
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


_WAIT_TIMEOUT = 30.0
_POLL_INTERVAL = 0.3


def _log(msg: str, root: Path | None = None) -> None:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    if root:
        log_file = root / "storage" / "restart_service.log"
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass


def _pid_alive_win32(pid: int) -> bool:
    """Check process existence on Windows using Win32 API handles."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        WAIT_TIMEOUT = 0x00000102

        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        h = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid))
        if not h:
            err = kernel32.GetLastError()
            # ERROR_ACCESS_DENIED (5) means the process exists but permissions restrict access
            return err == 5
        try:
            return kernel32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return False


def _pid_alive_posix(pid: int) -> bool:
    """Check process existence on POSIX using signal 0."""
    import errno

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as err:
        if err.errno == errno.ESRCH:
            return False
        if err.errno == errno.EPERM:
            return True
        return False
    except Exception:
        return False


def _pid_alive(pid: int) -> bool:
    """Return whether *pid* still exists, without requiring psutil."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            return _pid_alive_win32(pid)
        return _pid_alive_posix(pid)
    except Exception:
        return False


def wait_for_exit(pid: int, timeout: float = _WAIT_TIMEOUT, root: Path | None = None) -> bool:
    """Wait until a process exits; return False if it remains alive."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while _pid_alive(pid):
        if time.monotonic() >= deadline:
            _log(f"[WARN] wait_for_exit timed out for PID {pid}", root)
            return False
        time.sleep(_POLL_INTERVAL)
    _log(f"[OK] Process PID {pid} has exited", root)
    return True


def _port_is_free(port: int) -> bool:
    """Check whether a loopback TCP port is released and connectable."""
    if not 1 <= int(port) <= 65535:
        return False
    # Check 1: connection must fail (nothing actively listening)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        if s.connect_ex(("127.0.0.1", int(port))) == 0:
            return False
    # Check 2: normal bind attempt
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def wait_for_exit_and_ports(
    pid: int, ports: tuple[int, ...], timeout: float = _WAIT_TIMEOUT, root: Path | None = None
) -> bool:
    """Wait for the old process and all service ports to become available."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        pid_done = not _pid_alive(pid)
        ports_free = all(_port_is_free(port) for port in ports)
        if pid_done and ports_free:
            _log(f"[OK] Old PID {pid} gone and ports {ports} are free", root)
            return True
        if time.monotonic() >= deadline:
            _log(f"[WARN] wait_for_exit_and_ports timed out: pid_alive={_pid_alive(pid)} ports={ports}", root)
            return False
        time.sleep(_POLL_INTERVAL)


def launch_service(root: Path) -> subprocess.Popen:
    """Start the repository launcher without opening another browser window."""
    _log(f"[INFO] Launching service from {root}...", root)
    env = os.environ.copy()
    env["NE_SKIP_BROWSER"] = "1"

    if os.name == "nt":
        launcher = root / "launch.bat"
        if not launcher.is_file():
            _log(f"[ERROR] Launcher not found: {launcher}", root)
            raise FileNotFoundError(str(launcher))

        cmd = ["cmd.exe", "/c", str(launcher), "--no-browser"]
        _log(f"[INFO] Spawning Windows launcher: {' '.join(cmd)}", root)
        return subprocess.Popen(
            cmd,
            cwd=str(root),
            env=env,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            close_fds=True,
        )

    launcher = root / "launch.sh"
    if not launcher.is_file():
        _log(f"[ERROR] Launcher not found: {launcher}", root)
        raise FileNotFoundError(str(launcher))
    cmd = ["bash", str(launcher)]
    _log(f"[INFO] Spawning POSIX launcher: {' '.join(cmd)}", root)
    return subprocess.Popen(
        cmd,
        cwd=str(root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def restart_service(
    pid: int,
    root: Path,
    web_port: int = 58080,
    proxy_port: int = 58082,
    timeout: float = _WAIT_TIMEOUT,
) -> int:
    """Wait for the old service and launch its replacement."""
    _log(f"[START] restart_service requested for PID={pid}, root={root}, ports=({web_port}, {proxy_port})", root)
    if not wait_for_exit_and_ports(pid, (web_port, proxy_port), timeout, root=root):
        _log(f"[ERROR] Failed waiting for PID={pid} or ports to clear. Aborting restart.", root)
        return 2
    try:
        launch_service(root)
        _log("[SUCCESS] Replacement service launched successfully.", root)
        return 0
    except Exception as exc:
        _log(f"[FATAL] launch_service raised exception: {exc}", root)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--web-port", type=int, default=58080)
    parser.add_argument("--proxy-port", type=int, default=58082)
    parser.add_argument("--timeout", type=float, default=_WAIT_TIMEOUT)
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve()
        return restart_service(args.pid, root, args.web_port, args.proxy_port, args.timeout)
    except Exception as exc:
        _log(f"[ERROR] Helper main execution failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
