#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""StarAstra Anti-Freeze Guard (Задача 4.1).

Антивирус не имеет права вешать систему. Движок Sentinel (исходников нет)
ложной C2-эвристикой замораживал доверенные процессы (браузер, uTorrent,
OVRServer) — пользователь видел это как «пропал интернет» (см. инцидент
спринта 3). Модуль изолирует проблему на нашем слое, НЕ меняя движок:

1. Monkey-patch psutil.Process.suspend/terminate/kill — ставится в процессе
   движка ДО import sentinel.service (starastra_engine_bootstrap.main) и в
   лаунчере (starastra_launch.main). Вызов на whitelisted-процессе
   БЛОКИРУЕТСЯ и пишется в data/engine.log:
       [Anti-Freeze Guard] Blocked engine attempt to suspend <process>
   Если движок дергает psutil — действия не доходят до цели.

2. Guardian-поток (интервал 1 с) — страховка на случай, если движок
   использует ctypes/NtSuspendProcess напрямую (такой вызов патчем
   psutil не ловится). Поток проверяет whitelisted-процессы и немедленно
   вызывает .resume() у зависших (status == STATUS_STOPPED).

ВАЖНО (где guard ставить МОЖНО, а где НЕЛЬЗЯ):
  + процесс движка (bootstrap)  — патч + поток;
  + лаунчер                     — патч (оборонительный, недолгоживущий);
  + сторож                      — ТОЛЬКО поток: сторож обязан уметь убивать
                                  зависший движок (рестарт по stall), патч
                                  бы заблокировал и это;
  + дашборд                     — НЕ патчим: UI убивает scan-воркеров
                                  (python.exe — в whitelist) при «Отмене
                                  скана».
Whitelist содержит имя целиком, регистр не важен. Расширение списка против
пользовательского ТЗ (csrss/wininit/... ) — критические системные процессы:
антивирусу тем более нечего там морозить.

