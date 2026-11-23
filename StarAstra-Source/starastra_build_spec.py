#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генератор spec-файла PyInstaller для StarAstra (Задача 3.3).

Зачем spec, а не флаги CLI: в одной сборке нужно ДВА exe на ОДНОМ Analysis
(общий пул модулей/бинарников -> общая папка _internal, без дублирования):
  StarAstra.exe        console=False — лаунчер и UI (окно, без консоли);
  StarAstraWorker.exe  console=True  — engine/watchdog/scan/snippet, эти роли
                       печатают в stdout (JSON-протокол скана, сниппеты
                       карантина); у windowed-exe stdout перехватывается
                       загрузчиком и каналам PIPE достаётся пустота.

Оба exe — одна точка входа starastra_exe_entry.py, роль выбирается флагом
--star-worker=<роль> (см. докстринг entry).

Использование:
    python starastra_build_spec.py            # пишет starastra_build.spec
    python -m PyInstaller starastra_build.spec --noconfirm --clean

Итог: dist/StarAstra/ (onedir) — дальше build_exe.bat докладывает
sentinel_pkg (только .pyc, без dev-данных) и штатный рантайм движка
Sentinel/ (numpy/sklearn/lightgbm/yara + модели + sentinel_*.exe).
"""

from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))

SPEC = """# -*- mode: python ; coding: utf-8 -*-
# АВТОГЕНЕРАЦИЯ: starastra_build_spec.py (Задача 3.3). Не редактировать руками.

a = Analysis(
    [r'{entry}'],
    pathex=[r'{here}', r'{sentinel_ui}', r'{sentinel_pkg}'],
    binaries=[],
    datas=[
        (r'{locales}', 'starastra_i18n\\\\locales'),
        (r'{assets}', 'assets'),
        (r'{behavior_rules}', 'starastra_behavior'),
    ],
    hiddenimports=[
        'starastra_launch',
        'starastra_audit',
        'starastra_guard',       # Anti-Freeze Guard (4.1): bootstrap движка
        'starastra_behavior',    # Behavioral Analysis (Спринт 5): сенсор+правила
        'starastra_scan',
        'starastra_watchdog',
        'starastra_engine_bootstrap',
        'starastra_dashboard',
        'starastra_i18n',
        'starastra_notify',
        'starastra_ui.tray',
        'pystray',
        'psutil',
        # Сторонние зависимости движка (sentinel исключён из analysis, но
        # импортирует их с диска в рантайме; в бандле их быть обязано):
        'watchdog',              # sentinel.sensors.fs_sensor (fs-наблюдатель)
        'watchdog.observers',
        'yaml',                  # sentinel.config (settings.yaml)
        'pywintypes',            # sentinel.platform.ipc: NamedPipeServer
        'win32api',
        'win32file',
        'win32pipe',
        'win32con',              # sentinel.sensors.eventlog_sensor
        'win32evtlog',
        'win32event',
        'win32security',         # sentinel.ipc: DACL пайпа (_HAS_WIN32 guard)
        'pefile',                # sentinel.engine.static_classifier (consumer)
        # ML-стек: sentinel исключён из analysis, поэтому PyInstaller сам
        # по его импортам НЕ проходит — объявляем явно (в чистом окружении
        # sklearn из Sentinel/_internal — namespace-заглушка, venv отсутствует)
        'numpy',
        'scipy',
        'sklearn',
        'sklearn.ensemble',
        'lightgbm',
        'pyarrow',
        'requests',              # sentinel: обновления/Enrich (consumer import)
    ],
    # КРИТИЧНО (Задача 3.3): из бандла исключается ТОЛЬКО код движка
    # (sentinel) — FrozenImporter в meta_path перебивает sys.path, и код
    # движка из бандла резолвил бы свой data-dir внутри _internal, мимо
    # sentinel_pkg/sentinel/data, куда смотрят дашборд/сторож (candidates[0]).
    # ML-стек (numpy/sklearn/...) НАОБОРОТ обязан быть в бандле (в
    # hiddenimports ниже): в чистом окружении sklearn из каталога
    # Sentinel/_internal — namespace-заглушка без содержимого (реальный
    # sklearn в venv, которого у пользователя нет).
    excludes=[
        'sentinel',
        'setuptools',
    ],
    hookspath=[],
    runtime_hooks=[],
)

pyz = PYZ(a.pure)

exe_ui = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name='StarAstra',
    debug=False,
    console=False,                 # окно без консоли: лаунчер + UI
    icon=r'{icon}',
)

exe_worker = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name='StarAstraWorker',
    debug=False,
    console=True,                  # stdout обязателен: JSON-протокол, сниппеты
    icon=r'{icon}',
)

coll = COLLECT(
    exe_ui,
    exe_worker,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='StarAstra',
)
"""


def main() -> int:
    spec_path = os.path.join(HERE, "starastra_build.spec")
    with open(spec_path, "w", encoding="utf-8") as f:
        f.write(SPEC.format(
            entry=os.path.join(HERE, "starastra_exe_entry.py"),
            here=HERE,
            sentinel_ui=os.path.join(HERE, "Sentinel", "ui"),
            sentinel_pkg=os.path.join(HERE, "sentinel_pkg"),
            locales=os.path.join(HERE, "starastra_i18n", "locales"),
            assets=os.path.join(HERE, "assets"),
            behavior_rules=os.path.join(HERE, "starastra_behavior", "rules.json"),
            icon=os.path.join(HERE, "assets", "starastra.ico")))
    print("spec written:", spec_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
