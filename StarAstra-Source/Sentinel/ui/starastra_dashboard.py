"""StarAstra — современный зелёный дашборд для Sentinel Antivirus (StarAnonProject).

Запуск:
    pythonw Sentinel/ui/starastra_dashboard.py   # напрямую (пакета sentinel нет)
    или StarAstra.vbs / StarAstra.bat из корня проекта.

UI полностью отвязан от пакета движка (sentinel_pkg не импортируется):
- история:  sqlite3 read-only к events.db (ENGINE_DB_PATH);
- карантин: sqlite3 read-only к quarantine.db; действия — только через
  существующий код движка, запускаемый отдельным процессом
  (.venv python -c "from sentinel.response.quarantine_store import ..."),
  как это делает service_manager.ps1; если venv недоступен — кнопки disabled;
- пауза:    supervisor — psutil (процесс движка) / sc.exe (служба SentinelCoreSvc);
- ping:     штатный IPC ping (sentinel.platform.ipc) — проверка живости.

Анимации: только root.after(), без time.sleep(); кадр 33 мс (~30 FPS);
пульс щита/fade/slide останавливаются, когда окно свёрнуто.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

logger = logging.getLogger("starastra.ui")

# ── Пути проекта ────────────────────────────────────────────────────────────
# frozen (Задача 3.3): корень — каталог exe (рядом sentinel_pkg/, Sentinel/).
if getattr(sys, "frozen", False):
    _PROJECT_ROOT = os.path.dirname(os.path.abspath(sys.executable))
    _SENTINEL_DIR = os.path.join(_PROJECT_ROOT, "Sentinel")
    _UI_DIR = os.path.join(_SENTINEL_DIR, "ui")
else:
    _UI_DIR = os.path.dirname(os.path.abspath(__file__))
    _SENTINEL_DIR = os.path.dirname(_UI_DIR)                 # .../Sentinel
    _PROJECT_ROOT = os.path.dirname(_SENTINEL_DIR)           # корень Star-Astra
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)                    # starastra_i18n / starastra_notify

# ── База движка: тот же файл, куда пишет движок ─────────────────────────────
_ENGINE_DB_CANDIDATES = (
    os.path.join(_PROJECT_ROOT, "sentinel_pkg", "sentinel", "data", "events.db"),
    os.path.join(_SENTINEL_DIR, "_internal", "sentinel", "data", "events.db"),
    os.path.join(_PROJECT_ROOT, "data", "events.db"),
)


def _find_engine_db() -> str:
    for p in _ENGINE_DB_CANDIDATES:
        if os.path.isfile(p):
            return p
    # движок может ещё не создать БД — вернуть первый кандидат (создаст при старте)
    return _ENGINE_DB_CANDIDATES[0]


ENGINE_DB_PATH = _find_engine_db()
QUARANTINE_DB_PATH = os.path.join(os.path.dirname(ENGINE_DB_PATH), "quarantine.db")
ENGINE_SNIPPET_PYTHON = os.path.join(_PROJECT_ROOT, ".venv", "Scripts", "python.exe")


def _snippet_worker_cmd(code: str, argv: list[str] | None = None) -> list[str]:
    """Команда запуска сниппета движка: `venv python -c` (dev) или
    StarAstraWorker.exe --star-worker=snippet (frozen, Задача 3.3)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--star-worker=snippet", code, *(argv or [])]
    return [ENGINE_SNIPPET_PYTHON, "-c", code, *(argv or [])]


