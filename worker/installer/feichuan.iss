; Feichuan Downloader Windows installer (Inno Setup 6).
; Keep AppId unchanged in every release so upgrades replace older versions.

#ifndef MyAppName
  #error MyAppName must be supplied by scripts\build_installer.ps1
#endif

#define MyAppExeName MyAppName + ".exe"

#ifndef MyAppVersion
  #error MyAppVersion must be supplied by scripts\build_installer.ps1
#endif

#ifndef MyVersionInfoVersion
  #error MyVersionInfoVersion must be supplied by scripts\build_installer.ps1
#endif

#ifndef SourceDist
  #define SourceDist "..\dist"
#endif

#ifndef InstallerOutputDir
  #define InstallerOutputDir "..\release"
#endif

[Setup]
; Fixed identity for product_key=feichuan_download_tool. Never regenerate it per release.
AppId={{1E97F30E-8634-4032-A5A2-715394D9A471}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
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
UsePreviousGroup=yes
UsePreviousTasks=yes
UsePreviousLanguage=yes
CloseApplications=yes
CloseApplicationsFilter={#MyAppExeName}
RestartApplications=no
SetupLogging=yes
VersionInfoVersion={#MyVersionInfoVersion}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoDescription={#MyAppName} Setup

#ifdef MySignToolName
SignTool={#MySignToolName}
SignedUninstaller=yes
#endif

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[InstallDelete]
; Remove payloads and notices left by releases that included the retired bridge.
Type: files; Name: "{app}\tools\feichuan-wechat-bridge.exe"
Type: files; Name: "{app}\tools\feichuan-wechat-bridge.exe.sha256"
Type: files; Name: "{app}\开发构建-缺少视频号桥接.txt"
Type: files; Name: "{app}\发布限制.txt"

[Files]
Source: "{#SourceDist}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDist}\tools\*"; DestDir: "{app}\tools"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDist}\licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDist}\assets\*"; DestDir: "{app}\assets"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDist}\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#MyManifestPath}"; DestDir: "{app}"; Flags: ignoreversion

#ifdef MyLimitationsPath
Source: "{#MyLimitationsPath}"; DestDir: "{app}"; DestName: "发布限制.txt"; Flags: ignoreversion
#endif

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove only runtime artifacts owned by the application. User settings, the
; dedicated Douyin profile, download history and downloaded media live outside
; {app} and are intentionally retained.
Type: filesandordirs; Name: "{app}\logs"
Type: files; Name: "{app}\tools\.yt-dlp-*"
Type: files; Name: "{app}\tools\yt-dlp.exe.previous"
Type: files; Name: "{app}\tools\yt-dlp.exe.bad"
