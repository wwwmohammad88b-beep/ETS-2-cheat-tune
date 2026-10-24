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

    def test_search_truck_images_opens_encoded_google_images_query(self):
        with mock.patch("ets2_web_bridge.webbrowser.open", return_value=True) as opened:
            result = self.bridge.search_truck_images("Volvo FH 2024")
        self.assertTrue(result["opened"])
        self.assertIn("google.com/search?tbm=isch&q=", opened.call_args.args[0])
        self.assertIn("Volvo+FH+2024+ETS2+truck", opened.call_args.args[0])
    def test_native_window_is_not_exposed_as_public_js_api_attribute(self):
        native_window = object()
        self.bridge.attach_window(native_window)
        self.assertIs(self.bridge._window, native_window)
        self.assertFalse(hasattr(self.bridge, "window"))

    def test_bridge_rejects_invalid_and_boolean_indexes(self):
        for value in (True, False, -1, 1, "0", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.bridge._index(value, 1, "profile")

    def test_language_rejects_boolean_and_unknown_values(self):
        for value in (True, False, -1, 2, "1", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.bridge.set_language(value)
        self.assertEqual(self.bridge.set_language(1)["language"], 1)

    def test_enable_text_save_mode_changes_game_setting_only_for_future_saves(self):
        self.bridge._save = mock.Mock()
        self.bridge._game = mock.Mock(return_value=self.game)
        self.bridge.state = mock.Mock(return_value={"summary": None})
        with mock.patch.object(core, "game_running", return_value=False), \
             mock.patch.object(core, "read_config", return_value={"g_save_format": "0"}), \
             mock.patch.object(core, "write_config") as write_config:
            result = self.bridge.enable_text_save_mode()
        self.bridge._save.assert_called_once_with()
        write_config.assert_called_once_with(self.game, {"g_save_format": "2"})
        self.assertTrue(result["enabled"])
        self.assertEqual(result["state"], {"summary": None})

    def test_enable_text_save_mode_refuses_while_game_is_running(self):
        self.bridge._save = mock.Mock()
        with mock.patch.object(core, "game_running", return_value=True), \
             mock.patch.object(core, "write_config") as write_config:
            with self.assertRaisesRegex(ValueError, "Close ETS2"):
                self.bridge.enable_text_save_mode()
        write_config.assert_not_called()

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

    def test_dashboard_art_is_local_and_gauges_use_aligned_center_pivot(self):
        import xml.etree.ElementTree as ET

        asset = ROOT / "ui" / "assets" / "hero-road-truck.svg"
        self.assertTrue(asset.is_file())
        ET.parse(asset)
        js = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "app.css").read_text(encoding="utf-8")
        self.assertIn('viewBox","0 0 240 185', js)
        self.assertIn('rotate(180 120 124)', js)
        self.assertIn('180+180*p', js)
        self.assertIn('font-size:18px', css)
        self.assertNotIn('transform-origin:120px 113px', css)
        self.assertIn('function truckGraphic(label)', js)
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



