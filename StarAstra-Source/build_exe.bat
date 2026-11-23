@echo off
rem ============================================================================
rem  StarAstra build (Задача 3.3): PyInstaller-сборка UI-слоя в dist\StarAstra
rem
rem  Результат — ПОРТАТИВНАЯ папка, работающая БЕЗ установленного Python:
rem    dist\StarAstra\
rem      StarAstra.exe        windowed: лаунчер + UI (точка входа
rem                           starastra_exe_entry.py, роль по --star-worker=)
rem      StarAstraWorker.exe  console: engine/watchdog/scan/snippet — эти роли
rem                           печатают в stdout (JSON скана, сниппеты карантина)
rem      _internal\           общий рантайм PyInstaller (ML-стек НЕ дублируется)
rem      starastra_i18n\      локали ru/en (и в _internal для модуля i18n)
rem      assets\              иконки/превью
rem      sentinel_pkg\        .pyc движка (data\ создаётся при первом старте;
rem                           dev-данные — events.db и пр. — НЕ копируются)
rem      Sentinel\            штатный рантайм движка: sentinel_*.exe и
rem                           _internal\ (numpy/sklearn/lightgbm/yara + модели)
rem
rem  Использование: build_exe.bat          — собрать
rem                 build_exe.bat test     — собрать + смоук-тест чистой копии
rem  Требования: .venv (Python 3.12). PyInstaller ставится автоматически.
rem  Артефакты dist\, build\ и *.spec не коммитятся (.gitignore).
rem ============================================================================

setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [build] ERROR: .venv\Scripts\python.exe not found. Create venv first.
    exit /b 1
)

echo [build] ensuring PyInstaller...
%PY% -m pip install --disable-pip-version-check --quiet pyinstaller
if errorlevel 1 (
    echo [build] ERROR: pip install pyinstaller failed.
    exit /b 1
)

echo [build] generating spec...
%PY% starastra_build_spec.py
if errorlevel 1 (
    echo [build] ERROR: spec generation failed.
    exit /b 1
)

echo [build] PyInstaller: two exes on one Analysis (see starastra_build.spec)...
%PY% -m PyInstaller starastra_build.spec --noconfirm --clean
if errorlevel 1 (
    echo [build] ERROR: PyInstaller failed.
    exit /b 1
)

rem --- данные рядом с exe ----------------------------------------------------
echo [build] staging sentinel_pkg (code only) and Sentinel runtime...

rem sentinel_pkg: только код (.pyc); dev-данные движка (events.db, карантин,
rem pid) в дистрибутив не идут — движок создаст data\ при первом старте
if exist dist\StarAstra\sentinel_pkg rmdir /s /q dist\StarAstra\sentinel_pkg
robocopy sentinel_pkg dist\StarAstra\sentinel_pkg /E /XD data __pycache__ /NFL /NDL /NJH /NJS >nul

rem Штатный рантайм движка (exe + ML-зависимости + модели) — целиком, КРОМЕ
rem его собственного Tcl/Tk (Спринт 5): Sentinel/_internal — самостоятельный
rem PyInstaller-бандл со СТАРЫМ Tcl 8.6.15, а бандл StarAstra собран с
rem Tcl 8.6.12. Без исключения _tkinter.pyd/Tcl из Sentinel/_internal
rem перехватывает импорт tkinter у UI: "version conflict Tcl 8.6.15 != 8.6.12".
rem Движку tkinter не нужен (tkinter только в UI-слое), поэтому копии не нужны.
if exist dist\StarAstra\Sentinel rmdir /s /q dist\StarAstra\Sentinel
robocopy Sentinel dist\StarAstra\Sentinel /E /XD _tcl_data _tk_data tcl8 /XF tcl86t.dll tk86t.dll _tkinter.pyd /NFL /NDL /NJH /NJS >nul

echo.
echo [build] OK: dist\StarAstra  (portable, no Python required)
echo [build] smoke: dist\StarAstra\StarAstra.exe
endlocal
