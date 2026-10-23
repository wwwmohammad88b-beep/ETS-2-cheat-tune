"""Non-destructive regression checks for the existing ETS2 Save Editor core."""

from __future__ import annotations

import math
import logging
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ets2_save_editor as app  # noqa: E402

FLEET_SAMPLE = b'''SiiNunit
{
bank : bank.data {
 money_account: 10000
}
economy : economy.data {
 experience_points: 100
 adr: 0
 long_dist: 0
 heavy: 0
 fragile: 0
 urgent: 0
 mechanical: 0
 garages: 1
 garages[0]: garage.frankfurt
}
player : player.data {
 trucks: 2
 trucks[0]: vehicle.volvo
 trucks[1]: vehicle.scania
 trailers: 1
 trailers[0]: trailer.owned
}
garage : garage.frankfurt {
 vehicles: 2
 vehicles[0]: vehicle.volvo
 vehicles[1]: null
 trailers: 1
 trailers[0]: trailer.owned
}
vehicle : vehicle.volvo {
 odometer: 12345
 engine_wear: &3e000000
 transmission_wear: &3e800000
 cabin_wear: 0
 chassis_wear: &3d800000
 fuel_relative: &3f000000
 wheels_wear: 2
 wheels_wear[0]: 0
 wheels_wear[1]: &3e000000
 accessories: 4
 accessories[0]: accessory.volvo_data
 accessories[1]: accessory.volvo_chassis
 accessories[2]: accessory.volvo_engine
 accessories[3]: accessory.volvo_transmission
 license_plate: "TEST 123|test"
}
vehicle : vehicle.scania {
 odometer: 678
 engine_wear: &3dcccccd
 fuel_relative: &3e800000
 accessories: 1
 accessories[0]: accessory.scania_data
}
trailer : trailer.owned {
 trailer_definition: trailer_def.test.box
 odometer: 34
 trailer_body_wear: &3e800000
 cargo_damage: &3e000000
 wheels_wear: 1
 wheels_wear[0]: &3e800000
}
vehicle_accessory : accessory.volvo_data {
 data_path: "/def/vehicle/truck/volvo.fh_2024/data.sii"
}
vehicle_accessory : accessory.volvo_chassis {
 data_path: "/def/vehicle/truck/volvo.fh_2024/chassis/8x4.sii"
}
vehicle_accessory : accessory.volvo_engine {
 data_path: "/def/vehicle/truck/volvo.fh_2024/engine/d17a700.sii"
}
vehicle_accessory : accessory.volvo_transmission {
 data_path: "/def/vehicle/truck/volvo.fh_2024/transmission/ato3812g.sii"
}
vehicle_accessory : accessory.scania_data {
 data_path: "/def/vehicle/truck/scania.s_2016/data.sii"
}
}
'''


