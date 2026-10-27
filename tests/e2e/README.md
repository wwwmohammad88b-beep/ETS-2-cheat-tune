# End-to-end UI test (optional)

Runs the REAL `ui/app.js` inside jsdom against the REAL Python `EditorBridge` on a fake ETS2
Documents folder (sample save, mods, music files, live_streams.sii, config.cfg, game.log.txt).

    npm install jsdom            # once, anywhere (or set JSDOM_PATH to its folder)
    node tests/e2e/run.js .      # exits 1 on any failed check

`tests/test_e2e_ui.py` runs it automatically when `node` and `jsdom` are available and skips
otherwise. It cannot test WebView2, real audio output or the game itself.
