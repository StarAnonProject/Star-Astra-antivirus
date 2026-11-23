#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Behavior engine: правила + матчинг событий + реакция (SUSPEND).

Задача 2 (реакция) — точная последовательность ТЗ:
  1. имя процесса -> WHITELIST (ТОТ ЖЕ, что starastra_guard.py):
     в белом списке — НЕ ТРОГАТЬ, только запись в лог;
  2. имя в исключениях UI (type='process') — не трогать;
  3. режим 'audit' (settings.behavior_mode) — не трогать, только событие;
  4. иначе psutil.Process(pid).suspend() — процесс заморожен;
  5. событие 'behavior_alert' (severity HIGH из правила) в events.db
     с деталями: кто заморожен, какое правило сработало — UI покажет
     карточку с кнопками [Убить]/[Разрешить] (ToastPoller читает events.db).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import sqlite3
import sys

PROJECT_ROOT = (os.path.dirname(os.path.abspath(sys.executable))
                if getattr(sys, "frozen", False)
                else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger("starastra.behavior")

# WHITELIST — ЕДИНСТВЕННЫЙ источник истины: модуль Anti-Freeze Guard (4.1).
# explorer.exe/браузер/системные/наши процессы поведенческий движок никогда
# не замораживает (требование ТЗ спринта 5).
from starastra_guard import WHITELIST            # noqa: E402

RULES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "rules.json")
ENGINE_DB = os.path.join(PROJECT_ROOT, "sentinel_pkg", "sentinel", "data",
                         "events.db")

# Встроенная копия rules.json (fallback: файл не читается / потерян при
# упаковке — движок продолжает работать, правила не пустые).
DEFAULT_RULES: list[dict] = [
    {"id": "office_shell", "severity": "HIGH", "on": "new_process",
     "match": {"parent_names": ["winword.exe", "excel.exe", "powerpnt.exe",
                                 "outlook.exe", "mspub.exe", "access.exe",
                                 "soffice.bin"],
               "child_names": ["cmd.exe", "powershell.exe", "pwsh.exe",
                                 "wscript.exe", "cscript.exe", "mshta.exe",
                                 "rundll32.exe", "regsvr32.exe"]}},
    {"id": "lolbins", "severity": "HIGH", "on": "new_process",
     "match": {"child_names": ["certutil.exe", "bitsadmin.exe", "mshta.exe",
                                 "rundll32.exe", "regsvr32.exe", "regasm.exe",
                                 "installutil.exe", "msbuild.exe", "wmic.exe",
                                 "schtasks.exe", "certreq.exe", "at.exe",
                                 "curl.exe", "ftp.exe", "tftp.exe"]}},
    {"id": "ps_encoded", "severity": "HIGH", "on": "new_process",
     "match": {"child_names": ["powershell.exe", "pwsh.exe"],
               "cmdline_regex": ["-enc(odedcommand)?\\b", "-w\\s+hidden",
                                  "-windowstyle\\s+hidden", "-noni\\b"]}},
    {"id": "temp_network", "severity": "HIGH", "on": "network_activity",
     "match": {"exe_dir_markers": ["\\temp\\", "\\tmp\\", "\\downloads\\",
                                    "\\appdata\\local\\temp\\"]}},
    {"id": "script_engine", "severity": "HIGH", "on": "new_process",
     "match": {"parent_names": ["wscript.exe", "cscript.exe", "mshta.exe"]}},
]


