"""Offline WebView shell and narrow Python bridge checks."""
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ets2_save_editor as core
from ets2_web_bridge import EditorBridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.game = self.root / "Euro Truck Simulator 2"
        (self.game / "profiles").mkdir(parents=True)
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.finder = mock.patch.object(core, "find_game_dirs", return_value=[self.game])
        self.finder.start()
        self.addCleanup(self.finder.stop)
        self.bridge = EditorBridge(core)

    def test_bridge_rejects_invalid_and_boolean_indexes(self):
        for value in (True, False, -1, 1, "0", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.bridge._index(value, 1, "profile")

    def test_language_rejects_boolean_and_unknown_values(self):
        for value in (True, False, -1, 2, "1", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.bridge.set_language(value)
        self.assertEqual(self.bridge.set_language(1)["language"], 1)

    def test_profile_must_resolve_beneath_selected_game_root(self):
        self.bridge.profile_path = self.outside
        with mock.patch.object(core, "list_profiles", return_value=[("escape", self.outside)]):
            with self.assertRaisesRegex(ValueError, "no longer available"):
                self.bridge._profile()

    def test_theme_css_has_all_theme_mode_scopes_and_sanitized_accent(self):
        appearance = {"theme": "midnight_cyan", "mode": "night", "custom_accent": "#123456"}
        css = self.bridge.theme_css(appearance)
        self.assertIn(":root{", css)
        self.assertRegex(css, r"--accent:\s*#123456;")
        for theme in core.THEME_PRESETS:
            for mode in ("night", "day"):
                self.assertIn('[data-theme="' + theme + '"][data-mode="' + mode + '"]', css)

    def test_tuning_preset_rejects_unknown_truck(self):
        values = {key: 1 for key in core.TUNING_PRESET_FIELDS}
        values.update({"trucks": ["../../outside"], "hp": 700, "torque": 3000,
                       "rpm": 2200, "target_speed": 180, "target_accel": 30,
                       "mass": 40000, "gear_count": 12, "differential": 2.6,
                       "engine_brake": 3, "retarder": 2})
        with self.assertRaises(ValueError):
            self.bridge.save_tuning_preset("bad", values)


class WebAssetsTests(unittest.TestCase):
    def test_static_shell_has_strict_offline_csp_and_local_assets(self):
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertRegex(html, r"Content-Security-Policy")
        self.assertIn("default-src 'self'", html)
        self.assertNotRegex(html, r"(?:src|href)=['\"]https?://")
        self.assertNotIn("https://", html)

    def test_frontend_uses_existing_translation_keys_and_no_dynamic_html(self):
        js = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertNotRegex(js, r"\.innerHTML\b|\beval\s*\(|\bfetch\s*\(|XMLHttpRequest")
        self.assertNotRegex(js, r"(?:src|href)\s*=\s*['\"]https?://")
        keys = set(re.findall(r'\btr\("([^"]+)"', js))
        missing = sorted(keys - set(core.T))
        self.assertEqual(missing, [])
        self.assertIn("window.pywebview", js)
        self.assertIn("textContent", js)


if __name__ == "__main__":
    unittest.main()