Движок не изменяется: патч живёт только в памяти наших процессов.
"""

from __future__ import annotations

import os
import sys
import threading
import time

PROJECT_ROOT = (os.path.dirname(os.path.abspath(sys.executable))
                if getattr(sys, "frozen", False)
                else os.path.dirname(os.path.abspath(__file__)))

# ─────────────────────────────── whitelist ───────────────────────────────
# ТЗ: browser, explorer, svchost, SearchApp, ShellExperienceHost, System,
# Registry + наши (StarAstra.exe, python.exe). Расширения: pythonw.exe/
# StarAstraWorker.exe — наши процессы; csrss/wininit/smss/services/lsass/
# winlogon/dwm — критические для жизни системы.
WHITELIST: frozenset[str] = frozenset({
    # пользовательские/сетевые (ложные C2-цели движка)
    "browser.exe", "explorer.exe", "svchost.exe",
    "searchapp.exe", "shellexperiencehost.exe",
    # системные критические
    "system", "registry", "csrss.exe", "wininit.exe", "smss.exe",
    "services.exe", "lsass.exe", "winlogon.exe", "dwm.exe",
    # наши процессы
    "starastra.exe", "starastraworker.exe", "python.exe", "pythonw.exe",
})

ENGINE_LOG_FILE = os.path.join(PROJECT_ROOT, "data", "engine.log")

_log_path: str = ENGINE_LOG_FILE
_guard_installed = False
_thread: threading.Thread | None = None
_patched_originals: dict[str, object] = {}


# ─────────────────────────────── логирование ─────────────────────────────
def set_log_path(path: str) -> None:
    global _log_path
    _log_path = path


def _win_atomic_append(path: str, data: bytes) -> bool:
    """Дописать data в конец path атомарно (Windows-настоящий O_APPEND).

    CRT-режим "a"/os.O_APPEND на Windows — это 'seek to end перед каждым
    write' и НЕ атомарен (MSDN): при параллельных писателях в engine.log
    начало строки перезатиралось (наблюдалось). FILE_APPEND_DATA без
    FILE_WRITE_DATA заставляет ядро дописывать каждый WriteFile строго
    в конец файла — гонок нет.
    """
    try:
        import win32file
        handle = win32file.CreateFile(
            path, win32file.FILE_APPEND_DATA,
            win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE |
            win32file.FILE_SHARE_DELETE,
            None, win32file.OPEN_ALWAYS, 0, None)
        try:
            win32file.WriteFile(handle, data)
        finally:
            handle.Close()
        return True
    except Exception:
        return False


def _log(message: str) -> None:
    """Строка про guard в логи (формат задачи). Никогда не бросает.

    Порядок писателей:
    1. logging (если root уже сконфигурирован — движок): единый потокобезо-
       пасный писатель, без гонок с logging-хендлером движка;
    2. win32 FILE_APPEND_DATA (настоящий атомарный append: CRT O_APPEND на
       Windows — «seek to end перед write», НЕ атомарен — MSDN);
    3. os.open(O_APPEND) + один os.write (POSIX/без pywin32);
    4. logging как последний резерв, если файл недоступен.
    """
    try:
        import logging
        if logging.getLogger().handlers:
            logging.getLogger("starastra.guard").info(message)
            return
    except Exception:
        pass
    line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n").encode("utf-8")
    try:
        os.makedirs(os.path.dirname(_log_path), exist_ok=True)
        if _win_atomic_append(_log_path, line):
            return
    except OSError:
        pass
    try:
        fd = os.open(_log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return
    except OSError:
        pass
    try:
        import logging
        logging.getLogger("starastra.guard").info(message)
    except Exception:
        pass


def _audit_block(action: str, process: str, pid: int) -> None:
    """Блок пишется и в журнал действий StarAstra (не критично, если нет)."""
    try:
        import starastra_audit
        starastra_audit.audit("guard_block",
                              {"action": action, "process": process,
                               "pid": pid})
    except Exception:
        pass


# ─────────────────────────────── monkey-patch ────────────────────────────
def _make_guarded(op: str, original):
    """Обёртка psutil.Process.<op>: whitelist-цель -> блок + лог."""
    def guarded(self, *args, **kwargs):
        try:
            pname = (self.name() or "").lower()
        except Exception:
            pname = ""
        if pname in WHITELIST:
            _log(f"[Anti-Freeze Guard] Blocked engine attempt to {op} "
                 f"{pname} (pid={self.pid})")
            _audit_block(op, pname, self.pid)
            return None                       # действие НЕ выполняется
        return original(self, *args, **kwargs)
    guarded._starastra_guard = True           # type: ignore[attr-defined]
    try:
        guarded.__name__ = getattr(original, "__name__", f"guarded_{op}")
    except Exception:
        pass
    return guarded


def install_guard(log_path: str | None = None) -> bool:
    """Пропатчить psutil.Process.suspend/terminate/kill в ЭТОМ процессе.

    Идемпотентно: повторный вызов ничего не портит. Возвращает True, если
    установлен хотя бы один новый патч. psutil должен импортироваться тут
    же (в процессе движка он есть).
    """
    global _guard_installed
    if log_path:
        set_log_path(log_path)
    try:
        import psutil
    except Exception as exc:                  # pragma: no cover
        _log(f"[Anti-Freeze Guard] psutil import failed: {exc}")
        return False

    patched: list[str] = []
    for op in ("suspend", "terminate", "kill"):
        original = getattr(psutil.Process, op, None)
        if original is None or getattr(original, "_starastra_guard", False):
            continue
        # kill на Windows — алиас terminate (тот же объект): после патча
        # terminate второй обёртки не требует
        if any(original is orig for orig in _patched_originals.values()):
            continue
        wrapped = _make_guarded(op, original)
        setattr(psutil.Process, op, wrapped)  # type: ignore[assignment]
        _patched_originals[op] = original
        patched.append(op)

    _guard_installed = True
    if patched:
        _log(f"[Anti-Freeze Guard] active in pid={os.getpid()} "
             f"(patched: {', '.join(patched)}; whitelist={len(WHITELIST)} names)")
    return bool(patched)


def guard_installed() -> bool:
    return _guard_installed


# ───────────────────────────── guardian-поток ────────────────────────────
def _guardian_loop(interval: float) -> None:
    import psutil
    _log(f"[Anti-Freeze Guard] guardian thread started "
         f"(pid={os.getpid()}, interval={interval}s)")
    last_logged: dict[tuple[str, int], float] = {}
    while True:
        try:
            for proc in psutil.process_iter(["pid", "name", "status"]):
                try:
                    pname = (proc.info.get("name") or "").lower()
                    if pname not in WHITELIST:
                        continue
                    if proc.info.get("status") != psutil.STATUS_STOPPED:
                        continue
                    proc.resume()
                    key = (pname, proc.info["pid"])
                    now = time.monotonic()
                    # UWP-фоновики (SearchApp и пр.) Windows усыпляет сам —
                    # не спамим лог повторами про один и тот же pid
                    if now - last_logged.get(key, 0.0) > 10.0:
                        last_logged[key] = now
                        _log(f"[Anti-Freeze Guard] Unfroze whitelisted "
                             f"process {pname} (pid={proc.info['pid']})")
                except (psutil.NoSuchProcess, psutil.AccessDenied,
                        psutil.ZombieProcess):
                    continue
        except Exception as exc:              # не ронять поток никогда
            _log(f"[Anti-Freeze Guard] guardian error: {exc}")
        time.sleep(interval)


def start_guardian_thread(interval: float = 1.0) -> threading.Thread:
    """Фоновый поток: раз в interval секунд возвращать к жизни whitelisted
    процессы (страховка от прямых win32-вызовов движка). Daemon."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    _thread = threading.Thread(target=_guardian_loop, args=(interval,),
                               daemon=True, name="starastra-guard")
    _thread.start()
    return _thread
