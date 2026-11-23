# StarAstra Launcher — запуск Sentinel UI по клику

**StarAstra** — UI-оболочка антивируса Sentinel от **StarAnonProject**.
Лаунчеры лежат в корне проекта и используют **относительные пути**, поэтому папку
проекта можно свободно переносить.

## 0. Требования (важно!)

Движок Sentinel скомпилирован под **Python 3.12**. На Python 3.14+ модули
`sentinel.engine.*` не импортируются — UI запустится, но в режиме чтения.
В корне проекта уже создан **`.venv` на Python 3.12** с установленными
зависимостями (numpy, scikit-learn, lightgbm, watchdog, yara-python, pefile,
requests, psutil, pywin32). Лаунчеры проверяют его первыми.

Если переносите папку на другую машину — пересоздайте venv:

```bat
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install numpy scikit-learn scipy lightgbm watchdog yara-python pefile requests psutil pywin32
```

Пакет движка материализован из дистрибуции в `sentinel_pkg\` (pyc, Python 3.12)
— путь подхватывается автоматически.

## 1. Запуск двойным кликом (без консоли) — единый лаунчер

Оба файла теперь вызывают **`starastra_launch.py`** — «умный» лаунчер, который
сам гарантирует работающий движок и единственный экземпляр UI:

- `StarAstra.vbs` — **движок + UI**, без консоли: поднимает движок
  (если не запущен; stale `sentinel.pid` от мёртвого процесса удаляется),
  ждёт его готовности по штатному IPC (до 10 с), затем открывает UI. Если UI
  уже открыт — просто выводит существующее окно на передний план
  (второй экземпляр не создаётся).
- `StarAstraEngine.vbs` — **только движок**, без UI. Используется
  автозапуском при входе в Windows (см. раздел 2A).
- `StarAstra.bat` — тот же лаунчер **с консолью** (для отладки).

Журнал каждого шага: `data\launcher.log`. Движок запускается через
`starastra_engine_bootstrap.py` — тот же `sentinel.service`, плюс in-memory
фильтр само-шума (события внутри собственных папок проекта не попадают в
очередь; подробности — `EICAR_DIAGNOSIS.md`).

Диагностика из командной строки:

```bat
.venv\Scripts\python starastra_launch.py --status
```

## 2. Установка ярлыков (меню «Пуск» + рабочий стол)

```powershell
powershell -ExecutionPolicy Bypass -File installer.ps1
```

`installer.ps1` создаёт два ярлыка, оба указывают на `StarAstra.vbs`
(иконка `assets\starastra.ico`, если файл есть):

- `%APPDATA%\Microsoft\Windows\Start Menu\Programs\StarAstra.lnk`
- `%USERPROFILE%\Desktop\StarAstra.lnk`

Повторный запуск скрипта просто пересоздаёт ярлыки — это безопасно.

## 2A. Автозапуск при входе в Windows

Управляется галочкой в **Настройки → Автозапуск** (без UAC, запись в
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` → значение `StarAstra`):

- «Запускать StarAstra при входе в Windows» — при входе выполняется
  `StarAstraEngine.vbs` → `starastra_launch.py --engine-only`: поднимается
  **только движок** защиты, окно UI не открывается.
- «Открывать окно при входе» (по умолчанию выкл.) — если включить, при входе
  дополнительно откроется окно StarAstra (значение Run переключается на
  `StarAstra.vbs`).

Командный дубликат (для проверки/скриптов):

```bat
.venv\Scripts\python starastra_launch.py --autostart status
.venv\Scripts\python starastra_launch.py --autostart on
.venv\Scripts\python starastra_launch.py --autostart off
```

## 3. Кнопка «Остановить защиту» (supervisor)

UI управляет **жизненным циклом процесса движка**, а не выдуманным API:

- «Остановить защиту на 15 мин» = завершить процесс движка
  (`sentinel_service.exe` или `python -m sentinel.service`; если движок
  установлен как служба — `sc stop SentinelCoreSvc`, требуются права
  администратора);
- «Включить сейчас» / авто-таймер = запустить процесс заново
  (`.venv\Scripts\python -m sentinel.service` → `sentinel_service.exe` →
  `sc start SentinelCoreSvc`);
- диалог честно предупреждает: *«Движок будет ОСТАНОВЛЕН, новые угрозы в это
  время НЕ обнаруживаются»*.

Длительность — константа `PAUSE_DURATION_MIN` в начале
`Sentinel/ui/starastra_dashboard.py` (по умолчанию 15; для теста поставьте 1).

## 4. Сборка релизного .exe

`build_dist.py` в этом воркспейсе **не найден** — команда приведена как целевая
для репозитория с исходниками. Для сборки именно StarAstra UI:

```powershell
pyinstaller --onefile --windowed --name starastra ^
  --paths sentinel_pkg --paths Sentinel\_internal Sentinel\ui\starastra_dashboard.py
```

## 5. Язык / Language

UI поддерживает **русский** и **английский**. Переключение: вкладка
**⚙ Настройки → Язык / Language** → кнопки **RU** / **EN** — применяется
мгновенно, без перезапуска. Выбор сохраняется в `starastra_settings.json`
(в корне проекта). По умолчанию берётся системная локаль (`ru*` → русский,
иначе английский); при отсутствии ключа в словаре UI покажет ключ и записать
warning в лог — так сразу видно пробелы в переводе.

## Статус

| Компонент | Состояние |
|---|---|
| `.venv` (Python 3.12 + зависимости) | создан, движок импортируется и детектирует (честный EICAR: штатное правило `EICAR_Test_File`, score 85.0 > порога 80 → автономный карантин; см. `DETECTION_PROOF.md`) |
| `sentinel_pkg\` | материализованный из дистрибуции пакет движка (pyc, py3.12), 50 модулей; в UI **не импортируется** (история/карантин — sqlite read-only) |
| `Sentinel/ui/starastra_dashboard.py` | создан; оригинальный `dashboard.py` в воркспейсе отсутствует и не изменялся |
| `StarAstra.vbs` / `StarAstra.bat` | созданы, пути относительные, venv подхватывается автоматически |
| Кнопка паузы | supervisor-паттерн: режим A (процесс, psutil, без UAC) / режим B (служба `SentinelCoreSvc`, `sc stop/start` — **требует прав администратора**, UI показывает предупреждение) |
| Локализация | `starastra_i18n` (RU/EN), ~80 ключей, переключение на лету, сохранение выбора |
| Уведомления | `starastra_notify`: карточки в углу (В карантин / Отмена / Подробнее, стек ≤3, slide+fade 200 мс) + Windows toasts (`windows-toasts`, HIGH/CRITICAL) |
| Схема события | поле `severity` в schema-Event **отсутствует** (проверено) — severity живёт в `extra`; оригинальные потребители не затронуты |
| `build_dist.py` | **НЕ НАЙДЕНО** в воркспейсе |
