; Inno Setup 6 script. Run build.bat first, or invoke ISCC.exe installer.iss.
[Setup]
AppId={{CCF12E8E-EDC6-4C3D-9D1B-4C649220AEB2}
AppName=TR-EURO-EDITOR
AppVersion=2.6
AppPublisher=TR-EURO-EDITOR
DefaultDirName={localappdata}\Programs\ETS2 Save Editor
DefaultGroupName=TR-EURO-EDITOR
UninstallDisplayName=TR-EURO-EDITOR
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
Name: "{group}\TR-EURO-EDITOR"; Filename: "{app}\ETS2SaveEditor.exe"; IconFilename: "{app}\ETS2SaveEditor.exe"
Name: "{autodesktop}\TR-EURO-EDITOR"; Filename: "{app}\ETS2SaveEditor.exe"; Tasks: desktopicon; IconFilename: "{app}\ETS2SaveEditor.exe"

[Run]
Filename: "{app}\ETS2SaveEditor.exe"; Description: "Launch TR-EURO-EDITOR"; Flags: nowait postinstall skipifsilent

[Code]
function WebView2RuntimeAvailable(): Boolean;
var
  Version: String;
  RuntimeKey: String;
begin
  RuntimeKey := 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := False;
  if RegQueryStringValue(HKLM32, RuntimeKey, 'pv', Version) then
    Result := (Version <> '') and (Version <> '0.0.0.0');
  if (not Result) and RegQueryStringValue(HKCU, RuntimeKey, 'pv', Version) then
    Result := (Version <> '') and (Version <> '0.0.0.0');
  if (not Result) and IsWin64 then
  begin
    if RegQueryStringValue(HKCU64, RuntimeKey, 'pv', Version) then
      Result := (Version <> '') and (Version <> '0.0.0.0');
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and (not WebView2RuntimeAvailable()) then
    MsgBox('Microsoft Edge WebView2 Runtime was not detected. TR-EURO-EDITOR will open its classic interface until WebView2 is installed. You can install the runtime from: https://developer.microsoft.com/microsoft-edge/webview2/', mbInformation, MB_OK);
end;
