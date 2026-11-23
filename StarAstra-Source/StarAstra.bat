@echo off
rem StarAstra.bat - unified launcher (engine + UI) with console for debugging.
rem Calls starastra_launch.py: starts/verifies the engine, then the UI.
rem For the windowless variant use StarAstra.vbs (same launcher, hidden).

cd /d "%~dp0"

echo [StarAstra] Starting via starastra_launch.py ...

if exist ".venv\Scripts\python.exe" (
    set "STARASTRA_PY=.venv\Scripts\python.exe"
) else (
    set "STARASTRA_PY=python"
)

%STARASTRA_PY% "starastra_launch.py"
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [StarAstra] Launcher exited with code %RC%. See data\launcher.log
    echo             and check that Python 3.10+, tkinter and the sentinel
    echo             engine are available.
) else (
    echo [StarAstra] Launcher finished. UI window should be open.
)
pause
