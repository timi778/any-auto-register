"""Turnstile Solver 进程管理 - 后端启动时自动拉起"""
import signal
import subprocess
import sys
import os
import time
import threading
import requests

_proc: subprocess.Popen = None
_log_file = None
_lock = threading.Lock()


def _solver_enabled() -> bool:
    return os.getenv("APP_ENABLE_SOLVER", "1").lower() not in {"0", "false", "no"}


def _solver_port() -> int:
    return int(os.getenv("SOLVER_PORT", "8889"))


def _solver_url() -> str:
    return (os.getenv("LOCAL_SOLVER_URL") or f"http://127.0.0.1:{_solver_port()}").rstrip("/")


def _solver_bind_host() -> str:
    return os.getenv("SOLVER_BIND_HOST", "0.0.0.0")


def _solver_browser_type() -> str:
    return os.getenv("SOLVER_BROWSER_TYPE", "camoufox")


def _solver_thread() -> str:
    return os.getenv("SOLVER_THREAD", "").strip()


def _solver_start_timeout_seconds() -> int:
    try:
        return max(5, int(os.getenv("SOLVER_START_TIMEOUT", "120")))
    except Exception:
        return 120


def _solver_stop_timeout_seconds() -> int:
    try:
        return max(1, int(os.getenv("SOLVER_STOP_TIMEOUT", "10")))
    except Exception:
        return 10


def _solver_kill_timeout_seconds() -> int:
    try:
        return max(1, int(os.getenv("SOLVER_KILL_TIMEOUT", "5")))
    except Exception:
        return 5


def _solver_log_path() -> str:
    runtime_dir = os.getenv("APP_RUNTIME_DIR", "").strip()
    if runtime_dir:
        return os.path.join(runtime_dir, "logs", "solver.log")
    return os.path.join(os.path.dirname(__file__), "turnstile_solver", "solver.log")


def _tail_text_file(path: str, max_lines: int) -> str:
    try:
        if max_lines <= 0:
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if len(lines) <= max_lines:
            return "".join(lines)
        return "".join(lines[-max_lines:])
    except Exception:
        return ""



def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            return
        except Exception:
            pass
    try:
        proc.terminate()
    except Exception:
        pass


def _kill_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def is_running() -> bool:
    try:
        r = requests.get(f"{_solver_url()}/", timeout=2)
        return r.status_code < 500
    except Exception:
        return False


def start():
    global _proc, _log_file
    with _lock:
        if not _solver_enabled():
            print("[Solver] 已禁用，跳过自动启动")
            return
        if is_running():
            print("[Solver] 已在运行")
            return
        solver_script = os.path.join(
            os.path.dirname(__file__), "turnstile_solver", "start.py"
        )
        log_path = _solver_log_path()
        stdout_target = None
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            _log_file = open(log_path, "a", encoding="utf-8")
            stdout_target = _log_file
        except Exception:
            _log_file = None
        cmd = [
            sys.executable,
            "-u",
            solver_script,
            "--browser_type",
            _solver_browser_type(),
            "--host",
            _solver_bind_host(),
            "--port",
            str(_solver_port()),
        ]
        solver_thread = _solver_thread()
        if solver_thread:
            cmd.extend(["--thread", solver_thread])

        _proc = subprocess.Popen(
            cmd,
            stdout=stdout_target,
            stderr=subprocess.STDOUT if stdout_target else None,
            **_popen_kwargs(),
        )
        start_timeout = _solver_start_timeout_seconds()
        deadline = time.time() + start_timeout
        # 等待服务就绪
        while time.time() < deadline:
            time.sleep(1)
            if is_running():
                print(f"[Solver] 已启动 PID={_proc.pid}")
                return
            if _proc.poll() is not None:
                print(f"[Solver] 启动失败，退出码={_proc.returncode}，日志: {log_path}")
                tail = _tail_text_file(log_path, 120)
                if tail:
                    print("[Solver] solver.log 末尾内容：")
                    print(tail)
                _proc = None
                if _log_file:
                    _log_file.close()
                    _log_file = None
                return
        if _proc and _proc.poll() is None:
            print(f"[Solver] 启动超时（仍在初始化）PID={_proc.pid}，日志: {log_path}")
        else:
            print(f"[Solver] 启动超时，日志: {log_path}")


def stop():
    global _proc, _log_file
    with _lock:
        proc = _proc
        log_file = _log_file
        _proc = None
        _log_file = None

    if proc and proc.poll() is None:
        _terminate_process(proc)
        try:
            proc.wait(timeout=_solver_stop_timeout_seconds())
            print("[Solver] 已停止")
        except subprocess.TimeoutExpired:
            _kill_process(proc)
            try:
                proc.wait(timeout=_solver_kill_timeout_seconds())
            except Exception:
                pass
            print("[Solver] 停止超时，已强制终止")

    if log_file:
        try:
            log_file.close()
        except Exception:
            pass


def start_async():
    """在后台线程启动，不阻塞主进程"""
    t = threading.Thread(target=start, daemon=True)
    t.start()
