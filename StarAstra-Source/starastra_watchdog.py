#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""StarAstra watchdog — отдельный процесс-сторож движка (Задача 2.1).

Запуск (обычно из лаунчера, автоматически):
    python starastra_watchdog.py [--interval 2] [--stall-sec 600]
                                 [--probe-wait 25] [--once-check]

Что проверяется (каждые --interval секунд):
  1. ПРОЦЕСС движка жив? psutil-скан по cmdline ("starastra_engine_bootstrap"
     или "sentinel.service"). venv python.exe на этой машине — shim-прокладка:
     реальный движок это его ДОЧЕРНИЙ процесс (см. отчёт Спринта 1), поэтому
     pid-файлу не доверяем — ищем фактический процесс по cmdline.
  2. КОНВЕЙЕР детекции жив? Случай из Спринта 1: IPC отвечает 'GREEN',
     а сенсорный конвейер мёртв — событий в events.db нет минутами, EICAR
     не детектируется. Если последнее событие старше --stall-sec —
     выполняется АКТИВНАЯ проба: в watched-каталог (Downloads) пишется
     безобидный probe-файл; живой конвейер обязан породить событие о нём
     за --probe-wait секунд. Пробы нет -> конвейер мёртв -> kill + restart.
     Активная проба исключает ложные срабатывания на простаивающем ПК:
     мы спрашиваем движок напрямую, а не меряем тишину.
  3. IPC get_status(): если при живом процессе IPC завис/ошибка —
     фиксируется как симптом (очередь queue_size в статусе движка
     НЕ НАЙДЕНА — см. отчёт), сам по себе не критерий.

Действие при смерти/зависании: terminate/kill процессов движка -> запуск
starastra_engine_bootstrap.py заново (тот же способ, что у лаунчера) ->
запись события в events.db (event_type='watchdog', severity HIGH — UI
показывает карточку и пишет в Историю) + строка в data/watchdog.log +
запись в audit.log (starastra_audit).

Политика (чтобы не бороться с пользователем):
  - защита на паузе (settings.protection_paused_until > now) — рестарта НЕТ;
  - полный выход из UI (settings.engine_keep_alive == False) — рестарта НЕТ;
  - одиночный экземпляр: второй watchdog не стартует.

Ограничение честности: от убийства самого watchdog'а вредоносом защиты нет
(драйвер-защитник вне планов) — watchdog ловит «движок умер/завис», а не
«кто-то убил сторожа».
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import subprocess
import sys
import time