class ExistingCoreRegressionTests(unittest.TestCase):
    def test_sample_cli_reads_expected_values(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "ets2_save_editor.py"), "--cli", str(ROOT / "sample.sii")],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("'money': 12345", result.stdout)
        self.assertIn("'xp': 42", result.stdout)

    def test_profile_and_save_discovery_covers_both_profile_roots(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            base = Path(temp)
            for collection, profile in (("profiles", "profile-a"),
                                        ("steam_profiles", "profile-b")):
                save_dir = base / collection / profile / "save" / "autosave"
                save_dir.mkdir(parents=True)
                (save_dir / "game.sii").write_bytes((ROOT / "sample.sii").read_bytes())
            profiles = app.list_profiles(base)
            self.assertEqual(len(profiles), 2)
            self.assertEqual({Path(path).name for _, path in profiles},
                             {"profile-a", "profile-b"})
            self.assertEqual(app.default_profile_index(profiles), -1)
            self.assertEqual(app.default_profile_index(profiles, profiles[1][1]), 1)
            self.assertEqual(app.default_profile_index(profiles[:1]), 0)
            for _, profile in profiles:
                saves = app.list_saves(profile)
                self.assertEqual(len(saves), 1)
                self.assertEqual(saves[0][0], "autosave")
                self.assertGreater(saves[0][2], 0)

    def test_profile_mod_write_is_backed_up_and_atomic(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            profile_dir = root / "profiles" / "profile-a"
            profile_dir.mkdir(parents=True)
            profile = profile_dir / "profile.sii"
            original = ("SiiNunit\n{\nprofile : profile.data {\n"
                        " active_mods: 1\n active_mods[0]: \"old|Old mod\"\n}\n}\n").encode()
            profile.write_bytes(original)
            backup_root = root / "backups"
            with patch.object(app, "backup_root", return_value=backup_root):
                app.write_profile_mods(profile_dir, ["new|New mod"])
                backup = next(backup_root.rglob("profile.sii"))
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(app.ProfileSii(profile).get_mods(), ["new|New mod"])

    def test_profile_mod_write_failure_keeps_original(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            profile_dir = root / "profiles" / "profile-a"
            profile_dir.mkdir(parents=True)
            profile = profile_dir / "profile.sii"
            original = ("SiiNunit\n{\nprofile : profile.data {\n"
                        " active_mods: 1\n active_mods[0]: \"old|Old mod\"\n}\n}\n").encode()
            profile.write_bytes(original)
            real_writer = app._write_temp_bytes
            calls = {"count": 0}

            def fail_profile_temp(target, data):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("simulated profile write failure")
                return real_writer(target, data)

            with patch.object(app, "backup_root", return_value=root / "backups"), patch.object(
                    app, "_write_temp_bytes", side_effect=fail_profile_temp):
                with self.assertRaisesRegex(OSError, "profile write"):
                    app.write_profile_mods(profile_dir, ["new|New mod"])
            self.assertEqual(profile.read_bytes(), original)
            self.assertEqual(list(profile_dir.glob("*.tmp")), [])

    def test_mod_file_copy_requires_explicit_overwrite_and_preserves_old_on_failure(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            source, destination = root / "source.scs", root / "mod.scs"
            source.write_bytes(b"new mod bytes")
            destination.write_bytes(b"old mod bytes")
            with self.assertRaises(FileExistsError):
                app.copy_mod_file(source, destination)
            self.assertEqual(destination.read_bytes(), b"old mod bytes")
            self.assertTrue(app.copy_mod_file(source, destination, overwrite=True))
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_config_update_is_backed_up_and_atomic(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            config = root / "config.cfg"
            original = b'uset g_developer "0"\nuset g_console "0"\n'
            config.write_bytes(original)
            with patch.object(app, "backup_root", return_value=root / "backups"):
                app.write_config(root, {"g_developer": "1", "g_console": "1"})
                backup = next((root / "backups").rglob("config.cfg"))
            self.assertEqual(backup.read_bytes(), original)
            self.assertIn(b'uset g_developer "1"', config.read_bytes())

    def test_cli_save_copy_is_new_only_and_leaves_source_untouched(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            source = root / "source.sii"
            output = root / "copy.sii"
            original = (ROOT / "sample.sii").read_bytes()
            source.write_bytes(original)
            app.write_save_copy(source, output, {"money": 54321})
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(app.SaveFile(output).read()["money"], 54321)
            with self.assertRaises(FileExistsError):
                app.write_save_copy(source, output, {"money": 111})
            self.assertEqual(app.SaveFile(output).read()["money"], 54321)

    def test_game_version_detection_ignores_windows_and_xinput_versions(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            base = Path(temp)
            (base / "game.log.txt").write_text(
                "[sys] running on Windows (version 10.0.26300)\n"
                "[xinput] Using XInput version 1.4.0\n"
                "[ufs] Loaded pack set version 1.60.1.7 created at 123\n",
                encoding="utf-8")
            self.assertEqual(app.detect_game_version(base), "1.60.1.7")

    def test_startup_error_log_uses_a_writable_per_user_directory(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            with patch.dict("os.environ", {"LOCALAPPDATA": temp}):
                path = app.configure_error_logging()
                logging.error("log smoke test")
                for handler in logging.getLogger().handlers:
                    handler.flush()
                self.assertTrue(path.is_file())
                self.assertIn("log smoke test", path.read_text(encoding="utf-8"))
                for handler in logging.getLogger().handlers:
                    handler.close()
                logging.getLogger().handlers.clear()

    def test_text_save_update_round_trips_known_fields(self):
        source = (ROOT / "sample.sii").read_bytes()
        save = app.TextSave(source)
        save.apply({"money": 250000, "xp": 1800, "long_dist": 4})
        save.verify()
        values = app.TextSave(save.to_bytes()).read()
        self.assertEqual(values["money"], 250000)
        self.assertEqual(values["xp"], 1800)
        self.assertEqual(values["long_dist"], 4)
        self.assertEqual(values["mechanical"], 5)

    def test_edit_save_creates_backup_and_verifies_in_isolated_tree(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            game_save = root / "profiles" / "profile-a" / "save" / "1" / "game.sii"
            game_save.parent.mkdir(parents=True)
            shutil.copy2(ROOT / "sample.sii", game_save)
            backup_dir = root / "backups"
            with patch.object(app, "backup_root", return_value=backup_dir), patch.object(
                app, "game_running", return_value=False
            ), patch.object(app.time, "strftime", return_value="20261023-120000"):
                backup = app.edit_save(game_save, {"money": 7654321, "xp": 700})
                second_backup = app.edit_save(game_save, {"money": 7654322, "xp": 701})
            self.assertTrue(backup.is_file())
            self.assertEqual(backup, second_backup)
            self.assertEqual(len(list(backup_dir.rglob("game.sii"))), 1)
            self.assertEqual(app.SaveFile(backup).read()["money"], 7654321)
            with patch.object(app, "backup_root", return_value=backup_dir):
                self.assertEqual(app.latest_backup(game_save), second_backup)
            self.assertEqual(app.SaveFile(game_save).read()["money"], 7654322)
            self.assertEqual(app.SaveFile(game_save).read()["xp"], 701)
            self.assertFalse(game_save.with_name("game.sii.tmp").exists())

    def test_edit_save_cleans_temp_and_keeps_original_when_write_fails(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            game_save = root / "profiles" / "profile-a" / "save" / "1" / "game.sii"
            game_save.parent.mkdir(parents=True)
            original = (ROOT / "sample.sii").read_bytes()
            game_save.write_bytes(original)
            real_writer = app._write_temp_bytes
            calls = {"count": 0}

            def fail_save_temp(target, data):
                calls["count"] += 1
                if calls["count"] == 2:  # backup succeeds; candidate save write fails
                    raise OSError("simulated disk error")
                return real_writer(target, data)

            with patch.object(app, "backup_root", return_value=root / "backups"), patch.object(
                    app, "_write_temp_bytes", side_effect=fail_save_temp):
                with self.assertRaises(OSError):
                    app.edit_save(game_save, {"money": 999})
                self.assertTrue(app.latest_backup(game_save).is_file())
            self.assertEqual(game_save.read_bytes(), original)
            self.assertEqual(list(game_save.parent.glob("*.tmp")), [])

    def test_edit_save_refuses_untested_binary_or_encrypted_formats(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            path = Path(temp) / "game.sii"
            original = (ROOT / "sample.sii").read_bytes()
            path.write_bytes(original)

            class UnsupportedSave:
                kind = "binary"
                was_encrypted = False

            with patch.object(app, "SaveFile", return_value=UnsupportedSave()):
                with self.assertRaisesRegex(app.SiiError, "read-only"):
                    app.edit_save(path, {"money": 999})
            self.assertEqual(path.read_bytes(), original)

    def test_edit_save_validation_failure_keeps_original_and_cleans_temp(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            game_save = root / "profiles" / "profile-a" / "save" / "1" / "game.sii"
            game_save.parent.mkdir(parents=True)
            original = (ROOT / "sample.sii").read_bytes()
            game_save.write_bytes(original)
            with patch.object(app, "backup_root", return_value=root / "backups"):
                with self.assertRaisesRegex(app.SiiError, "no supported vehicle"):
                    app.edit_save(game_save, {}, extras=("repair",))
                self.assertTrue(app.latest_backup(game_save).is_file())
            self.assertEqual(game_save.read_bytes(), original)
            self.assertEqual(list(game_save.parent.glob("*.tmp")), [])

    def test_edit_save_replace_failure_keeps_original_and_cleans_temp(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            game_save = root / "profiles" / "profile-a" / "save" / "1" / "game.sii"
            game_save.parent.mkdir(parents=True)
            original = (ROOT / "sample.sii").read_bytes()
            game_save.write_bytes(original)
            real_replace = app.os.replace
            calls = {"count": 0}

            def fail_final_replace(source, destination):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("simulated final replace failure")
                return real_replace(source, destination)

            with patch.object(app, "backup_root", return_value=root / "backups"), patch.object(
                    app.os, "replace", side_effect=fail_final_replace):
                with self.assertRaisesRegex(OSError, "final replace"):
                    app.edit_save(game_save, {"money": 777})
            self.assertEqual(game_save.read_bytes(), original)
            self.assertEqual(list(game_save.parent.glob("*.tmp")), [])

    def test_restore_refuses_unverified_binary_format_without_touching_current_save(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            current = root / "game.sii"
            backup = root / "backup.sii"
            original = (ROOT / "sample.sii").read_bytes()
            current.write_bytes(original)
            backup.write_bytes(original)

            class UnsupportedSave:
                kind = "binary"
                was_encrypted = False

                def read(self):
                    return {}

            with patch.object(app, "SaveFile", return_value=UnsupportedSave()):
                with self.assertRaisesRegex(app.SiiError, "plain-text"):
                    app.restore_save_backup(current, backup)
            self.assertEqual(current.read_bytes(), original)

    def test_text_save_verify_rejects_missing_header(self):
        with self.assertRaises(app.SiiError):
            app.TextSave(b"not a save").verify()
        with self.assertRaisesRegex(app.SiiError, "incomplete block"):
            app.TextSave(b'SiiNunit\n{\nbank : bank.data {\n money_account: 1\n}\n').verify()

    def test_only_plain_unencrypted_saves_are_writable(self):
        self.assertTrue(app.save_writes_supported("text", False))
        self.assertFalse(app.save_writes_supported("text", True))
        self.assertFalse(app.save_writes_supported("binary", False))

    def test_repair_and_refuel_verification_requires_recognized_fields(self):
        with self.assertRaises(app.SiiError):
            app._verify_text_extras((ROOT / "sample.sii").read_text(), ["repair"])
        data = ("SiiNunit\n{\nvehicle : vehicle.data {\n"
                " engine_wear: 0.2\n wheels_wear[0]: 0.4\n fuel_relative: 0.3\n"
                "}\n}\n").encode()
        save = app.TextSave(data)
        save.apply({}, extras=("repair", "refuel"))
        app._verify_text_extras(save.t, ("repair", "refuel"))
        self.assertIn("engine_wear: 0", save.t)
        self.assertIn("fuel_relative: 1", save.t)

    def test_extras_inspector_reads_supported_save_metadata(self):
        result = app.inspect_save_file(ROOT / "sample.sii")
        self.assertEqual(result["format"], "text")
        self.assertFalse(result["encrypted"])
        self.assertGreater(result["size"], 0)
        self.assertEqual(result["values"]["money"], 12345)

    def test_fleet_extractor_resolves_owned_trucks_garage_specs_and_metrics(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            save = Path(temp) / "game.sii"
            save.write_bytes(FLEET_SAMPLE)
            fleet = app.extract_fleet(save)
        self.assertEqual(len(fleet["trucks"]), 2)
        self.assertEqual(len(fleet["trailers"]), 1)
        self.assertTrue(fleet["trailers_available"])
        self.assertEqual(fleet["garage_count"], 1)
        self.assertEqual(fleet["garages"], [{"city": "frankfurt",
                                              "trucks": ["vehicle.volvo"],
                                              "trailers": ["trailer.owned"]}])
        volvo = next(x for x in fleet["trucks"] if x["id"] == "vehicle.volvo")
        self.assertEqual(volvo["model"], "volvo.fh_2024")
        self.assertEqual(volvo["garage"], "frankfurt")
        self.assertEqual(volvo["odometer"], 12345)
        self.assertAlmostEqual(volvo["damage"]["engine_wear"], 0.125)
        self.assertAlmostEqual(volvo["damage"]["transmission_wear"], 0.25)
        self.assertEqual(volvo["damage"]["cabin_wear"], 0.0)
        self.assertEqual(volvo["damage"]["chassis_wear"], 0.0625)
        self.assertEqual(volvo["wheels_wear"], [0.0, 0.125])
        self.assertEqual(volvo["fuel_relative"], 0.5)
        self.assertIsNone(volvo["fuel_liters"])
        self.assertEqual(volvo["specs"]["chassis"], "8x4.sii")
        self.assertEqual(volvo["specs"]["engine"], "d17a700.sii")
        scania = next(x for x in fleet["trucks"] if x["id"] == "vehicle.scania")
        self.assertIsNone(scania["garage"])
        self.assertIsNone(scania["specs"]["engine"])
        trailer = fleet["trailers"][0]
        self.assertEqual(trailer["definition"], "trailer_def.test.box")
        self.assertEqual(trailer["garage"], "frankfurt")
        self.assertEqual(trailer["odometer"], 34)
        self.assertEqual(trailer["damage"]["trailer_body_wear"], 0.25)
        self.assertIsNone(trailer["damage"]["chassis_wear"])
        self.assertEqual(trailer["damage"]["cargo_damage"], 0.125)
        self.assertEqual(trailer["wheels_wear"], [0.25])

    def test_missing_trailer_ownership_and_unreadable_metrics_stay_unavailable(self):
        text = FLEET_SAMPLE.decode("utf-8").replace(
            " trailers: 1\n trailers[0]: trailer.owned\n", "")
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            save = Path(temp) / "game.sii"
            save.write_text(text, encoding="utf-8")
            fleet = app.extract_fleet(save)
        self.assertFalse(fleet["trailers_available"])
        self.assertEqual(fleet["trailers"], [])
        self.assertIsNone(app._sii_fraction({"fuel_relative": "&ffffffff"}, "fuel_relative"))

    def test_fleet_extractor_does_not_guess_binary_or_missing_ownership_layout(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            path = Path(temp) / "game.sii"
            path.write_bytes(b"BSII")
            with self.assertRaisesRegex(app.SiiError, "save could not be parsed"):
                app.extract_fleet(path)
            path.write_text("SiiNunit\n{\nvehicle : vehicle.one {\n odometer: 4\n}\n}\n",
                            encoding="utf-8")
            with self.assertRaisesRegex(app.SiiError, "player unit"):
                app.extract_fleet(path)

    def test_scoped_repair_and_refuel_only_change_selected_owned_vehicle(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            path = root / "profiles" / "p" / "save" / "1" / "game.sii"
            path.parent.mkdir(parents=True)
            path.write_bytes(FLEET_SAMPLE)
            with patch.object(app, "backup_root", return_value=root / "backups"), patch.object(
                    app, "game_running", return_value=False):
                app.edit_save(path, {}, extras=("repair", "refuel"),
                              vehicle_ids=["vehicle.volvo"])
            fleet = app.extract_fleet(path)
            volvo = next(x for x in fleet["trucks"] if x["id"] == "vehicle.volvo")
            scania = next(x for x in fleet["trucks"] if x["id"] == "vehicle.scania")
            self.assertEqual(volvo["fuel_relative"], 1.0)
            self.assertEqual(volvo["damage"]["engine_wear"], 0.0)
            self.assertEqual(volvo["wheels_wear"], [0.0, 0.0])
            self.assertAlmostEqual(scania["damage"]["engine_wear"], 0.1, places=5)
            self.assertEqual(scania["fuel_relative"], 0.25)

    def test_extras_backup_history_and_restore_preserve_current_save(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            game_save = root / "profiles" / "profile-a" / "save" / "1" / "game.sii"
            game_save.parent.mkdir(parents=True)
            original = (ROOT / "sample.sii").read_bytes()
            game_save.write_bytes(original)
            backup_dir = root / "backups"
            with patch.object(app, "backup_root", return_value=backup_dir), patch.object(
                app.time, "strftime", return_value="20261023-120000"
            ):
                first_backup = app.make_backup(game_save)
                modified = app.SaveFile(game_save)
                modified.apply({"money": 900000})
                game_save.write_bytes(modified.to_bytes())
                second_backup = app.make_backup(game_save)
                history = app.backup_history(game_save)
                recovery = app.restore_save_backup(game_save, second_backup)

            self.assertEqual(first_backup, second_backup)
            self.assertEqual(history, [second_backup])
            self.assertEqual(app.SaveFile(game_save).read()["money"], 900000)
            self.assertTrue(recovery.is_file())
            self.assertEqual(app.SaveFile(recovery).read()["money"], 900000)

    def test_editor_storage_and_rolling_backup_stay_inside_ets2_data_folder(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            game_data = Path(temp) / "Euro Truck Simulator 2"
            save = game_data / "profiles" / "profile-a" / "save" / "1" / "game.sii"
            save.parent.mkdir(parents=True)
            save.write_bytes((ROOT / "sample.sii").read_bytes())
            self.assertEqual(app.editor_storage_root(game_data), game_data / "TR-EURO-EDITOR")
            backup = app.make_backup(save)
            self.assertEqual(backup, game_data / "TR-EURO-EDITOR" / "backups" /
                             "profile-a" / "1" / "game.sii")

    def test_failed_rolling_backup_keeps_previous_backup_and_cleans_temp(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            game_save = root / "profiles" / "profile-a" / "save" / "1" / "game.sii"
            game_save.parent.mkdir(parents=True)
            original = (ROOT / "sample.sii").read_bytes()
            game_save.write_bytes(original)
            backup_dir = root / "backups"
            with patch.object(app, "backup_root", return_value=backup_dir):
                backup = app.make_backup(game_save)
                modified = app.SaveFile(game_save)
                modified.apply({"money": 654321})
                game_save.write_bytes(modified.to_bytes())
                with patch.object(app, "_write_temp_bytes", side_effect=OSError("simulated backup failure")):
                    with self.assertRaisesRegex(OSError, "backup failure"):
                        app.make_backup(game_save)
                latest = app.latest_backup(game_save)
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(latest, backup)
            self.assertEqual(list(backup.parent.glob("*.tmp")), [])

    def test_restore_external_backup_preserves_current_in_rolling_slot(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            game_save = root / "profiles" / "profile-a" / "save" / "1" / "game.sii"
            game_save.parent.mkdir(parents=True)
            old = (ROOT / "sample.sii").read_bytes()
            game_save.write_bytes(old)
            current = app.SaveFile(game_save)
            current.apply({"money": 777777})
            game_save.write_bytes(current.to_bytes())
            selected = root / "older-backup.sii"
            selected.write_bytes(old)
            backup_dir = root / "backups"
            with patch.object(app, "backup_root", return_value=backup_dir):
                recovery = app.restore_save_backup(game_save, selected)
                self.assertEqual(app.SaveFile(game_save).read()["money"], 12345)
                self.assertEqual(app.SaveFile(recovery).read()["money"], 777777)

    def test_performance_estimate_returns_positive_finite_values(self):
        result = app.estimate_tuning_performance(750, 3500, 3000, 200, 2.64, 40000)
        self.assertGreater(result["top_speed"], 0)
        self.assertGreater(result["zero_to_100"], 0)
        self.assertTrue(math.isfinite(result["top_speed"]))
        self.assertTrue(math.isfinite(result["zero_to_100"]))

    def test_new_navigation_and_dashboard_labels_are_bilingual(self):
        required = ("tab_dashboard", "tab_tune", "tab_profiles", "tab_garage",
                    "tab_extras", "tab_mods", "tab_settings", "dashboard_title",
                    "garage_title", "dash_fleet_note", "garage_limit")
        for key in required:
            with self.subTest(key=key):
                self.assertIn(key, app.T)
                self.assertEqual(len(app.T[key]), 2)
                self.assertTrue(all(app.T[key]))

    def test_mod_generation_uses_templates_and_produces_readable_archive(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            output = Path(temp) / "test_tune.scs"
            count = app.create_tuning_mod(
                output,
                ["Volvo FH 2024"],
                target_hp=750,
                target_torque=3500,
                target_rpm=3000,
                gear_count=12,
                target_speed=200,
                differential=2.64,
                engine_brake=1.5,
                retarder_level=3,
            )
            self.assertEqual(count, 2)
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                names = set(archive.namelist())
                self.assertIn("manifest.sii", names)
                self.assertTrue(any("/engine/ets2se_" in name for name in names))
                self.assertTrue(any("/transmission/ets2se_" in name for name in names))
                manifest = archive.read("manifest.sii").decode()
                self.assertNotIn("compatible_versions[]", manifest)
                engine_name = next(name for name in names if "/engine/ets2se_" in name)
                part_id = engine_name.rsplit("ets2se_", 1)[1].removesuffix(".sii")
                self.assertTrue(app.validate_generated_tuning_mod(
                    output, ["Volvo FH 2024"], part_id))

    def test_generated_mod_uses_unique_ids_and_configurable_compatibility(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            paths = [Path(temp) / f"tune_{i}.scs" for i in range(2)]
            for path in paths:
                app.create_tuning_mod(
                    path, ["Mercedes Actros 2014"], 750, 3500, 3000,
                    target_speed=180, compatible_versions=["1.60.*"])
            ids = []
            for path in paths:
                with zipfile.ZipFile(path) as archive:
                    self.assertIsNone(archive.testzip())
                    manifest = app.parse_manifest(archive.read("manifest.sii").decode())
                    self.assertEqual(manifest["compatible_versions"], ["1.60.*"])
                    engine_name = next(name for name in archive.namelist()
                                       if "/engine/ets2se_" in name)
                    ids.append(engine_name.rsplit("ets2se_", 1)[1].removesuffix(".sii"))
                    self.assertTrue(app.validate_generated_tuning_mod(path,
                                    ["Mercedes Actros 2014"], ids[-1].split(".", 1)[0]))
            self.assertNotEqual(ids[0].split(".", 1)[0], ids[1].split(".", 1)[0])

    def test_static_mod_validation_flags_unsafe_archive_paths(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            archive_path = root / "unsafe.scs"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("manifest.sii", "SiiNunit\n{}\n")
                archive.writestr("../outside.txt", "bad")
            verdict, details = app.validate_mod(archive_path, root)
            self.assertEqual(verdict, "err")
            self.assertIn("v_unsafe_path", [item[1] for item in details])

    def test_mod_generation_refuses_to_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            output = Path(temp) / "existing.scs"
            original = b"user-owned file"
            output.write_bytes(original)
            with self.assertRaises(FileExistsError):
                app.create_tuning_mod(
                    output, ["Volvo FH 2024"], 750, 3500, 3000,
                    gear_count=12, target_speed=200, differential=2.64,
                    engine_brake=1.5, retarder_level=3,
                )
            self.assertEqual(output.read_bytes(), original)

    def test_named_tuning_presets_round_trip_and_estimate(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            path = Path(temp) / "settings" / "presets.json"
            preset = {
                "hp": 750, "torque": 3500, "rpm": 3000,
                "target_speed": 200, "target_accel": 30, "mass": 28000,
                "gear_count": 12, "differential": 2.64,
                "engine_brake": 1.5, "retarder": 3,
                "trucks": ["Volvo FH 2024"],
            }
            app.save_tuning_presets(path, {"Balanced": preset})
            loaded = app.load_tuning_presets(path)
            self.assertEqual(loaded["Balanced"], preset)
            estimate = app.estimate_tuning_preset(loaded["Balanced"])
            self.assertGreater(estimate["top_speed"], 0)
            self.assertGreater(estimate["zero_to_100"], 0)


if __name__ == "__main__":
    unittest.main()
