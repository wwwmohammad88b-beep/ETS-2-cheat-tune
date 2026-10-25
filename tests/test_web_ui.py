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
        self.assertIsNone(result["state"]["summary"])
        self.assertEqual(result["state"]["operation_history"][0]["operation"], "game config")

    def test_state_enables_editable_text_saves_by_default_with_config_backup(self):
        with mock.patch.object(core, "read_config", side_effect=[
                {"g_save_format": "0"}, {"g_save_format": "2"}]) as read_config, \
             mock.patch.object(core, "write_config") as write_config, \
             mock.patch.object(core, "game_running", return_value=False):
            result = self.bridge.state()
        write_config.assert_called_once_with(
            self.game, {"g_save_format": core.DEFAULT_SAVE_FORMAT})
        self.assertEqual(read_config.call_count, 2)
        self.assertEqual(result["config"]["g_save_format"], "2")

    def test_cheat_profile_settings_write_only_to_selected_profile_config(self):
        profile = self.game / "profiles" / "profile-a"
        profile.mkdir(parents=True)
        self.bridge.profile_path = profile.resolve()
        self.bridge.profile_label = "Profile A"
        self.bridge._profile = mock.Mock(return_value=("Profile A", profile.resolve()))
        self.bridge.state = mock.Mock(return_value={"operation_history": []})
        self.bridge._record_cheat = mock.Mock()
        self.bridge.cheat_history = mock.Mock(return_value=[])
        with mock.patch.object(core, "read_config", side_effect=[
                {"g_fatigue": "1", "g_police": "1"},
                {"g_fatigue": "1", "g_police": "1"}]) as read_config, \
             mock.patch.object(core, "write_config") as write_config, \
             mock.patch.object(core, "game_running", return_value=False):
            self.bridge.save_game_settings({"fatigue": "0", "police": "0"})
        write_config.assert_called_once_with(profile.resolve(), {
            "g_fatigue": "0", "g_police": "0"})
        self.assertEqual(read_config.call_count, 2)

    def test_enable_text_save_mode_refuses_while_game_is_running(self):
        self.bridge._save = mock.Mock()
        with mock.patch.object(core, "game_running", return_value=True), \
             mock.patch.object(core, "write_config") as write_config:
            with self.assertRaisesRegex(ValueError, "Close ETS2"):
                self.bridge.enable_text_save_mode()
        write_config.assert_not_called()

    def _select_sample_save(self):
        profile = self.game / "profiles" / "profile-a"
        save = profile / "save" / "1" / "game.sii"
        save.parent.mkdir(parents=True, exist_ok=True)
        save.write_bytes((ROOT / "sample.sii").read_bytes())
        self.bridge.profile_label = "Profile A"
        self.bridge.profile_path = profile.resolve()
        self.bridge.save_slot = "1"
        return save

    def test_runtime_status_reports_detected_version_and_process_state(self):
        with mock.patch.object(core, "detect_game_version", return_value="1.60.1.7s"), \
             mock.patch.object(core, "game_running", return_value=True):
            result = self.bridge.state()
        self.assertEqual(result["game_version"], "1.60.1.7s")
        self.assertTrue(result["game_running"])

    def test_cheat_preview_validates_and_does_not_write(self):
        save = self._select_sample_save()
        with mock.patch.object(self.bridge, "_save", return_value=save):
            result = self.bridge.cheat_preview({"category": "money", "operation": "add", "amount": 500})
            self.assertEqual(result["before"], {"money": 12345})
            self.assertEqual(result["after"], {"money": 12845})
            self.assertTrue(result["writable"])
            with self.assertRaisesRegex(ValueError, "between 0"):
                self.bridge.cheat_preview({"category": "money", "operation": "set", "amount": -1})
        self.assertEqual(save.read_bytes(), (ROOT / "sample.sii").read_bytes())

    def test_cheat_apply_requires_confirmation_and_records_verified_edit(self):
        save = self._select_sample_save()
        with mock.patch.object(self.bridge, "_save", return_value=save), \
             mock.patch.object(core, "backup_root", return_value=self.root / "backups"), \
             mock.patch.object(core, "game_running", return_value=False):
            with self.assertRaisesRegex(ValueError, "Confirm"):
                self.bridge.cheat_apply({"category": "money", "operation": "set", "amount": 25000})
            result = self.bridge.cheat_apply({"category": "money", "operation": "set",
                                              "amount": 25000, "confirmed": True})
        self.assertEqual(core.SaveFile(save).read()["money"], 25000)
        self.assertTrue(Path(result["backup"]).is_file())
        self.assertTrue(result["history_saved"])
        self.assertEqual(result["operation_history"][0]["result"], "success")

    def test_cheat_runtime_or_unknown_categories_cannot_write(self):
        save = self._select_sample_save()
        with mock.patch.object(self.bridge, "_save", return_value=save), \
             mock.patch.object(core, "game_running", return_value=False), \
             mock.patch.object(self.bridge, "_save_edits") as write:
            with self.assertRaisesRegex(ValueError, "Unsupported Cheat Center category"):
                self.bridge.cheat_preview({"category": "unlimited_fuel"})
            with self.assertRaisesRegex(ValueError, "Unsupported Cheat Center category"):
                self.bridge.cheat_apply({"category": "unlimited_fuel", "confirmed": True})
        write.assert_not_called()

    def test_cheat_apply_refuses_binary_payload_at_bridge_boundary(self):
        save = self._select_sample_save()

        class BinarySave:
            kind = "binary"
            was_encrypted = False
            def verify(self): pass
            def read(self): return {"money": 12345, "xp": 100}

        with mock.patch.object(self.bridge, "_save", return_value=save), \
             mock.patch.object(core, "SaveFile", return_value=BinarySave()), \
             mock.patch.object(core, "game_running", return_value=False), \
             mock.patch.object(self.bridge, "_save_edits") as write:
            with self.assertRaisesRegex(ValueError, "unsupported for writing"):
                self.bridge.cheat_apply({"category": "money", "operation": "set",
                                          "amount": 20000, "confirmed": True})
        write.assert_not_called()

    def test_skill_cheat_rejects_out_of_range_values(self):
        save = self._select_sample_save()
        with mock.patch.object(self.bridge, "_save", return_value=save):
            with self.assertRaisesRegex(ValueError, "between 0 and 6"):
                self.bridge.cheat_preview({"category": "skills", "values": {"long_dist": 7}})
            result = self.bridge.cheat_preview({"category": "skills", "values": {"long_dist": 6}})
        self.assertEqual(result["before"], {"long_dist": 1})
        self.assertEqual(result["after"], {"long_dist": 6})

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
    def test_editable_text_save_is_the_default_when_config_has_no_value(self):
        js = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        source = (ROOT / "ets2_save_editor.py").read_text(encoding="utf-8")
        self.assertEqual(core.DEFAULT_SAVE_FORMAT, "2")
        self.assertIn('c.g_save_format||"2"', js)
        self.assertIn('c.get("g_save_format", DEFAULT_SAVE_FORMAT) == "2"', source)

    def test_guide_page_is_last_and_localized(self):
        js = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        nav = re.search(r"const nav=\[(.*?)\];", js).group(1)
        self.assertTrue(nav.rstrip().endswith('["guide","cheat_guide_title","ⓘ"]'))
        self.assertIn('guide:guide', js)
        for key in ("cheat_guide_title", "guide_first_steps_body", "guide_binary_limit_body",
                    "guide_cheats_body", "guide_backups_body", "guide_mods_body"):
            self.assertIn(key, core.T)

    def test_cheat_center_is_registered_and_localized(self):
        js = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn('["cheats","web_cheat_center"', js)
        self.assertIn('cheats:cheatCenter', js)
        self.assertIn("web_cheat_center", core.T)
        self.assertIn("cheat_unsupported_runtime", core.T)
        self.assertIn("cheat_history", core.T)
        used = set(re.findall(r'tr\("([^"]+)"', js))
        self.assertEqual(sorted(used - set(core.T)), [])

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