class RuleEngine:
    """Загрузка правил (rules.json или DEFAULT_RULES) + матчинг событий."""

    def __init__(self, rules_file: str = RULES_FILE) -> None:
        self.rules: list[dict] = self._load(rules_file)

    @staticmethod
    def _load(path: str) -> list[dict]:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            rules = [r for r in (data.get("rules") or [])
                     if isinstance(r, dict) and r.get("id")
                     and isinstance(r.get("match"), dict)]
            if rules:
                return rules
        except Exception as exc:
            logger.warning("rules load failed (%s) — using built-in copy", exc)
        return [dict(r) for r in DEFAULT_RULES]

    # ─────────────────────── матчинг (Задача 1) ───────────────────────
    @staticmethod
    def _rule_matches(rule: dict, event: dict) -> bool:
        m = rule["match"]
        kind = event.get("kind")
        if rule.get("on", "new_process") != kind:
            return False
        child = event["child"]
        cname = (child.get("name") or "").lower()
        parent = (event.get("parent_name") or "").lower()
        pn = m.get("parent_names")
        if pn and parent not in pn:
            return False
        cn = m.get("child_names")
        if cn and cname not in cn:
            return False
        rx = m.get("cmdline_regex")
        if rx:
            cl = (child.get("cmdline") or "").lower()
            if not any(re.search(r, cl) for r in rx):
                return False
        # правило обязано иметь хоть один критерий (не пустое = не матчит)
        return bool(pn or cn or rx)

    @staticmethod
    def _rule_matches_network(rule: dict, event: dict) -> bool:
        m = rule["match"]
        if rule.get("on") != "network_activity":
            return False
        dirs = m.get("exe_dir_markers") or []
        if not dirs:
            return False
        # trailing-слеш обязателен: маркер '\temp\' требует, чтобы Temp был
        # именем каталога, а не частью более длинного имени
        exe_dir = (event["child"].get("exe_dir") or "").lower().rstrip("\\/") + "\\"
        return any(marker in exe_dir for marker in dirs)

    def match(self, event: dict) -> dict | None:
        """Первое сработавшее правило или None. События:
        kind='new_process'  — {child:{pid,ppid,name,cmdline,exe,exe_dir},
        parent_name}; kind='network_activity' — {child:{...}}."""
        kind = event.get("kind")
        for rule in self.rules:
            if kind == "new_process" and self._rule_matches(rule, event):
                return rule
            if kind == "network_activity" and \
                    self._rule_matches_network(rule, event):
                return rule
        return None

    # ─────────────── реакция (Задача 2: SUSPEND) ───────────────
    @staticmethod
    def _process_excluded(settings: dict, name: str) -> bool:
        """Исключения UI (type='process') из starastra_settings.json."""
        for item in settings.get("exclusions") or []:
            if isinstance(item, dict) and \
                    str(item.get("type", "")).lower() == "process" and \
                    str(item.get("value") or "").strip().lower() == name:
                return True
        return False

    @staticmethod
    def _paused(settings: dict, now: float) -> bool:
        """Пауза защиты (тот же ключ, что читает сторож)."""
        until = settings.get("protection_paused_until")
        try:
            return bool(until) and now < float(until)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _write_event(child: dict, rule: dict, parent_name: str,
                     suspended: bool, engine_db: str) -> None:
        """behavior_alert в events.db (формат карточек/Истории StarAstra)."""
        try:
            now = _dt.datetime.now(_dt.timezone.utc)
            ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + \
                f"{now.microsecond // 1000:03d}Z"
            extra = {
                "severity": str(rule.get("severity") or "HIGH").upper(),
                "alert_kind": "behavior",
                "pid": child["pid"],            # ЦЕЛЕВОЙ процесс (кнопки UI)
                "process": child.get("name") or "",
                "rule": rule["id"],
                "title_key": "behavior_rule_" + rule["id"],
                "suspended": suspended,
                "parent": parent_name,
                "cmdline": (child.get("cmdline") or "")[:300],
            }
            con = sqlite3.connect(engine_db, timeout=5)
            con.execute(
                "INSERT INTO events (timestamp, source, event_type, pid,"
                " image_path, command_line, hash_sha256, extra)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, "starastra_behavior", "behavior_alert", os.getpid(),
                 child.get("exe") or child.get("name") or "",
                 (child.get("cmdline") or "")[:500], "",
                 json.dumps(extra, ensure_ascii=False)))
            con.commit()
            con.close()
        except Exception as exc:
            logger.warning("behavior event write failed: %s", exc)

    def react(self, event: dict, rule: dict, settings: dict,
              engine_db: str = ENGINE_DB) -> dict:
        """Реакция по ТЗ Задачи 2. Возвращает итог для лога/тестов:
        {suspended: bool, skipped: 'whitelist'|'excluded'|'audit'|'dead'|
        'access_denied'|'psutil_unavailable'|None}."""
        child = event["child"]
        pid = child["pid"]
        name = (child.get("name") or "").lower()
        parent_name = event.get("parent_name") or ""
        result = {"pid": pid, "process": name, "rule": rule["id"],
                  "suspended": False, "skipped": None}

        # 1. WHITELIST Anti-Freeze Guard: НЕ ТРОГАТЬ, только запись в лог
        if name in WHITELIST:
            logger.info(
                "[Behavior] matched rule '%s' for %s (pid=%s) — в WHITELIST "
                "Anti-Freeze Guard, заморозка ЗАПРЕЩЕНА (только лог)",
                rule["id"], name, pid)
            result["skipped"] = "whitelist"
            return result
        # 2. исключения UI
        if self._process_excluded(settings, name):
            logger.info("[Behavior] %s (pid=%s) в исключениях UI — пропуск",
                        name, pid)
            result["skipped"] = "excluded"
            return result
        # пауза защиты: поведение как audit-режим (никого не трогаем)
        if self._paused(settings, _dt.datetime.now().timestamp()):
            self._write_event(child, rule, parent_name, False, engine_db)
            result["skipped"] = "paused"
            return result

        # 3. режим 'audit': только уведомление, процесс не трогаем
        if str(settings.get("behavior_mode") or "active").lower() == "audit":
            self._write_event(child, rule, parent_name, False, engine_db)
            logger.info("[Behavior] %s (pid=%s) rule='%s' — audit: "
                        "уведомление без заморозки", name, pid, rule["id"])
            result["skipped"] = "audit"
            return result

        # 4. SUSPEND
        try:
            import psutil
            psutil.Process(pid).suspend()
        except psutil.NoSuchProcess:              # type: ignore[attr-defined]
            logger.info("[Behavior] %s (pid=%s) уже завершился — пропуск",
                        name, pid)
            result["skipped"] = "dead"
            return result
        except psutil.AccessDenied as exc:        # type: ignore[attr-defined]
            logger.warning("[Behavior] suspend %s (pid=%s) denied: %s",
                           name, pid, exc)
            result["skipped"] = "access_denied"
            return result
        except Exception as exc:
            logger.warning("[Behavior] suspend %s (pid=%s) failed: %s",
                           name, pid, exc)
            result["skipped"] = "psutil_unavailable"
            return result
        result["suspended"] = True
        logger.warning(
            "[Behavior] SUSPENDED %s (pid=%s) rule='%s' parent='%s'",
            name, pid, rule["id"], parent_name)

        # 5. событие behavior_alert (HIGH) -> карточка UI + История
        self._write_event(child, rule, parent_name, True, engine_db)
        return result
