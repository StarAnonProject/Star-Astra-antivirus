#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bootstrap движка StarAstra: запуск sentinel.service c фильтром само-шума.

Единственное отличие от `python -m sentinel.service`: после старта FsSensor
его watchdog-обработчики оборачиваются фильтром, который отбрасывает события
файловой активности внутри собственных каталогов Star-Astra (sentinel_pkg,
Sentinel, .venv, assets, starastra_i18n, starastra_notify, data).

Зачем: проект лежит на Desktop, Desktop в watched-списке движка -> запись
собственных events.db/-journal порождала новые события, EventBus-очередь
переполнялась (`queue full; dropped oldest item`), реальные угрозы стояли
в очереди минуты. См. EICAR_DIAGNOSIS.md.

Сам sentinel_pkg не изменяется — патч ставится в памяти при старте.
"""

from __future__ import annotations

import logging
import os
import sys

PROJECT_ROOT = (os.path.dirname(os.path.abspath(sys.executable))
                if getattr(sys, "frozen", False)
                else os.path.dirname(os.path.abspath(__file__)))
ENGINE_LOG_FILE = os.path.join(PROJECT_ROOT, "data", "engine.log")
for _p in (os.path.join(PROJECT_ROOT, "sentinel_pkg"),
           os.path.join(PROJECT_ROOT, "Sentinel", "_internal")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Каталоги, события из которых движку не интересны (свои файлы).
# .git: git-операции порождают сотни file_write (объекты, index, locks) —
#   шторм забивает EventBus-очередь (см. EICAR_DIAGNOSIS.md) и вызывает
#   пересканирование (LastAccess-модификации читаемых git'ом файлов).
# __pycache__: то же — байткод-кэш интерпретатора.
_NOISE_DIRS: tuple[str, ...] = tuple(os.path.normcase(os.path.join(
    PROJECT_ROOT, name)) + os.sep for name in (
    "sentinel_pkg", "Sentinel", ".venv", "venv", ".git", "__pycache__",
    "assets", "starastra_i18n", "starastra_notify", "data"))


def _is_noise(path: str | None) -> bool:
    if not path:
        return False
    norm = os.path.normcase(os.path.normpath(path))
    return any(norm.startswith(d) for d in _NOISE_DIRS)


def _wrap_handler(handler) -> None:
    """Обернуть on_* обработчика watchdog фильтром само-шума."""
    for name in ("on_created", "on_modified", "on_moved",
                 "on_deleted", "on_any_event"):
        original = getattr(handler, name, None)
        if original is None or getattr(original, "_starastra_noise_filter", False):
            continue

        def filtered(event, _orig=original):
            src = getattr(event, "src_path", None) or getattr(event, "dest_path", None)
            if _is_noise(src) or _is_noise(getattr(event, "dest_path", None)):
                return
            return _orig(event)

        filtered._starastra_noise_filter = True  # type: ignore[attr-defined]
        try:
            setattr(handler, name, filtered)  # instance attr > class method
        except Exception:
            pass


def _patch_fs_sensor() -> None:
    from sentinel.sensors import fs_sensor as fs_mod

    fs_cls = fs_mod.FsSensor
    orig_start = fs_cls.start

    def start_with_filter(self):
        orig_start(self)
        wrapped = 0
        for obs in getattr(self, "_observers", []) or []:
            handlers_map = getattr(obs, "_handlers", None)
            if not handlers_map:
                continue
            seen = set()
            for _watch, handler_set in list(handlers_map.items()):
                for h in handler_set or ():
                    if id(h) in seen:
                        continue
                    seen.add(id(h))
                    before = set(vars(h).keys())
                    _wrap_handler(h)
                    wrapped += len(set(vars(h).keys()) - before)
        if wrapped:
            print(f"[StarAstra] FsSensor noise filter active on {wrapped} handler(s)",
                  flush=True)

    fs_cls.start = start_with_filter  # type: ignore[method-assign]


def _setup_engine_logging() -> str:
    """Root-logger движка -> data/engine.log (mode="a").

    Ставится ДО импорта sentinel.service, чтобы все logging.*-вызовы модулей
    движка (мы не видим их исходников) попадали в наш файл. Если движок сам
    вызовет logging.basicConfig(), тот будет no-op: у root уже есть handler.
    Дубли не страшны: stdout/stderr процесса дополнительно перенаправляется
    в тот же файл лаунчером (starastra_launch.py).
    """
    os.makedirs(os.path.dirname(ENGINE_LOG_FILE), exist_ok=True)
    handler = logging.FileHandler(ENGINE_LOG_FILE, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    return ENGINE_LOG_FILE


def main() -> int:
    try:
        log_path = _setup_engine_logging()
        print(f"[StarAstra] engine log: {log_path}", flush=True)
    except OSError as exc:
        # лог недоступен — движок всё равно должен работать
        print(f"[StarAstra] engine.log setup failed: {exc}", file=sys.stderr,
              flush=True)
    # Anti-Freeze Guard (Задача 4.1) — ДО импорта sentinel.service:
    # блокировка suspend/terminate whitelisted-процессов + guardian-поток
    # (страховка от прямых win32-вызовов движка).
    import starastra_guard
    starastra_guard.set_log_path(ENGINE_LOG_FILE)
    starastra_guard.install_guard()
    starastra_guard.start_guardian_thread(interval=1.0)
    _patch_fs_sensor()
    import sentinel.service as svc

    # тот же контракт, что и `python -m sentinel.service --standalone`
    sys.argv = [os.path.abspath(sys.argv[0]), "--standalone"]
    return svc.main()


if __name__ == "__main__":
    sys.exit(main())
