"""StarAstra notify — всплывающие карточки угроз + системные toasts.

Только UI-слой. Источник событий — read-only sqlite-поллер к data/events.db
(канал Задачи 2). Никаких вызовов API движка, кроме существующего карантина.
"""
from __future__ import annotations

import logging
import json
import os
import queue
import sqlite3
import threading
import time
import tkinter as tk
from collections import deque

try:
    from starastra_i18n import tr
except ImportError:  # запуск не из корня проекта
    sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys_path not in os.path:
        pass
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from starastra_i18n import tr

logger = logging.getLogger("starastra.notify")

ANIM_FRAME_MS = 33
SLIDE_MS = 200            # slide+fade появления карточки
AUTO_HIDE_MS = 15_000     # автоскрытие MEDIUM/LOW
MAX_CARDS = 3             # одновременно видимых карточек
POLL_SEC = 2.0            # период поллера events.db
BURST_WINDOW_SEC = 10.0   # окно анти-спама (Задача 1.5)
BURST_THRESHOLD = 5       # >5 уведомлений за окно -> одна групповая карточка

SEV_COLORS = {"HIGH": "#D96A6A", "CRITICAL": "#D96A6A",
              "MEDIUM": "#D9A441", "LOW": "#4CAF7D", "INFO": "#9BB3A2"}


def _mix(c1: str, c2: str, t: float) -> str:
    h1, h2 = c1.lstrip("#"), c2.lstrip("#")
    r = round(int(h1[0:2], 16) + (int(h2[0:2], 16) - int(h1[0:2], 16)) * t)
    g = round(int(h1[2:4], 16) + (int(h2[2:4], 16) - int(h1[2:4], 16)) * t)
    b = round(int(h1[4:6], 16) + (int(h2[4:6], 16) - int(h1[4:6], 16)) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _rounded(canvas: tk.Canvas, x1, y1, x2, y2, r: int, **kw) -> int:
    """Скруглённый прямоугольник на Canvas (в Tk нет из коробки)."""
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
           x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class ToastPoller(threading.Thread):
    """Daemon-поток: раз в POLL_SEC читает новые строки events.db (WHERE id > last_id)."""

    def __init__(self, db_path: str, out: "queue.Queue[dict]", stop_event: threading.Event):
        super().__init__(daemon=True, name="starastra-notify-poller")
        self.db_path = db_path
        self.out = out
        self._stop = stop_event
        self.last_id = 0
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            row = con.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()
            self.last_id = row[0] if row else 0
            con.close()
            self.db_ok = True
        except Exception as exc:
            logger.warning("poller: events.db unavailable (%s)", exc)
            self.db_ok = False

    def run(self) -> None:
        if not self.db_ok:
            return
        while not self._stop.is_set():
            try:
                con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2)
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT 50",
                    (self.last_id,)).fetchall()
                con.close()
                for r in rows:
                    self.last_id = max(self.last_id, r["id"])
                    self.out.put(dict(r))
            except Exception as exc:
                logger.debug("poll cycle failed: %s", exc)
            self._stop.wait(POLL_SEC)


