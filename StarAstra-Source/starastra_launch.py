#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""StarAstra smart launcher — один клик: движок + UI.

Логика:
  1. Движок: sentinel.pid -> psutil (жив?) -> IPC ping. Stale pid удаляется.
  2. Движок не запущен -> Popen(python starastra_engine_bootstrap.py --standalone),
     запись PID, ожидание IPC-готовности до 10 c (0.5 c шаг). Не готов -> exit(1).
  3. UI: lock-файл %TEMP%\\starastra_ui.lock. Занят живым UI -> фокус существующего
     окна (ctypes FindWindowW/SetForegroundWindow). Свободен -> запуск dashboard.
  4. Каждый шаг пишется в data/launcher.log.

Режимы:  (без флагов) движок + UI;  --engine-only только движок (автозапуск);
         --status показать состояние;  --autostart on|off|status|openwin|engine-only
         управление записью в HKCU ...\\Run (используется галочками в Настройках).
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import subprocess
import sys
import tempfile
import time

PROJECT_ROOT = (os.path.dirname(os.path.abspath(sys.executable))
                if getattr(sys, "frozen", False)
                else os.path.dirname(os.path.abspath(__file__)))
ENGINE_DATA_DIR = os.path.join(PROJECT_ROOT, "sentinel_pkg", "sentinel", "data")
ENGINE_PID_FILE = os.path.join(ENGINE_DATA_DIR, "sentinel.pid")
ENGINE_BOOTSTRAP = os.path.join(PROJECT_ROOT, "starastra_engine_bootstrap.py")
ENGINE_LOG_FILE = os.path.join(PROJECT_ROOT, "data", "engine.log")
DASHBOARD = os.path.join(PROJECT_ROOT, "Sentinel", "ui", "starastra_dashboard.py")
VBS_UI = os.path.join(PROJECT_ROOT, "StarAstra.vbs")
VBS_ENGINE = os.path.join(PROJECT_ROOT, "StarAstraEngine.vbs")
UI_LOCK_FILE = os.path.join(tempfile.gettempdir(), "starastra_ui.lock")
LOG_FILE = os.path.join(PROJECT_ROOT, "data", "launcher.log")
WINDOW_TITLE = "StarAstra — Sentinel Antivirus"

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "StarAstra"

# cmdline-маркеры процессов движка (в frozen — воркер нашего exe)
ENGINE_MARKERS = ("starastra_engine_bootstrap", "sentinel.service",
                  "--star-worker=engine")

CREATE_NO_WINDOW = 0x08000000

# ═══════════════════════════ Лог ═══════════════════════════
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logger = logging.getLogger("starastra.launch")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    _fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _fh.setFormatter(_fmt)
    logger.addHandler(_fh)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    logger.addHandler(_sh)


def _python_exe() -> str:
    """venv-питон, если есть; иначе текущий интерпретатор."""
    cand = os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")
    return cand if os.path.isfile(cand) else sys.executable


def _pythonw_exe() -> str | None:
    cand = os.path.join(PROJECT_ROOT, ".venv", "Scripts", "pythonw.exe")
    return cand if os.path.isfile(cand) else None


def _worker_exe() -> str | None:
    """Консольный воркер-exe рядом с основным (только frozen; для ролей,
    печатающих в stdout: engine/watchdog/scan/snippet)."""
    if not getattr(sys, "frozen", False):
        return None
    cand = os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                        "StarAstraWorker.exe")
    return cand if os.path.isfile(cand) else None


def _engine_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(PROJECT_ROOT, "sentinel_pkg"),
         os.path.join(PROJECT_ROOT, "Sentinel", "_internal")])
    return env


# ═══════════════════════ Движок ═══════════════════════
def read_pid_file() -> int | None:
    try:
        with open(ENGINE_PID_FILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def write_pid_file(pid: int) -> None:
    try:
        os.makedirs(ENGINE_DATA_DIR, exist_ok=True)
        with open(ENGINE_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(pid))
    except OSError as exc:
        logger.warning("write pid file failed: %s", exc)


def remove_pid_file() -> None:
    try:
        if os.path.isfile(ENGINE_PID_FILE):
            os.remove(ENGINE_PID_FILE)
            logger.info("stale sentinel.pid removed (%s)", ENGINE_PID_FILE)
    except OSError as exc:
        logger.warning("remove pid file failed: %s", exc)


def pid_is_engine(pid: int | None) -> bool:
    """Жив ли процесс и является ли он sentinel.service."""
    if not pid:
        return False
    try:
        import psutil
        if not psutil.pid_exists(pid):
            return False
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.info.get("cmdline") or []).lower()
        return any(m in cmdline for m in ENGINE_MARKERS)
    except Exception:
        return False


