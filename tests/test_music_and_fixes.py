"""Tuning truck-selection fix, music/radio module, diagnosis, reversible fixes, loopback server,
bridge guards and static UI checks."""
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ets2_music as music  # noqa: E402
import ets2_save_editor as core  # noqa: E402
from ets2_web_bridge import EditorBridge  # noqa: E402


def make_mp3(path, id3_payload=0, frames=30, tags=True):
    body = (b"\xff\xfb\x90\x64" + b"\x00" * 413) * frames
    head = b""
    if id3_payload:
        n = id3_payload
        head = b"ID3\x03\x00\x00" + bytes([(n >> 21) & 127, (n >> 14) & 127, (n >> 7) & 127, n & 127]) + b"\x00" * n
    elif tags:
        title, artist = b"\x00Test Song", b"\x00Test Artist"
        frames_ = (b"TIT2" + len(title).to_bytes(4, "big") + b"\x00\x00" + title +
                   b"TPE1" + len(artist).to_bytes(4, "big") + b"\x00\x00" + artist)
        n = len(frames_)
        head = b"ID3\x03\x00\x00" + bytes([(n >> 21) & 127, (n >> 14) & 127, (n >> 7) & 127, n & 127]) + frames_
    Path(path).write_bytes(head + body)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.game = self.home / "Documents" / "Euro Truck Simulator 2"
        profile = self.game / "profiles" / "447269766572"
        (profile / "save" / "quicksave").mkdir(parents=True)
        (self.game / "mod").mkdir()
        (self.game / "music").mkdir()
        (profile / "save" / "quicksave" / "game.sii").write_bytes((ROOT / "sample.sii").read_bytes())
        (profile / "profile.sii").write_text(
            'SiiNunit\n{\nuser_profile : _nameless.1 {\n profile_name: "Driver"\n active_mods: 0\n}\n}\n',
            encoding="utf-8")
        for patcher in (mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home),
                                                     "APPDATA": str(self.home / "ad"),
                                                     "LOCALAPPDATA": str(self.home / "la")}),
                        mock.patch.object(core, "documents_dir", lambda: self.home / "Documents"),
                        mock.patch.object(core, "find_game_dirs", lambda: [self.game]),
                        mock.patch.object(core, "game_running", lambda: False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bridge = EditorBridge(core)
        self.bridge.select_game(0)
        self.bridge.select_profile(0)
        self.bridge.select_save(0)
        self.storage = core.editor_storage_root()


class TruckSelectionTests(Fixture):
    PAYLOAD = {"name": "ETS2SE_Tune", "hp": 750, "torque": 3500, "rpm": 2400, "speed": 200, "gears": 12,
               "differential": 2.64, "engine_brake": 3, "retarder": 3, "compatible_versions": []}

    def test_names_and_ids_are_both_accepted_and_normalised_to_names(self):
        ids = list(core.TUNING_TRUCKS.values())
        names = list(core.TUNING_TRUCKS)
        self.assertEqual(self.bridge._truck_names(ids), names)
        self.assertEqual(self.bridge._truck_names(names), names)
        self.assertEqual(self.bridge._truck_names([ids[0], names[0]]), [names[0]])   # de-duplicated

    def test_invalid_selections_are_rejected(self):
        for bad in (None, [], "x", [True], [1], ["nope"], [{"a": 1}], list(core.TUNING_TRUCKS) * 3):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.bridge._truck_names(bad)

    def test_build_tuning_mod_works_with_ids_like_the_old_web_ui_sent_them(self):
        for trucks in (list(core.TUNING_TRUCKS.values()), list(core.TUNING_TRUCKS)):
            result = self.bridge.build_tuning_mod(dict(self.PAYLOAD, trucks=trucks))
            self.assertTrue(Path(result["path"]).is_file(), trucks)
            self.assertGreater(result["files"], 0)

    def test_saved_presets_store_display_names(self):
        preset = {"hp": 750, "torque": 3500, "rpm": 2400, "target_speed": 90, "target_accel": 3.0, "mass": 18000,
                  "gear_count": 12, "differential": 2.64, "engine_brake": 3, "retarder": 3}
        self.assertEqual(set(preset) | {"trucks"}, set(core.TUNING_PRESET_FIELDS))
        self.bridge.save_tuning_preset("Mine", dict(preset, trucks=list(core.TUNING_TRUCKS.values())[:2]))
        stored = json.dumps(self.bridge.tuning_presets(), ensure_ascii=False)
        self.assertIn("Mercedes Actros 2014", stored)
        self.assertNotIn("mercedes.actros2014", stored)


class MusicModuleTests(Fixture):
    def test_id3_tags_and_sniffing(self):
        good = self.game / "music" / "good.mp3"
        make_mp3(good)
        tags, total = music.read_id3(good)
        self.assertEqual((tags["title"], tags["artist"]), ("Test Song", "Test Artist"))
        self.assertGreater(total, 10)
        self.assertTrue(music.sniff_audio(good)["ok"])
        empty = self.game / "music" / "e.mp3"
        empty.write_bytes(b"")
        self.assertEqual(music.sniff_audio(empty)["kind"], "empty")
        fake = self.game / "music" / "f.mp3"
        fake.write_bytes(b"not audio at all" * 50)
        self.assertFalse(music.sniff_audio(fake)["ok"])
        broken_tag = self.game / "music" / "b.mp3"
        broken_tag.write_bytes(b"ID3\x04\x00\x00\x7f\x7f\x7f\x7f" + b"\x00" * 5)       # lies about its size
        music.read_id3(broken_tag)                                                      # must not raise

    def test_diagnosis_reports_each_real_problem_with_its_basis(self):
        m = self.game / "music"
        make_mp3(m / "good.mp3")
        make_mp3(m / "big.mp3", id3_payload=700 * 1024)
        (m / "empty.mp3").write_bytes(b"")
        (m / "x.wma").write_bytes(b"1234")
        (m / "pl.m3u").write_text("a")
        (m / "fake.mp3").write_bytes(b"x" * 900)
        (self.game / "live_streams.sii").write_text(
            'x\n stream_data[]: "mms://a/b|MMS|g"\n stream_data[]: "http://a/b.pls|PLS|g"\n'
            ' stream_data[]: "http://a/ok.mp3|Fine|g"\n', encoding="utf-8")
        log = "ok line\n[audio] error: could not open stream\nunrelated error\n"
        result = music.diagnose(self.game, log, {"g_music_volume": "0", "g_other": "1"}, True)
        codes = {f["code"]: f for f in result["findings"]}
        for code in ("m_zero_byte", "m_bad_format", "m_playlist_file", "m_no_frames", "m_big_tag",
                     "m_stream_scheme", "m_stream_playlist", "m_log_lines", "m_cfg_key", "m_options_audio",
                     "m_restart_hint", "m_game_running"):
            self.assertIn(code, codes, code)
        self.assertEqual(codes["m_zero_byte"]["basis"], "fact")
        self.assertEqual(codes["m_bad_format"]["basis"], "report")
        self.assertEqual(codes["m_stream_scheme"]["basis"], "docs")
        self.assertEqual(codes["m_cfg_key"]["level"], "warn")
        self.assertNotIn("g_other", json.dumps(result))
        self.assertEqual(result["summary"]["playable"], 2)           # good + big (valid audio)
        self.assertEqual(result["summary"]["stations"], 3)

    def test_missing_folder_and_clean_folder(self):
        (self.game / "music").rmdir()
        self.assertEqual(music.diagnose(self.game)["findings"][0]["code"], "m_no_folder")
        (self.game / "music").mkdir()
        make_mp3(self.game / "music" / "a.mp3")
        errors = [f for f in music.diagnose(self.game)["findings"] if f["level"] == "err"]
        self.assertEqual(errors, [])

    def test_fix_quarantines_only_unplayable_files_and_undo_restores_them(self):
        m = self.game / "music"
        make_mp3(m / "good.mp3")
        (m / "empty.mp3").write_bytes(b"")
        (m / "x.wma").write_bytes(b"1234")
        (m / "fake.mp3").write_bytes(b"x" * 900)
        before = {p.name: p.read_bytes() for p in m.iterdir()}
        result = music.run_fix(self.game, self.storage, "quarantine_bad")
        self.assertEqual(sorted(c["rel"] for c in result["changed"]), ["empty.mp3", "fake.mp3", "x.wma"])
        self.assertEqual([p.name for p in m.iterdir()], ["good.mp3"])
        self.assertEqual(music.list_fixes(self.storage)[0]["count"], 3)
        music.undo_fix(self.game, self.storage, result["stamp"])
        self.assertEqual({p.name: p.read_bytes() for p in m.iterdir()}, before)

    def test_strip_big_tags_keeps_audio_makes_a_backup_and_is_undoable(self):
        m = self.game / "music"
        make_mp3(m / "big.mp3", id3_payload=700 * 1024)
        original = (m / "big.mp3").read_bytes()
        result = music.run_fix(self.game, self.storage, "strip_big_tags")
        self.assertEqual([c["action"] for c in result["changed"]], ["stripped"])
        stripped = (m / "big.mp3").read_bytes()
        self.assertLess(len(stripped), 100 * 1024)
        self.assertTrue(music.sniff_audio(m / "big.mp3")["ok"])
        self.assertTrue(original.endswith(stripped))                  # audio bytes untouched
        music.undo_fix(self.game, self.storage, result["stamp"])
        self.assertEqual((m / "big.mp3").read_bytes(), original)

    def test_fix_ids_never_collide_and_bad_ids_are_rejected(self):
        m = self.game / "music"
        (m / "a.wma").write_bytes(b"1")
        (m / "b.wma").write_bytes(b"2")
        first = music.run_fix(self.game, self.storage, "quarantine_bad")
        (m / "c.wma").write_bytes(b"3")
        second = music.run_fix(self.game, self.storage, "quarantine_bad")
        self.assertNotEqual(first["stamp"], second["stamp"])
        for bad in ("../x", "2026", "20260101-1234", "x" * 20, ""):
            with self.assertRaises(ValueError):
                music.undo_fix(self.game, self.storage, bad)
        with self.assertRaises(ValueError):
            music.run_fix(self.game, self.storage, "rm -rf")

    def test_undo_cannot_write_outside_the_music_folder(self):
        m = self.game / "music"
        (m / "a.wma").write_bytes(b"1")
        result = music.run_fix(self.game, self.storage, "quarantine_bad")
        manifest = music.fix_root(self.storage) / result["stamp"] / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["changed"].append({"rel": "../../escaped.txt", "action": "moved"})
        manifest.write_text(json.dumps(data), encoding="utf-8")
        music.undo_fix(self.game, self.storage, result["stamp"])
        self.assertFalse((self.game / "escaped.txt").exists())
        self.assertFalse((self.game.parent / "escaped.txt").exists())

    def test_live_streams_parsing(self):
        (self.game / "live_streams.sii").write_text(
            'SiiNunit\n{\n stream_data[0]: "http://h/a.mp3|One|Pop|EN|128|0"\n'
            ' stream_data[]: "ftp://h/b|Two"\n garbage\n}\n', encoding="utf-8")
        result = music.parse_live_streams(self.game)
        self.assertEqual([s["name"] for s in result["stations"]], ["One", "Two"])
        self.assertEqual(result["stations"][0]["bitrate"], "128")
        self.assertEqual(result["stations"][1]["issue"], "scheme")


class AudioServerTests(Fixture):
    def setUp(self):
        super().setUp()
        make_mp3(self.game / "music" / "a.mp3")
        self.path = self.game / "music" / "a.mp3"
        self.server = music.AudioServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.server.set_library({0: self.path})

    def fetch(self, url, headers=None):
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as err:
            return err.code, b"", dict(err.headers)

    def test_full_range_and_tokens(self):
        base = self.server.base_url()
        self.assertTrue(base.startswith("http://127.0.0.1:"))
        status, body, headers = self.fetch(base + "0")
        self.assertEqual((status, body), (200, self.path.read_bytes()))
        self.assertEqual(headers["Content-Type"], "audio/mpeg")
        status, body, headers = self.fetch(base + "0", {"Range": "bytes=2-5"})
        self.assertEqual((status, body), (206, self.path.read_bytes()[2:6]))
        self.assertTrue(headers["Content-Range"].startswith("bytes 2-5/"))
        self.assertEqual(self.fetch(base + "0", {"Range": "bytes=999999999-"})[0], 416)
        self.assertEqual(self.fetch(base.replace(self.server.token, "wrong") + "0")[0], 404)
        self.assertEqual(self.fetch(base + "7")[0], 404)                  # not in the library
        self.assertEqual(self.fetch(f"http://127.0.0.1:{self.server.port}/")[0], 404)
        self.assertEqual(self.fetch(base + "0/../..")[0], 404)

    def test_wrong_host_header_is_refused_against_dns_rebinding(self):
        self.assertEqual(self.fetch(self.server.base_url() + "0", {"Host": "evil.example"})[0], 403)

    def test_binds_loopback_only(self):
        self.assertEqual(self.server._srv.server_address[0], "127.0.0.1")


class BridgeMusicTests(Fixture):
    def test_state_lists_only_playable_tracks_inside_the_music_folder(self):
        make_mp3(self.game / "music" / "good.mp3")
        (self.game / "music" / "empty.mp3").write_bytes(b"")
        state = self.bridge.music_state()
        by_name = {t["file"]: t for t in state["tracks"]}
        self.assertTrue(by_name["good.mp3"]["playable"])
        self.assertFalse(by_name["empty.mp3"]["playable"])
        self.assertEqual(by_name["good.mp3"]["artist"], "Test Artist")
        self.assertRegex(state["stream_base"], r"^http://127\.0\.0\.1:\d+/t/[\w-]+/$")
        self.assertNotIn(str(self.game), json.dumps(state["tracks"]))        # no absolute paths to the UI
        self.bridge._audio.stop()

    def test_fix_and_undo_refuse_while_the_game_runs_and_validate_arguments(self):
        (self.game / "music" / "a.wma").write_bytes(b"1")
        with mock.patch.object(core, "game_running", lambda: True):
            with self.assertRaises(ValueError):
                self.bridge.music_fix("quarantine_bad")
            with self.assertRaises(ValueError):
                self.bridge.music_undo("20260101-010101")
        for bad in ("../../x", None, 5, True, "strip"):
            with self.assertRaises(ValueError):
                self.bridge.music_fix(bad)
        for bad in ("../x", None, 5, "2026"):
            with self.assertRaises(ValueError):
                self.bridge.music_undo(bad)
        done = self.bridge.music_fix("quarantine_bad")
        self.assertEqual(len(done["changed"]), 1)
        self.assertEqual(self.bridge.music_undo(done["stamp"])["restored"], 1)
        self.assertTrue((self.game / "music" / "a.wma").exists())

    def test_diagnose_reads_log_and_config(self):
        (self.game / "game.log.txt").write_text("[audio] error: failed to open radio\n", encoding="utf-8")
        (self.game / "config.cfg").write_text('uset g_music_volume "0"\n', encoding="utf-8")
        codes = {f["code"] for f in self.bridge.music_diagnose()["findings"]}
        self.assertTrue({"m_log_lines", "m_cfg_key"} <= codes)

    def test_open_folder_is_a_whitelist(self):
        with mock.patch("ets2_web_bridge.os.startfile", create=True) as opener:
            self.bridge.open_folder("music")
            opener.assert_called_once()
            self.assertTrue(Path(opener.call_args.args[0]).samefile(self.game / "music"))
            for bad in ("../..", "C:\\Windows", "", None, "MUSIC", 3):
                with self.assertRaises(ValueError):
                    self.bridge.open_folder(bad)

    def test_dashboard_extras_only_report_real_values(self):
        (self.game / "game.log.txt").write_text("00:00:00.000 : [ufs] Loaded pack set version 1.60.1.7s\n",
                                                encoding="utf-8")
        (self.game / "config.cfg").write_text('uset g_developer "1"\nuset g_console "0"\n', encoding="utf-8")
        make_mp3(self.game / "music" / "a.mp3")
        extras = self.bridge.dashboard_extras()
        self.assertEqual(extras["game_version"], "1.60.1.7s")
        self.assertEqual((extras["developer"], extras["console"]), (True, False))
        self.assertEqual(extras["music_files"], 1)
        self.assertFalse(extras["game_running"])
        self.assertIsNone(extras["backup_age_days"])            # no backup was ever made -> no invented number


class StaticUiTests(unittest.TestCase):
    JS = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
    HTML = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    def test_every_translation_key_used_by_the_ui_exists_in_both_languages(self):
        used = set(re.findall(r'tr\("([A-Za-z0-9_]+)"', self.JS))
        broad = set(re.findall(r'"((?:music|dash|cheat|badge|web)_[a-z0-9_]+)"', self.JS))
        used |= {k for k in broad if not hasattr(EditorBridge, k)}          # bridge method names are not keys
        used |= {"music_basis_fact", "music_basis_docs", "music_basis_report", "badge_ok", "badge_warn", "badge_err"}
        for code in ("m_no_folder", "m_empty_folder", "m_shortcuts", "m_playlist_file", "m_bad_format",
                     "m_maybe_format", "m_zero_byte", "m_unreadable", "m_no_frames", "m_big_tag", "m_double_ext",
                     "m_stream_scheme", "m_stream_playlist", "m_no_stations", "m_streams_unreadable",
                     "m_log_lines", "m_cfg_key", "m_options_audio", "m_restart_hint", "m_game_running"):
            used.add(code)
        missing = sorted(k for k in used if k in ("",) or (k not in core.T and not k.endswith("_")))
        self.assertEqual([k for k in missing if k not in core.T], [], "keys missing from T")
        for key in used:
            if key in core.T:
                english, persian = core.T[key]
                self.assertTrue(english.strip() and persian.strip(), key)

    def test_diagnosis_codes_produced_by_python_all_have_translations(self):
        source = (ROOT / "ets2_music.py").read_text(encoding="utf-8")
        for code in set(re.findall(r'"(m_[a-z_]+)"', source)):
            self.assertIn(code, core.T, code)

    def test_csp_allows_media_but_stays_strict(self):
        self.assertIn("media-src 'self' http: https:", self.HTML)
        self.assertIn("default-src 'self'", self.HTML)
        self.assertIn("object-src 'none'", self.HTML)
        self.assertIn("script-src 'self'", self.HTML)
        self.assertNotIn("unsafe-eval", self.HTML)
        self.assertNotRegex(self.HTML, r"(?:src|href)=['\"]https?://")

    def test_new_ui_code_never_uses_innerhtml_or_eval(self):
        self.assertNotIn("innerHTML", self.JS)
        self.assertNotIn("eval(", self.JS)
        self.assertNotIn("document.write", self.JS)

    def test_tuning_checkboxes_use_display_names(self):
        self.assertIn("c.value=x[0]", self.JS)
        self.assertNotIn("c.value=x[1]", self.JS)


if __name__ == "__main__":
    unittest.main()
