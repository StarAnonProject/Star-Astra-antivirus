' StarAstraEngine.vbs - запуск ТОЛЬКО движка защиты (без UI).
' Используется автозапуском при входе в Windows
' (HKCU\Software\Microsoft\Windows\CurrentVersion\Run -> "StarAstra").
' Окно консоли не показывается (WindowStyle = 0).

Option Explicit

Dim fso, shell, projectDir, python, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

projectDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = projectDir

python = projectDir & "\.venv\Scripts\python.exe"
If Not fso.FileExists(python) Then python = "python.exe"

cmd = """" & python & """ """ & projectDir & "\starastra_launch.py"" --engine-only"
shell.Run cmd, 0, False
