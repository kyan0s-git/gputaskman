; Inno Setup script for the GPU Task Manager Windows installer.
; Built by tools/build_portable.py --installer, which passes the defines below with /D:
;   AppVersion, Arch (x64 or arm64), SourceDir (the frozen bundle), OutputDir, RepoRoot
; The installer only copies the frozen bundle. Like the portable ZIP, it never contains GPU or
; Windows driver libraries: the build refuses to package them before this script runs.

#ifndef AppVersion
  #error AppVersion must be defined with /DAppVersion=...
#endif
#ifndef Arch
  #error Arch must be defined with /DArch=x64 or /DArch=arm64
#endif
#ifndef SourceDir
  #error SourceDir must be defined with /DSourceDir=...
#endif
#ifndef OutputDir
  #define OutputDir "dist"
#endif

#define AppName "GPU Task Manager"
#define AppExe "gputaskman.exe"

[Setup]
; Fixed GUID so upgrades replace the previous install instead of adding a second entry.
AppId={{6F0C7B1E-3D52-4B0E-9A41-5C2E8D7A9B13}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=gputaskman contributors
AppPublisherURL=https://github.com/shanselman/gputaskman
AppSupportURL=https://github.com/shanselman/gputaskman/issues
AppUpdatesURL=https://github.com/shanselman/gputaskman/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Per-user by default (no UAC prompt); the dialog offers an all-users install.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
LicenseFile={#SourceDir}\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename=gputaskman-{#AppVersion}-windows-{#Arch}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
ChangesEnvironment=yes
#if Arch == "arm64"
ArchitecturesAllowed=arm64
ArchitecturesInstallIn64BitMode=arm64
#else
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "addtopath"; Description: "Add gputaskman (and gtm) to PATH so you can run it from any terminal"
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; `gtm` is the short command the Python package also installs.
Source: "{#SourceDir}\{#AppExe}"; DestDir: "{app}"; DestName: "gtm.exe"; Flags: ignoreversion; Tasks: addtopath

[Icons]
; The program is a console app: these shortcuts open it in the user's default terminal
; (Windows Terminal on Windows 11), so the full-screen TUI looks exactly as it does from a shell.
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{userdocs}"; Comment: "Live GPU memory, per process and per model"
Name: "{group}\{#AppName} (one-shot report)"; Filename: "{cmd}"; Parameters: "/k ""{app}\{#AppExe}"" --once"; WorkingDir: "{userdocs}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{userdocs}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent unchecked

[Code]
const
  UserEnvKey = 'Environment';
  SystemEnvKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';

function EnvRoot(): Integer;
begin
  if IsAdminInstallMode then Result := HKLM else Result := HKCU;
end;

function EnvKey(): String;
begin
  if IsAdminInstallMode then Result := SystemEnvKey else Result := UserEnvKey;
end;

function PathContains(const Path, Dir: String): Boolean;
begin
  Result := Pos(';' + Lowercase(Dir) + ';', ';' + Lowercase(Path) + ';') > 0;
end;

procedure AddToPath();
var
  Path: String;
begin
  if not RegQueryStringValue(EnvRoot(), EnvKey(), 'Path', Path) then Path := '';
  if PathContains(Path, ExpandConstant('{app}')) then Exit;
  if (Path <> '') and (Path[Length(Path)] <> ';') then Path := Path + ';';
  RegWriteExpandStringValue(EnvRoot(), EnvKey(), 'Path', Path + ExpandConstant('{app}'));
end;

procedure RemoveFromPath();
var
  Path, Dir: String;
  P: Integer;
begin
  if not RegQueryStringValue(EnvRoot(), EnvKey(), 'Path', Path) then Exit;
  Dir := ExpandConstant('{app}');
  Path := ';' + Path + ';';
  P := Pos(';' + Lowercase(Dir) + ';', Lowercase(Path));
  if P = 0 then Exit;
  Delete(Path, P, Length(Dir) + 1);
  if (Length(Path) > 0) and (Path[1] = ';') then Delete(Path, 1, 1);
  if (Length(Path) > 0) and (Path[Length(Path)] = ';') then Delete(Path, Length(Path), 1);
  RegWriteExpandStringValue(EnvRoot(), EnvKey(), 'Path', Path);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then AddToPath();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then RemoveFromPath();
end;
