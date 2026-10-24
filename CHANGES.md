# CHANGES — TR-EURO-EDITOR UI redesign

## Design and application shell
- Added an offline HTML/CSS/JS interface inspired by the provided board: navy/charcoal surfaces, cyan accent, rounded panels, left navigation, top bar, status footer, card grids and SVG tuning gauges.
- Added bilingual page navigation and RTL document layout. Python's theme palette remains the color source for all eight palettes and both display modes.
- Added live theme previews, custom accent, day/night switching, and persistence through the existing editor settings folder.

## Pages and bridge
- Added Dashboard, Tuning, Save Profiles, Garage, Game Settings, Mod Manager, Extras and Appearance pages.
- Connected UI actions to a narrow ets2_web_bridge.py API; the JavaScript side does not receive generic filesystem or process access.
- Garage and Dashboard display data from the existing fleet/save extractors; unavailable values remain unavailable. The tuning gauges report calculations from user-entered targets, not live game telemetry.
- Kept the Tkinter UI and added --classic plus automatic fallback if WebView2 cannot start.

## Packaging and runtime
- Added pywebview dependency and PyInstaller collection for WebView2 UI assets, Crypto and tuning templates.
- Added a per-user WebView2 runtime notice in installer.iss; the application also shows a fallback notice when WebView2 startup fails.
- Updated build workflow checks, build.bat, and README build instructions. App title/product name remain TR-EURO-EDITOR; version remains in executable metadata.

## Core preservation
- AST comparison against the supplied v2.6 ZIP found 18 protected save/backup/fleet functions and classes identical.
- Rolling backup policy, save mutation logic and automatic backup before writes were not changed.

## Test results
- node --check ui/app.js: passed.
- python -m py_compile ets2_save_editor.py ets2_web_bridge.py tests\test_web_ui.py: passed.
- python -m unittest discover -s tests -v: 56 passed; no failures.
- Existing GUI tests emitted one Tk teardown diagnostic but completed with exit code 0.
- The web UI was not executed in pywebview. The Windows EXE/installer and GitHub Actions build were not run in this environment. Edge headless screenshot capture crashed (-1073741819), so no screenshot is claimed.

## Remaining verification
- Run GitHub Actions or a Windows build machine with PyInstaller and Inno Setup.
- Verify the WebView2 UI and installer on a real Windows user account, including Persian text shaping.
- Capture and inspect the requested screenshot matrix and test with a real ETS2 1.60.1.7 save. In-game generated-mod compatibility remains unverified.
