#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""StarAstra audit — журнал действий пользователя и продукта (Задача 2.4).

Формат: JSON Lines, одна строка = одно действие:

    {"ts": "2026-11-10T01:23:45", "user": "User",
     "action": "pause", "details": {"minutes": 30}}

Файл: data/audit.log. Пишут и UI-процесс, и watchdog (действия движка).
Append-only: никакая логика не редактирует прошлые записи.

Использование:
    from starastra_audit import audit
    audit("pause", {"minutes": 30})
    audit("exclusion_add", {"type": "path", "value": r"C:\\Tools"})
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import sys
import threading

logger = logging.getLogger("starastra.audit")

PROJECT_ROOT = (os.path.dirname(os.path.abspath(sys.executable))
                if getattr(sys, "frozen", False)   # 3.3: данные рядом с exe
                else os.path.dirname(os.path.abspath(__file__)))
AUDIT_LOG = os.path.join(PROJECT_ROOT, "data", "audit.log")

_lock = threading.Lock()

# Известные действия (для фильтра в UI, Задача 2.4)
ACTIONS = (
    "engine_start", "engine_stop", "engine_restart",         # жизненный цикл
    "pause", "resume",                                        # защита
    "quarantine_add", "quarantine_restore", "quarantine_delete",
    "exclusion_add", "exclusion_remove", "exclusion_external_change",
    "scan_started", "scan_finished", "scan_cancelled",
    "settings_change", "language_change", "autostart_change",
    "update_check", "watchdog_alert",
)


def audit(action: str, details: dict | None = None, user: str | None = None) -> bool:
    """Добавить запись в audit.log. True — запись записана."""
    rec = {
        "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "user": user or _current_user(),
        "action": action,
        "details": details if isinstance(details, dict) else
                   ({"value": details} if details is not None else {}),
    }
    try:
        line = json.dumps(rec, ensure_ascii=False)
        if len(line.encode("utf-8")) > 32_000:      # держим строку атомарно короткой
            rec["details"] = {"truncated": True}
            line = json.dumps(rec, ensure_ascii=False)
        with _lock:
            os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
            with open(AUDIT_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    except OSError as exc:
        logger.warning("audit write failed: %s", exc)
        return False


def read_audit(limit: int = 200, action_filter: str | None = None) -> list[dict]:
    """Последние записи журнала (новые сверху), опционально по действию."""
    try:
        with open(AUDIT_LOG, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if action_filter and rec.get("action") != action_filter:
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USERNAME", "unknown")
