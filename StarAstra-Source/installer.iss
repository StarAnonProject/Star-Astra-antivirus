; ============================================================================
;  StarAstra installer (Задача 3.4) — Inno Setup 6
;
;  Собирает установщик из dist\StarAstra\ (результат build_exe.bat):
;      iscc installer.iss
;  Готовый файл: dist\StarAstra-Setup-<версия>.exe
;
;  ВАЖНО (наблюдение сборки v1.0.0): если StarAstra работает на этой машине
;  (движок мониторит Desktop), iscc в конце может упасть с
;  «Resource update error: EndUpdateResource failed (110)» — антивирус держит
;  свежесобранный Setup.exe. Обход — компилировать вне отслеживаемых каталогов:
;      iscc installer.iss /O"%TEMP%\setup_out"
;  и перенести готовый файл в dist\ (в v1.0.0 собрано именно так).
;
;  ПОЧЕМУ PER-USER ({localappdata}\Programs), А НЕ PROGRAM FILES:
;  движок Sentinel хранит свои данные (events.db, quarantine.db, pid, карантин)
;  рядом со своим пакетом (sentinel_pkg\sentinel\data) — переопределяющая
;  переменная окружения в движке НЕ НАЙДЕНА (проверено интроспекцией байткода).
;  В Program Files запись запрещена UAC -> защита не сможет писать события.
;  Per-user каталог полностью записываем и не требует прав администратора.
;
;  Автозапуск регистрируется самим приложением (HKCU Run, галочка в
;  Настройках) — инсталлятор лишь создаёт ярлыки и возможность старта.
; ============================================================================

#define MyAppName "StarAstra"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "StarAnonProject"
#define MyAppExeName "StarAstra.exe"

[Setup]
AppId={{7C3A2E91-6B44-4F7A-9C2D-STARASTRA0001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; per-user: без UAC, каталог записываем (движок пишет данные рядом с пакетом)
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=StarAstra-Setup-{#MyAppVersion}
SetupIconFile=assets\starastra.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; не выгружаем explorer: движок ставит свои каталоги в исключения сам
UninstallFilesDir={app}\uninst

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"

[Files]
; всё содержимое портативной папки из build_exe.bat:
;   StarAstra.exe + StarAstraWorker.exe + _internal (внутри — бандл:
;   starastra_i18n\locales, assets), sentinel_pkg (внутри — код движка
;   и sentinel\config\rules), штатный рантайм Sentinel/.
Source: "dist\StarAstra\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
; рантайм-каталог наших логов (launcher.log/engine.log/watchdog.log/audit.log):
; создаётся и приложением, но пусть существует сразу после установки
Name: "{app}\data"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    Comment: "{#MyAppName} — Sentinel Antivirus dashboard"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    Tasks: desktopicon

[Run]
; предложение запустить сразу после установки (как обычное приложение)
Filename: "{app}\{#MyAppExeName}"; Description: \
    "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; НЕ удаляем пользовательские данные движка за пределами каталога установки
; (карантин и история — на совести пользователя); удаляем только наше.
Type: filesandordirs; Name: "{app}\sentinel_pkg\sentinel\data"
Type: filesandordirs; Name: "{app}\data"