def ipc_ping(timeout_proc: float = 6.0) -> bool:
    """Штатный IPC: NamedPipeClient.get_status() == 'ok'."""
    try:
        for p in (os.path.join(PROJECT_ROOT, "sentinel_pkg"),
                  os.path.join(PROJECT_ROOT, "Sentinel", "_internal")):
            if p not in sys.path:
                sys.path.insert(0, p)
        from sentinel.platform.ipc import create_ipc_client  # noqa: E402
        st = create_ipc_client().get_status()
        return isinstance(st, dict) and st.get("status") == "ok"
    except Exception:
        return False


def find_engine_pid() -> int | None:
    """Найти PID живого движка сканом процессов (psutil)."""
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                if any(m in cmdline for m in ENGINE_MARKERS):
                    return proc.info["pid"]
            except Exception:
                continue
    except Exception:
        pass
    return None


def ensure_engine() -> bool:
    """Гарантировать работающий движок. False -> запустить не удалось."""
    pid = read_pid_file()
    if pid_is_engine(pid):
        logger.info("engine already running (pid=%s, by pid-file)", pid)
        return True
    if pid and not pid_is_engine(pid):
        remove_pid_file()  # stale sentinel.pid — процесс мёртв
    if ipc_ping():
        logger.info("engine already running (ipc ok, pid-file missing/stale)")
        found = find_engine_pid()
        if found:
            write_pid_file(found)  # восстановить pid-файл
        return True

    if getattr(sys, "frozen", False):
        # frozen: движок — консольный воркер нашего exe
        # (stdout -> engine_stdout.log, см. комментарий ниже)
        worker = _worker_exe()
        if not worker:
            logger.error("worker exe missing next to %s", sys.executable)
            return False
        cmd = [worker, "--star-worker=engine", "--standalone"]
    else:
        python = _python_exe()
        if not os.path.isfile(ENGINE_BOOTSTRAP):
            logger.error("bootstrap missing: %s", ENGINE_BOOTSTRAP)
            return False
        cmd = [python, ENGINE_BOOTSTRAP, "--standalone"]
    logger.info("starting engine: %s", " ".join(cmd))
    # stdout/stderr движка (print, необработанные трейсбеки) ->
    # data/engine_stdout.log. НЕ в engine.log: там пишет logging движка
    # (root-logger FileHandler), а CRT-append («seek to end перед write»)
    # не атомарен — два хендлера перезаписывали друг другу начало строк
    # (наблюдалось: FsSensor-принт стирал первые ~58 байт guard-строки).
    # Разные файлы = один писатель на файл = ноль гонок.
    log_fh = None
    try:
        stdout_log = os.path.join(os.path.dirname(ENGINE_LOG_FILE),
                                  "engine_stdout.log")
        os.makedirs(os.path.dirname(stdout_log), exist_ok=True)
        log_fh = open(stdout_log, "ab")
    except OSError as exc:
        logger.warning("engine_stdout.log unavailable, engine output lost: %s",
                       exc)
    try:
        proc = subprocess.Popen(
            cmd, cwd=PROJECT_ROOT, env=_engine_env(),
            creationflags=CREATE_NO_WINDOW,
            stdout=log_fh if log_fh else subprocess.DEVNULL,
            stderr=subprocess.STDOUT)
    except Exception as exc:
        logger.error("engine Popen failed: %s", exc)
        print(f"[StarAstra] failed to start engine: {exc}", file=sys.stderr)
        return False
    finally:
        if log_fh:
            log_fh.close()  # дочерний процесс сохранит унаследованный хэндл
    write_pid_file(proc.pid)
    logger.info("engine spawned pid=%s, waiting for IPC...", proc.pid)

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            logger.error("engine exited early rc=%s", proc.returncode)
            print(f"[StarAstra] engine exited with code {proc.returncode}",
                  file=sys.stderr)
            return False
        if ipc_ping():
            logger.info("engine ready (pid=%s) after ~%.1fs",
                        proc.pid, 10.0 - (deadline - time.monotonic()))
            return True
        time.sleep(0.5)
    logger.error("engine not ready in 10s (pid=%s)", proc.pid)
    print("[StarAstra] engine did not become ready within 10 s", file=sys.stderr)
    return False


