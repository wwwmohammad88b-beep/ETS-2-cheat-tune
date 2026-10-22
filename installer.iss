; Inno Setup 6 script. Run build.bat first, or invoke ISCC.exe installer.iss.
[Setup]
AppId={{CCF12E8E-EDC6-4C3D-9D1B-4C649220AEB2}
AppName=ETS2 Save Editor
AppVersion=2.1
AppPublisher=ETS2 Save Editor
DefaultDirName={localappdata}\Programs\ETS2 Save Editor
DefaultGroupName=ETS2 Save Editor
UninstallDisplayName=ETS2 Save Editor
UninstallDisplayIcon={app}\ETS2SaveEditor.exe
PrivilegesRequired=lowest
OutputDir=installer
OutputBaseFilename=ETS2SaveEditor_Setup
SetupIconFile=app.ico
Compression=lzma
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "dist\ETS2SaveEditor.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "app.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\ETS2 Save Editor"; Filename: "{app}\ETS2SaveEditor.exe"; IconFilename: "{app}\ETS2SaveEditor.exe"
Name: "{autodesktop}\ETS2 Save Editor"; Filename: "{app}\ETS2SaveEditor.exe"; Tasks: desktopicon; IconFilename: "{app}\ETS2SaveEditor.exe"

[Run]
Filename: "{app}\ETS2SaveEditor.exe"; Description: "Launch ETS2 Save Editor"; Flags: nowait postinstall skipifsilent
