# installer.ps1 - creates StarAstra shortcuts (Start Menu + Desktop).
# Run:  powershell -ExecutionPolicy Bypass -File installer.ps1

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbs        = Join-Path $projectDir "StarAstra.vbs"
$ico        = Join-Path $projectDir "assets\starastra.ico"

if (-not (Test-Path $vbs)) {
    Write-Error "StarAstra.vbs not found next to installer.ps1 ($vbs)"
    exit 1
}

$wsh = New-Object -ComObject WScript.Shell

# 1) Start Menu: %APPDATA%\Microsoft\Windows\Start Menu\Programs\StarAstra.lnk
$startMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$startLnkPath = Join-Path $startMenuDir "StarAstra.lnk"
$lnk = $wsh.CreateShortcut($startLnkPath)
$lnk.TargetPath       = "$env:SystemRoot\System32\wscript.exe"
$lnk.Arguments        = "`"$vbs`""
$lnk.WorkingDirectory = $projectDir
$lnk.Description       = "StarAstra Antivirus (Sentinel engine + UI)"
if (Test-Path $ico) { $lnk.IconLocation = "$ico,0" }
$lnk.Save()
Write-Host "[OK] Start Menu shortcut: $startLnkPath"

# 2) Desktop: %USERPROFILE%\Desktop\StarAstra.lnk
$desktopDir = [Environment]::GetFolderPath("Desktop")
$deskLnkPath = Join-Path $desktopDir "StarAstra.lnk"
$lnk2 = $wsh.CreateShortcut($deskLnkPath)
$lnk2.TargetPath       = "$env:SystemRoot\System32\wscript.exe"
$lnk2.Arguments        = "`"$vbs`""
$lnk2.WorkingDirectory = $projectDir
$lnk2.Description       = "StarAstra Antivirus (Sentinel engine + UI)"
if (Test-Path $ico) { $lnk2.IconLocation = "$ico,0" }
$lnk2.Save()
Write-Host "[OK] Desktop shortcut:   $deskLnkPath"

Write-Host ""
Write-Host "Done. Launch StarAstra from the Start menu or the Desktop icon."
