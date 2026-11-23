#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Точка входа StarAstra.exe (PyInstaller, Задача 3.3).

Один exe — несколько ролей, выбор флагом --star-worker=<роль>:
  (без флага)   лаунчер: движок + сторож + UI (starastra_launch.main)
  ui            дашборд (starastra_dashboard.main) — отдельным процессом,
                чтобы lock/focus-логика лаунчера работала как в dev
  engine        движок через starastra_engine_bootstrap (фильтр само-шума)
  watchdog      starastra_watchdog.main
  scan <kind>   starastra_scan.main (quick|full), JSON-протокол в stdout
  snippet <code> [argv...]   exec кода движка (карантин) — аналог `python -c`

Сборка (build_exe.bat) — ДВА exe из этого файла:
  StarAstra.exe        --windowed: лаунчер и UI (консоль не нужна);
  StarAstraWorker.exe  --console: engine/watchdog/scan/snippet — эти роли
                       печатают в stdout (JSON-протокол скана, сниппеты
                       карантина), у windowed-exe stdout перехватывается
                       PyInstaller'ом и каналом PIPE теряется.

В frozen-режиме sentinel_pkg (.pyc движка) и Sentinel/_internal (ML-рантайм:
numpy/sklearn/lightgbm/yara + модели) НЕ вшиты в exe — они лежат рядом и
добавляются в sys.path (тот же PYTHONPATH, что и в dev-режиме). Код движка
не дублируется в exe — работает ровно та же раскладка пакетов, что в dev.
"""

from __future__ import annotations

import os
import sys


def _setup_frozen_paths() -> None:
    """sys.path/cwd как в dev: корень = каталог exe."""
    base = os.path.dirname(os.path.abspath(sys.executable))
    try:
        os.chdir(base)
    except OSError:
        pass
    for p in (os.path.join(base, "sentinel_pkg"),
              os.path.join(base, "Sentinel", "_internal")):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)


def main() -> int:
    worker: str | None = None
    rest: list[str] = []
    for a in sys.argv[1:]:
        if a.startswith("--star-worker="):
            worker = a.split("=", 1)[1]
        else:
            rest.append(a)

    if not getattr(sys, "frozen", False):
        # отладочный запуск скриптом: без флага — обычный лаунчер
        worker = worker or "launcher"

    if worker in (None, "launcher", "ui"):
        if worker == "ui":
            _setup_frozen_paths()
            sys.argv = [sys.argv[0], *rest]
            import starastra_dashboard
            return starastra_dashboard.main()
        # лаунчер: сам знает, frozen ли он (sys.executable)
        import starastra_launch
        return starastra_launch.main()

    # рабочие роли: путь к sentinel-пакетам обязателен ДО импортов
    _setup_frozen_paths()
    sys.argv = [sys.argv[0], *rest]

    if worker == "engine":
        import starastra_engine_bootstrap
        return starastra_engine_bootstrap.main()
    if worker == "watchdog":
        import starastra_watchdog
        return starastra_watchdog.main()
    if worker == "scan":
        import starastra_scan
        return starastra_scan.main()
    if worker == "snippet":
        # код движка (карантин): эквивалент `python -c "<code>" argv...`
        code = rest[0] if rest else ""
        sys.argv = [sys.argv[0], *rest[1:]]
        exec(compile(code, "<starastra-snippet>", "exec"),
             {"__name__": "__main__"})   # noqa: S102 (доверенный код UI)
        return 0

    print(f"StarAstra: unknown --star-worker: {worker!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
