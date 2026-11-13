; Inno Setup script for the GPU Task Manager Windows installer.
; Built by tools/build_portable.py --installer, which passes the defines below with /D:
;   AppVersion, Arch (x64 or arm64), SourceDir (the frozen bundle), OutputDir, IconFile,
;   and optionally PawnIOSetup / PawnIOVersion (the pinned, hash-checked PawnIO setup program).
; The app folder never contains GPU or Windows driver libraries or any kernel driver: the build refuses to
; package them. The optional sensor driver (PawnIO) is installed only by PawnIO's own signed setup, which this
; installer runs from its temporary folder when the user keeps the "sensor driver" task selected.

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
#define Launcher "GPU Task Manager.exe"

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
UninstallDisplayIcon={app}\{#Launcher}
#ifdef IconFile
SetupIconFile={#IconFile}
#endif
CloseApplications=force
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
#ifdef PawnIOSetup
Name: "sensordriver"; Description: "Install the PawnIO sensor driver {#PawnIOVersion} for CPU temperatures, fans and package power (asks for administrator approval; shared with other monitoring apps, so uninstalling GPU Task Manager keeps it)"; Check: not PawnIOInstalled
#endif

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; `gtm` is the short command the Python package also installs.
Source: "{#SourceDir}\{#AppExe}"; DestDir: "{app}"; DestName: "gtm.exe"; Flags: ignoreversion; Tasks: addtopath
#ifdef PawnIOSetup
Source: "{#PawnIOSetup}"; DestDir: "{tmp}"; DestName: "PawnIO_setup.exe"; Flags: deleteafterinstall; Tasks: sensordriver
#endif

[Icons]
; The windowless launcher opens the TUI in a console window of its own (title, icon, size, font), never a shell.
Name: "{group}\{#AppName}"; Filename: "{app}\{#Launcher}"; WorkingDir: "{userdocs}"; Comment: "GPU memory, CPU, disks, network and sensors"
Name: "{group}\{#AppName} (administrator)"; Filename: "{app}\{#Launcher}"; Parameters: "--elevate"; WorkingDir: "{userdocs}"; Comment: "Run elevated for CPU temperatures, fans and package power"
Name: "{group}\{#AppName} (one-shot report)"; Filename: "{cmd}"; Parameters: "/k ""{app}\{#AppExe}"" --once --hardware"; WorkingDir: "{userdocs}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#Launcher}"; WorkingDir: "{userdocs}"; Tasks: desktopicon

[Run]
#ifdef PawnIOSetup
Filename: "{tmp}\PawnIO_setup.exe"; Parameters: "-install -silent"; StatusMsg: "Installing the PawnIO sensor driver..."; Flags: shellexec waituntilterminated; Verb: "runas"; Tasks: sensordriver
#endif
; The in-app updater passes /RELAUNCH=1 so the new version opens when the silent update finishes.
Filename: "{app}\{#Launcher}"; Flags: nowait runasoriginaluser; Check: RelaunchRequested
Filename: "{app}\{#Launcher}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent unchecked runasoriginaluser

[Code]
function PawnIOInstalled(): Boolean;
begin
  Result := RegKeyExists(HKLM64, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO') or
            RegKeyExists(HKLM32, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO');
end;

function RelaunchRequested(): Boolean;
begin
  Result := ExpandConstant('{param:relaunch|0}') = '1';
end;

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