PROJECT_ROOT = (os.path.dirname(os.path.abspath(sys.executable))
                if getattr(sys, "frozen", False)
                else os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

ENGINE_DB = os.path.join(PROJECT_ROOT, "sentinel_pkg", "sentinel", "data", "events.db")
SETTINGS_FILE = os.path.join(PROJECT_ROOT, "starastra_settings.json")
BOOTSTRAP = os.path.join(PROJECT_ROOT, "starastra_engine_bootstrap.py")
VENV_PYTHON = os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")
WATCHDOG_LOG = os.path.join(PROJECT_ROOT, "data", "watchdog.log")
ENGINE_LOG = os.path.join(PROJECT_ROOT, "data", "engine.log")   # log Anti-Freeze Guard
PID_FILE = os.path.join(PROJECT_ROOT, "sentinel_pkg", "sentinel", "data", "sentinel.pid")
PROBE_DIR = os.path.join(os.path.expanduser("~"), "Downloads")   # watched-каталог движка

ENGINE_MARKERS = ("starastra_engine_bootstrap", "sentinel.service",
                  "--star-worker=engine")
CREATE_NO_WINDOW = 0x08000000

os.makedirs(os.path.dirname(WATCHDOG_LOG), exist_ok=True)
logger = logging.getLogger("starastra.watchdog")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_fh = logging.FileHandler(WATCHDOG_LOG, encoding="utf-8")
_fh.setFormatter(_fmt)
logger.addHandler(_fh)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
logger.addHandler(_sh)


# ─────────────────────────── вспомогательные ───────────────────────────
def _read_settings() -> dict | None:
    """Настройки; None = файл нечитаем (битый/недописанный параллельной
    записью) — вызывающий код обязан считать политику НЕИЗВЕСТНОЙ."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _paused_now(settings: dict) -> bool:
    until = settings.get("protection_paused_until")
    try:
        return bool(until) and time.time() < float(until)
    except (TypeError, ValueError):
        return False


def find_engine_pids() -> list[int]:
    """Все процессы движка по cmdline (shim + реальный), кроме себя."""
    import psutil
    me = os.getpid()
    pids: list[int] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        if proc.info["pid"] == me:
            continue
        try:
            cmdline = " ".join(proc.info["cmdline"] or []).lower()
            if any(m in cmdline for m in ENGINE_MARKERS):
                pids.append(proc.info["pid"])
        except Exception:
            continue
    return pids


def last_event() -> tuple[int, str] | None:
    """(id, timestamp ISO) последнего события events.db; None если БД пуста."""
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{ENGINE_DB}?mode=ro", uri=True, timeout=3)
        row = con.execute(
            "SELECT id, timestamp FROM events ORDER BY id DESC LIMIT 1").fetchone()
        con.close()
        return (row[0], row[1]) if row else None
    except Exception as exc:
        logger.warning("events.db read failed: %s", exc)
        return None


def event_age_seconds(ts_iso: str) -> float | None:
    """Возраст события (timestamp '2026-11-09T21:34:51.290Z') в секундах."""
    try:
        ts = _dt.datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        now = _dt.datetime.now(_dt.timezone.utc)
        return max(0.0, (now - ts).total_seconds())
    except Exception:
        return None


def ipc_status(timeout_ms: int = 3000) -> dict | None:
    """Штатный IPC get_status() с коротким таймаутом; None при сбое."""
    try:
        for p in (os.path.join(PROJECT_ROOT, "sentinel_pkg"),
                  os.path.join(PROJECT_ROOT, "Sentinel", "_internal")):
            if p not in sys.path:
                sys.path.insert(0, p)
        from sentinel.platform.ipc import create_ipc_client  # noqa: E402
        client = create_ipc_client()
        client.timeout_ms = timeout_ms
        st = client.get_status()
        return st if isinstance(st, dict) else None
    except Exception as exc:
        logger.debug("ipc get_status failed: %s", exc)
        return None


def write_ui_event(action: str, details: str) -> None:
    """Событие HIGH в events.db — UI покажет карточку и запись в Истории."""
    try:
        import sqlite3
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") \
            + f"{_dt.datetime.now(_dt.timezone.utc).microsecond // 1000:03d}Z"
        con = sqlite3.connect(ENGINE_DB, timeout=5)
        con.execute(
            "INSERT INTO events (timestamp, source, event_type, pid, image_path,"
            " command_line, hash_sha256, extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now, "starastra_watchdog", "watchdog", os.getpid(),
             "Star Astra Watchdog", "", "", json.dumps(
                 {"severity": "HIGH", "action": action, "description": details},
                 ensure_ascii=False)))
        con.commit()
        con.close()
    except Exception as exc:
        logger.warning("events.db write failed: %s", exc)


def audit_log(action: str, details: dict) -> None:
    try:
        import starastra_audit
        starastra_audit.audit(action, details)
    except Exception as exc:
        logger.warning("audit write failed: %s", exc)


# ─────────────── компенсация ложных заморозок (C2 FP) ───────────────
def excluded_processes(settings: dict | None) -> tuple[str, ...]:
    """Имена процессов из исключений UI (type='process')."""
    if not settings:
        return ()
    out = []
    for item in settings.get("exclusions") or []:
        if isinstance(item, dict) and \
                str(item.get("type", "")).lower() == "process":
            name = str(item.get("value") or "").strip().lower()
            if name:
                out.append(name)
    return tuple(out)


def unfreeze_excluded(settings: dict | None) -> int:
    """Разморозка процессов, внесённых пользователем в исключения UI.

    Зачем: C2-эвристика движка (whitelist НЕ НАЙДЕН) периодически замораживает
    доверенные процессы (браузер, uTorrent, OVRServer) — замороженный
    фоновый процесс браузера выглядит у пользователя как «пропал интернет».
    Разморозка возвращает процесс к жизни; детекция движка не глушится.
    Возврат: число размороженных за вызов.
    """
    names = excluded_processes(settings)
    if not names:
        return 0
    import psutil
    resumed = 0
    for p in psutil.process_iter(["pid", "name", "status"]):
        try:
            name = (p.info.get("name") or "").lower()
            if p.info.get("status") == psutil.STATUS_STOPPED and \
                    name in names:
                p.resume()
                resumed += 1
                logger.warning(
                    "UNFREEZE: %s (pid=%s) был заморожен движком — "
                    "разморожен (процесс в исключениях UI)",
                    name, p.info["pid"])
                audit_log("unfreeze_excluded",
                          {"pid": p.info["pid"], "process": name})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return resumed


# ─────────────────── поддержание UI/трея ───────────────────
def find_ui_pids() -> list[int]:
    """Процессы дашборда (frozen: --star-worker=ui; dev: starastra_dashboard)."""
    import psutil
    me = os.getpid()
    pids: list[int] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        if proc.info["pid"] == me:
            continue
        try:
            cmdline = " ".join(proc.info["cmdline"] or []).lower()
            if "--star-worker=ui" in cmdline or \
                    "starastra_dashboard" in cmdline:
                pids.append(proc.info["pid"])
        except Exception:
            continue
    return pids


def spawn_ui() -> int | None:
    """Запуск UI с трей-иконкой (тот же, что делает лаунчер).

    Окружение БЕЗ PYTHONPATH/PYTHONHOME (как _ui_env лаунчера): сторож
    получает env с PYTHONPATH=Sentinel\\_internal (для движка), и унаследованный
    _internal затеняет Tcl -> pythonw-дашборд умирает молча (см.
    EICAR_DIAGNOSIS.md). Слесть к «respawn каждые 30 с» вместо живого UI.
    """
    dashboard = os.path.join(PROJECT_ROOT, "Sentinel", "ui",
                             "starastra_dashboard.py")
    if getattr(sys, "frozen", False):
        exe = os.path.join(PROJECT_ROOT, "StarAstra.exe")
        if not os.path.isfile(exe):
            logger.warning("ui respawn failed: %s missing", exe)
            return None
        cmd = [exe, "--star-worker=ui"]
        flags = CREATE_NO_WINDOW
    else:
        pythonw = os.path.join(PROJECT_ROOT, ".venv", "Scripts",
                               "pythonw.exe")
        if os.path.isfile(pythonw):
            cmd = [pythonw, dashboard]
            flags = 0
        else:
            python = VENV_PYTHON if os.path.isfile(VENV_PYTHON) \
                else sys.executable
            cmd = [python, dashboard]
            flags = CREATE_NO_WINDOW
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    try:
        proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT, env=env,
                                creationflags=flags,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        return proc.pid
    except Exception as exc:
        logger.warning("ui respawn failed: %s", exc)
        return None


# ─────────────────────────── активная проба ───────────────────────────
def pipeline_probe(probe_wait: float) -> bool:
    """Жив ли конвейер: безобидный файл в watched-каталог -> событие о нём?

    True = событие появилось (конвейер работает, тишина была просто простоем).
    """
    probe_path = os.path.join(PROBE_DIR, f"starastra_probe_{os.getpid()}.tmp")
    base = last_event()
    base_id = base[0] if base else 0
    try:
        os.makedirs(PROBE_DIR, exist_ok=True)
        with open(probe_path, "w", encoding="utf-8") as f:
            f.write("StarAstra watchdog pipeline probe - harmless text.\n")
    except Exception as exc:
        logger.warning("probe write failed: %s", exc)
        return True   # не можем создать пробу — не убиваем движок по своей вине
    try:
        deadline = time.monotonic() + probe_wait
        while time.monotonic() < deadline:
            time.sleep(1.0)
            cur = last_event()
            if cur and cur[0] > base_id:
                # есть новое событие; убедимся, что это реакция на пробу
                try:
                    import sqlite3
                    con = sqlite3.connect(f"file:{ENGINE_DB}?mode=ro", uri=True,
                                          timeout=3)
                    row = con.execute(
                        "SELECT image_path FROM events WHERE id = ?",
                        (cur[0],)).fetchone()
                    con.close()
                except Exception:
                    row = None
                if row and "starastra_probe_" in str(row[0] or ""):
                    return True
        return False
    finally:
        try:
            os.remove(probe_path)
        except OSError:
            pass


# ─────────────────────────── действия ───────────────────────────
def kill_engine(pids: list[int]) -> None:
    import psutil
    for pid in pids:
        try:
            psutil.Process(pid).terminate()
        except Exception:
            pass
    time.sleep(2.0)
    for pid in pids:
        try:
            if psutil.pid_exists(pid):
                psutil.Process(pid).kill()
        except Exception:
            pass
    time.sleep(0.5)


def spawn_engine() -> int | None:
    """Запуск движка тем же способом, что и лаунчер (stdout -> engine.log)."""
    if getattr(sys, "frozen", False):
        # frozen: консольный воркер нашего exe (см. starastra_exe_entry.py)
        worker = os.path.join(PROJECT_ROOT, "StarAstraWorker.exe")
        if not os.path.isfile(worker):
            logger.error("engine respawn failed: %s missing", worker)
            return None
        cmd = [worker, "--star-worker=engine", "--standalone"]
    else:
        python = VENV_PYTHON if os.path.isfile(VENV_PYTHON) else sys.executable
        cmd = [python, BOOTSTRAP, "--standalone"]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(PROJECT_ROOT, "sentinel_pkg"),
         os.path.join(PROJECT_ROOT, "Sentinel", "_internal")])
    try:
        # stdout/stderr -> отдельный файл: engine.log пишет ТОЛЬКО logging
        # (см. комментарий в starastra_launch.ensure_engine — CRT-append
        # не атомарен, два хендлера в одном файле перезаписывали друг друга)
        log_fh = open(os.path.join(PROJECT_ROOT, "data",
                                   "engine_stdout.log"), "ab")
    except OSError:
        log_fh = None
    try:
        proc = subprocess.Popen(
            cmd, cwd=PROJECT_ROOT, env=env,
            creationflags=CREATE_NO_WINDOW,
            stdout=log_fh if log_fh else subprocess.DEVNULL,
            stderr=subprocess.STDOUT)
    except Exception as exc:
        logger.error("engine respawn failed: %s", exc)
        return None
    finally:
        if log_fh:
            log_fh.close()
    try:
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(proc.pid))
    except OSError:
        pass
    return proc.pid


def restart_engine(reason: str) -> None:
    old_pids = find_engine_pids()
    logger.warning("RESTART: %s (pids=%s)", reason, old_pids)
    if old_pids:
        kill_engine(old_pids)
    new_pid = spawn_engine()
    ready = False
    if new_pid:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            st = ipc_status()
            if st and st.get("status") == "ok":
                ready = True
                break
            time.sleep(0.5)
    logger.warning("RESTART done: new_pid=%s ready=%s", new_pid, ready)
    audit_log("engine_restart", {
        "reason": reason, "old_pids": old_pids, "new_pid": new_pid,
        "ready": ready})
    write_ui_event(
        "engine_restart",
        ("Защита восстановлена автоматически: движок был "
         f"{'зависший' if 'stalled' in reason else 'остановлен'}"
         f" — перезапущен (pid={new_pid}). Причина: {reason}"))


# ─────────────────────────── одиночный экземпляр ───────────────────────────
LOCK_FILE = os.path.join(
    os.environ.get("TEMP") or os.path.join(PROJECT_ROOT, "data"),
    "starastra_watchdog.lock")


def acquire_lock() -> bool:
    """O_EXCL-lock + проверка живости владельца (детерминированно, без гонок
    скана процессов). Stale-lock мёртвого/чужого pid перехватывается."""
    import psutil
    for _ in range(3):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                with open(LOCK_FILE, encoding="utf-8") as f:
                    old = int(f.read().strip() or 0)
            except Exception:
                old = 0
            if old and old != os.getpid():
                try:
                    if psutil.pid_exists(old) and (
                            "starastra_watchdog" in
                            " ".join(psutil.Process(old).cmdline() or [])
                            or "--star-worker=watchdog" in
                            " ".join(psutil.Process(old).cmdline() or [])):
                        return False          # живой watchdog существует
                except Exception:
                    pass
            # stale (владелец мёртв или pid переиспользован чужим процессом)
            try:
                fd = os.open(LOCK_FILE, os.O_TRUNC | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                logger.info("stale watchdog lock (pid=%s) taken over", old)
                return True
            except OSError:
                time.sleep(0.3)
    return False


# ─────────────────────────── главный цикл ───────────────────────────
_last_ui_ensure = 0.0


def run(interval: float, stall_sec: float, probe_wait: float,
        once_check: bool) -> None:
    global _last_ui_ensure
    if not acquire_lock():
        logger.info("watchdog already running (lock held) — exit")
        return
    logger.info("watchdog started (pid=%s, interval=%ss, stall=%ss)",
                os.getpid(), interval, stall_sec)
    # Anti-Freeze Guard (Задача 4.1): guardian-поток 1 с — независимая от
    # главного цикла разморозка whitelisted-процессов. ПАТЧ psutil сюда
    # НЕ ставим: сторож обязан уметь убивать зависший движок (рестарт).
    try:
        import starastra_guard
        starastra_guard.set_log_path(ENGINE_LOG)
        starastra_guard.start_guardian_thread(interval=1.0)
    except Exception as exc:
        logger.warning("guardian thread failed (non-fatal): %s", exc)
    # Behavioral Analysis (Спринт 5): сенсор-поток 2 с — новые процессы ->
    # правила -> реакция (SUSPEND в active mode). daemon — умирает вместе
    # со сторожем; отдельный процесс не нужен.
    try:
        import starastra_behavior
        starastra_behavior.start_sensor(interval=2.0)
    except Exception as exc:
        logger.warning("behavior sensor failed (non-fatal): %s", exc)
    baseline = last_event()
    logger.info("baseline last event: %s", baseline)
    while True:
        try:
            settings = _read_settings()
            # 0) разморозка процессов из исключений UI — независимо от
            # политики рестартов: замороженный браузер = «нет интернета»
            unfreeze_excluded(settings)
            # 1) трей/UI: пользователь должен видеть работу защиты в трее.
            #    Полный выход (engine_keep_alive=False) уважаем — не воскресаем.
            if time.time() - _last_ui_ensure > 30.0:
                _last_ui_ensure = time.time()
                if settings is not None and \
                        settings.get("engine_keep_alive") is not False and \
                        not find_ui_pids():
                    ui_pid = spawn_ui()
                    if ui_pid:
                        logger.info("UI (tray) respawned: pid=%s", ui_pid)
            if settings is None:
                # настройки нечитаемы (битый/недописанный файл): политика
                # НЕИЗВЕСТНА — консервативно НЕ трогаем движок эту итерацию
                logger.warning("settings unreadable — skip (no actions)")
            elif _paused_now(settings):
                pass                       # пауза: рестартов нет
            elif settings.get("engine_keep_alive") is False:
                pass                       # полный выход из UI: рестартов нет
            else:
                pids = find_engine_pids()
                if not pids:
                    restart_engine("engine process not found")
                else:
                    cur = last_event()
                    if cur:
                        age = event_age_seconds(cur[1])
                        if age is not None and age > stall_sec:
                            st = ipc_status()
                            logger.info(
                                "stall suspected: last event %ss ago (id=%s), "
                                "ipc=%s — probing pipeline", int(age), cur[0],
                                "ok" if st else "failed")
                            if pipeline_probe(probe_wait):
                                logger.info("probe OK — pipeline alive, was idle")
                            else:
                                restart_engine(
                                    f"stalled: no events for {int(age)}s "
                                    "and probe failed")
                    # пустая БД — движок ещё ни одного события не написал:
                    # не считаем это зависанием (первые минуты после установки)
            if once_check:
                # выход ПОСЛЕ первой итерации в ЛЮБОЙ ветке политики
                # (иначе --once-check при паузе зацикливается навсегда)
                logger.info("once-check done — exit")
                return
        except Exception as exc:
            logger.error("watchdog loop error: %s", exc)
        time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description="StarAstra engine watchdog")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="период опроса, сек (по умолчанию 2)")
    ap.add_argument("--stall-sec", type=float, default=600.0,
                    help="тишина событий, после которой подозревается "
                         "зависание (по умолчанию 600)")
    ap.add_argument("--probe-wait", type=float, default=25.0,
                    help="сколько ждать событие от probe-файла, сек")
    ap.add_argument("--once-check", action="store_true",
                    help="одна проверка и выход (для тестов)")
    args = ap.parse_args()
    run(args.interval, args.stall_sec, args.probe_wait, args.once_check)
    return 0


if __name__ == "__main__":
    sys.exit(main())
