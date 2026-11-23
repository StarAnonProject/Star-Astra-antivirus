# StarAstra v1.0

UI-оболочка и защитный слой для антивирусного движка **Sentinel Antivirus**:
современный дашборд, трей, поведенческий анализ с заморозкой процессов,
сторож движка, Anti-Freeze Guard, русский и английский интерфейс.

> **Важно:** бинарники движка Sentinel (`sentinel_pkg/`, `Sentinel/_internal/`)
> в репозиторий не входят — их нужно скачать отдельно.
> Инструкция: [README_USER.md → Установка из исходников](README_USER.md#требования-для-разработчиков--установка-из-исходников).

## Документация

- [README_USER.md](README_USER.md) — руководство пользователя и установка из исходников
- [LAUNCHER.md](LAUNCHER.md) — запуск, роли процессов, сборка
- [FINAL_RELEASE_CHECKLIST.md](FINAL_RELEASE_CHECKLIST.md) — чек-лист приёмки v1.0

## Быстрый старт (из исходников)

```bat
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
:: положите папку sentinel_pkg\ из дистрибутива Sentinel в корень проекта
StarAstra.bat
```

Сборка portable-версии: `build_exe.bat`. Установщик: `ISCC.exe installer.iss`
(Inno Setup 6).

## Лицензия

MIT — см. [LICENSE](LICENSE). © 2026 StarAnonProject.
