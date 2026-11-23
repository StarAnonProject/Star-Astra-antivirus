#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""StarAstra scan runner — процесс-сканер для дашборда (Задача 1.3).

Запускается UI ОТДЕЛЬНЫМ процессом (UI не импортирует sentinel_pkg напрямую):

    python starastra_scan.py quick   # OnDemandScanner.get_quick_scan_targets()
    python starastra_scan.py full    # OnDemandScanner.get_full_scan_targets()

Протокол — JSON-строки в stdout (после каждой — flush):
  {"type":"progress","files_scanned":N,"total_estimate":M,"current_file":"...","elapsed":S}
  {"type":"threat","path":"...","name":"...","score":S,"severity":"HIGH","quarantined":true,"is_memory":false}
  {"type":"done","status":"COMPLETED|CANCELLED|ERROR","files_scanned":N,
   "threats_found":M,"duration":S,"error":null|"..."}

Посторонние строки (логи движка в stdout) UI молча пропускает — это не JSON.

Отмена: UI завершает процесс (terminate). Интроспекция движка подтверждает
наличие OnDemandScanner.cancel()/pause()/resume(), но у UI нет канала вызвать
их в чужом процессе — терминация процесса-сканера единственный надёжный
cancel. SQLite-записи сканера краш-безопасны (журнал транзакций).
Пауза/продолжение скана через pause()/resume() в таком сценарии недоступны —
осознанное решение, не баг.
"""

from __future__ import annotations

import json
import os
import sys
import time

PROJECT_ROOT = (os.path.dirname(os.path.abspath(sys.executable))
                if getattr(sys, "frozen", False)
                else os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(PROJECT_ROOT, "sentinel_pkg"),
           os.path.join(PROJECT_ROOT, "Sentinel", "_internal")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _scan_kind_arg() -> str:
    kind = (sys.argv[1] if len(sys.argv) > 1 else "quick").lower()
    if kind not in ("quick", "full"):
        raise SystemExit(f"usage: {os.path.basename(sys.argv[0])} quick|full")
    return kind


def main() -> int:
    kind = _scan_kind_arg()
    try:
        from sentinel.engine.scanner import OnDemandScanner
    except Exception as exc:
        _emit({"type": "done", "status": "ERROR", "files_scanned": 0,
               "threats_found": 0, "duration": 0.0,
               "error": f"OnDemandScanner import failed: {exc}"})
        return 1

    scanner = OnDemandScanner()

    def on_progress(p) -> None:
        try:
            _emit({"type": "progress",
                   "files_scanned": int(getattr(p, "files_scanned", 0) or 0),
                   "total_estimate": int(getattr(p, "total_files_estimate", 0) or 0),
                   "current_file": str(getattr(p, "current_file", "") or ""),
                   "elapsed": float(getattr(p, "elapsed_seconds", 0.0) or 0.0)})
        except Exception:
            pass  # прогресс не критичен — не роняем скан

    def on_threat(t) -> None:
        try:
            _emit({"type": "threat",
                   "path": str(getattr(t, "file_path", "") or ""),
                   "name": str(getattr(t, "threat_name", "") or ""),
                   "score": float(getattr(t, "score", 0.0) or 0.0),
                   "severity": str(getattr(t, "severity", "")).split(".")[-1],
                   "quarantined": bool(getattr(t, "quarantined", False)),
                   "is_memory": bool(getattr(t, "is_memory", False))})
        except Exception:
            pass

    def on_done(summary) -> None:
        try:
            _emit({"type": "done",
                   "status": str(getattr(summary, "status", "")).split(".")[-1],
                   "files_scanned": int(getattr(summary, "files_scanned", 0) or 0),
                   "threats_found": int(getattr(summary, "threats_found", 0) or 0),
                   "duration": float(getattr(summary, "duration_seconds", 0.0) or 0.0),
                   "error": getattr(summary, "error_message", None)})
        except Exception:
            pass

    started = time.monotonic()
    try:
        if kind == "quick":
            thread = scanner.start_quick_scan(
                progress_callback=on_progress,
                threat_callback=on_threat,
                completion_callback=on_done)
        else:
            thread = scanner.start_full_scan(
                progress_callback=on_progress,
                threat_callback=on_threat,
                completion_callback=on_done)
    except Exception as exc:
        _emit({"type": "done", "status": "ERROR", "files_scanned": 0,
               "threats_found": 0, "duration": 0.0,
               "error": f"start_{kind}_scan failed: {exc}"})
        return 1

    if thread is not None:
        thread.join()
    else:
        # движок вернул None вместо потока — ждём завершения по статусу
        while str(getattr(scanner, "status", "")).upper().endswith("SCANNING"):
            time.sleep(0.3)
        on_done(scanner.last_summary)  # type: ignore[arg-type]

    if getattr(scanner, "last_summary", None) is None and \
            time.monotonic() - started > 0.5:
        # completion_callback не прилетел (движок не вызвал) — синтезируем done,
        # чтобы UI не завис в «выполняется» до терминации процесса
        _emit({"type": "done", "status": "ERROR",
               "files_scanned": 0, "threats_found": 0,
               "duration": time.monotonic() - started,
               "error": "scanner returned no summary (completion_callback not called)"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
