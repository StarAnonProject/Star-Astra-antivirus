#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Behavior sensor: фоновый опрос psutil раз в 2 с (PID/PPID/cmdline).

Принципы «легко, без нагрузки» (ТЗ спринта 5):
- события только о процессах, появившихся ПОСЛЕ старта сенсора (baseline
  при старте не алертим — не замораживаем то, что уже работало);
- сетевая проверка (Temp->Network) только для новых процессов, чей exe
  лежит в Temp/Downloads — net_connections не зовётся для остальных;
- настройки (режим/пауза/исключения) перечитываются из
  starastra_settings.json каждую итерацию — переключение режима в UI
  действует без рестарта.

События (kind): new_process (новый pid) и network_activity (у процесса
из Temp/Downloads появились внешние соединения). Матчинг и реакция —
starastra_behavior.engine.RuleEngine.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading

PROJECT_ROOT = (os.path.dirname(os.path.abspath(sys.executable))
                if getattr(sys, "frozen", False)
                else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starastra_behavior.engine import RuleEngine      # noqa: E402

logger = logging.getLogger("starastra.behavior")

SETTINGS_FILE = os.path.join(PROJECT_ROOT, "starastra_settings.json")


def _read_settings() -> dict | None:
    """Настройки; None = нечитаемы (политику считаем неизвестной)."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _snapshot() -> dict[int, dict]:
    """{pid: {pid, ppid, name, cmdline, exe, exe_dir}} — одно process_iter."""
    import psutil
    out: dict[int, dict] = {}
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline", "exe"]):
        info = proc.info
        try:
            exe = info.get("exe") or ""
            out[info["pid"]] = {
                "pid": info["pid"],
                "ppid": info.get("ppid"),
                "name": info.get("name") or "",
                "cmdline": " ".join(info.get("cmdline") or []),
                "exe": exe,
                "exe_dir": os.path.dirname(exe) if exe else "",
            }
        except Exception:
            continue
    return out


def _has_external_connection(pid: int) -> bool:
    """У процесса есть установленные соединения на ВНЕШНИЕ адреса?"""
    import psutil
    try:
        proc = psutil.Process(pid)
        for conn in proc.net_connections(kind="inet"):
            if conn.status != psutil.CONN_ESTABLISHED:
                continue
            raddr = conn.raddr
            if not raddr or not raddr.ip:
                continue
            ip = raddr.ip
            if ip.startswith("127.") or ip in ("::1", "0.0.0.0", "::"):
                continue
            # link-local / multicast не считаем «сетевой активностью»
            if ip.startswith("169.254.") or ip.startswith("224.") or \
                    ip.startswith("239.") or ip.startswith("fe80:"):
                continue
            return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    except Exception as exc:
        logger.debug("net_connections failed (pid=%s): %s", pid, exc)
        return False
    return False


class BehaviorSensor(threading.Thread):
    """Поток сенсора: опрос раз в interval секунд -> правила -> реакция."""

    def __init__(self, interval: float = 2.0,
                 settings_file: str = SETTINGS_FILE,
                 engine_db: str | None = None,
                 stop_event: threading.Event | None = None):
        super().__init__(daemon=True, name="starastra-behavior")
        self.interval = interval
        self.settings_file = settings_file
        self.engine = RuleEngine()
        self.engine_db = engine_db or os.path.join(
            PROJECT_ROOT, "sentinel_pkg", "sentinel", "data", "events.db")
        self._stop = stop_event or threading.Event()
        self._seen: set[int] = set()
        self._net_checked: set[int] = set()
        self._baseline_pids: set[int] = set()

    def run(self) -> None:
        # baseline: процессы, жившие ДО старта сенсора, не алертим —
        # не замораживаем то, что уже работало (ТЗ: «без нагрузки»)
        try:
            self._baseline_pids = set(_snapshot())
        except Exception as exc:
            logger.warning("behavior baseline failed: %s", exc)
        logger.info(
            "behavior sensor started (pid=%s, interval=%ss, rules=%s: %s)",
            os.getpid(), self.interval, len(self.engine.rules),
            ", ".join(r["id"] for r in self.engine.rules))
        while not self._stop.wait(self.interval):
            try:
                self._cycle()
            except Exception as exc:
                logger.warning("behavior cycle failed: %s", exc)

    # ────────────────────────── один опрос ──────────────────────────
    def _cycle(self) -> None:
        settings = _read_settings()
        if not settings:
            return                       # политика неизвестна — без действий
        snap = _snapshot()

        # 1) НОВЫЕ процессы -> правила new_process
        for pid, child in snap.items():
            if pid in self._seen:
                continue
            self._seen.add(pid)
            parent = snap.get(child.get("ppid"))
            event = {"kind": "new_process", "child": child,
                     "parent_name": (parent or {}).get("name", "")}
            rule = self.engine.match(event)
            if rule:
                self.engine.react(event, rule, settings, self.engine_db)

        # 2) Temp/Downloads + сеть: только процессы, появившиеся ПОСЛЕ
        #    старта сенсора, раз на pid (baseline не проверяем сетью)
        for pid, child in snap.items():
            if pid in self._net_checked or pid in self._baseline_pids:
                continue
            self._net_checked.add(pid)
            if not child.get("exe_dir"):
                continue
            event = {"kind": "network_activity", "child": child}
            rule = self.engine.match(event)
            if not rule:
                continue
            if _has_external_connection(pid):
                self.engine.react(event, rule, settings, self.engine_db)


_sensor: BehaviorSensor | None = None
_sensor_lock = threading.Lock()


def _setup_behavior_logging() -> None:
    """FileHandler data/behavior.log (один на процесс; повторный вызов — no-op)."""
    lg = logging.getLogger("starastra.behavior")
    for h in lg.handlers:
        if isinstance(h, logging.FileHandler) and \
                getattr(h, "baseFilename", "") == os.path.join(
                    PROJECT_ROOT, "data", "behavior.log"):
            return
    os.makedirs(os.path.join(PROJECT_ROOT, "data"), exist_ok=True)
    fh = logging.FileHandler(os.path.join(PROJECT_ROOT, "data", "behavior.log"),
                             encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    lg.addHandler(fh)
    lg.setLevel(logging.INFO)
    lg.propagate = False


def start_sensor(interval: float = 2.0,
                 settings_file: str = SETTINGS_FILE,
                 engine_db: str | None = None) -> BehaviorSensor:
    """Запуск сенсора (идемпотентно); поток daemon — умирает с процессом."""
    global _sensor
    with _sensor_lock:
        if _sensor is not None and _sensor.is_alive():
            return _sensor
        _setup_behavior_logging()
        _sensor = BehaviorSensor(interval=interval,
                                 settings_file=settings_file,
                                 engine_db=engine_db)
        _sensor.start()
        return _sensor
