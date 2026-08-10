; Feichuan Download Converter 1.0 local test candidate.
; AppId is intentionally identical to the former Feichuan downloader.

#ifndef SourceStage
  #error SourceStage must be supplied by scripts\build_candidate.ps1
#endif

#ifndef InstallerOutputDir
  #error InstallerOutputDir must be supplied by scripts\build_candidate.ps1
#endif

#define MyAppName "飞船下载转换工具"
#define MyAppVersion "1.0"
#define MyAppExeName "飞船下载转换工具.exe"
#define OldAppName "飞船下载工具"
#define OldAppExeName "飞船下载工具.exe"

[Setup]
AppId={{1E97F30E-8634-4032-A5A2-715394D9A471}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableWelcomePage=no
DisableDirPage=auto
AlwaysShowDirOnReadyPage=yes
DisableProgramGroupPage=yes
AllowNoIcons=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern dynamic
WizardResizable=yes
Compression=lzma2
SolidCompression=yes
OutputDir={#InstallerOutputDir}
OutputBaseFilename={#MyAppName}-Setup-{#MyAppVersion}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}
UsePreviousAppDir=yes
UsePreviousGroup=no
UsePreviousTasks=yes
UsePreviousLanguage=yes
CloseApplications=yes
CloseApplicationsFilter={#MyAppExeName},{#OldAppExeName},feichuan-worker.exe,yt-dlp.exe,ffmpeg.exe,ffprobe.exe
RestartApplications=no
SetupLogging=yes
VersionInfoVersion=1.0.0.0
VersionInfoProductVersion=1.0
VersionInfoProductName={#MyAppName}
VersionInfoDescription={#MyAppName} Setup

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[InstallDelete]
; Retire the former single-file downloader and its program-owned payloads.
; User data, cloud credentials, browser profile, pending jobs and media are not under {app}.
Type: files; Name: "{app}\{#OldAppExeName}"
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\assets"
Type: filesandordirs; Name: "{app}\licenses"
Type: filesandordirs; Name: "{app}\logs"
Type: files; Name: "{app}\THIRD_PARTY_NOTICES.txt"
Type: files; Name: "{app}\版本与校验.txt"
Type: files; Name: "{app}\发布限制.txt"
Type: files; Name: "{autodesktop}\{#OldAppName}.lnk"
Type: filesandordirs; Name: "{userprograms}\{#OldAppName}"

[Files]
Source: "{#SourceStage}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove only application-owned runtime artifacts. Both legacy LocalAppData roots and media remain.
Type: filesandordirs; Name: "{app}\logs"
Type: files; Name: "{app}\tools\.yt-dlp-*"
Type: files; Name: "{app}\tools\yt-dlp.exe.previous"
Type: files; Name: "{app}\tools\yt-dlp.exe.bad"
