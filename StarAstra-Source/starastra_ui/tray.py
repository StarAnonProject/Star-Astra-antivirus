# -*- coding: utf-8 -*-
"""Трей-иконка StarAstra (pystray + Pillow).

Потоки: pystray крутится в своём потоке (run_detached); колбэки из трея
кладутся в очередь и исполняются В ГЛАВНОМ потоке Tk (pump() по after),
напрямую Tkinter из потока трея не вызывается.

Иконки: assets/starastra.ico (активна), assets/starastra_paused.ico
(пауза, янтарная звезда; при отсутствии файла рисуется программно).
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import sys

logger = logging.getLogger("starastra.ui.tray")

PROJECT_ROOT = (os.path.dirname(os.path.abspath(sys.executable))
                if getattr(sys, "frozen", False)
                else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (PROJECT_ROOT, os.path.join(PROJECT_ROOT, "Sentinel", "ui")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from starastra_i18n import tr  # noqa: E402
ICON_ACTIVE = os.path.join(PROJECT_ROOT, "assets", "starastra.ico")
ICON_PAUSED = os.path.join(PROJECT_ROOT, "assets", "starastra_paused.ico")

PAUSE_CHOICES = (10, 15, 30, 45, 60)


def _fallback_star(color: str):
    """Простая зелёная/янтарная звезда через PIL (fallback, если .ico нет)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    import math
    cx = cy = 32.0
    r_out, r_in = 30.0, 12.0
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    d.polygon(pts, fill=color)
    return img


def _load_active_image():
    from PIL import Image
    try:
        return Image.open(ICON_ACTIVE)
    except Exception:
        return _fallback_star("#4CAF7D")


def _load_paused_image():
    """Янтарная иконка паузы: файл assets/starastra_paused.ico (создаётся
    программно при первом запуске, если отсутствует)."""
    from PIL import Image
    try:
        if os.path.isfile(ICON_PAUSED):
            return Image.open(ICON_PAUSED)
    except Exception:
        pass
    try:
        img = _fallback_star("#D9A441")
        os.makedirs(os.path.dirname(ICON_PAUSED), exist_ok=True)
        img.save(ICON_PAUSED, format="ICO", sizes=[(64, 64)])
        logger.info("paused tray icon generated: %s", ICON_PAUSED)
        return img
    except Exception:
        return _fallback_star("#D9A441")


class TrayController:
    """Управление треем. Все действия с Tk — только через очередь + pump()."""

    def __init__(self, app) -> None:
        self.app = app
        self.icon = None
        self._actions: "queue.Queue[tuple]" = queue.Queue()
        self._img_active = None
        self._img_paused = None
        # зеркала состояния для меню/тултипа (обновляются из главного потока)
        self._status_text = "—"
        self._tooltip = "StarAstra"
        self._lock = threading.Lock()

    # ── запуск / остановка ──────────────────────────────────────────────
    def start(self) -> bool:
        try:
            import pystray  # noqa: F401
        except ImportError:
            logger.warning("PYSTRAY: NOT AVAILABLE — tray icon disabled")
            return False
        try:
            self._img_active = _load_active_image()
            self._img_paused = _load_paused_image()
        except Exception as exc:
            logger.warning("tray images failed: %s", exc)
            return False

        import pystray
        menu = pystray.Menu(
            pystray.MenuItem(lambda *a: tr("tray_open"),
                             self._on_open, default=True),
            pystray.MenuItem(lambda *a: self._status_text,
                             self._status_text_action, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda *a: tr("tray_pause"),
                             self._pause_submenu_items),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda *a: tr("tray_quit"), self._on_quit),
        )
        self.icon = pystray.Icon("StarAstra", self._img_active,
                                 self._tooltip, menu)
        try:
            self.icon.run_detached()
            logger.info("tray icon started (detached)")
        except Exception as exc:
            logger.warning("tray run_detached failed: %s", exc)
            self.icon = None
            return False
        self.pump()
        return True

    def stop(self) -> None:
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None
            logger.info("tray icon stopped")

    # ── мост потоков: трей -> Tk ────────────────────────────────────────
    def run_in_main(self, fn, *args) -> None:
        self._actions.put((fn, args))

    def pump(self) -> None:
        """Исполняет отложенные действия в главном потоке Tk (every 120 ms)."""
        while True:
            try:
                fn, args = self._actions.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception as exc:
                logger.warning("tray action failed: %s", exc)
        try:
            self.app.after(120, self.pump)
        except Exception:
            pass  # окно уже уничтожено

    # ── действия пользователя ───────────────────────────────────────────
    def _on_open(self, icon=None, item=None) -> None:
        self.run_in_main(self.app.tray_show_window)

    def _status_text_action(self, icon=None, item=None) -> None:  # disabled
        pass

    def _pause_submenu_items(self):
        import pystray
        for n in PAUSE_CHOICES:
            def act(icon, item, _n=n):
                self.run_in_main(self.app.tray_pause, _n)
            yield pystray.MenuItem(f"{n} min", act)

    def _on_quit(self, icon=None, item=None) -> None:
        self.run_in_main(self.app.shutdown_from_tray)

    # ── статус (вызывается из главного потока) ──────────────────────────
    def update_status(self, paused: bool, status_text: str,
                      tooltip: str) -> None:
        """Обновить текст статуса в меню, тултип и картинку иконки."""
        self._status_text = status_text
        self._tooltip = tooltip
        if self.icon is None:
            return
        try:
            self.icon.tooltip = tooltip
            if self._img_paused is not None:
                self.icon.icon = self._img_paused if paused else self._img_active
        except Exception as exc:
            logger.debug("tray status update failed: %s", exc)