def _scan_worker_cmd(kind: str) -> list[str]:
    """Команда запуска сканера (JSON-протокол в stdout): venv python +
    starastra_scan.py (dev) или StarAstraWorker.exe --star-worker=scan
    (frozen, Задача 3.3)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--star-worker=scan", kind]
    runner = os.path.join(_PROJECT_ROOT, "starastra_scan.py")
    return [ENGINE_SNIPPET_PYTHON, runner, kind]

_LAUNCH_MOD: object | None = None


def _launch_mod():
    """Модуль starastra_launch.py (кэшируется). Нужен для автозапуска и замков."""
    global _LAUNCH_MOD
    if _LAUNCH_MOD is None:
        if getattr(sys, "frozen", False):
            import starastra_launch                # вшит в exe (Задача 3.3)
            _LAUNCH_MOD = starastra_launch
        else:
            import importlib.util
            path = os.path.join(_PROJECT_ROOT, "starastra_launch.py")
            spec = importlib.util.spec_from_file_location("starastra_launch", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _LAUNCH_MOD = mod
    return _LAUNCH_MOD

# ── Палитра (тёмная тема) ───────────────────────────────────────────────────
BG_DARK = "#0F1410"
SURFACE_1 = "#1B241C"
SURFACE_2 = "#223025"
SURFACE_STRIPE = "#1F2A20"
ACCENT = "#4CAF7D"
ACCENT_HOVER = "#6FD1A3"
TEXT_MAIN = "#E6F0E8"
TEXT_SUB = "#9BB3A2"
TEXT_DIM = "#6E8377"   # «свои» файлы — приглушённый серо-зелёный
OK_GREEN = "#4CAF7D"
WARN_AMBER = "#D9A441"
DANGER_RED = "#D96A6A"
BORDER = "#2A3830"

# ── Анимации / пауза ────────────────────────────────────────────────────────
PAUSE_CHOICES = (10, 15, 30, 45, 60)   # минуты, выбор в диалоге и в трею
DEFAULT_PAUSE_MIN = 15                 # дефолт, если нет в starastra_settings.json
RECENT_POLL_MS = 2000                  # блок «Последние события»
ANIM_FRAME_MS = 33          # ~30 FPS
ANIM_FADE_MS = 200
ANIM_HOVER_MS = 150
ANIM_PULSE_MS = 2000
HISTORY_POLL_MS = 4000

SETTINGS_FILE = os.path.join(_PROJECT_ROOT, "starastra_settings.json")


def load_app_settings() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_app_settings(update: dict) -> None:
    try:
        data = load_app_settings()
        data.update(update)
        # атомарная замена: читатели (watchdog) не видят недописанный файл
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except OSError as exc:
        logger.warning("save settings failed: %s", exc)


def write_ui_event(event_type: str, image_path: str, extra: dict,
                   source: str = "starastra_ui") -> bool:
    """Синтетическое событие StarAstra в events.db (История/карточки).

    image_path без маркеров собственных файлов (см. _is_own_file),
    иначе История классифицирует событие как OWN вместо extra.severity.
    """
    try:
        now = (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
               + ".%03dZ" % int(time.time() * 1000 % 1000))
        con = sqlite3.connect(ENGINE_DB_PATH, timeout=5)
        con.execute(
            "INSERT INTO events (timestamp, source, event_type, pid,"
            " image_path, command_line, hash_sha256, extra)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now, source, event_type, os.getpid(), image_path, "", "",
             json.dumps(extra, ensure_ascii=False)))
        con.commit()
        con.close()
        return True
    except Exception as exc:
        logger.warning("events.db write failed: %s", exc)
        return False

F_HEAD = ("Segoe UI", 16, "bold")
F_BASE = ("Segoe UI", 11)
F_SUB = ("Segoe UI", 10)
F_COUNTER = ("Segoe UI", 28, "bold")

from starastra_i18n import tr, set_language, get_language, available_languages  # noqa: E402
from starastra_notify import NotifyPanel, ToastPoller, make_system_notifier     # noqa: E402
from starastra_audit import audit, read_audit, ACTIONS                # noqa: E402  (2.2/2.4)


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(c1: str, c2: str, t: float) -> str:
    r1, g1, b1 = _hex_to_rgb(c1)
    r2, g2, b2 = _hex_to_rgb(c2)
    return "#{:02x}{:02x}{:02x}".format(
        round(r1 + (r2 - r1) * t), round(g1 + (g2 - g1) * t), round(b1 + (b2 - b1) * t))


def _is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _short(path: str, limit: int = 46) -> str:
    if len(path) > limit:
        return "…" + path[-(limit - 1):]
    return path


def _fmt_duration(seconds: float | None) -> str:
    """Секунды -> '12 с' / '3:05'. Без i18n-ключей: 1-символьные единицы."""
    if seconds is None or seconds < 0:
        return "—"
    total = int(seconds)
    if total < 60:
        unit = "с" if get_language() == "ru" else "s"
        return f"{total} {unit}"
    return f"{total // 60}:{total % 60:02d}"


# ═════════════════════════ Данные: sqlite read-only ═════════════════════════
class DataProxy:
    """Чтение events.db / quarantine.db (read-only) + действия карантина через
    существующий код движка в отдельном процессе (не импортируя его в UI)."""

    def __init__(self) -> None:
        self.db_path = ENGINE_DB_PATH
        self._cols_cache: list[str] | None = None

    def _connect(self, db: str) -> sqlite3.Connection | None:
        if not os.path.isfile(db):
            return None
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            con.row_factory = sqlite3.Row
            return con
        except sqlite3.Error as exc:
            logger.warning("open %s failed: %s", db, exc)
            return None

    def events_columns(self) -> list[str]:
        if self._cols_cache is None:
            con = self._connect(self.db_path)
            if con is None:
                return ["timestamp", "source", "event_type", "image_path", "extra"]
            self._cols_cache = [r[1] for r in con.execute("PRAGMA table_info(events)")]
            con.close()
        return self._cols_cache

    def recent_events(self, limit: int = 100) -> list[dict]:
        con = self._connect(self.db_path)
        if con is None:
            return []
        cols = self.events_columns()
        order = "id" if "id" in cols else "rowid"
        try:
            rows = [dict(r) for r in con.execute(
                f"SELECT * FROM events ORDER BY {order} DESC LIMIT ?", (limit,))]
        except sqlite3.Error as exc:
            logger.warning("recent_events failed: %s", exc)
            rows = []
        con.close()
        return rows

    def count_events(self) -> int:
        con = self._connect(self.db_path)
        if con is None:
            return 0
        try:
            n = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        except sqlite3.Error:
            n = 0
        con.close()
        return n

    def quarantine_list(self) -> list[dict]:
        con = self._connect(QUARANTINE_DB_PATH)
        if con is None:
            return []
        try:
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM quarantine ORDER BY timestamp DESC LIMIT 200")]
        except sqlite3.Error:
            rows = []
        con.close()
        return rows

    def quarantine_count(self) -> int:
        con = self._connect(QUARANTINE_DB_PATH)
        if con is None:
            return 0
        try:
            n = con.execute(
                "SELECT COUNT(*) FROM quarantine WHERE decision='pending'").fetchone()[0]
        except sqlite3.Error:
            n = 0
        con.close()
        return n

    # ── Действия карантина: существующий код движка, отдельный процесс ──
    def engine_actions_available(self) -> bool:
        if getattr(sys, "frozen", False):
            # frozen: воркер рядом с exe (см. build_exe.bat, Задача 3.3)
            return os.path.isfile(
                os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                             "StarAstraWorker.exe"))
        return os.path.isfile(ENGINE_SNIPPET_PYTHON)

    def _run_engine_snippet(self, code: str, argv: list[str] | None = None) -> tuple[bool, str]:
        if not self.engine_actions_available():
            return False, "venv python not available"
        try:
            if getattr(sys, "frozen", False):
                # frozen: консольный воркер сам строит sys.path (entry)
                r = subprocess.run(
                    _snippet_worker_cmd(code, argv),
                    capture_output=True, text=True, timeout=60,
                    cwd=_PROJECT_ROOT, creationflags=0x08000000)
            else:
                env = os.environ.copy()
                env["PYTHONPATH"] = os.pathsep.join(
                    [os.path.join(_PROJECT_ROOT, "sentinel_pkg"),
                     os.path.join(_PROJECT_ROOT, "Sentinel", "_internal")])
                r = subprocess.run([ENGINE_SNIPPET_PYTHON, "-c", code, *(argv or [])],
                                   capture_output=True, text=True, timeout=60,
                                   cwd=_PROJECT_ROOT, env=env,
                                   creationflags=0x08000000)
            if r.returncode == 0:
                return True, r.stdout.strip()
            return False, (r.stderr.strip() or f"exit {r.returncode}")
        except Exception as exc:
            return False, str(exc)

    def quarantine_restore(self, record_id: str) -> tuple[bool, str]:
        """Восстановить файл штатным QuarantineStore.restore(record_id, notes)."""
        ok, out = self._run_engine_snippet(
            "from sentinel.response.quarantine_store import QuarantineStore\n"
            "import sys\n"
            "s = QuarantineStore()\n"
            "r = s.restore(sys.argv[1], notes='restored from StarAstra UI')\n"
            "print(('RESTORED_TO:' + str(r)) if r else 'FAIL: not restored "
            "(record missing or already resolved)')\n",
            argv=[record_id])
        if not ok:
            return False, out
        if out.startswith("RESTORED_TO:"):
            return True, out[len("RESTORED_TO:"):]
        return False, out or "engine returned no result"

    def quarantine_delete(self, record_id: str) -> tuple[bool, str]:
        """Удалить навсегда штатным QuarantineStore.delete(record_id, notes)."""
        ok, out = self._run_engine_snippet(
            "from sentinel.response.quarantine_store import QuarantineStore\n"
            "import sys\n"
            "s = QuarantineStore()\n"
            "r = s.delete(sys.argv[1], notes='deleted from StarAstra UI')\n"
            "print('OK' if r else 'FAIL: delete returned False')\n",
            argv=[record_id])
        if not ok:
            return False, out
        return (True, "OK") if out == "OK" else (False, out)


# ═════════════════════ Supervisor: пауза защиты (2 режима) ══════════════════
SERVICE_NAME = "SentinelCoreSvc"


class Supervisor:
    """Режим A: движок-процесс (psutil, без UAC). Режим B: служба (sc.exe, UAC)."""

    def __init__(self) -> None:
        self.last_mode: str | None = None

    @staticmethod
    def engine_pid() -> int | None:
        try:
            import psutil
            me = os.getpid()
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    if proc.info["pid"] == me:
                        continue
                    name = (proc.info["name"] or "").lower()
                    cmdline = " ".join(proc.info["cmdline"] or []).lower()
                    if "sentinel_service.exe" in name:
                        return proc.info["pid"]
                    if "sentinel.service" in cmdline and "python" in name:
                        return proc.info["pid"]
                    if "--star-worker=engine" in cmdline:   # frozen (3.3)
                        return proc.info["pid"]
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("engine pid lookup failed: %s", exc)
        return None

    @staticmethod
    def service_installed() -> bool:
        try:
            r = subprocess.run(["sc.exe", "query", SERVICE_NAME],
                               capture_output=True, text=True, timeout=15,
                               creationflags=0x08000000)
            return r.returncode != 1060  # 1060 = service does not exist
        except Exception:
            return False

    @staticmethod
    def ping() -> bool:
        """Живость движка штатным IPC (NamedPipeClient.get_status)."""
        try:
            code = ("from sentinel.platform.ipc import create_ipc_client\n"
                    "st = create_ipc_client().get_status()\n"
                    "print('ok' if st.get('status') == 'ok' else 'down')\n")
            r = subprocess.run(
                _snippet_worker_cmd(code),
                capture_output=True, text=True, timeout=10, cwd=_PROJECT_ROOT,
                creationflags=0x08000000)
            return r.returncode == 0 and "ok" in r.stdout
        except Exception:
            return False

    def pause(self) -> tuple[bool, str, bool]:
        """Остановить защиту. Возвращает (ok, описание|'not_found', uac_required)."""
        pid = self.engine_pid()
        if pid is not None:
            try:
                import psutil
                proc = psutil.Process(pid)
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
                # дочерние процессы движка (если сервис породил worker) тоже гасим
                try:
                    for ch in proc.children(recursive=True):
                        ch.terminate()
                except Exception:
                    pass
                self.last_mode = "process"
                return True, f"PID {pid}", False
            except Exception as exc:
                logger.warning("terminate engine failed: %s", exc)
        if self.service_installed():
            if not _is_admin():
                return False, SERVICE_NAME, True
            r = subprocess.run(["sc.exe", "stop", SERVICE_NAME],
                               capture_output=True, text=True, timeout=30,
                               creationflags=0x08000000)
            if r.returncode in (0, 1062):
                self.last_mode = "service"
                return True, SERVICE_NAME, False
            return False, r.stderr.strip() or f"sc exit {r.returncode}", True
        return False, "not_found", False

    def resume(self) -> tuple[bool, str]:
        """Запустить движок заново (режим A: процесс; режим B: служба)."""
        if self.last_mode == "service":
            if not _is_admin():
                return False, "uac"
            r = subprocess.run(["sc.exe", "start", SERVICE_NAME],
                               capture_output=True, text=True, timeout=30,
                               creationflags=0x08000000)
            if r.returncode in (0, 1056):
                return True, SERVICE_NAME
            return False, r.stderr.strip() or f"sc exit {r.returncode}"
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [os.path.join(_PROJECT_ROOT, "sentinel_pkg"),
             os.path.join(_PROJECT_ROOT, "Sentinel", "_internal")])
        python = os.path.join(_PROJECT_ROOT, ".venv", "Scripts", "python.exe")
        svc_exe = os.path.join(_SENTINEL_DIR, "sentinel_service.exe")
        bootstrap = os.path.join(_PROJECT_ROOT, "starastra_engine_bootstrap.py")
        candidates: list[list[str]] = []
        if getattr(sys, "frozen", False):
            # frozen (3.3): наш воркер с фильтром само-шума — первый кандидат
            worker = os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                                  "StarAstraWorker.exe")
            if os.path.isfile(worker):
                candidates.append([worker, "--star-worker=engine", "--standalone"])
        if os.path.isfile(python) and os.path.isfile(bootstrap):
            candidates.append([python, bootstrap, "--standalone"])
        if os.path.isfile(python):
            candidates.append([python, "-m", "sentinel.service"])
        if os.path.isfile(svc_exe):
            candidates.append([svc_exe])
        for cmd in candidates:
            try:
                subprocess.Popen(cmd, cwd=_PROJECT_ROOT, env=env,
                                 creationflags=0x00000008 | 0x08000000)
                return True, os.path.basename(cmd[0])
            except Exception as exc:
                logger.warning("resume via %s failed: %s", cmd, exc)
        if self.service_installed():
            if not _is_admin():
                return False, "uac"
            r = subprocess.run(["sc.exe", "start", SERVICE_NAME],
                               capture_output=True, text=True, timeout=30,
                               creationflags=0x08000000)
            if r.returncode in (0, 1056):
                return True, SERVICE_NAME
            return False, r.stderr.strip() or f"sc exit {r.returncode}"
        return False, "no engine entry point (python -m sentinel.service / sentinel_service.exe)"


# ═════════════════════ Виджеты: кнопки, щит, карточки ═══════════════════════
class RoundedButton(tk.Canvas):
    """Скруглённая кнопка: hover 150 мс, нажатие — сдвиг 1 px. Строки через tr()."""

    def __init__(self, master, text: str, command, style: str = "primary",
                 font=F_BASE, width: int | None = None, **kw):
        self._style = style
        self._bg, self._fg, self._hbg = {
            "primary":   (ACCENT, BG_DARK, ACCENT_HOVER),
            "secondary": (SURFACE_1, TEXT_MAIN, SURFACE_2),
            "outline":   (BG_DARK, TEXT_MAIN, BG_DARK),
            "danger":    (WARN_AMBER, BG_DARK, "#E5B55C"),
        }[style]
        self._disabled = False
        tw = max(60, int(len(text) * 8.2) + 34)
        super().__init__(master, width=width or tw, height=36,
                         bg=master["bg"], highlightthickness=0, **kw)
        self._cmd, self._label = command, text
        self._job = None
        self._pressed = 0
        self._draw(self._bg)
        self.bind("<Enter>", lambda _e: self._animate(self._hbg))
        self.bind("<Leave>", lambda _e: self._animate(self._bg))
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    # публичное
    def set_text(self, text: str) -> None:
        self._label = text
        self._draw(self._bg)

    def set_disabled(self, disabled: bool) -> None:
        self._disabled = disabled
        self.configure(cursor="arrow" if disabled else "hand2")
        self._draw(self._bg if not disabled else _mix(self._bg, BG_DARK, 0.5))

    # внутреннее
    def _draw(self, bg: str) -> None:
        self.delete("all")
        w, h = int(self["width"]), int(self["height"])
        r = 9
        pts = [r, 1, w - r, 1, w - 1, 1, w - 1, r, w - 1, h - r, w - 1, h - 1,
               w - r, h - 1, r, h - 1, 1, h - 1, 1, h - r, 1, r, 1, 1]
        if self._style == "outline":
            self.create_polygon(pts, smooth=True, fill=bg, outline=ACCENT if self._hbg == ACCENT else BORDER, width=1)
        else:
            self.create_polygon(pts, smooth=True, fill=bg, outline=bg)
        self.create_text(w / 2, h / 2 + self._pressed, text=self._label,
                         fill=self._fg if not self._disabled else _mix(self._fg, bg, 0.55),
                         font=F_BASE)
        self._bg_now = bg

    def _animate(self, target: str) -> None:
        if self._disabled:
            return
        if self._job:
            self.after_cancel(self._job)
        start, steps, state = self._bg_now, max(1, ANIM_HOVER_MS // ANIM_FRAME_MS), {"i": 0}

        def step() -> None:
            state["i"] += 1
            t = state["i"] / steps
            self._draw(_mix(start, target, t))
            if state["i"] < steps:
                self._job = self.after(ANIM_FRAME_MS, step)
            else:
                self._job = None

        step()

    def _on_press(self, _e=None) -> None:
        if self._disabled:
            return
        self._pressed = 1
        self._draw(self._bg_now)
        self.move("all", 0, 1)

    def _on_release(self, _e=None) -> None:
        if self._disabled:
            return
        self.move("all", 0, -self._pressed)
        self._pressed = 0
        if self._cmd:
            self._cmd()


class ShieldCanvas(tk.Canvas):
    """Щит: пульс зелёным (~2 c), статичный янтарный на паузе."""

    def __init__(self, master, size: int = 96, **kw):
        super().__init__(master, width=size, height=size, bg=master["bg"],
                         highlightthickness=0, **kw)
        import math
        self._math = math
        self._size = size
        self._paused = False
        self._job = None
        self._phase = 0.0
        self._draw(0.0)

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._restart()

    def start(self) -> None:
        self._restart()

    def stop(self) -> None:
        if self._job:
            self.after_cancel(self._job)
            self._job = None

    def _restart(self) -> None:
        self.stop()
        if not self._paused:
            self._loop()

    def _loop(self) -> None:
        try:
            if self.winfo_toplevel().state() == "iconic":
                self._job = self.after(ANIM_FRAME_MS * 10, self._loop)
                return
        except tk.TclError:
            return
        self._phase = (self._phase + ANIM_FRAME_MS / ANIM_PULSE_MS) % 1.0
        self._draw(self._phase)
        self._job = self.after(ANIM_FRAME_MS, self._loop)

    def _draw(self, pulse: float) -> None:
        self.delete("all")
        s = self._size
        cx = cy = s / 2
        if self._paused:
            color = WARN_AMBER
        else:
            k = 0.5 - 0.5 * self._math.cos(2 * self._math.pi * pulse)
            color = _mix(ACCENT, ACCENT_HOVER, 0.35 + 0.65 * k)
        pts = [cx, cy - s * 0.42, cx + s * 0.34, cy - s * 0.24, cx + s * 0.34, cy + 0.10 * s,
               cx, cy + s * 0.44, cx - s * 0.34, cy + 0.10 * s, cx - s * 0.34, cy - s * 0.24]
        self.create_polygon(pts, fill=color, outline=BORDER, width=2, smooth=True)
        self.create_text(cx, cy - s * 0.02, text="✓" if not self._paused else "⏸",
                         fill=BG_DARK, font=("Segoe UI", int(s * 0.28), "bold"))


def rounded_rect(canvas: tk.Canvas, x1, y1, x2, y2, r: int, **kw) -> int:
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class Card(tk.Frame):
    """Карточка на Canvas со скруглёнными углами; тянется вместе с окном.

    stretch=True  — карточка занимает всё отведённое место (pack/grid expand),
    stretch=False — фиксированная минимальная высота min_height.
    """

    def __init__(self, master, min_height: int = 96, bg: str = SURFACE_1,
                 stretch: bool = True):
        self._bg = bg
        self._min_h = min_height
        self._stretch = stretch
        super().__init__(master, bg=master["bg"])
        self.canvas = tk.Canvas(self, bg=master["bg"], highlightthickness=0,
                                height=min_height)
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.canvas.bind("<Configure>", self._on_resize)
        self._ids: list[int] = []

    def _on_resize(self, event) -> None:
        h = max(self._min_h, event.height) if self._stretch else self._min_h
        if not self._stretch:
            self.canvas.configure(height=h)
        for i in self._ids:
            self.canvas.delete(i)
        self._ids = [rounded_rect(self.canvas, 1, 1, event.width - 2, h - 2, 14,
                                  fill=self._bg, outline=BORDER)]
        self.canvas.tag_lower(self._ids[0])
        # padding 16, внутренний фрейм тянется вместе с карточкой
        self.canvas.coords(self._win, 16, 16)
        self.canvas.itemconfigure(self._win, width=max(1, event.width - 32),
                                  height=max(1, h - 32))


# ═══════════════════════════ Главное окно ═══════════════════════════════════
class StarAstraApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.data = DataProxy()
        self.supervisor = Supervisor()

        # Watchdog (Задача 2.1): UI жив -> движок должен быть под защитой
        # сторожа. Полный выход (_shutdown_app) ставит флаг обратно в False.
        save_app_settings({"engine_keep_alive": True})

        self._paused = False
        self._pause_how: str | None = None
        self._pause_until: float | None = None
        self._auto_resume_job = None
        self._pause_tick_job = None
        self._tick_job = None
        self._hist_job = None
        self._recent_job = None
        self._dismissed: set[int] = set()

        # Исключения (Задача 1.4): in-memory источник истины для UI-фильтра
        self._exclusions: list[dict] = []
        self._excl_self_mtime: float | None = None
        self._excl_guard: threading.Thread | None = None

        # Проверка (Задача 1.3): процесс-раннер starastra_scan.py + JSON-lines
        self._scan_proc: subprocess.Popen | None = None
        self._scan_q: "queue.Queue[dict]" = queue.Queue()
        self._scan_kind: str | None = None
        self._scan_done_flag = False
        self._scan_cancel_requested = False
        self._scan_quarantined = 0
        self._scan_last_done = 0
        self._scan_threat_seq = 0

        # настройки: длительность паузы, закрытие в трей
        cfg = load_app_settings()
        self._pause_minutes = int(cfg.get("pause_duration_min",
                                          DEFAULT_PAUSE_MIN))
        if self._pause_minutes not in PAUSE_CHOICES:
            self._pause_minutes = DEFAULT_PAUSE_MIN
        self._close_to_tray = bool(cfg.get("close_to_tray", True))

        # Планировщик (Задача 2.2): ежедневный фоновый скан
        self._sched_enabled = bool(cfg.get("schedule_enabled", False))
        self._sched_time = str(cfg.get("schedule_time", "03:00"))
        if ":" not in self._sched_time:            # защита от битого значения
            self._sched_time = "03:00"
        self._sched_kind = str(cfg.get("schedule_kind", "quick"))
        if self._sched_kind not in ("quick", "full"):
            self._sched_kind = "quick"
        self._sched_last_run = cfg.get("schedule_last_run")   # "YYYY-MM-DD"|None
        self._sched_proc: subprocess.Popen | None = None
        self._sched_q: "queue.Queue[dict]" = queue.Queue()
        self._sched_done_flag = False
        self._sched_quarantined = 0
        self.tray = None

        self.title(tr("app_title"))
        self.geometry("1080x700")
        self.minsize(900, 600)
        self.configure(bg=BG_DARK)
        icon = os.path.join(_PROJECT_ROOT, "assets", "starastra.ico")
        if os.path.isfile(icon):
            try:
                self.iconbitmap(icon)
            except tk.TclError:
                logger.info("icon not applicable on this platform")

        self._build_sidebar()
        self._build_content()
        self._build_notify()
        self._start_poller()
        self._refresh_history()
        self._tick_counters()
        self.show_section("overview")
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self._refresh_texts_hook: list = []
        # Планировщик (2.2): первый проход — catch-up (приложение могло
        # стартовать позже назначенного времени), далее каждые 30 c.
        self.after(5000, self._schedule_tick)
        self._ensure_engine_at_start()
        self.after(300, self._init_tray)

    # ───────────────────────── Трей ─────────────────────────────────
    def _init_tray(self) -> None:
        try:
            from starastra_ui.tray import TrayController
            self.tray = TrayController(self)
            started = self.tray.start()
        except Exception as exc:
            logger.warning("tray init failed: %s", exc)
            self.tray = None
            return
        if started:
            self._tray_sync_status()

    def _tray_sync_status(self) -> None:
        """Тултип/меню трея по текущему состоянию."""
        if self.tray is None:
            return
        try:
            if self._paused and self._pause_until is not None:
                until = time.strftime(
                    "%H:%M", time.localtime(self._pause_until))
                self.tray.update_status(
                    True, tr("tray_status_paused", until=until),
                    tr("tray_paused_until", until=until))
            else:
                self.tray.update_status(
                    False, tr("tray_status_active"),
                    tr("tray_active_tooltip"))
        except Exception as exc:
            logger.debug("tray sync failed: %s", exc)

    def tray_show_window(self) -> None:
        """Левый клик по иконке трея / «Открыть StarAstra»."""
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
            self.attributes("-topmost", True)
            self.after(200, lambda: self.attributes("-topmost", False))
        except Exception:
            pass

    def tray_pause(self, minutes: int) -> None:
        """Пауза из трея: без диалогов и без открытия окна."""
        if self._paused:
            return
        ok, how, uac = self.supervisor.pause()
        if not ok:
            # движок и так не работает — пауза фиксирует это состояние
            logger.info("tray pause: engine not running (%s)", how)
        self._start_pause(minutes, how if ok else None)

    def shutdown_from_tray(self) -> None:
        """«Выход» в трее: полный выход (движок + UI + иконка)."""
        self._close_to_tray = False
        self._shutdown_app()

    def _on_window_close(self) -> None:
        """X в заголовке: в трей (по настройке) или полный выход."""
        if self._close_to_tray:
            self._minimize_to_tray()
        else:
            self._shutdown_app()

    def _minimize_to_tray(self) -> None:
        self.withdraw()
        cfg = load_app_settings()
        if not cfg.get("tray_note_shown"):
            messagebox.showinfo(
                tr("tray_minimized_title"), tr("tray_minimized_text"))
            save_app_settings({"tray_note_shown": True})
        logger.info("window minimized to tray")

    def _shutdown_app(self) -> None:
        """Полный выход: иконка, таймеры, движок, окно."""
        logger.info("shutdown: full exit requested")
        # watchdog (2.1): полный выход — движок останавливаем НАМЕРЕННО
        save_app_settings({"engine_keep_alive": False})
        # плановый скан (2.2): фоновой процесс не должен пережить выход
        if self._sched_proc is not None and self._sched_proc.poll() is None:
            try:
                self._sched_proc.terminate()
            except Exception as exc:
                logger.warning("sched scan terminate failed: %s", exc)
        audit("engine_stop", {"reason": "app exit"})   # журнал (2.4)
        if self.tray is not None:
            self.tray.stop()
        if not self._paused:
            # останавливаем движок только при полном выходе из активного состояния
            try:
                self.supervisor.pause()
            except Exception as exc:
                logger.warning("engine stop on exit failed: %s", exc)
        self.shield.stop()
        self._poll_stop.set()
        try:
            if self._scan_busy():
                self._scan_proc.terminate()  # type: ignore[union-attr]
        except Exception:
            pass
        for job in (self._tick_job, self._hist_job, self._recent_job,
                    self._auto_resume_job, self._pause_tick_job):
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
        try:
            _launch_mod().release_ui_lock()
        except Exception:
            pass
        self.destroy()

    def _ensure_engine_at_start(self) -> None:
        """Автозапуск движка из UI: если не запущен — поднять в фоне."""

        def worker():
            try:
                lm = _launch_mod()
                pid = lm.read_pid_file()
                if lm.pid_is_engine(pid) or lm.ipc_ping():
                    logger.info("UI startup: engine already running")
                    return
                logger.info("UI startup: engine not running — starting it")
                lm.ensure_engine()
                audit("engine_start", {"by": "ui startup"})  # журнал (2.4)
            except Exception as exc:
                logger.warning("UI startup engine autostart failed: %s", exc)

        threading.Thread(target=worker, daemon=True).start()

    # ───────────────────────── Боковое меню ─────────────────────────
    def _build_sidebar(self) -> None:
        self.sidebar = tk.Frame(self, bg=SURFACE_1, width=200)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self._logo = tk.Label(self.sidebar, text="★ StarAstra", bg=SURFACE_1,
                              fg=ACCENT, font=F_HEAD, pady=18)
        self._logo.pack()
        self._sub = tk.Label(self.sidebar, text=tr("app_subtitle"), bg=SURFACE_1,
                             fg=TEXT_SUB, font=F_SUB)
        self._sub.pack()
        self._nav: dict[str, dict] = {}
        for key, label_key in (("overview", "nav_overview"), ("history", "nav_history"),
                               ("quarantine", "nav_quarantine"), ("settings", "nav_settings")):
            row = tk.Frame(self.sidebar, bg=SURFACE_1, cursor="hand2")
            strip = tk.Frame(row, bg=SURFACE_1, width=3)
            strip.pack(side="left", fill="y")
            lbl = tk.Label(row, text="  " + tr(label_key), bg=SURFACE_1, fg=TEXT_SUB,
                           font=F_BASE, anchor="w", pady=9)
            lbl.pack(fill="x", padx=(6, 0))
            row.pack(fill="x", padx=8, pady=2)
            row.bind("<Button-1>", lambda _e, k=key: self.show_section(k))
            lbl.bind("<Button-1>", lambda _e, k=key: self.show_section(k))
            row.bind("<Enter>", lambda _e, r=row, l=lbl: (
                l.configure(bg=SURFACE_2), r.configure(bg=SURFACE_2)))
            row.bind("<Leave>", lambda _e, r=row, l=lbl: (
                l.configure(bg=SURFACE_1), r.configure(bg=SURFACE_1)))
            self._nav[key] = {"row": row, "label": lbl, "strip": strip,
                              "label_key": label_key}
        foot = tk.Label(self.sidebar, text=tr("nav_footer"), bg=SURFACE_1,
                        fg=TEXT_SUB, font=("Segoe UI", 8), justify="left")
        foot.pack(side="bottom", pady=10)

    def _mark_active_nav(self, key: str) -> None:
        for k, w in self._nav.items():
            active = (k == key)
            w["label"].configure(bg=SURFACE_2 if active else SURFACE_1,
                                 fg=ACCENT if active else TEXT_SUB)
            w["row"].configure(bg=SURFACE_2 if active else SURFACE_1)
            w["strip"].configure(bg=ACCENT if active else SURFACE_1)

    # ───────────────────────── Разделы ─────────────────────────
    def _build_content(self) -> None:
        self._sections: dict[str, tk.Frame] = {}
        for key, builder in (("overview", self._build_overview),
                             ("history", self._build_history),
                             ("quarantine", self._build_quarantine),
                             ("settings", self._build_settings)):
            frame = tk.Frame(self, bg=BG_DARK)
            self._sections[key] = frame
            builder(frame)

    def show_section(self, key: str) -> None:
        self._mark_active_nav(key)
        for k, frame in self._sections.items():
            if k == key:
                frame.pack(fill="both", expand=True, padx=16, pady=12)
                self._fade_in(frame)
            else:
                frame.pack_forget()
        if key == "history":
            self._refresh_history()
        elif key == "quarantine":
            self._refresh_quarantine()

    def _fade_in(self, frame: tk.Frame) -> None:
        steps = max(1, ANIM_FADE_MS // ANIM_FRAME_MS)
        state = {"i": 0}

        def step() -> None:
            if self.state() == "iconic":
                self.after(ANIM_FRAME_MS * 4, step)
                return
            state["i"] += 1
            t = state["i"] / steps
            frame.configure(bg=_mix(BG_DARK, BG_DARK, min(1.0, t)))
            if state["i"] < steps:
                self.after(ANIM_FRAME_MS, step)

        step()

    # ───────────────────────── Обзор ─────────────────────────
    def _build_overview(self, parent: tk.Frame) -> None:
        # адаптивная сетка: статус 3 : счётчики 2 : последние события 2, прогресс фикс.
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=3)
        parent.rowconfigure(1, weight=2)
        parent.rowconfigure(2, weight=2)
        parent.rowconfigure(3, weight=0)

        # карточка статуса (тянется по высоте окна)
        card = Card(parent, min_height=150)
        card.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        body = card.body
        inner = tk.Frame(body, bg=SURFACE_1)
        inner.place(relx=0, rely=0.5, anchor="w", relwidth=1)
        self.shield = ShieldCanvas(inner, size=110)
        self.shield.pack(side="left", padx=(4, 22))
        self.shield.start()
        info = tk.Frame(inner, bg=SURFACE_1)
        info.pack(side="left", fill="y", pady=8)
        self._lbl_status = tk.Label(info, text=tr("status_active"), bg=SURFACE_1,
                                    fg=OK_GREEN, font=F_HEAD)
        self._lbl_status.pack(anchor="w")
        self._lbl_substatus = tk.Label(info, text=tr("status_engine_running"),
                                       bg=SURFACE_1, fg=TEXT_SUB, font=F_SUB)
        self._lbl_substatus.pack(anchor="w")
        self.btn_pause = RoundedButton(info, tr("btn_pause_protection_short"),
                                       self._on_pause_click, style="danger")
        self.btn_pause.pack(anchor="w", pady=(12, 0))
        self._lbl_pause_for = tk.Label(
            info, text=tr("pause_for_label", min=self._pause_minutes),
            bg=SURFACE_1, fg=TEXT_SUB, font=("Segoe UI", 9))
        self._lbl_pause_for.pack(anchor="w", pady=(2, 0))
        # Честный UI (Задача 4.2): без «зелёных галочек» у неработающих
        # функций — явная пометка, чтобы не создавать ложное чувство защиты
        self._lbl_unsupported = tk.Label(
            info, text=tr("overview_unsupported_note"), bg=SURFACE_1,
            fg=TEXT_DIM, font=("Segoe UI", 8), wraplength=420,
            justify="left", anchor="w")
        self._lbl_unsupported.pack(anchor="w", pady=(8, 0))

        # 3 карточки-счётчика (равные доли ширины, тянутся по высоте)
        self._counters: dict[str, tuple[tk.Label, callable]] = {}
        for col, (key, getter) in enumerate((
                ("counters_files", self.data.count_events),
                ("counters_threats", self._threats_count),
                ("counters_quarantine", self.data.quarantine_count))):
            card2 = Card(parent, min_height=110)
            card2.grid(row=1, column=0, sticky="nsew", pady=5)
            card2.body.columnconfigure(0, weight=1)
            card2.body.rowconfigure(0, weight=1)
            cell = tk.Frame(card2.body, bg=SURFACE_1)
            cell.grid(row=0, column=0, sticky="nsew")
            val = tk.Label(cell, text="—", bg=SURFACE_1, fg=ACCENT, font=F_COUNTER)
            val.pack(expand=True)
            cap = tk.Label(cell, text=tr(key), bg=SURFACE_1, fg=TEXT_SUB, font=F_SUB)
            cap.pack(expand=True)
            self._counters[key] = (val, getter)

        # «Последние события»: 5 строк, клик → История
        self._build_recent(parent)

        # ── Проверка (Задача 1.3): Quick/Full + прогресс/ETA/отмена ──
        scan_card = Card(parent, min_height=150, stretch=False)
        scan_card.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        scan_btns = tk.Frame(scan_card.body, bg=SURFACE_1)
        scan_btns.pack(fill="x")
        self._btn_scan_quick = RoundedButton(
            scan_btns, tr("scan_btn_quick"), self._on_scan_quick, style="primary")
        self._btn_scan_quick.pack(side="left", padx=(0, 8))
        self._btn_scan_full = RoundedButton(
            scan_btns, tr("scan_btn_full"), self._on_scan_full, style="secondary")
        self._btn_scan_full.pack(side="left")
        self._btn_scan_cancel = RoundedButton(
            scan_btns, tr("scan_btn_cancel"), self._on_scan_cancel, style="danger")
        self._lbl_scan_state = tk.Label(scan_card.body, text=tr("scan_idle"),
                                        bg=SURFACE_1, fg=TEXT_SUB, font=F_SUB)
        self._lbl_scan_state.pack(anchor="w", pady=(8, 0))
        self._lbl_scan_file = tk.Label(scan_card.body, text="", bg=SURFACE_1,
                                       fg=TEXT_MAIN, font=("Consolas", 9),
                                       anchor="w")
        self._lbl_scan_file.pack(fill="x")
        self._scan_bar = ttk.Progressbar(scan_card.body, mode="determinate",
                                         maximum=100, value=0)
        self._scan_bar.pack(fill="x", pady=(4, 0))
        self._lbl_scan_stats = tk.Label(scan_card.body, text="", bg=SURFACE_1,
                                        fg=TEXT_SUB, font=("Segoe UI", 9),
                                        anchor="w")
        self._lbl_scan_stats.pack(fill="x")
        # восстановить состояние, если скан уже идёт (после _retranslate)
        self._set_scan_buttons(running=self._scan_busy())

    # ───────────────────────── Последние события ────────────────────
    def _build_recent(self, parent: tk.Frame) -> None:
        card = Card(parent, min_height=120)
        card.grid(row=2, column=0, sticky="nsew", pady=5)
        head = tk.Frame(card.body, bg=SURFACE_1)
        head.pack(fill="x")
        self._recent_title = tk.Label(head, text=tr("recent_events_title"),
                                      bg=SURFACE_1, fg=TEXT_MAIN, font=F_HEAD)
        self._recent_title.pack(side="left")
        self._recent_all_btn = RoundedButton(head, tr("btn_all_events"),
                                             lambda: self.show_section("history"),
                                             style="outline", width=110)
        self._recent_all_btn.pack(side="right")
        self._recent_rows: list[tk.Label] = []
        for _ in range(5):
            row = tk.Label(card.body, text="", bg=SURFACE_1, fg=TEXT_MAIN,
                           font=("Consolas", 9), anchor="w", cursor="hand2")
            row.pack(fill="x", pady=1)
            row.bind("<Button-1>", self._on_recent_click)
            self._recent_rows.append(row)
        self._recent_data: list[dict] = []
        self._tick_recent()

    def _tick_recent(self) -> None:
        rows = self.data.recent_events(limit=5)
        # свежие вверху: recent_events уже DESC — оставляем как есть
        self._recent_data = rows
        for i, rowlbl in enumerate(self._recent_rows):
            if i < len(rows):
                row = rows[i]
                sev = self._severity_of(row)
                color = {"HIGH": DANGER_RED, "MEDIUM": WARN_AMBER,
                         "LOW": OK_GREEN}.get(sev, TEXT_SUB)
                dot = {"HIGH": "●", "MEDIUM": "●", "LOW": "●"}.get(sev, "○")
                ts = (row.get("timestamp") or "")[11:19]
                etype = row.get("event_type") or ""
                path = _short(row.get("image_path") or "", 60)
                rowlbl.configure(
                    text=f"[{ts}] {dot} [{etype}] {path}", fg=color)
            else:
                rowlbl.configure(
                    text=tr("recent_events_empty") if i == 0 else "",
                    fg=TEXT_SUB)
        self._recent_job = self.after(RECENT_POLL_MS, self._tick_recent)

    def _on_recent_click(self, event) -> None:
        row = self._recent_rows.index(event.widget) \
            if event.widget in self._recent_rows else -1
        if row < 0 or row >= len(self._recent_data):
            return
        data = self._recent_data[row]
        self.show_section("history")
        self._refresh_history()  # свежий снимок таблицы
        table = self._history_table
        want_ts = data.get("timestamp") or ""
        want_path = _short(data.get("image_path") or "", 60)
        for item in table.get_children():
            vals = table.item(item, "values")
            if vals and vals[0] == want_ts and vals[3] == want_path:
                table.selection_set(item)
                table.focus(item)
                table.see(item)
                break

    def _threats_count(self) -> int:
        # факты угроз = записи карантина (движок пишет их при score > порога)
        return self.data.quarantine_count()

    def _tick_counters(self) -> None:
        for val, getter in self._counters.values():
            try:
                val.configure(text=str(getter()))
            except Exception:
                val.configure(text="—")
        self._tick_job = self.after(5000, self._tick_counters)

    # ───────────────────────── Проверка (Quick/Full scan) ───────────────────
    # Задача 1.3. Скан выполняет OnDemandScanner движка в ОТДЕЛЬНОМ процессе
    # (starastra_scan.py, JSON-lines в stdout). UI: прогресс/ETA/отмена.
    # Отмена — terminate процесса-сканера: у UI нет IPC-канала к объекту
    # сканера, чтобы вызвать его cancel()/pause()/resume() (см. starastra_scan.py).
    SCAN_RUNNER = os.path.join(_PROJECT_ROOT, "starastra_scan.py")

    def _scan_busy(self) -> bool:
        return self._scan_proc is not None and self._scan_proc.poll() is None

    def _on_scan_quick(self) -> None:
        self._start_scan("quick")

    def _on_scan_full(self) -> None:
        if messagebox.askyesno(tr("scan_confirm_full_title"),
                               tr("scan_confirm_full_text"), icon="warning"):
            self._start_scan("full")

    def _start_scan(self, kind: str) -> None:
        if self._sched_busy():
            messagebox.showwarning(tr("scan_busy_title"),
                                   tr("sched_busy_text"))
            return
        if self._scan_busy():
            messagebox.showwarning(tr("scan_busy_title"), tr("scan_busy_text"))
            return
        if not getattr(sys, "frozen", False) and not os.path.isfile(self.SCAN_RUNNER):
            messagebox.showerror(tr("err_title_generic"), tr("scan_runner_missing"))
            return
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [os.path.join(_PROJECT_ROOT, "sentinel_pkg"),
             os.path.join(_PROJECT_ROOT, "Sentinel", "_internal")])
        try:
            self._scan_proc = subprocess.Popen(
                _scan_worker_cmd(kind),
                cwd=_PROJECT_ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=0x08000000)
        except Exception as exc:
            messagebox.showerror(tr("err_title_generic"),
                                 tr("scan_start_failed", err=exc))
            return
        self._scan_kind = kind
        self._scan_done_flag = False
        self._scan_cancel_requested = False
        self._scan_quarantined = 0
        self._scan_last_done = 0
        self._set_scan_buttons(running=True)
        audit("scan_started", {"kind": kind, "scheduled": False})  # 2.4
        self._lbl_scan_state.configure(
            text=tr("scan_running", kind=tr("scan_kind_" + kind)), fg=ACCENT)
        self._lbl_scan_stats.configure(text="")
        threading.Thread(target=self._read_scan_output, daemon=True).start()
        self.after(300, self._scan_poll)

    def _read_scan_output(self) -> None:
        """Фон: stdout процесса-сканера -> очередь (UI забирает в своём потоке)."""
        proc = self._scan_proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue  # посторонний stdout движка — не наш протокол
                self._scan_q.put(obj)
        except Exception as exc:
            logger.warning("scan reader failed: %s", exc)

    def _scan_poll(self) -> None:
        """Дренаж очереди сканера (UI-поток, after — без блокировок)."""
        while True:
            try:
                obj = self._scan_q.get_nowait()
            except queue.Empty:
                break
            kind = obj.get("type")
            if kind == "progress":
                self._on_scan_progress(obj)
            elif kind == "threat":
                self._on_scan_threat(obj)
            elif kind == "done":
                self._on_scan_done(obj)
        if self._scan_busy():
            self.after(300, self._scan_poll)
            return
        if not self._scan_done_flag:
            # процесс умер без 'done': отмена пользователем или падение.
            # Залпачиваем: состояние финальное, повторные poll ничего не делают.
            self._scan_done_flag = True
            self._reset_scan_ui()
            if self._scan_cancel_requested:
                self._lbl_scan_state.configure(text=tr("scan_cancelled_state"),
                                               fg=WARN_AMBER)
                messagebox.showinfo(tr("scan_cancelled_title"),
                                    tr("scan_cancelled_text",
                                       files=self._scan_last_done))
            else:
                self._lbl_scan_state.configure(text=tr("scan_failed_state"),
                                               fg=DANGER_RED)
                messagebox.showerror(tr("scan_failed_title"),
                                     tr("scan_failed_text"))
        self._scan_proc = None
        self._scan_cancel_requested = False

    def _on_scan_progress(self, p: dict) -> None:
        done = int(p.get("files_scanned") or 0)
        total = int(p.get("total_estimate") or 0)
        elapsed = float(p.get("elapsed") or 0.0)
        self._scan_last_done = done
        self._lbl_scan_file.configure(
            text=_short(p.get("current_file") or "", 72))
        if total > 0:
            try:
                self._scan_bar.stop()
            except Exception:
                pass
            self._scan_bar.configure(mode="determinate", maximum=total,
                                     value=min(done, total))
            eta = (elapsed / done * (total - done)) if done > 0 else None
            self._lbl_scan_stats.configure(
                text=tr("scan_stats_eta", done=done, total=total,
                        eta=_fmt_duration(eta)))
        else:
            # оценки объёма нет (память/автозагрузка) — неопределённый режим
            if str(self._scan_bar["mode"]) != "indeterminate":
                self._scan_bar.configure(mode="indeterminate", value=0)
                self._scan_bar.start(60)
            self._lbl_scan_stats.configure(
                text=tr("scan_stats_noeta", done=done,
                        eta=_fmt_duration(elapsed)))

    def _on_scan_threat(self, t: dict) -> None:
        if t.get("quarantined"):
            self._scan_quarantined += 1
        sev = (str(t.get("severity") or "HIGH").upper() or "HIGH")
        self._scan_threat_seq += 1
        row = {"id": -1000 - self._scan_threat_seq,   # вне id-пространства events.db
               "event_type": "scan_threat",
               "image_path": t.get("path") or "",
               "extra": json.dumps(
                   {"severity": sev,
                    "action": f"{t.get('name', '')}: score={t.get('score')}"},
                   ensure_ascii=False)}
        self.notify.push_event(row)   # карточка «В карантин / Отмена / Подробнее»
        if sev in ("HIGH", "CRITICAL") and self.system_toast:
            try:
                self.system_toast(tr("notify_threat_title"), t.get("path") or "")
            except Exception as exc:
                logger.debug("toast failed: %s", exc)

    def _on_scan_done(self, obj: dict) -> None:
        self._scan_done_flag = True
        status = str(obj.get("status") or "")
        files = int(obj.get("files_scanned") or 0)
        threats = int(obj.get("threats_found") or 0)
        dur = _fmt_duration(float(obj.get("duration") or 0.0))
        err = obj.get("error")
        self._reset_scan_ui()
        kind = tr("scan_kind_" + (self._scan_kind or "quick"))
        if status == "CANCELLED":
            self._lbl_scan_state.configure(text=tr("scan_cancelled_state"),
                                           fg=WARN_AMBER)
            messagebox.showinfo(tr("scan_cancelled_title"),
                                tr("scan_cancelled_text", files=files))
        elif status == "ERROR" or err:
            self._lbl_scan_state.configure(text=tr("scan_failed_state"),
                                           fg=DANGER_RED)
            messagebox.showerror(tr("scan_failed_title"),
                                 tr("scan_failed_text")
                                 + (f"\n\n{err}" if err else ""))
        else:
            self._lbl_scan_state.configure(
                text=tr("scan_done_state", kind=kind), fg=OK_GREEN)
            text = tr("scan_done_text", files=files, threats=threats,
                      quar=self._scan_quarantined, dur=dur)
            if threats:
                messagebox.showwarning(tr("scan_done_title"), text, icon="warning")
            else:
                messagebox.showinfo(tr("scan_done_title"), text)
        logger.info("scan %s finished: status=%s files=%s threats=%s "
                    "quarantined=%s", self._scan_kind, status, files, threats,
                    self._scan_quarantined)
        audit("scan_finished", {"kind": self._scan_kind, "scheduled": False,
                                "status": status, "files": files,
                                "threats": threats})     # журнал (2.4)
        try:
            self._tick_counters()
            if self._sections["quarantine"].winfo_ismapped():
                self._refresh_quarantine()
        except Exception:
            pass

    def _on_scan_cancel(self) -> None:
        if not self._scan_busy():
            return
        if not messagebox.askyesno(tr("scan_cancel_confirm_title"),
                                   tr("scan_cancel_confirm_text"),
                                   icon="warning"):
            return
        self._scan_cancel_requested = True
        audit("scan_cancelled", {"kind": self._scan_kind})   # журнал (2.4)
        try:
            self._scan_proc.terminate()
        except Exception as exc:
            logger.warning("scan terminate failed: %s", exc)
        # завершение подберёт _scan_poll (проц умрёт -> ветка without 'done')

    def _reset_scan_ui(self) -> None:
        self._set_scan_buttons(running=False)
        self._lbl_scan_file.configure(text="")
        try:
            self._scan_bar.stop()
        except Exception:
            pass
        self._scan_bar.configure(mode="determinate", value=0)
        self._lbl_scan_stats.configure(text="")

    def _set_scan_buttons(self, running: bool) -> None:
        if running:
            self._btn_scan_quick.set_disabled(True)
            self._btn_scan_full.set_disabled(True)
            self._btn_scan_cancel.pack(side="left", padx=(8, 0))
        else:
            self._btn_scan_quick.set_disabled(False)
            self._btn_scan_full.set_disabled(False)
            self._btn_scan_cancel.pack_forget()

    # ───────────────────────── Планировщик (Задача 2.2) ─────────────────────
    def _sched_busy(self) -> bool:
        return self._sched_proc is not None and self._sched_proc.poll() is None

    def _schedule_tick(self) -> None:
        """Каждые 30 c: пора ли запускать ежедневный фоновый скан.

        Catch-up: если приложение стартовало позже назначенного времени,
        скан всё равно выполнится сегодня (now >= цель И last_run != сегодня).
        Пауза защиты / запущенный ручной скан откладывают запуск до следующего
        тика (в тот же день), но не отменяют его.
        """
        self.after(30_000, self._schedule_tick)
        try:
            if not self._sched_enabled:
                return
            if self._paused:
                return
            until = load_app_settings().get("protection_paused_until")
            if until and time.time() < float(until):
                return                                   # пауза (в т.ч. извне)
            if self._sched_busy() or self._scan_busy():
                return
            today = time.strftime("%Y-%m-%d")
            if self._sched_last_run == today:
                return
            hh, mm = self._sched_time.split(":")
            target_min = int(hh) * 60 + int(mm)
            now = time.localtime()
            if now.tm_hour * 60 + now.tm_min < target_min:
                return
            self._sched_last_run = today                 # фиксируем ДО старта
            save_app_settings({"schedule_last_run": today})
            self._start_scheduled_scan()
        except Exception as exc:
            logger.warning("schedule tick failed: %s", exc)

    def _start_scheduled_scan(self) -> None:
        """Фоновый скан без открытия окна (starastra_scan.py, JSON-lines)."""
        kind = self._sched_kind
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [os.path.join(_PROJECT_ROOT, "sentinel_pkg"),
             os.path.join(_PROJECT_ROOT, "Sentinel", "_internal")])
        # Переопределяемая команда (для быстрых тестов): ключ
        # schedule_scan_cmd — список аргументов, "{kind}" подставляется.
        cmd = load_app_settings().get("schedule_scan_cmd")
        if not cmd:
            if not getattr(sys, "frozen", False) and \
                    not os.path.isfile(self.SCAN_RUNNER):
                logger.error("scheduled scan: runner missing")
                return
            cmd = _scan_worker_cmd(kind)
        else:
            # "{kind}" подставляется только в аргументах, где плейсхолдер
            # есть (в остальных могут быть фигурные скобки, напр. JSON)
            cmd = [c.format(kind=kind)
                   if isinstance(c, str) and "{kind}" in c else c
                   for c in cmd]
        try:
            self._sched_proc = subprocess.Popen(
                cmd, cwd=_PROJECT_ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=0x08000000)
        except Exception as exc:
            logger.error("scheduled scan start failed: %s", exc)
            return
        self._sched_done_flag = False
        self._sched_quarantined = 0
        self._set_scan_buttons(running=True)   # ручной скан недоступен, пока
        self._btn_scan_cancel.pack_forget()    # плановый не отменяется из UI
        kind_txt = tr("scan_kind_" + kind)
        if self.system_toast:
            try:
                self.system_toast(tr("sched_started_toast"), kind_txt)
            except Exception as exc:
                logger.debug("toast failed: %s", exc)
        audit("scan_started", {"kind": kind, "scheduled": True})
        threading.Thread(target=self._read_sched_output,
                         daemon=True).start()
        self.after(300, self._sched_poll)

    def _read_sched_output(self) -> None:
        """Фон: stdout процесса планового скана -> очередь (аналог ручного)."""
        proc = self._sched_proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                self._sched_q.put(obj)
        except Exception as exc:
            logger.warning("sched reader failed: %s", exc)

    def _sched_poll(self) -> None:
        """Дренаж очереди планового скана (UI-поток, after)."""
        while True:
            try:
                obj = self._sched_q.get_nowait()
            except queue.Empty:
                break
            kind = obj.get("type")
            if kind == "threat":
                self._on_scan_threat(obj)   # карточки угроз — как у ручного
            elif kind == "done":
                self._on_sched_done(obj)
        if self._sched_busy():
            self.after(300, self._sched_poll)
            return
        if not self._sched_done_flag:
            # процесс умер без 'done' (падение/убит извне)
            self._sched_done_flag = True
            if not self._scan_busy():       # кнопки ручного не заняты
                self._set_scan_buttons(running=False)
            self._on_sched_done({"type": "done", "status": "ERROR",
                                 "files_scanned": 0, "threats_found": 0,
                                 "duration": 0.0, "error": None})
        self._sched_proc = None

    def _on_sched_done(self, obj: dict) -> None:
        """Результат планового скана: тост + запись в Историю + audit."""
        self._sched_done_flag = True   # не даём _sched_poll додумать ERROR
        status = str(obj.get("status") or "")
        files = int(obj.get("files_scanned") or 0)
        threats = int(obj.get("threats_found") or 0)
        dur = _fmt_duration(float(obj.get("duration") or 0.0))
        err = obj.get("error")
        kind_txt = tr("scan_kind_" + (self._sched_kind or "quick"))
        if status in ("COMPLETED", "") and not err:
            title = tr("sched_done_title")
            text = tr("sched_done_toast", kind=kind_txt, files=files,
                      threats=threats, dur=dur)
            sev, action = "INFO", tr("sched_result_action", kind=kind_txt,
                                     files=files, threats=threats)
            audit("scan_finished", {"kind": self._sched_kind, "scheduled": True,
                                    "status": status, "files": files,
                                    "threats": threats})
        else:
            title = tr("sched_failed_title")
            text = tr("sched_failed_toast") + (f"\n\n{err}" if err else "")
            sev, action = "LOW", tr("sched_failed_action")
            audit("scan_finished", {"kind": self._sched_kind, "scheduled": True,
                                    "status": status or "ERROR",
                                    "error": str(err) if err else None})
        if self.system_toast:
            try:
                self.system_toast(title, text)
            except Exception as exc:
                logger.debug("toast failed: %s", exc)
        write_ui_event("scan_scheduled", "Star Astra Scheduler",
                       {"severity": sev, "action": action},
                       source="starastra_scheduler")
        logger.info("scheduled scan done: status=%s files=%s threats=%s",
                    status, files, threats)
        try:
            self._tick_counters()
        except Exception:
            pass

    # ───────────────────────── Пауза защиты ─────────────────────────
    def _ask_pause_duration(self) -> int | None:
        """Диалог выбора длительности: radio 10/15/30/45/60. None = отмена."""
        dlg = tk.Toplevel(self)
        dlg.title(tr("confirm_pause_title"))
        dlg.configure(bg=SURFACE_1)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()
        var = tk.IntVar(value=self._pause_minutes)
        tk.Label(dlg, text=tr("pause_dialog_choose"), bg=SURFACE_1,
                 fg=TEXT_MAIN, font=F_HEAD).pack(anchor="w", padx=16, pady=(14, 6))
        for n in PAUSE_CHOICES:
            rb = tk.Radiobutton(dlg, text=tr("pause_minutes_item", min=n),
                                variable=var, value=n, bg=SURFACE_1,
                                fg=TEXT_MAIN, selectcolor=SURFACE_2,
                                activebackground=SURFACE_1,
                                activeforeground=ACCENT, font=F_BASE)
            rb.pack(anchor="w", padx=24)
        btns = tk.Frame(dlg, bg=SURFACE_1)
        btns.pack(fill="x", padx=16, pady=14)
        result: list[int | None] = [None]

        def confirm():
            result[0] = var.get()
            dlg.destroy()

        def _refresh_btn(*_a):
            try:
                stop_btn.set_text(tr("pause_btn_stop_for", min=var.get()))
            except Exception:
                pass

        stop_btn = RoundedButton(btns, tr("pause_btn_stop_for", min=var.get()),
                                 confirm, style="danger")
        stop_btn.pack(side="right")
        var.trace_add("write", _refresh_btn)
        RoundedButton(btns, tr("btn_cancel"),
                      lambda: dlg.destroy(), style="secondary").pack(side="right",
                                                                     padx=(0, 8))
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        dlg.wait_window()
        return result[0]

    def _on_pause_click(self) -> None:
        if self._paused:
            self._resume()
            return
        minutes = self._ask_pause_duration()
        if minutes is None:
            return
        if minutes > 30:
            if not messagebox.askyesno(
                    tr("pause_long_title"),
                    tr("pause_long_confirm", min=minutes), icon="warning"):
                return
        if not messagebox.askyesno(tr("confirm_pause_title"),
                                   tr("confirm_pause_text", min=minutes),
                                   icon="warning"):
            return
        ok, how, uac = self.supervisor.pause()
        if uac and not ok:
            messagebox.showwarning(tr("err_uac_required_title"),
                                   tr("err_uac_required_text"))
            return
        if not ok:
            # движок и так не работает: честно сообщаем, но пауза «включается»,
            # чтобы кнопка стала «Включить сейчас» и можно было запустить движок
            messagebox.showwarning(
                tr("err_engine_not_running_title"),
                tr("err_engine_not_running_text", min=minutes))
        self._start_pause(minutes, how if ok else None)

    def _start_pause(self, minutes: int, how: str | None) -> None:
        """Общий сценарий паузы (UI-диалог и трей): движок уже остановлен."""
        self._pause_minutes = minutes
        save_app_settings({"pause_duration_min": minutes,
                           "protection_paused_until":
                               float(self.tk.call("clock", "seconds"))
                               + minutes * 60})   # watchdog (2.1) читает это
        self._paused = True
        self._pause_how = how
        self._pause_until = self.tk.call("clock", "seconds") + minutes * 60
        self._apply_pause_state()
        audit("pause", {"minutes": minutes, "how": how})   # журнал (2.4)
        # автовозобновление отдельным id, тик-таймер — своим (не затирают друг друга)
        self._auto_resume_job = self.after(
            minutes * 60 * 1000, self._resume)
        self._tray_sync_status()

    def _resume(self, *_a) -> None:
        if not self._paused:
            return
        for job in (self._auto_resume_job, self._pause_tick_job):
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
        self._auto_resume_job = None
        self._pause_tick_job = None
        if self._pause_how is None and self.supervisor.engine_pid() is not None:
            # движок уже кто-то запустил — не дублируем процесс
            ok, how = True, "already running"
        else:
            ok, how = self.supervisor.resume()
        self._paused = False
        self._pause_until = None
        save_app_settings({"protection_paused_until": None})   # watchdog (2.1)
        self._apply_pause_state()
        audit("resume", {})          # журнал (2.4)
        self._tray_sync_status()
        if ok:
            self._lbl_substatus.configure(text=tr("status_engine_started_how", how=how))
        else:
            messagebox.showwarning(tr("err_engine_start_failed_title"),
                                   tr("err_engine_start_failed_text", how=how))

    def _apply_pause_state(self) -> None:
        if self._paused:
            self.shield.set_paused(True)
            self.btn_pause.set_text(tr("btn_resume_now"))
            self._lbl_substatus.configure(text=tr("status_engine_stopped"))
            self._tick_pause_label()
        else:
            self.shield.set_paused(False)
            self._lbl_status.configure(text=tr("status_active"), fg=OK_GREEN)
            self.btn_pause.set_text(tr("btn_pause_protection_short"))
            self._lbl_substatus.configure(text=tr("status_engine_running"))
        if hasattr(self, "_lbl_pause_for"):
            self._lbl_pause_for.configure(
                text=tr("pause_for_label", min=self._pause_minutes))

    def _tick_pause_label(self) -> None:
        if not self._paused or self._pause_until is None:
            return
        left = max(0, int(self._pause_until - self.tk.call("clock", "seconds")))
        self._lbl_status.configure(
            text=tr("status_paused_until",
                    until=time.strftime("%H:%M", time.localtime(self._pause_until)),
                    min=left // 60),
            fg=WARN_AMBER)
        if left % 5 == 0:
            self._tray_sync_status()
        self._pause_tick_job = self.after(1000, self._tick_pause_label)

    # ───────────────────────── История ─────────────────────────
    def _build_history(self, parent: tk.Frame) -> None:
        top = tk.Frame(parent, bg=BG_DARK)
        top.pack(fill="x", pady=(0, 8))
        tk.Label(top, text=tr("history_filter_label"), bg=BG_DARK, fg=TEXT_SUB,
                 font=F_SUB).pack(side="left")
        self._hist_filter = ttk.Combobox(top, state="readonly", width=14,
                                         values=(tr("history_filter_all"), "HIGH",
                                                 "MEDIUM", "LOW", "INFO",
                                                 tr("sev_own")))
        self._hist_filter.current(0)
        self._hist_filter.pack(side="left", padx=6)
        self._hist_refresh_btn = RoundedButton(top, tr("btn_refresh"),
                                               self._refresh_history, style="primary")
        self._hist_refresh_btn.pack(side="left", padx=6)

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Star.Treeview", background=SURFACE_1, fieldbackground=SURFACE_1,
                        foreground=TEXT_MAIN, rowheight=30, font=F_SUB, borderwidth=0)
        style.configure("Star.Treeview.Heading", background=SURFACE_2, foreground=TEXT_SUB,
                        font=F_SUB, relief="flat")
        style.map("Star.Treeview", background=[("hover", SURFACE_2)])
        style.map("Star.Treeview.Heading", background=[("active", SURFACE_2)])

        cols = ("time", "severity", "type", "path", "action")
        table = ttk.Treeview(parent, columns=cols, show="headings", height=22,
                             style="Star.Treeview")
        for c, key, w in zip(cols, ("col_time", "col_severity", "col_type",
                                    "col_path", "col_action"),
                             (150, 110, 130, 430, 130)):
            table.heading(c, text=tr(key))
            table.column(c, width=w, anchor="w")
        table.pack(fill="both", expand=True)
        table.tag_configure("stripe", background=SURFACE_STRIPE)
        table.tag_configure("HIGH", foreground=DANGER_RED)
        table.tag_configure("MEDIUM", foreground=WARN_AMBER)
        table.tag_configure("LOW", foreground=OK_GREEN)
        table.tag_configure("INFO", foreground=TEXT_SUB)
        table.tag_configure("OWN", foreground=TEXT_DIM)
        table.tag_configure("EXCLUDED", foreground=TEXT_DIM)
        # колонка «путь» тянется при изменении ширины окна
        def _stretch_hist(event, tbl=table, cols=cols):
            widths = {c: tbl.column(c, "width") for c in cols}
            fixed = sum(w for c, w in widths.items() if c != "path")
            tbl.column("path", width=max(220, event.width - fixed - 24))
        table.bind("<Configure>", _stretch_hist)
        self._history_table = table
        self._history_cols = cols

    _OWN_PREFIXES: tuple[str, ...] = tuple(
        os.path.normcase(os.path.normpath(p))
        for p in (
            os.path.join(_PROJECT_ROOT, "sentinel_pkg"),
            os.path.join(_PROJECT_ROOT, "Sentinel"),
            os.path.join(_PROJECT_ROOT, "assets"),
            os.path.join(_PROJECT_ROOT, "starastra_i18n"),
            os.path.join(_PROJECT_ROOT, "starastra_notify"),
            os.path.join(_PROJECT_ROOT, ".venv"),
        ))

    def _is_own_file(self, path: str) -> bool:
        """Файл самого StarAstra/движка — не угроза (реакция на свои файлы)."""
        if not path:
            return False
        norm = os.path.normcase(os.path.normpath(path))
        if norm.startswith(self._OWN_PREFIXES):
            return True
        low = norm.lower()
        # компоненты самого продукта в любом расположении (venv, TEMP-копии)
        markers = ("sentinel_service.exe", "sentinel_gui.exe", "sentinel_cli.exe",
                   "sentinel_tray.exe", "starastra", "sentinel_ipc", "sentinel",
                    "events.db", "quarantine.db", "python.exe", "pythonw.exe")
        base = os.path.basename(low)
        if any(m in base for m in markers):
            return True
        return "star-astra" in low and ("\\sentinel" in low or low.endswith(".db"))

    def _severity_of(self, row: dict) -> str:
        try:
            extra = json.loads(row.get("extra") or "{}")
        except Exception:
            extra = {}
        path = row.get("image_path") or ""
        # свои файлы не могут быть угрозой — в каких бы extra они ни пришли
        if self._is_own_file(path):
            return "OWN"
        # исключения пользователя (UI-слой, Задача 1.4): событие гасится —
        # не поднимает карточку/тост, в истории помечается как EXCLUDED
        if self._excl_matches(row, path):
            return "EXCLUDED"
        sev = str(extra.get("severity", "")).upper()
        if sev in ("HIGH", "CRITICAL", "MEDIUM", "LOW", "INFO"):
            return "HIGH" if sev == "CRITICAL" else sev
        if row.get("event_type") == "file_quarantined":
            return "HIGH"
        # yara_hit: путь файла присутствует в карантине движка = факт угрозы
        plow = path.lower()
        if plow and any(
                (q.get("original_path") or "").lower() == plow
                or (q.get("sha256") and q["sha256"] == (row.get("hash_sha256") or ""))
                for q in self._quar_paths()):
            return "HIGH"
        return "INFO"

    def _quar_paths(self) -> list[dict]:
        if not hasattr(self, "_quar_cache") or time.monotonic() - self._quar_cache[0] > 5:
            self._quar_cache = (time.monotonic(), self.data.quarantine_list())
        return self._quar_cache[1]

    def _refresh_history(self) -> None:
        table = self._history_table
        table.delete(*table.get_children())
        flt = self._hist_filter.get()
        for i, row in enumerate(self.data.recent_events(limit=100)):
            sev = self._severity_of(row)
            if flt != tr("history_filter_all") and sev != flt:
                continue
            try:
                extra = json.loads(row.get("extra") or "{}")
            except Exception:
                extra = {}
            dots = {"HIGH": "●", "MEDIUM": "●", "LOW": "●", "INFO": "○",
                    "OWN": "◇", "EXCLUDED": "◇"}
            sev_cell = f'{dots[sev]} {tr("sev_" + sev.lower())}'
            action = str(extra.get("action") or extra.get("description") or "")
            if not action and extra.get("alert_kind") == "behavior":
                # behavior_alert: локализованное имя правила + итог
                action = tr(str(extra.get("title_key") or "behavior_alert_title"))
                if "suspended" in extra:
                    action += (" — " + (tr("behavior_suspended")
                                        if extra.get("suspended")
                                        else tr("behavior_not_suspended")))
            vals = (row.get("timestamp") or "", sev_cell, row.get("event_type") or "",
                    _short(row.get("image_path") or "", 60), action)
            # NB: [] + [sev], а не тернарник с [] — иначе severity-tag терялся на чётных строках
            tags = (["stripe"] if i % 2 else []) + [sev]
            table.insert("", "end", values=vals, tags=tags)
        self._hist_job = self.after(HISTORY_POLL_MS, self._refresh_history) \
            if self._sections.get("history") and self._sections["history"].winfo_ismapped() \
            else None

    # ───────────────────────── Карантин ─────────────────────────
    def _build_quarantine(self, parent: tk.Frame) -> None:
        head = tk.Label(parent, text=tr("quarantine_title"), bg=BG_DARK,
                        fg=TEXT_MAIN, font=F_HEAD)
        head.pack(anchor="w", pady=(0, 8))
        cols = ("name", "path", "score", "status")
        table = ttk.Treeview(parent, columns=cols, show="headings", height=20,
                             style="Star.Treeview")
        for c, key, w in zip(cols, ("col_name", "col_path", "col_score", "col_status"),
                             (200, 380, 70, 110)):
            table.heading(c, text=tr(key))
            table.column(c, width=w, anchor="w")
        table.pack(fill="both", expand=True)
        table.tag_configure("stripe", background=SURFACE_STRIPE)
        def _stretch_q(event, tbl=table, cols=cols):
            widths = {c: tbl.column(c, "width") for c in cols}
            fixed = sum(w for c, w in widths.items() if c != "path")
            tbl.column("path", width=max(200, event.width - fixed - 24))
        table.bind("<Configure>", _stretch_q)
        self._quarantine_table = table

        row = tk.Frame(parent, bg=BG_DARK)
        row.pack(fill="x", pady=8)
        self.btn_restore = RoundedButton(row, tr("btn_restore"),
                                         self._on_restore, style="primary")
        self.btn_restore.pack(side="left", padx=4)
        self.btn_qdel = RoundedButton(row, tr("btn_delete_forever"),
                                      self._on_delete, style="danger")
        self.btn_qdel.pack(side="left", padx=4)
        RoundedButton(row, tr("btn_refresh"), self._refresh_quarantine,
                      style="secondary").pack(side="left", padx=4)
        if not self.data.engine_actions_available():
            for b in (self.btn_restore, self.btn_qdel):
                b.set_disabled(True)
                _tooltip(b, tr("notify_unavailable_quarantine"))

    def _refresh_quarantine(self) -> None:
        table = self._quarantine_table
        table.delete(*table.get_children())
        items = self.data.quarantine_list()
        if not items:
            table.insert("", "end", values=(tr("quarantine_empty"), "", "", ""))
            return
        for i, rec in enumerate(items):
            path = rec.get("original_path") or ""
            tags = ["stripe"] if i % 2 else []
            table.insert("", "end", iid=rec.get("id"),
                         values=(os.path.basename(path), _short(path, 55),
                                 rec.get("score", ""), rec.get("decision", "")),
                         tags=tuple(tags))

    def _selected_qid(self) -> str | None:
        sel = self._quarantine_table.selection()
        return sel[0] if sel else None

    def _on_restore(self) -> None:
        qid = self._selected_qid()
        if not qid:
            return
        if messagebox.askyesno(tr("confirm_restore_title"), tr("confirm_restore_text")):
            ok, err = self.data.quarantine_restore(qid)
            audit("quarantine_restore",         # журнал (2.4)
                  {"id": qid, "ok": ok, "error": err if not ok else None})
            if not ok:
                messagebox.showerror(tr("quarantine_title"), err)
            self._refresh_quarantine()

    def _on_delete(self) -> None:
        qid = self._selected_qid()
        if not qid:
            return
        if messagebox.askyesno(tr("confirm_delete_title"), tr("confirm_delete_text"),
                               icon="warning"):
            ok, err = self.data.quarantine_delete(qid)
            audit("quarantine_delete",          # журнал (2.4)
                  {"id": qid, "ok": ok, "error": err if not ok else None})
            if not ok:
                messagebox.showerror(tr("quarantine_title"), err)
            self._refresh_quarantine()

    # ───────────────────────── Настройки ─────────────────────────
    def _build_settings(self, parent: tk.Frame) -> None:
        # Режим работы
        c1 = Card(parent, min_height=140)
        c1.pack(fill="x")
        self._mode_title = tk.Label(c1.body, text=tr("settings_mode_title"),
                                    bg=SURFACE_1, fg=TEXT_MAIN, font=F_HEAD)
        self._mode_title.pack(anchor="w")
        self._mode_var = tk.StringVar(value="active")
        for val, key in (("active", "mode_active"), ("safe", "mode_safe")):
            rb = tk.Radiobutton(c1.body, text=tr(key), variable=self._mode_var,
                                value=val, bg=SURFACE_1, fg=TEXT_MAIN,
                                selectcolor=SURFACE_2, activebackground=SURFACE_1,
                                activeforeground=ACCENT, font=F_BASE)
            rb.pack(anchor="w", pady=2)

        # Поведенческий анализ (Спринт 5): режим audit/active
        c_beh = Card(parent, min_height=150)
        c_beh.pack(fill="x", pady=10)
        self._beh_title = tk.Label(c_beh.body, text=tr("behavior_settings_title"),
                                   bg=SURFACE_1, fg=TEXT_MAIN, font=F_HEAD)
        self._beh_title.pack(anchor="w", pady=(0, 4))
        self._beh_mode = str(load_app_settings().get("behavior_mode")
                             or "active").lower()
        if self._beh_mode not in ("audit", "active"):
            self._beh_mode = "active"
        self._beh_var = tk.StringVar(value=self._beh_mode)
        for val, key in (("active", "behavior_mode_active"),
                         ("audit", "behavior_mode_audit")):
            rb = tk.Radiobutton(c_beh.body, text=tr(key),
                                variable=self._beh_var, value=val,
                                command=self._on_behavior_mode_changed,
                                bg=SURFACE_1, fg=TEXT_MAIN,
                                selectcolor=SURFACE_2,
                                activebackground=SURFACE_1,
                                activeforeground=ACCENT, font=F_BASE)
            rb.pack(anchor="w", pady=2)
        self._beh_note = tk.Label(c_beh.body, text=tr("behavior_settings_note"),
                                  bg=SURFACE_1, fg=TEXT_DIM,
                                  font=("Segoe UI", 8), wraplength=560,
                                  justify="left", anchor="w")
        self._beh_note.pack(fill="x", pady=(2, 0))

        # Лабораторные функции (Задача 4.2): Memory Scan и Canary в текущей
        # версии движка НЕ РАБОТАЮТ (STARASTRA_SPRINT3_REPORT.md: 3.1 FAIL,
        # 3.2 live FAIL) — честно показываем серыми (disabled) чекбоксами.
        c_lab = Card(parent, min_height=140)
        c_lab.pack(fill="x", pady=10)
        self._lab_title = tk.Label(c_lab.body, text=tr("settings_lab_title"),
                                   bg=SURFACE_1, fg=TEXT_MAIN, font=F_HEAD)
        self._lab_title.pack(anchor="w", pady=(0, 4))
        self._lab_vars: dict[str, tk.BooleanVar] = {}
        for feat, tr_key in (("memory_scan", "feature_memory_scan"),
                             ("canary", "feature_canary")):
            var = tk.BooleanVar(value=False)
            cb = tk.Checkbutton(
                c_lab.body, text=tr(tr_key), variable=var,
                state="disabled",                       # серым, включить нельзя
                bg=SURFACE_1, selectcolor=SURFACE_2,
                disabledforeground=TEXT_DIM,
                activebackground=SURFACE_1, font=F_BASE, anchor="w")
            cb.pack(fill="x")
            self._lab_vars[feat] = var
        self._lab_note = tk.Label(c_lab.body, text=tr("opt_not_supported"),
                                  bg=SURFACE_1, fg=TEXT_DIM,
                                  font=("Segoe UI", 8), anchor="w")
        self._lab_note.pack(fill="x", pady=(2, 0))

        # Язык / Language
        c2 = Card(parent, min_height=110)
        c2.pack(fill="x", pady=10)
        self._lang_title = tk.Label(c2.body, text=tr("settings_language"),
                                    bg=SURFACE_1, fg=TEXT_MAIN, font=F_HEAD)
        self._lang_title.pack(anchor="w", pady=(0, 6))
        lang_row = tk.Frame(c2.body, bg=SURFACE_1)
        lang_row.pack(anchor="w")
        self._lang_buttons: dict[str, RoundedButton] = {}
        for code in ("ru", "en"):
            b = RoundedButton(lang_row, code.upper(),
                              lambda c=code: self._apply_language(c),
                              style="primary" if get_language() == code else "outline",
                              width=56)
            b.pack(side="left", padx=(0, 8))
            self._lang_buttons[code] = b

        # Пути исключений (Задача 1.4: path / hash / process + Anti-Exclusion Guard)
        c3 = Card(parent, min_height=170)
        c3.pack(fill="x")
        self._excl_title = tk.Label(c3.body, text=tr("settings_exclusions"),
                                    bg=SURFACE_1, fg=TEXT_MAIN, font=F_HEAD)
        self._excl_title.pack(anchor="w", pady=(0, 2))
        self._excl_note = tk.Label(c3.body, text=tr("excl_note_engine"),
                                   bg=SURFACE_1, fg=TEXT_DIM,
                                   font=("Segoe UI", 8), justify="left",
                                   wraplength=560, anchor="w")
        self._excl_note.pack(fill="x", pady=(0, 4))
        self._excl_list = tk.Listbox(c3.body, bg=SURFACE_2, fg=TEXT_MAIN,
                                     highlightbackground=BORDER, height=3, font=F_SUB)
        self._excl_list.pack(fill="x")
        row = tk.Frame(c3.body, bg=SURFACE_1)
        row.pack(fill="x", pady=(8, 0))
        self._excl_add = RoundedButton(row, tr("btn_add_path"), self._add_exclusion,
                                       style="primary")
        self._excl_add.pack(side="left", padx=(0, 8))
        self._excl_del = RoundedButton(row, tr("btn_remove_path"), self._del_exclusion,
                                       style="secondary")
        self._excl_del.pack(side="left")
        self._exclusions = self._load_exclusions()   # синхронизация с файлом
        for item in self._exclusions:
            self._excl_list.insert("end", self._excl_display(item))

        # Автозапуск при входе в Windows (HKCU Run, без UAC)
        c4 = Card(parent, min_height=150)
        c4.pack(fill="x", pady=10)
        self._auto_title = tk.Label(c4.body, text=tr("settings_autostart_title"),
                                    bg=SURFACE_1, fg=TEXT_MAIN, font=F_HEAD)
        self._auto_title.pack(anchor="w", pady=(0, 4))
        lm = _launch_mod()
        enabled = lm.autostart_enabled()
        self._autostart_var = tk.BooleanVar(value=enabled)
        self._openwin_var = tk.BooleanVar(
            value=enabled and lm.autostart_opens_window())
        self._close_to_tray_var = tk.BooleanVar(value=self._close_to_tray)
        cb1 = tk.Checkbutton(c4.body, text=tr("settings_autostart"),
                             variable=self._autostart_var,
                             command=self._toggle_autostart,
                             bg=SURFACE_1, fg=TEXT_MAIN, selectcolor=SURFACE_2,
                             activebackground=SURFACE_1, activeforeground=ACCENT,
                             font=F_BASE, anchor="w")
        cb1.pack(fill="x")
        _tooltip(cb1, tr("settings_autostart_tip"))
        cb2 = tk.Checkbutton(c4.body, text=tr("settings_autostart_openwin"),
                             variable=self._openwin_var,
                             command=self._toggle_openwin,
                             bg=SURFACE_1, fg=TEXT_MAIN, selectcolor=SURFACE_2,
                             activebackground=SURFACE_1, activeforeground=ACCENT,
                             font=F_BASE, anchor="w")
        cb2.pack(fill="x")
        _tooltip(cb2, tr("settings_autostart_openwin_tip"))

        # Защита: длительность паузы + закрытие в трей
        c5 = Card(parent, min_height=150)
        c5.pack(fill="x", pady=10)
        self._prot_title = tk.Label(c5.body, text=tr("settings_protection_title"),
                                    bg=SURFACE_1, fg=TEXT_MAIN, font=F_HEAD)
        self._prot_title.pack(anchor="w", pady=(0, 4))
        row5 = tk.Frame(c5.body, bg=SURFACE_1)
        row5.pack(fill="x")
        tk.Label(row5, text=tr("settings_pause_default"), bg=SURFACE_1,
                 fg=TEXT_MAIN, font=F_BASE).pack(side="left")
        self._pause_default_var = tk.StringVar(
            value=f"{self._pause_minutes}")
        cb = ttk.Combobox(row5, textvariable=self._pause_default_var,
                          values=[str(n) for n in PAUSE_CHOICES],
                          state="readonly", width=5)
        cb.pack(side="left", padx=8)
        cb.bind("<<ComboboxSelected>>", self._on_pause_default_changed)
        cb3 = tk.Checkbutton(c5.body, text=tr("settings_close_to_tray"),
                             variable=self._close_to_tray_var,
                             command=self._toggle_close_to_tray,
                             bg=SURFACE_1, fg=TEXT_MAIN, selectcolor=SURFACE_2,
                             activebackground=SURFACE_1, activeforeground=ACCENT,
                             font=F_BASE, anchor="w")
        cb3.pack(fill="x", pady=(6, 0))
        _tooltip(cb3, tr("settings_close_to_tray_tip"))

        # Сканирование по расписанию (Задача 2.2)
        c6 = Card(parent, min_height=150)
        c6.pack(fill="x", pady=10)
        self._sched_title = tk.Label(c6.body, text=tr("schedule_title"),
                                     bg=SURFACE_1, fg=TEXT_MAIN, font=F_HEAD)
        self._sched_title.pack(anchor="w", pady=(0, 4))
        self._sched_var = tk.BooleanVar(value=self._sched_enabled)
        cb_sched = tk.Checkbutton(c6.body, text=tr("schedule_enable"),
                                  variable=self._sched_var,
                                  command=self._on_schedule_toggle,
                                  bg=SURFACE_1, fg=TEXT_MAIN,
                                  selectcolor=SURFACE_2,
                                  activebackground=SURFACE_1,
                                  activeforeground=ACCENT, font=F_BASE,
                                  anchor="w")
        cb_sched.pack(fill="x")
        _tooltip(cb_sched, tr("schedule_enable_tip"))
        sched_row = tk.Frame(c6.body, bg=SURFACE_1)
        sched_row.pack(fill="x", pady=(6, 0))
        tk.Label(sched_row, text=tr("schedule_time_label"), bg=SURFACE_1,
                 fg=TEXT_SUB, font=F_SUB).pack(side="left")
        hh, mm = (self._sched_time.split(":") + ["0", "0"])[:2]
        self._sched_hh = ttk.Combobox(sched_row, state="readonly", width=3,
                                      values=[f"{n:02d}" for n in range(24)])
        self._sched_hh.set(f"{int(hh):02d}")
        self._sched_hh.pack(side="left", padx=(6, 2))
        self._sched_mm = ttk.Combobox(sched_row, state="readonly", width=3,
                                      values=[f"{n:02d}" for n in range(60)])
        self._sched_mm.set(f"{int(mm):02d}")
        self._sched_mm.pack(side="left", padx=(0, 10))
        for w in (self._sched_hh, self._sched_mm):
            w.bind("<<ComboboxSelected>>", self._on_schedule_time_changed)
        tk.Label(sched_row, text=tr("schedule_kind_label"), bg=SURFACE_1,
                 fg=TEXT_SUB, font=F_SUB).pack(side="left")
        self._sched_kind_var = tk.StringVar(
            value=tr("scan_kind_" + self._sched_kind))
        self._sched_kind_map = {tr("scan_kind_quick"): "quick",
                                tr("scan_kind_full"): "full"}
        kb = ttk.Combobox(sched_row, textvariable=self._sched_kind_var,
                          state="readonly", width=8,
                          values=list(self._sched_kind_map))
        kb.pack(side="left", padx=(6, 0))
        kb.bind("<<ComboboxSelected>>", self._on_schedule_kind_changed)
        last = self._sched_last_run or tr("schedule_never")
        self._sched_last_lbl = tk.Label(
            c6.body, text=tr("schedule_last_run", date=last),
            bg=SURFACE_1, fg=TEXT_SUB, font=F_SUB, anchor="w")
        self._sched_last_lbl.pack(fill="x", pady=(4, 0))

        # Журнал действий (Задача 2.4): data/audit.log с фильтром по типу
        c7 = Card(parent, min_height=260)
        c7.pack(fill="x", pady=10)
        self._audit_title_lbl = tk.Label(c7.body, text=tr("audit_title"),
                                         bg=SURFACE_1, fg=TEXT_MAIN, font=F_HEAD)
        self._audit_title_lbl.pack(anchor="w", pady=(0, 4))
        audit_row = tk.Frame(c7.body, bg=SURFACE_1)
        audit_row.pack(fill="x")
        tk.Label(audit_row, text=tr("audit_filter_label"), bg=SURFACE_1,
                 fg=TEXT_SUB, font=F_SUB).pack(side="left")
        self._audit_filter_var = tk.StringVar(value=tr("audit_filter_all"))
        self._audit_filter_map = {tr("audit_filter_all"): None}
        self._audit_filter_map.update(
            {tr("audit_act_" + a): a for a in ACTIONS})
        cb_audit = ttk.Combobox(audit_row, textvariable=self._audit_filter_var,
                                state="readonly", width=34,
                                values=list(self._audit_filter_map))
        cb_audit.pack(side="left", padx=(6, 8))
        cb_audit.bind("<<ComboboxSelected>>", self._on_audit_filter_changed)
        RoundedButton(audit_row, tr("audit_refresh"), self._audit_refresh,
                      style="secondary").pack(side="left")
        self._audit_list = tk.Listbox(c7.body, bg=SURFACE_2, fg=TEXT_MAIN,
                                      highlightbackground=BORDER, height=9,
                                      font=("Consolas", 9))
        self._audit_list.pack(fill="x", pady=(6, 0))
        self._audit_refresh()

        # Обновления (Задача 2.3): заглушка — сервер не настроен
        c8 = Card(parent, min_height=120)
        c8.pack(fill="x", pady=10)
        self._upd_title_lbl = tk.Label(c8.body, text=tr("updates_title"),
                                       bg=SURFACE_1, fg=TEXT_MAIN, font=F_HEAD)
        self._upd_title_lbl.pack(anchor="w", pady=(0, 4))
        self._upd_last = load_app_settings().get("last_update_check")
        last_upd = self._upd_last or tr("updates_never")
        self._upd_last_lbl = tk.Label(
            c8.body, text=tr("updates_last_check", date=last_upd),
            bg=SURFACE_1, fg=TEXT_SUB, font=F_SUB, anchor="w")
        self._upd_last_lbl.pack(fill="x")
        RoundedButton(c8.body, tr("updates_check_btn"), self._on_check_updates,
                      style="secondary").pack(anchor="w", pady=(6, 0))

    def _on_check_updates(self) -> None:
        """Проверка обновлений: сервер не настроен (честная заглушка 2.3).

        Никакого фиктивного скачивания: фиксируем факт проверки
        (настройки + журнал) и сообщаем пользователю.
        """
        self._upd_last = time.strftime("%Y-%m-%d %H:%M")
        save_app_settings({"last_update_check": self._upd_last})
        self._upd_last_lbl.configure(
            text=tr("updates_last_check", date=self._upd_last))
        audit("update_check", {"result": "not_configured"})
        messagebox.showinfo(tr("updates_title"),
                            tr("updates_not_configured_text"))

    def _on_audit_filter_changed(self, _e=None) -> None:
        self._audit_refresh()

    def _audit_refresh(self) -> None:
        """Перечитать data/audit.log (read_audit) в список с фильтром."""
        if not hasattr(self, "_audit_list") or not self._audit_list.winfo_exists():
            return
        code = self._audit_filter_map.get(self._audit_filter_var.get())
        rows = read_audit(limit=200, action_filter=code)
        self._audit_list.delete(0, "end")
        if not rows:
            self._audit_list.insert("end", tr("audit_empty"))
            return
        for rec in rows:
            d = rec.get("details") or {}
            if isinstance(d, dict):
                det = ", ".join(f"{k}={v}" for k, v in d.items())
            else:
                det = str(d)
            act = tr("audit_act_" + str(rec.get("action", "")))
            self._audit_list.insert(
                "end", f"{rec.get('ts', '')}  {act}  {det}"[:120])

    def _on_behavior_mode_changed(self) -> None:
        """Радио «Поведенческий анализ»: active / audit -> settings + журнал."""
        mode = self._beh_var.get()
        if mode not in ("active", "audit"):
            return
        self._beh_mode = mode
        save_app_settings({"behavior_mode": mode})
        audit("settings_change", {"behavior_mode": mode})

    def _on_schedule_toggle(self) -> None:
        self._sched_enabled = bool(self._sched_var.get())
        save_app_settings({"schedule_enabled": self._sched_enabled})
        audit("settings_change", {"schedule_enabled": self._sched_enabled})

    def _on_schedule_time_changed(self, _e=None) -> None:
        hh = self._sched_hh.get() or "03"
        mm = self._sched_mm.get() or "00"
        self._sched_time = f"{hh}:{mm}"
        save_app_settings({"schedule_time": self._sched_time})
        audit("settings_change", {"schedule_time": self._sched_time})

    def _on_schedule_kind_changed(self, _e=None) -> None:
        kind = self._sched_kind_map.get(self._sched_kind_var.get())
        if kind in ("quick", "full"):
            self._sched_kind = kind
            save_app_settings({"schedule_kind": kind})
            audit("settings_change", {"schedule_kind": kind})

    def _on_pause_default_changed(self, _e=None) -> None:
        try:
            n = int(self._pause_default_var.get())
        except ValueError:
            return
        if n in PAUSE_CHOICES:
            self._pause_minutes = n
            save_app_settings({"pause_duration_min": n})
            audit("settings_change", {"pause_duration_min": n})  # 2.4
            if hasattr(self, "_lbl_pause_for"):
                self._lbl_pause_for.configure(
                    text=tr("pause_for_label", min=n))

    def _toggle_close_to_tray(self) -> None:
        save_app_settings({"close_to_tray": bool(self._close_to_tray_var.get())})
        audit("settings_change",                       # журнал (2.4)
              {"close_to_tray": bool(self._close_to_tray_var.get())})

    def _toggle_autostart(self) -> None:
        lm = _launch_mod()
        if not lm.set_autostart(self._autostart_var.get(),
                                open_window=self._openwin_var.get()):
            self._autostart_var.set(lm.autostart_enabled())
            messagebox.showerror(tr("err_title_generic"),
                                 tr("err_autostart_failed"))
        audit("autostart_change",                      # журнал (2.4)
              {"enabled": bool(self._autostart_var.get()),
               "opens_window": bool(self._openwin_var.get())})

    def _toggle_openwin(self) -> None:
        if self._autostart_var.get():
            lm = _launch_mod()
            lm.set_autostart(True, open_window=self._openwin_var.get())
            audit("autostart_change",                  # журнал (2.4)
                  {"enabled": True,
                   "opens_window": bool(self._openwin_var.get())})

    # ─────────────── Исключения (Задача 1.4: path/hash/process) ───────────────
    # Механизм исключений НА СТОРОНЕ ДВИЖКА НЕ НАЙДЕН: в
    # sentinel_pkg/sentinel/config/ только rules/, settings.yaml отсутствует.
    # Исключения StarAstra — UI-слой: гасят уведомления и подсветку событий
    # (см. _severity_of/_excl_matches); на сканирование движком не влияют.
    EXCL_TYPES = ("path", "hash", "process")

    @staticmethod
    def _excl_display(item: dict) -> str:
        """Строка для Listbox: [PATH] C:\\... / [HASH] abcd... / [PROCESS] app.exe."""
        return f'[{str(item.get("type", "path")).upper()}] {item.get("value", "")}'

    def _load_exclusions(self) -> list[dict]:
        """Исключения из starastra_settings.json + миграция старого формата
        (плоский список строк -> [{"type": "path", "value": ...}])."""
        try:
            raw = load_app_settings().get("exclusions", [])
        except Exception:
            raw = []
        result: list[dict] = []
        for item in raw:
            if isinstance(item, dict):
                t = str(item.get("type") or "path").lower()
                v = str(item.get("value") or "")
                if v:
                    result.append({"type": t if t in self.EXCL_TYPES else "path",
                                   "value": v})
            elif isinstance(item, str) and item:      # старый формат
                result.append({"type": "path", "value": item})
        return result

    def _save_exclusions(self) -> None:
        save_app_settings({"exclusions": list(self._exclusions)})
        try:
            self._excl_self_mtime = os.stat(SETTINGS_FILE).st_mtime
        except OSError:
            self._excl_self_mtime = None

    def _refresh_excl_list(self) -> None:
        self._excl_list.delete(0, "end")
        for item in self._exclusions:
            self._excl_list.insert("end", self._excl_display(item))

    def _add_exclusion(self) -> None:
        item = self._excl_dialog()
        if item is None:
            return
        self._exclusions.append(item)
        self._save_exclusions()
        self._refresh_excl_list()
        audit("exclusion_add", item)          # журнал (2.4)
        logger.info("exclusion added by user: %s", item)

    def _del_exclusion(self) -> None:
        sel = self._excl_list.curselection()
        if not sel:
            return
        removed = self._exclusions.pop(sel[0])
        self._save_exclusions()
        self._refresh_excl_list()
        audit("exclusion_remove", removed)    # журнал (2.4)
        logger.info("exclusion removed by user: %s", removed)

    def _excl_dialog(self) -> dict | None:
        """Модальный диалог «Добавить исключение»: тип (path/hash/process) +
        значение; для path — выбор каталога. None = отмена/пустое значение."""
        top = tk.Toplevel(self)
        top.title(tr("excl_add_title"))
        top.configure(bg=SURFACE_1)
        top.transient(self)
        top.resizable(False, False)
        top.grab_set()
        var = tk.StringVar(value="path")
        tk.Label(top, text=tr("excl_type_label"), bg=SURFACE_1, fg=TEXT_MAIN,
                 font=F_SUB).pack(anchor="w", padx=14, pady=(12, 2))
        for tval, key in (("path", "excl_type_path"),
                          ("hash", "excl_type_hash"),
                          ("process", "excl_type_process")):
            tk.Radiobutton(top, text=tr(key), variable=var, value=tval,
                           bg=SURFACE_1, fg=TEXT_MAIN, selectcolor=SURFACE_2,
                           activebackground=SURFACE_1, activeforeground=ACCENT,
                           font=F_BASE, anchor="w").pack(anchor="w", padx=14)
        tk.Label(top, text=tr("excl_value_label"), bg=SURFACE_1, fg=TEXT_MAIN,
                 font=F_SUB).pack(anchor="w", padx=14, pady=(10, 2))
        ent = tk.Entry(top, width=52, bg=SURFACE_2, fg=TEXT_MAIN,
                       insertbackground=TEXT_MAIN, highlightbackground=BORDER)
        ent.pack(fill="x", padx=14)
        result: list[tuple[str, str]] = []

        def browse() -> None:
            p = filedialog.askdirectory(parent=top, title=tr("excl_browse"))
            if p:
                ent.delete(0, "end")
                ent.insert(0, p)

        def ok() -> None:
            result.append((var.get(), ent.get().strip()))
            top.destroy()

        btns = tk.Frame(top, bg=SURFACE_1)
        btns.pack(fill="x", padx=14, pady=12)
        RoundedButton(btns, tr("excl_browse"), browse, style="secondary").pack(side="left")
        RoundedButton(btns, tr("excl_ok"), ok, style="primary").pack(side="right", padx=(8, 0))
        RoundedButton(btns, tr("btn_cancel"), top.destroy, style="secondary").pack(side="right")
        top.wait_window()
        if not result:
            return None
        tval, value = result[0]
        if not value:
            messagebox.showwarning(tr("err_title_generic"), tr("excl_empty_value"))
            return None
        if tval == "path":
            value = os.path.normcase(os.path.normpath(os.path.expanduser(value)))
        elif tval == "hash":
            value = value.lower()
        return {"type": tval, "value": value}

    def _excl_matches(self, row: dict, path: str) -> bool:
        """Событие покрыто исключением пользователя?
        path — префикс каталога/точное имя файла; hash — sha256;
        process — имя образа (basename пути события или process из extra)."""
        if not self._exclusions:
            return False
        try:
            extra = json.loads(row.get("extra") or "{}")
        except Exception:
            extra = {}
        plow = (path or "").lower()
        base = os.path.basename(plow) if plow else ""
        row_hash = str(row.get("hash_sha256") or extra.get("sha256") or "").lower()
        proc_name = str(extra.get("process") or extra.get("image") or "").lower()
        for ex in self._exclusions:
            t, vl = ex["type"], ex["value"].lower()
            if t == "path":
                if plow and (plow == vl
                             or plow.startswith(vl.rstrip("\\/") + "\\")
                             or plow.startswith(vl.rstrip("\\/") + "/")):
                    return True
            elif t == "hash":
                if row_hash and row_hash == vl:
                    return True
            elif t == "process":
                if base and base == vl:
                    return True
                if proc_name and (proc_name == vl
                                  or proc_name.endswith("\\" + vl)):
                    return True
        return False

    # ── Anti-Exclusion Guard ────────────────────────────────────────────────
    # Вредонос может ослабить защиту, дописав исключение прямо в
    # starastra_settings.json (единственное хранилище — движок исключений
    # не имеет). Guard замечает внешнее изменение списка исключений.
    def _start_exclusion_guard(self) -> None:
        self._exclusions = self._load_exclusions()
        self._excl_q: "queue.Queue[tuple]" = queue.Queue()
        try:
            self._excl_self_mtime = os.stat(SETTINGS_FILE).st_mtime
        except OSError:
            self._excl_self_mtime = None
        self._excl_guard = threading.Thread(
            target=self._exclusion_guard_loop, daemon=True, name="excl-guard")
        self._excl_guard.start()
        self._drain_excl_queue()

    def _exclusion_guard_loop(self) -> None:
        """Раз в 3 сек: mtime настроек изменился не из-за нашего сохранения ->
        сравнить списки исключений; при расхождении — положить (old, new)
        в очередь для UI-потока (Tkinter не трогаем из чужого потока)."""
        last_mtime = self._excl_self_mtime
        prev = list(self._exclusions)
        while not self._poll_stop.wait(3.0):
            try:
                mtime = os.stat(SETTINGS_FILE).st_mtime
            except OSError:
                continue
            if mtime == last_mtime or mtime == self._excl_self_mtime:
                continue
            last_mtime = mtime
            cur = self._load_exclusions()
            if cur == prev:
                continue            # изменились другие ключи настроек — не наше
            self._exclusions = cur
            self._excl_self_mtime = mtime
            self._excl_q.put((prev, cur))
            prev = cur

    def _drain_excl_queue(self) -> None:
        """UI-поток: забрать сообщения guard'а (main-thread Tkinter)."""
        while True:
            try:
                old, new = self._excl_q.get_nowait()
            except queue.Empty:
                break
            self._on_exclusions_changed_externally(old, new)
        self._excl_q_job = self.after(1000, self._drain_excl_queue)

    def _on_exclusions_changed_externally(self, old: list, new: list) -> None:
        added = [x for x in new if x not in old]
        removed = [x for x in old if x not in new]
        logger.warning(
            "ANTI-EXCLUSION GUARD: exclusions changed externally "
            "(added=%s, removed=%s) -- severity HIGH", added, removed)
        audit("exclusion_external_change",     # журнал (2.4)
              {"added": [x.get("value") for x in added],
               "removed": [x.get("value") for x in removed]})
        detail = (tr("guard_alert_text_added", n=len(added)) if added
                  else tr("guard_alert_text_removed", n=len(removed)))
        if hasattr(self, "notify"):
            self.notify.push_event({
                "id": -5000, "event_type": "exclusion_guard",
                "image_path": "; ".join(x["value"] for x in added + removed)[:200],
                "extra": json.dumps(
                    {"severity": "HIGH", "action": detail,
                     "title_key": "guard_alert_title"}, ensure_ascii=False)})
        if hasattr(self, "_excl_list") and self._excl_list.winfo_exists():
            self._refresh_excl_list()

    def _apply_language(self, code: str) -> None:
        set_language(code)
        audit("language_change", {"language": code})   # журнал (2.4)
        for k, b in self._lang_buttons.items():
            b._style = "primary" if k == code else "outline"
            b._bg, b._fg, b._hbg = {
                "primary": (ACCENT, BG_DARK, ACCENT_HOVER),
                "outline": (BG_DARK, TEXT_MAIN, BG_DARK),
            }[b._style]
            b._draw(b._bg)
        self._retranslate()

    def _retranslate(self) -> None:
        """Применить tr() ко всем видимым текстам без рестарта (пересоздание разделов)."""
        self.title(tr("app_title"))
        self._logo.configure(text="★ StarAstra")
        self._sub.configure(text=tr("app_subtitle"))
        for key, w in self._nav.items():
            w["label"].configure(text="  " + tr(w["label_key"]))
        # пересоздать разделы с новыми строками
        for frame in self._sections.values():
            for child in frame.winfo_children():
                child.destroy()
        self._build_overview(self._sections["overview"])
        self._build_history(self._sections["history"])
        self._build_quarantine(self._sections["quarantine"])
        self._build_settings(self._sections["settings"])
        self._apply_pause_state()
        self.show_section("overview")

    # ───────────────────────── Уведомления ─────────────────────────
    def _build_notify(self) -> None:
        self.notify = NotifyPanel(self, on_quarantine=self._notify_quarantine,
                                  on_dismiss=self._notify_dismiss,
                                  on_details=self._notify_details,
                                  on_quarantine_all=self._notify_quarantine_all,
                                  on_show_all=self._notify_show_all,
                                  on_behavior_kill=self._on_behavior_kill,
                                  on_behavior_allow=self._on_behavior_allow)
        self.system_toast = make_system_notifier()

    def _notify_quarantine(self, path: str) -> tuple[bool, str]:
        # существующий код карантина движка — отдельным процессом (store.add)
        ok, err = self.data._run_engine_snippet(
            "from sentinel.response.quarantine_store import QuarantineStore\n"
            "import sys\n"
            "s = QuarantineStore()\n"
            "rec = s.add(sys.argv[1], subject='file:' + sys.argv[1], reason='StarAstra UI: user action', score=85.0)\n"
            "print(rec.id if rec else 'FAIL')\n",
            argv=[path])
        audit("quarantine_add",              # журнал (2.4)
              {"path": path, "ok": ok, "error": err if not ok else None,
               "source": "notification"})
        logger.info("notify quarantine %s -> %s", path, "ok" if ok else err)
        return ok, err

    def _notify_dismiss(self, row: dict) -> None:
        self._dismissed.add(row.get("id", 0))

    def _notify_details(self, row: dict) -> None:
        self.show_section("history")
        table = self._history_table
        for item in table.get_children():
            vals = table.item(item, "values")
            if vals and vals[3] and vals[3] in (row.get("image_path") or ""):
                table.selection_set(item)
                table.see(item)
                break

    # ── Анти-спам (Задача 1.5): групповые действия ──────────────────────────
    def _notify_quarantine_all(self, paths: list[str]) -> None:
        """«В карантин все»: штатный QuarantineStore.add для каждого файла
        (движок-сниппет, по одному процессу на файл) — в фоне, без блокировки UI."""
        def worker() -> None:
            ok = fail = 0
            for p in paths:
                try:
                    done, err = self._notify_quarantine(p)
                    ok += 1 if done else 0
                    fail += 0 if done else 1
                except Exception:
                    fail += 1
            logger.info("burst quarantine-all: ok=%s fail=%s", ok, fail)
            audit("quarantine_add",              # журнал (2.4)
                  {"ok": ok, "failed": fail, "source": "quarantine-all"})
        threading.Thread(target=worker, daemon=True).start()

    def _notify_show_all(self, events: list[dict]) -> None:
        """«Показать все»: вкладка История (события окна — самые свежие)."""
        self.show_section("history")

    # ── Поведенческий анализ (Спринт 5): кнопки карточки ─────────────────────
    def _on_behavior_kill(self, extra: dict, row: dict) -> None:
        """[Убить]: kill замороженного процесса (фон) + запись в журнал."""
        pid = int(extra.get("pid") or 0)
        name = str(extra.get("process") or "")
        rule = str(extra.get("rule") or "")

        def worker() -> None:
            ok = False
            err = None
            if pid > 0:
                try:
                    import psutil
                    psutil.Process(pid).kill()
                    ok = True
                except Exception as exc:
                    err = str(exc)
            audit("behavior_kill", {"pid": pid, "process": name,
                                    "rule": rule, "ok": ok, "error": err})
            logger.info("behavior kill pid=%s (%s): ok=%s err=%s",
                        pid, name, ok, err)
        threading.Thread(target=worker, daemon=True).start()

    def _on_behavior_allow(self, extra: dict, row: dict) -> None:
        """[Разрешить]: resume + исключение (process=name) + список в UI."""
        pid = int(extra.get("pid") or 0)
        name = str(extra.get("process") or "")
        rule = str(extra.get("rule") or "")

        def worker() -> None:
            ok = False
            err = None
            if pid > 0:
                try:
                    import psutil
                    psutil.Process(pid).resume()   # разморозить
                    ok = True
                except Exception as exc:
                    err = str(exc)
            audit("behavior_allow", {"pid": pid, "process": name,
                                     "rule": rule, "ok": ok, "error": err})
            logger.info("behavior allow pid=%s (%s): ok=%s err=%s",
                        pid, name, ok, err)
            # исключение + обновление списка — строго в главном потоке Tk
            self._ui_jobs.put(lambda: self._add_behavior_exclusion(name))
        threading.Thread(target=worker, daemon=True).start()

    def _add_behavior_exclusion(self, name: str) -> None:
        """Добавить process-исключение (главный поток Tk)."""
        name = (name or "").strip().lower()
        if not name:
            return
        if not any(x.get("type") == "process"
                   and str(x.get("value", "")).lower() == name
                   for x in self._exclusions):
            self._exclusions.append({"type": "process", "value": name})
            self._save_exclusions()
            audit("exclusion_add", {"type": "process", "value": name,
                                    "source": "behavior_allow"})
            logger.info("behavior exclusion added: %s", name)
        if hasattr(self, "_excl_list") and self._excl_list.winfo_exists():
            self._refresh_excl_list()

    def _start_poller(self) -> None:
        self._poll_stop = threading.Event()
        self._poll_queue: "queue.Queue[dict]" = __import__("queue").Queue()
        self._poller = ToastPoller(ENGINE_DB_PATH, self._poll_queue, self._poll_stop)
        self._poller.start()
        self._start_exclusion_guard()
        # UI-jobs (Спринт 5): фоновые потоки (kill/allow) не трогают Tkinter
        # напрямую — работы на виджетах исполняются в главном потоке
        self._ui_jobs: "queue.Queue" = queue.Queue()
        self._drain_ui_jobs()
        self._drain_queue()

    def _drain_ui_jobs(self) -> None:
        while True:
            try:
                fn = self._ui_jobs.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as exc:
                logger.warning("ui job failed: %s", exc)
        self.after(200, self._drain_ui_jobs)

    def _drain_queue(self) -> None:
        import queue as q
        while True:
            try:
                row = self._poll_queue.get_nowait()
            except q.Empty:
                break
            if row.get("id", 0) in self._dismissed:
                continue
            try:
                extra = json.loads(row.get("extra") or "{}")
            except Exception:
                extra = {}
            is_behavior = str(extra.get("alert_kind") or "") == "behavior"
            sev = self._severity_of(row)
            if sev in ("HIGH", "MEDIUM", "LOW"):
                self.notify.push_event(row)
                if sev == "HIGH" and self.system_toast:
                    title = (tr("behavior_alert_title") if is_behavior
                             else tr("notify_threat_title"))
                    if is_behavior:
                        text = tr("behavior_alert_text",
                                  proc=str(extra.get("process") or ""),
                                  rule=tr(str(extra.get("title_key")
                                              or "behavior_alert_title")))
                    else:
                        text = row.get("image_path") or ""
                    try:
                        self.system_toast(title, text)
                    except Exception as exc:
                        logger.debug("toast failed: %s", exc)
        self.after(1000, self._drain_queue)

    # ───────────────────────── Жизненный цикл ─────────────────────────
    def _on_close(self) -> None:
        # совместимое имя: теперь X обрабатывается в _on_window_close
        self._on_window_close()


def _tooltip(widget: tk.Widget, text: str) -> None:
    tip = {"win": None}

    def enter(_e):
        x = widget.winfo_rootx() + 10
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        win = tk.Toplevel(widget)
        win.wm_overrideredirect(True)
        win.wm_geometry(f"+{x}+{y}")
        tk.Label(win, text=text, bg=SURFACE_2, fg=TEXT_MAIN,
                 font=("Segoe UI", 8), padx=6, pady=3).pack()
        tip["win"] = win

    def leave(_e):
        if tip["win"]:
            tip["win"].destroy()
            tip["win"] = None

    widget.bind("<Enter>", enter)
    widget.bind("<Leave>", leave)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("engine db: %s", ENGINE_DB_PATH)
    app = StarAstraApp()
    app.mainloop()
    logger.info(tr("ui_closed_cleanly"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
