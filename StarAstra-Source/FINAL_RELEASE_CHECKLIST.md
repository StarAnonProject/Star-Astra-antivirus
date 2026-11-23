# FINAL_RELEASE_CHECKLIST.md — StarAstra v1.0

Дата: 16.11.2026 · Статус: **ГОТОВО К РАСПРОСТРАНЕНИЮ**
Артефакты: `dist\StarAstra-Setup-1.0.0.exe`, `dist\StarAstra-Portable-1.0.0.zip`,
`dist\StarAstra\` (портативная папка).

## Что проверено

| Проверка | Статус | Подтверждение |
|----------|--------|---------------|
| **EICAR / детекция файлов** | **PASS** | YARA-правило `eicar.yar`: EICAR-проба детектируется и карантинится (`DETECTION_PROOF.md`, спринт 1; smoke чистой копии — спринт 4). Живое подтверждение 16.11: движок в реальном времени карантинил случайную копию eicar.yar прямо посреди сборки установщика |
| **Behavior (Sprint 5)** | **PASS** | 13/13 по ТЗ (`SPRINT5_REPORT.md`): сенсор 2 с, 5 правил, suspend с WHITELIST-guard'ом, behavior_alert (HIGH) в events.db, карточка с [Убить]/[Разрешить], audit/active в Настройках, История; юнит 20/20; live: заморозка 4 с, kill, resume+исключение, browser.exe/explorer.exe не заморожены |
| **Watchdog (Sprint 2)** | **PASS** | авто-рестарт убитого движка ~2 с; probe-проба конвейера (stall 600 с); skip при нечитаемых настройках/паузе/выходе; respawn UI (трей) раз в 30 с; guardian-поток Anti-Freeze Guard (баг `ENGINE_LOG` исправлен в Sprint 5) |
| **Tray / UI (Sprint 2–3)** | **PASS** | иконка в трее (зелёная/янтарная), меню (Открыть/Пауза/Выход), пауза 10–60 мин с авторезюмом, close-to-tray, карточки угроз + системные тосты, анти-спам burst, История/Карантин/Настройки/журнал |
| **Честность UI** | **PASS** | неработающие функции (Memory Scan, Canary) показаны серыми — не выдумываем (`STARASTRA_SPRINT3_REPORT.md`) |
| **Сборка портативной** | **PASS** | `build_exe.bat` rc=0; без Python; один Analysis → StarAstra.exe + StarAstraWorker.exe + `_internal` (бандл: i18n/locales, assets, starastra_behavior/rules.json) + sentinel_pkg (.pyc) + Sentinel (ML-рантайм без Tcl/Tk) |
| **Установщик** | **PASS** | ISCC 6.7.3 rc=0; per-user без UAC; состав и чистая установка/деинсталляция проверены фактически; UI-окно открывается (Tcl-конфликт 8.6.15≠8.6.12 исправлен исключением Tcl/Tk из staging Sentinel) |
| **Frozen smoke** | **PASS** | чистая копия без Python: движок + сторож + behavior-сенсор (5 правил) + UI; контрольная заморозка fake `powershell -enc` в frozen — 2 с |
| **Логи/самошум** | **PASS** | один писатель на файл (engine.log, `b3a179e`); собственные события фильтруются (OWN) |

## Ограничения v1.0 (честно, не скрыты)

- Memory Scan и Canary-ловушки не поддерживаются текущей версией движка —
  выключены в UI с пояснением.
- Обновления (сервер) не настроены — кнопка фиксирует факт проверки.

## Перед публикацией

- [ ] Прогнать `dist\StarAstra-Setup-1.0.0.exe` на чистой Windows 10/11 VM.
- [ ] Проверить, что ZIP распаковывается и `StarAstra\StarAstra.exe` стартует.
- [ ] Загрузить артефакты на канал распространения.
