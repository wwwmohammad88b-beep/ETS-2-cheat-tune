@echo off
setlocal
REM Builds the standalone app and, when Inno Setup is installed, the installer.
py -3.12 -m pip install --upgrade pip
if errorlevel 1 goto :fail
py -3.12 -m pip install -r requirements.txt
if errorlevel 1 goto :fail
py -3.12 -m unittest discover -s tests -v
if errorlevel 1 goto :fail
py -3.12 -m PyInstaller --clean --noconfirm --onefile --windowed --name ETS2SaveEditor --icon app.ico --version-file version_info.txt --collect-all Crypto --collect-all webview --hidden-import webview.platforms.edgechromium --hidden-import clr --add-data "ui;ui" --add-data "tuning_templates;tuning_templates" ets2_save_editor.py
if errorlevel 1 goto :fail

echo.
echo Standalone app: dist\ETS2SaveEditor.exe
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" (
  "%ISCC%" installer.iss
  if errorlevel 1 goto :fail
  echo Installer: installer\ETS2SaveEditor_Setup.exe
) else (
  echo Inno Setup 6 was not found. Install it before building the installer.
  exit /b 1
)
if not exist "dist\ETS2SaveEditor.exe" goto :fail
if not exist "installer\ETS2SaveEditor_Setup.exe" goto :fail
pause
exit /b 0
:fail
echo Build failed.
pause
exit /b 1