# ═══════════════════════ UI ═══════════════════════
def _lock_pid() -> int | None:
    try:
        with open(UI_LOCK_FILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _acquire_ui_lock() -> bool:
    """Создать lock (O_EXCL). Если файл есть от мёртвого UI — перехватить."""
    try:
        fd = os.open(UI_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        old = _lock_pid()
        try:
            import psutil
            alive = bool(old) and psutil.pid_exists(old)
        except Exception:
            alive = bool(old)
        if alive:
            return False  # UI реально работает
        logger.info("stale UI lock (pid=%s) — taking over", old)
        try:
            fd = os.open(UI_LOCK_FILE, os.O_TRUNC | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except OSError:
            return False


def focus_existing_window() -> bool:
    """Вытащить окно StarAstra на передний план (ctypes, без pywin32)."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            return False
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception as exc:
        logger.warning("focus window failed: %s", exc)
        return False


def _ui_env() -> dict[str, str]:
    """Окружение для UI-процесса: БЕЗ PYTHONPATH (Sentinel\\_internal затеняет
    Tcl 8.6.12/8.6.15 — UI умирает молча под pythonw; см. EICAR_DIAGNOSIS.md)."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def ensure_ui() -> str:
    """Запустить UI или сфокусировать существующий. Возвращает 'started'|'focused'|'busy'."""
    if not _acquire_ui_lock():
        logger.info("UI already running (lock held by pid=%s)", _lock_pid())
        if focus_existing_window():
            logger.info("existing UI window focused")
            return "focused"
        print("[StarAstra] UI already running", file=sys.stderr)
        return "busy"

    if getattr(sys, "frozen", False):
        # frozen: UI — тот же windowed-exe с ролью ui (консоль не нужна)
        cmd = [sys.executable, "--star-worker=ui"]
        flags = CREATE_NO_WINDOW
    else:
        pythonw = _pythonw_exe()
        if pythonw:
            cmd = [pythonw, DASHBOARD]
            flags = 0
        else:
            cmd = [_python_exe(), DASHBOARD]
            flags = CREATE_NO_WINDOW
    logger.info("starting UI: %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT, env=_ui_env(),
                                creationflags=flags)
    except Exception as exc:
        logger.error("UI Popen failed: %s", exc)
        print(f"[StarAstra] failed to start UI: {exc}", file=sys.stderr)
        _release_ui_lock()
        return "busy"
    # переписать lock на PID реального UI-процесса (launcher вскоре завершится)
    try:
        with open(UI_LOCK_FILE, "w", encoding="utf-8") as f:
            f.write(str(proc.pid))
    except OSError:
        pass
    logger.info("UI started pid=%s", proc.pid)
    return "started"


def release_ui_lock() -> None:
    """Удалить UI-lock (только если он наш). Вызывается UI при закрытии."""
    try:
        if _lock_pid() == os.getpid() and os.path.isfile(UI_LOCK_FILE):
            os.remove(UI_LOCK_FILE)
    except OSError:
        pass


_release_ui_lock = release_ui_lock  # совместимость со старым именем


# ═══════════════════════ Автозапуск (HKCU Run) ═══════════════════════
def autostart_value() -> str | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as k:
            val, _typ = winreg.QueryValueEx(k, RUN_VALUE_NAME)
            return str(val)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def autostart_enabled() -> bool:
    return autostart_value() is not None


def autostart_opens_window() -> bool:
    val = autostart_value() or ""
    if getattr(sys, "frozen", False):
        return "--engine-only" not in val      # exe без флага = движок + UI
    return "StarAstra.vbs" in val and "Engine" not in val


def set_autostart(enabled: bool, open_window: bool = False) -> bool:
    """Вкл/выкл автозапуск. open_window=False -> только движок (Engine.vbs)."""
    import winreg
    try:
        if not enabled:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH,
                                    0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, RUN_VALUE_NAME)
                logger.info("autostart disabled (Run value deleted)")
            except FileNotFoundError:
                pass
            return True
        if getattr(sys, "frozen", False):
            # frozen: сам exe; без окна — флаг --engine-only
            data = f'"{sys.executable}"' + \
                ("" if open_window else " --engine-only")
        else:
            vbs = VBS_UI if open_window else VBS_ENGINE
            data = f'wscript.exe "{vbs}"'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH,
                            0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, RUN_VALUE_NAME, 0, winreg.REG_SZ, data)
        logger.info("autostart enabled: %s", data)
        return True
    except OSError as exc:
        logger.error("set_autostart failed: %s", exc)
        return False


# ═══════════════════════ Watchdog ═══════════════════════
WATCHDOG = os.path.join(PROJECT_ROOT, "starastra_watchdog.py")


def ensure_watchdog() -> bool:
    """Запустить сторож движка (одиночный экземпляр), если ещё не запущен."""
    try:
        import psutil
        me = os.getpid()
        for proc in psutil.process_iter(["pid", "cmdline"]):
            if proc.info["pid"] == me:
                continue
            cmdline = " ".join(proc.info["cmdline"] or [])
            if ("starastra_watchdog.py" in cmdline
                    or "--star-worker=watchdog" in cmdline):
                logger.info("watchdog already running (pid=%s)", proc.info["pid"])
                return True
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        worker = _worker_exe()
        if not worker:
            logger.warning("worker exe missing, watchdog not started")
            return False
        cmd = [worker, "--star-worker=watchdog"]
    else:
        cmd = [_python_exe(), WATCHDOG]
    try:
        proc = subprocess.Popen(
            cmd, cwd=PROJECT_ROOT, env=_engine_env(),
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("watchdog started pid=%s", proc.pid)
        return True
    except Exception as exc:
        logger.warning("watchdog start failed: %s", exc)
        return False


# ═══════════════════════ Точка входа ═══════════════════════
def _print_status() -> None:
    pid = read_pid_file()
    print(f"pid-file: {pid} alive={pid_is_engine(pid)}")
    print(f"ipc ping: {ipc_ping()}")
    lock_pid = _lock_pid()
    print(f"ui lock: {UI_LOCK_FILE} pid={lock_pid}")
    print(f"autostart: {autostart_value() or 'off'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="StarAstra launcher")
    ap.add_argument("--engine-only", action="store_true",
                    help="запустить только движок (без UI), для автозапуска")
    ap.add_argument("--status", action="store_true", help="показать состояние")
    ap.add_argument("--autostart", choices=["on", "off", "status", "openwin-on",
                                            "openwin-off"],
                    help="управление автозапуском в реестре (HKCU Run)")
    args = ap.parse_args()

    if args.status:
        _print_status()
        return 0

    if args.autostart:
        if args.autostart == "status":
            _print_status()
        elif args.autostart == "on":
            set_autostart(True, open_window=autostart_opens_window())
        elif args.autostart == "off":
            set_autostart(False)
        elif args.autostart == "openwin-on":
            set_autostart(True, open_window=True)
        elif args.autostart == "openwin-off":
            set_autostart(True, open_window=False)
        return 0

    logger.info("=== launcher start (engine_only=%s) ===", args.engine_only)
    # Anti-Freeze Guard (Задача 4.1): патч psutil в нашем слое ДО запуска
    # движка. Основная точка — процесс движка (starastra_engine_bootstrap:
    # патч обязан жить в ЕГО процессе, чтобы ловить его вызовы psutil).
    # Дашборд и сторож сознательно НЕ патчат: им разрешено убивать наши
    # процессы (отмена скана / рестарт зависшего движка).
    try:
        import starastra_guard
        starastra_guard.set_log_path(ENGINE_LOG_FILE)
        starastra_guard.install_guard()
    except Exception as exc:
        logger.warning("guard install failed (non-fatal): %s", exc)
    if not ensure_engine():
        logger.error("launcher abort: engine unavailable")
        return 1
    # сторож: следит, чтобы движок не умер/не завис (Задача 2.1)
    ensure_watchdog()
    if not args.engine_only:
        result = ensure_ui()
        logger.info("launcher done: ui=%s", result)
    logger.info("=== launcher exit 0 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