class NotifyPanel(tk.Frame):
    """Стек карточек в правом нижнем углу окна (overlay)."""

    def __init__(self, master, on_quarantine, on_dismiss, on_details,
                 on_quarantine_all=None, on_show_all=None,
                 on_behavior_kill=None, on_behavior_allow=None):
        super().__init__(master, bg="#000000")
        self.place(relx=1.0, rely=1.0, x=-16, y=-16, anchor="se")
        self.configure(bg=master["bg"])
        self._cards: list[dict] = []
        self._queue: list[dict] = []
        self._on_quarantine = on_quarantine
        self._on_dismiss = on_dismiss
        self._on_details = on_details
        # анти-спам (Задача 1.5)
        self._on_quarantine_all = on_quarantine_all
        self._on_show_all = on_show_all
        # behavioral actions (Спринт 5): [Убить]/[Разрешить] на карточке
        self._on_behavior_kill = on_behavior_kill
        self._on_behavior_allow = on_behavior_allow
        self._recent: deque = deque()      # (monotonic_ts, ev) за окно
        self._burst_rec: dict | None = None

    # ── публичное ──
    def push_event(self, row: dict) -> None:
        """Новое событие из поллера: показать карточку, если похоже на угрозу.

        Анти-спам: если за BURST_WINDOW_SEC пришло больше BURST_THRESHOLD
        уведомлений — вместо ленты карточек показывается одна групповая
        «Обнаружено N угроз» с [Показать все][В карантин все][Игнорировать].
        """
        sev, path = self._classify(row)
        if sev is None:
            return
        ev = {"row": row, "severity": sev, "path": path}
        now = time.monotonic()
        self._recent.append((now, ev))
        while self._recent and now - self._recent[0][0] > BURST_WINDOW_SEC:
            self._recent.popleft()
        if len(self._recent) > BURST_THRESHOLD:
            self._handle_burst()
            return
        if len([c for c in self._cards if c["alive"]]) >= MAX_CARDS:
            self._queue.append(ev)
            return
        self._spawn_card(ev)

    def _handle_burst(self) -> None:
        events = [ev for _, ev in self._recent]
        self._queue.clear()        # спам не показываем по одному
        rec = self._burst_rec
        if rec is not None and rec["alive"]:
            self._update_burst_card(events)
        else:
            self._spawn_burst_card(events)

    # ── внутреннее ──
    @staticmethod
    def _classify(row: dict) -> tuple[str | None, str]:
        extra = {}
        try:
            import json
            extra = json.loads(row.get("extra") or "{}")
        except Exception:
            pass
        sev = str(extra.get("severity", "")).upper()
        yara_hit = "yara" in str(extra.get("action", "")).lower() or row.get("event_type") == "file_quarantined"
        if sev in ("HIGH", "CRITICAL", "MEDIUM", "LOW") or yara_hit:
            return (sev or "HIGH"), (row.get("image_path") or "")
        return None, ""

    def _spawn_card(self, ev: dict) -> None:
        # behavior_alert (Спринт 5) — специальная карточка с [Убить]/[Разрешить]
        try:
            extra0 = json.loads(ev["row"].get("extra") or "{}")
        except Exception:
            extra0 = {}
        if str(extra0.get("alert_kind") or "") == "behavior":
            self._spawn_behavior_card(ev)
            return
        card = tk.Frame(self, bg="#1B241C", highlightbackground="#2A3830",
                        highlightthickness=1, padx=12, pady=10)
        sev = ev["severity"]
        color = SEV_COLORS.get(sev, "#9BB3A2")
        # заголовок можно переопределить через extra.title_key
        # (например, карточка Anti-Exclusion Guard — не «угроза»)
        try:
            extra = json.loads(ev["row"].get("extra") or "{}")
        except Exception:
            extra = {}
        title_key = str(extra.get("title_key") or "notify_threat_title")
        head = tk.Frame(card, bg="#1B241C")
        head.pack(fill="x")
        tk.Label(head, text="●", bg="#1B241C", fg=color,
                 font=("Segoe UI", 12, "bold")).pack(side="left")
        tk.Label(head, text=tr(title_key), bg="#1B241C", fg=color,
                 font=("Segoe UI", 11, "bold")).pack(side="left", padx=(6, 0))
        tk.Label(head, text=sev, bg="#1B241C", fg=color,
                 font=("Segoe UI", 9, "bold")).pack(side="right")
        path = ev["path"]
        if len(path) > 46:
            path = "…" + path[-45:]
        tk.Label(card, text=path, bg="#1B241C", fg="#9BB3A2",
                 font=("Segoe UI", 9), anchor="w").pack(fill="x", pady=(2, 6))
        btns = tk.Frame(card, bg="#1B241C")
        btns.pack(fill="x")

        rec = {"frame": card, "alive": True, "job": None, "ev": ev, "x": None}

        def qbtn(text, cmd, bg, fg, state=tk.NORMAL, tip=None):
            b = tk.Label(btns, text=text, bg=bg, fg=fg, padx=10, pady=4,
                         cursor="hand2" if state == tk.NORMAL else "arrow",
                         font=("Segoe UI", 9, "bold"))
            b.pack(side="left", padx=(0, 6))
            if state == tk.NORMAL:
                b.bind("<Button-1>", lambda _e: cmd(rec))
            else:
                b.configure(fg="#555E75")
                if tip:
                    _attach_tooltip(b, tip)
            return b

        if self._on_quarantine:
            qbtn(tr("btn_quarantine"), self._do_quarantine, "#4CAF7D", "#0F1410")
        else:
            qbtn(tr("btn_quarantine"), None, "#1B241C", "#9BB3A2", state=tk.DISABLED,
                 tip=tr("notify_unavailable_quarantine"))
        qbtn(tr("btn_cancel"), self._dismiss, "#223025", "#E6F0E8")
        qbtn(tr("btn_details"), self._do_details, "#223025", "#9BB3A2")

        card.pack(fill="x", pady=4, anchor="e")
        self._cards.append(rec)
        self._slide_in(rec)
        if sev in ("MEDIUM", "LOW"):
            rec["job"] = self.after(AUTO_HIDE_MS, lambda: self._dismiss(rec))

    # ── карточка behavioral_alert (Спринт 5) ──────────────────────────────
    def _spawn_behavior_card(self, ev: dict) -> None:
        """«⚠️ Подозрительное поведение: [Процесс] заморожен. Причина: [Правило].»

        suspended=True  -> [Убить] (kill) [Разрешить] (resume + исключение)
        suspended=False -> процесс не тронут (audit/пауза/whitelist) —
                           только [Подробнее]/[Игнорировать].
        """
        try:
            extra = json.loads(ev["row"].get("extra") or "{}")
        except Exception:
            extra = {}
        proc = str(extra.get("process") or "")
        rule_title = tr(str(extra.get("title_key") or "behavior_alert_title"))
        suspended = bool(extra.get("suspended"))
        sev = ev["severity"] or "HIGH"
        color = SEV_COLORS.get(sev, "#D96A6A")

        card = tk.Frame(self, bg="#1B241C", highlightbackground=color,
                        highlightthickness=1, padx=12, pady=10)
        head = tk.Frame(card, bg="#1B241C")
        head.pack(fill="x")
        tk.Label(head, text="●", bg="#1B241C", fg=color,
                 font=("Segoe UI", 12, "bold")).pack(side="left")
        tk.Label(head, text=tr("behavior_alert_title"), bg="#1B241C", fg=color,
                 font=("Segoe UI", 11, "bold")).pack(side="left", padx=(6, 0))
        tk.Label(head, text=sev, bg="#1B241C", fg=color,
                 font=("Segoe UI", 9, "bold")).pack(side="right")
        tk.Label(card, text=tr("behavior_alert_text", proc=proc, rule=rule_title),
                 bg="#1B241C", fg="#E6F0E8", font=("Segoe UI", 9),
                 anchor="w", wraplength=380, justify="left").pack(fill="x",
                                                                  pady=(2, 2))
        cmdline = str(extra.get("cmdline") or "")
        if cmdline:
            if len(cmdline) > 64:
                cmdline = cmdline[:63] + "…"
            tk.Label(card, text=cmdline, bg="#1B241C", fg="#9BB3A2",
                     font=("Segoe UI", 8), anchor="w",
                     wraplength=380, justify="left").pack(fill="x", pady=(0, 6))
        btns = tk.Frame(card, bg="#1B241C")
        btns.pack(fill="x")

        rec = {"frame": card, "alive": True, "job": None, "ev": ev, "x": None}

        def qbtn(text, cmd, bg, fg):
            b = tk.Label(btns, text=text, bg=bg, fg=fg, padx=10, pady=4,
                         cursor="hand2", font=("Segoe UI", 9, "bold"))
            b.pack(side="left", padx=(0, 6))
            b.bind("<Button-1>", lambda _e: cmd(rec))
            return b

        def do_kill(rec_: dict) -> None:
            if self._on_behavior_kill:
                try:
                    self._on_behavior_kill(extra, rec_["ev"]["row"])
                except Exception as exc:
                    logger.warning("behavior kill failed: %s", exc)
            self._dismiss(rec_)

        def do_allow(rec_: dict) -> None:
            if self._on_behavior_allow:
                try:
                    self._on_behavior_allow(extra, rec_["ev"]["row"])
                except Exception as exc:
                    logger.warning("behavior allow failed: %s", exc)
            self._dismiss(rec_)

        if suspended:
            qbtn(tr("btn_kill"), do_kill, "#D96A6A", "#0F1410")
            qbtn(tr("btn_allow"), do_allow, "#4CAF7D", "#0F1410")
        else:
            qbtn(tr("btn_details"), self._do_details, "#223025", "#9BB3A2")
        qbtn(tr("btn_cancel"), self._dismiss, "#223025", "#E6F0E8")

        card.pack(fill="x", pady=4, anchor="e")
        self._cards.append(rec)
        self._slide_in(rec)

    # ── групповая карточка анти-спама (Задача 1.5) ──────────────────────────
    def _spawn_burst_card(self, events: list[dict]) -> None:
        card = tk.Frame(self, bg="#1B241C", highlightbackground="#D96A6A",
                        highlightthickness=1, padx=12, pady=10)
        head = tk.Frame(card, bg="#1B241C")
        head.pack(fill="x")
        tk.Label(head, text="●", bg="#1B241C", fg="#D96A6A",
                 font=("Segoe UI", 12, "bold")).pack(side="left")
        title = tk.Label(head, bg="#1B241C", fg="#D96A6A",
                         font=("Segoe UI", 11, "bold"))
        title.pack(side="left", padx=(6, 0))
        sub = tk.Label(card, bg="#1B241C", fg="#9BB3A2",
                       font=("Segoe UI", 9), anchor="w")
        sub.pack(fill="x")
        btns = tk.Frame(card, bg="#1B241C")
        btns.pack(fill="x")

        rec = {"frame": card, "alive": True, "job": None, "events": list(events),
               "title": title, "sub": sub}

        def gbtn(text, cmd, bg, fg):
            b = tk.Label(btns, text=text, bg=bg, fg=fg, padx=10, pady=4,
                         cursor="hand2", font=("Segoe UI", 9, "bold"))
            b.pack(side="left", padx=(0, 6))
            b.bind("<Button-1>", lambda _e: cmd())

        def ignore() -> None:
            # «Игнорировать» -> пометить все события окна как отклонённые
            for ev in rec["events"]:
                try:
                    self._on_dismiss(ev["row"])
                except Exception as exc:
                    logger.warning("burst dismiss failed: %s", exc)
            self._dismiss_burst()

        def quarantine_all() -> None:
            paths = sorted({ev["row"].get("image_path") or ""
                            for ev in rec["events"]} - {""})
            if self._on_quarantine_all and paths:
                try:
                    self._on_quarantine_all(paths)
                except Exception as exc:
                    logger.warning("burst quarantine-all failed: %s", exc)
            self._dismiss_burst()

        def show_all() -> None:
            if self._on_show_all:
                try:
                    self._on_show_all(list(rec["events"]))
                except Exception as exc:
                    logger.warning("burst show-all failed: %s", exc)
            self._dismiss_burst()

        gbtn(tr("burst_quarantine_all"), quarantine_all, "#4CAF7D", "#0F1410")
        gbtn(tr("burst_show_all"), show_all, "#223025", "#E6F0E8")
        gbtn(tr("burst_ignore"), ignore, "#223025", "#E6F0E8")

        card.pack(fill="x", pady=4, anchor="e")
        self._burst_rec = rec
        self._update_burst_card(rec["events"])
        self._slide_in(rec)

    def _update_burst_card(self, events: list[dict]) -> None:
        rec = self._burst_rec
        if rec is None or not rec["alive"]:
            return
        rec["events"] = list(events)
        n = len(events)
        rec["title"].configure(text=tr("burst_title", n=n))
        rec["sub"].configure(text=tr("burst_subtitle", n=n))

    def _dismiss_burst(self) -> None:
        rec = self._burst_rec
        if rec is None or not rec["alive"]:
            return
        rec["alive"] = False
        if rec["job"]:
            self.after_cancel(rec["job"])
            rec["job"] = None
        rec["frame"].destroy()
        self._burst_rec = None

    def _slide_in(self, rec: dict) -> None:
        """Slide справа + fade, 200 мс, шагами по таймеру."""
        steps = max(1, SLIDE_MS // ANIM_FRAME_MS)
        state = {"i": 0}

        def step() -> None:
            if not rec["alive"]:
                return
            state["i"] += 1
            t = state["i"] / steps
            try:
                self.master.update_idletasks()
                # лёгкий сдвиг: имитация через отступ якоря (place x)
                self.place_configure(x=-16 + int((1 - t) * 40))
            except tk.TclError:
                return
            if state["i"] < steps:
                rec["job"] = self.after(ANIM_FRAME_MS, step)

        step()

    def _dismiss(self, rec: dict) -> None:
        if not rec["alive"]:
            return
        rec["alive"] = False
        if rec["job"]:
            self.after_cancel(rec["job"])
            rec["job"] = None
        rec["frame"].destroy()
        self._cards = [c for c in self._cards if c["alive"]]
        self._on_dismiss(rec["ev"]["row"])
        # очередь: показать следующую
        if self._queue and len([c for c in self._cards if c["alive"]]) < MAX_CARDS:
            nxt = self._queue.pop(0)
            self._spawn_card(nxt)

    def _do_quarantine(self, rec: dict) -> None:
        path = rec["ev"]["row"].get("image_path") or ""
        try:
            self._on_quarantine(path)
        except Exception as exc:
            logger.warning("quarantine action failed: %s", exc)
        self._dismiss(rec)

    def _do_details(self, rec: dict) -> None:
        self._on_details(rec["ev"]["row"])
        self._dismiss(rec)


def _attach_tooltip(widget: tk.Widget, text: str) -> None:
    tip = {"win": None}

    def enter(_e):
        x, y = widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height() + 4
        win = tk.Toplevel(widget)
        win.wm_overrideredirect(True)
        win.wm_geometry(f"+{x}+{y}")
        tk.Label(win, text=text, bg="#223025", fg="#E6F0E8",
                 font=("Segoe UI", 8), padx=6, pady=3).pack()
        tip["win"] = win

    def leave(_e):
        if tip["win"]:
            tip["win"].destroy()
            tip["win"] = None

    widget.bind("<Enter>", enter)
    widget.bind("<Leave>", leave)


def make_system_notifier():
    """Windows toast для HIGH/CRITICAL. None, если библиотека недоступна."""
    try:
        from windows_toasts import Toast, WindowsToaster
        toaster = WindowsToaster("StarAstra")

        def send(title: str, message: str) -> None:
            try:
                t = Toast()
                t.text_fields = [title, message]
                toaster.show_toast(t)
            except Exception as exc:
                logger.debug("toast failed: %s", exc)

        return send
    except ImportError:
        logger.info("windows-toasts not installed — in-app cards only")
        return None
