# CHANGES — TR-EURO-EDITOR UI redesign

## Design and application shell
- Added an offline HTML/CSS/JS interface inspired by the provided board: navy/charcoal surfaces, cyan accent, rounded panels, left navigation, top bar, status footer, card grids and SVG tuning gauges.
- Added bilingual page navigation and RTL document layout. Python's theme palette remains the color source for all eight palettes and both display modes.
- Added live theme previews, custom accent, day/night switching, and persistence through the existing editor settings folder.

## Pages and bridge
- Refined the speed/RPM dials: reduced oversized readouts, aligned needle pivot with the arc, and corrected the needle sweep to stay inside the gauge.
- Added local illustrated truck cards in Garage and an original road-truck illustration in the Dashboard hero; added an explicit Google Images search button per owned truck. Online results open in the external browser and are not fetched into the app.
- Fixed the WebView2 startup hang seen in the user log by keeping the native window in a private bridge attribute so pywebview does not introspect WinForms accessibility objects.
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
- node --check ui/app.js: passed after the gauge, garage and dashboard changes.
- python -m py_compile ets2_web_bridge.py tests\test_web_ui.py: passed.
- python -m unittest discover -s tests -p test_web_ui.py -v: 10 passed, including the private-window recursion regression, Google Images URL and SVG/gauge checks.
- A full unittest run was attempted, but this restricted execution environment returned errors while tests accessed or cleaned its isolated temporary directories; the full suite is not claimed as passing in this run.
- The WebView2 window was not executed here. The Windows EXE/installer and GitHub Actions build were not run in this environment.

## Remaining verification
- Run GitHub Actions or a Windows build machine with PyInstaller and Inno Setup.
- Verify the WebView2 UI and installer on a real Windows user account, including Persian text shaping.
- Capture and inspect the requested screenshot matrix and test with a real ETS2 1.60.1.7 save. In-game generated-mod compatibility remains unverified.


## Writable encrypted text saves

- Enabled verified edits for saves whose decoded payload is SiiN text, including ScsC-wrapped saves. The editor writes the verified result as plain SiiN text after its existing rolling backup and atomic validation flow.
- Kept BSII binary saves read-only because end-to-end binary serialization has not been validated.
- Updated the UI limitation text and regression tests; added a synthetic ScsC-wrapped save edit/backup round-trip test.


## Cheat Center

- Added a bilingual Cheat Center sidebar page with category filtering, selected profile/save context, save-backed money/XP/skills previews, confirmed writes, existing verified truck repair/refuel actions, and persistent operation history under the ETS2 Documents data directory.
- Exposed the detected ETS2 game version and process state for runtime compatibility context; added a backed-up config action for developer mode/console.
- Documented free-camera and manual truck teleport keys without claiming the app executes them. Runtime fuel/damage/fatigue/fine/time/simulation-speed toggles remain unsupported; BSII binary saves remain read-only.

## Debug + music/radio + dashboard pass
### Fixed
- **Tuning always said "Select supported truck definitions".** The web UI sent truck *definition ids* (`mercedes.actros2014`) but the bridge and `create_tuning_mod` use the *display names* (`Mercedes Actros 2014`), so every build and every preset save was rejected. The UI now sends display names; the bridge accepts either spelling (`_truck_names`) and always stores names, so presets saved by older builds keep working. Reproduced on the unmodified upload with the end-to-end harness, fixed, and covered by tests.
- Two tuning builds in the same second crashed with `FileExistsError`; the second file now gets a `_2` suffix.

### Cheat Center - Runtime features (honest status)
- **Change game time:** now an actionable card. It shows the documented console command `g_set_time` (copy button) and a real status from `config.cfg` (Requires Developer Console / manual in-game). The argument format is NOT verified for the installed build; the app cannot send commands to the game.
- **Infinite fuel / No damage / Simulation speed:** still Unsupported, with the precise reason. They are run-time values of the game; no save field, mod mechanism or documented command exists, and writing memory without a verified signature for the exact `eurotrucks2.exe` would be guesswork. The app does not fake them.

### New: Music & Radio (page + dashboard player)
- `ets2_music.py`: scans `Documents\Euro Truck Simulator 2\music` (depth-limited, no symlinks), reads ID3v1/v2 tags, sniffs MP3/OGG headers, parses `live_streams.sii`.
- Playback happens in the app through a read-only, token-protected server bound to 127.0.0.1 (Host-header check against DNS rebinding, Range support, only files found by the scan). Radio stations stream directly from their URL (this connects to the internet and only happens when you press play). CSP gained `media-src 'self' http: https:`; scripts, styles and everything else stay strict.
- **Why doesn't in-game music play? (diagnosis):** every finding is labelled by evidence: *checked on disk*, *SCS documentation* or *community report (not verified here)*. Checks: missing/empty folder, zero-byte, damaged/not-really-MP3, unsupported formats (WMA/M4A/AAC...), playlist files, doubled extensions, oversized ID3 tags, shortcuts, MMS/playlist radio entries, audio-related lines in `game.log.txt`, music/radio keys in `config.cfg`, Options > Audio reminder, restart hint.
- **Fix mode (reversible):** "Move unplayable files away" and "Strip oversized ID3 tags" (audio bytes untouched, validated before replacing). Originals are kept under `TR-EURO-EDITOR\music_fixes\<id>\` with a manifest; every fix has an Undo. Refuses while ETS2 runs. Whether the game plays the files afterwards cannot be verified here.

### New: Dashboard
- Game health card (ETS2 running, game version from the log, developer/console flags, last-backup age + backup size, mods in folder, music files/stations) - only values that can be read, otherwise "not available".
- Radio & music mini player, quick actions (open game/music/mods/backups/settings folders; fixed whitelist), rank progress bar from the real XP tiers.

### Tests
- `tests/test_music_and_fixes.py` (25 tests), `tests/test_e2e_ui.py` + `tests/e2e/` (real `app.js` in jsdom against the real bridge; runs when node + jsdom are available).
