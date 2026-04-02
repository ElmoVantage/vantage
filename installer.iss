; Vantage Tracker — Inno Setup installer script
; Produces a single VantageSetup.exe that users download and run.
;
; Build this AFTER PyInstaller has populated dist\Vantage\
; Command: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss

#define MyAppName    "Vantage Tracker"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppURL     "https://github.com/ElmoVantage/vantage"
#define MyAppExe     "Vantage.exe"

[Setup]
AppName                 = {#MyAppName}
AppVersion              = {#MyAppVersion}
AppPublisher            = ElmoVantage
AppPublisherURL         = {#MyAppURL}
AppSupportURL           = {#MyAppURL}/issues
AppUpdatesURL           = {#MyAppURL}/releases/latest

; Install to user's AppData — no admin rights required
DefaultDirName          = {localappdata}\Vantage
DefaultGroupName        = Vantage Tracker
DisableProgramGroupPage = yes
PrivilegesRequired      = lowest

; Output
OutputDir               = dist
OutputBaseFilename      = VantageSetup
SetupIconFile           = icon.ico

; Compression
Compression             = lzma2/ultra64
SolidCompression        = yes
LZMAUseSeparateProcess  = yes

; Appearance
WizardStyle             = modern
WizardSmallImageFile    = icon.ico
DisableWelcomePage      = no

; Version info shown in Add/Remove Programs
VersionInfoVersion      = {#MyAppVersion}
VersionInfoCompany      = ElmoVantage
VersionInfoDescription  = Vantage Tracker Installer
UninstallDisplayName    = Vantage Tracker
UninstallDisplayIcon    = {app}\Vantage.exe

; Don't let Windows restart during install
RestartIfNeededByRun    = no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; \
  Description: "Create a &desktop shortcut"; \
  GroupDescription: "Additional icons:"

[Files]
; ── App files (always overwrite on update) ────────────────────────────────────
; Exclude user-data files so updates never wipe them
Source: "dist\Vantage\*"; \
  DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs; \
  Excludes: "settings.ini,.env,tracker.db,sync_state.json,license_cache.json,backups\*,exports\*"

; ── First-run defaults (only written if the file doesn't exist yet) ───────────
; settings.ini — shipped with clean defaults; user edits it after install
Source: "settings_default.ini"; \
  DestDir: "{app}"; \
  DestName: "settings.ini"; \
  Flags: onlyifdoesntexist

[Dirs]
; Pre-create user-data folders so the app finds them on first run
Name: "{app}\backups"
Name: "{app}\exports"

[Icons]
; Desktop shortcut (optional — user can uncheck during install)
Name: "{userdesktop}\Vantage Tracker"; \
  Filename: "{app}\{#MyAppExe}"; \
  IconFilename: "{app}\icon.ico"; \
  Tasks: desktopicon

; Start Menu
Name: "{group}\Vantage Tracker"; \
  Filename: "{app}\{#MyAppExe}"; \
  IconFilename: "{app}\icon.ico"

Name: "{group}\Uninstall Vantage Tracker"; \
  Filename: "{uninstallexe}"

[Run]
; Offer to launch the app at the end of setup
Filename: "{app}\{#MyAppExe}"; \
  Description: "Launch Vantage Tracker now"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
; Nothing special needed — standard uninstall removes all app files

[Code]
// On update: warn the user their data (.env, tracker.db) is preserved
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then begin
    if FileExists(ExpandConstant('{app}\tracker.db')) then begin
      MsgBox(
        'Updating Vantage Tracker.' + #13#10 + #13#10 +
        'Your database, .env file, and settings are preserved.' + #13#10 +
        'Only the application files will be replaced.',
        mbInformation, MB_OK
      );
    end;
  end;
end;
