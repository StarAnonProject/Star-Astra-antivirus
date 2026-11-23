' StarAstra.vbs - единый запуск StarAstra одним кликом (движок + UI).
' Вызывает starastra_launch.py, который сам решает: движок уже запущен или
' его надо поднять, UI уже открыт (тогда фокус) или запустить новый.
' Окно консоли не показывается (WindowStyle = 0).

Option Explicit

Dim fso, shell, projectDir, python, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

' Каталог проекта = каталог этого .vbs
projectDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = projectDir

' 1) venv-питон, 2) системный python.exe из PATH
python = projectDir & "\.venv\Scripts\python.exe"
If Not fso.FileExists(python) Then python = "python.exe"

cmd = """" & python & """ """ & projectDir & "\starastra_launch.py"""
shell.Run cmd, 0, False
