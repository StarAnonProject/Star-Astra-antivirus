#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""StarAstra Behavioral Analysis (Спринт 5) — поведенческий детектор.

Состав пакета:
  sensor.py   — фоновый опрос psutil (PID/PPID/cmdline) раз в 2 с;
  rules.json  — 5 правил (Office->Shell, LOLBins, PowerShell -enc,
                Temp->Network, Script Engines);
  engine.py   — проверка событий по правилам + реакция (SUSPEND).

Ключевые принципы (ТЗ спринта 5):
- «Легко, без нагрузки»: опрос раз в 2 с, сетевая проверка только для
  процессов, запущенных из Temp/Downloads, события только о НОВЫХ
  процессах (появившихся после старта сенсора) — без сканов всего диска;
- Anti-Freeze Guard: перед SUSPEND имя процесса проверяется по ТОМУ ЖЕ
  WHITELIST, что в starastra_guard.py (explorer.exe/браузер/системные
  никогда не замораживаются — только запись в лог);
- уважение паузы защиты (protection_paused_until) и исключений UI
  (type='process') из starastra_settings.json;
- два режима (settings.behavior_mode): 'active' — suspend подозрительного
  (по умолчанию), 'audit' — только уведомлять.

Запуск: поток внутри процесса сторожа (starastra_watchdog.run) — сторож
долгоживущий и уже следит за настройками; отдельный процесс не нужен.
"""

from __future__ import annotations

import threading

from starastra_behavior.sensor import BehaviorSensor, start_sensor  # noqa: F401

__all__ = ["BehaviorSensor", "start_sensor"]
