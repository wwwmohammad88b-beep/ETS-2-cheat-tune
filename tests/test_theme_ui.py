"""Theme palette + live theme switching tests.

Pure palette tests always run. The GUI tests need tkinter and a display (e.g. Xvfb on Linux,
a normal desktop on Windows); they are skipped otherwise instead of passing silently.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ets2_save_editor as m  # noqa: E402

try:
    import tkinter as tk
    from tkinter import ttk
    _probe = tk.Tk()
    _probe.destroy()
    HAVE_DISPLAY = True
except Exception:          # no tkinter / no display
    HAVE_DISPLAY = False


class PalettePureTests(unittest.TestCase):
    def test_every_preset_has_all_roles_and_valid_colours(self):
        self.assertEqual(len(m.THEME_PRESETS), 8)
        for theme_id, preset in m.THEME_PRESETS.items():
            for field in m.THEME_FIELDS:
                self.assertIn(field, preset, (theme_id, field))
                self.assertRegex(preset[field], r"^#[0-9a-f]{6}$", (theme_id, field))
            self.assertIn(preset["mode"], ("day", "night"))

    def test_text_roles_are_readable_in_every_theme_mode_and_accent(self):
        for theme_id in m.THEME_PRESETS:
            for mode in ("night", "day"):
                for accent in (None, "#ffff00", "#000000", "#7f7f7f", "#ff0000", "#00ffff"):
                    c = m.ui_palette({"theme": theme_id, "mode": mode, "custom_accent": accent})
                    for name, fg, bg in (("text", c["text_primary"], c["card"]),
                                         ("text-bg", c["text_primary"], c["background"]),
                                         ("secondary", c["text_secondary"], c["sidebar"]),
                                         ("input", c["input_foreground"], c["input_background"]),
                                         ("accent-text", c["accent_text"], c["card"]),
                                         ("accent-text-side", c["accent_text"], c["sidebar"]),
                                         ("on-accent", c["accent_foreground"], c["accent"])):
                        self.assertGreaterEqual(m.contrast_ratio(fg, bg), 4.5,
                                                (theme_id, mode, accent, name))

    def test_custom_accent_only_replaces_accent_roles(self):
        base = m.ui_palette({"theme": "emerald", "mode": "night", "custom_accent": None})
        custom = m.ui_palette({"theme": "emerald", "mode": "night", "custom_accent": "#FF8800"})
        self.assertEqual(custom["accent"], "#ff8800")
        for role in ("background", "surface", "card", "sidebar", "text_primary", "border"):
            self.assertEqual(base[role], custom[role])
        self.assertNotEqual(base["accent_hover"], custom["accent_hover"])

    def test_hex_normalisation_and_appearance_sanitising(self):
        self.assertEqual(m.normalize_hex_color("#ABC"), "#aabbcc")
        self.assertEqual(m.normalize_hex_color(" 38BDF8 "), "#38bdf8")
        for bad in ("", "#12", "#12345", "#1234567", "#gggggg", None, 5, "rgb(1,2,3)"):
            self.assertIsNone(m.normalize_hex_color(bad), bad)
        self.assertEqual(m.sanitize_appearance(None),
                         {"theme": "midnight_cyan", "mode": "night", "custom_accent": None})
        got = m.sanitize_appearance({"theme": "light_professional", "mode": "bogus", "custom_accent": "#12"})
        self.assertEqual(got, {"theme": "light_professional", "mode": "day", "custom_accent": None})
        legacy = m.sanitize_appearance({"theme": "emerald", "accent": "Rose"})
        self.assertEqual(legacy["custom_accent"], "#fb7185")
        self.assertEqual(m.sanitize_appearance({"theme": "emerald", "mode": "day"})["mode"], "day")

    def test_blend_endpoints(self):
        self.assertEqual(m.blend_colors("#000000", "#ffffff", 0), "#000000")
        self.assertEqual(m.blend_colors("#000000", "#ffffff", 1), "#ffffff")
        self.assertEqual(m.blend_colors("#102030", "#ffffff", 5), "#ffffff")   # clamped


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


@unittest.skipUnless(HAVE_DISPLAY, "needs tkinter and a display (run under Xvfb on Linux)")
class LiveThemeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        game = self.home / "Documents" / "Euro Truck Simulator 2"
        (game / "profiles").mkdir(parents=True)
        self.ui_json = game / "TR-EURO-EDITOR" / "settings" / "ui.json"
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home),
                                               "APPDATA": str(self.home / "appdata"),
                                               "LOCALAPPDATA": str(self.home / "local")})
        patcher.start()
        self.addCleanup(patcher.stop)
        docs = mock.patch.object(m, "documents_dir", lambda: self.home / "Documents")
        docs.start()
        self.addCleanup(docs.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_app(self, driver):
        errors = []

        def fake_mainloop(root):
            try:
                root.update()
                driver(root)
            except BaseException as exc:      # reported after the window is closed
                import traceback
                errors.append((exc, traceback.format_exc()))
            finally:
                try:                                   # no timers may outlive this window
                    for job in root.tk.splitlist(root.tk.call("after", "info")):
                        root.after_cancel(job)
                    root.destroy()
                except tk.TclError:
                    pass
        with mock.patch.object(tk.Tk, "mainloop", fake_mainloop):
            m.run_gui()
        if errors:
            self.fail(errors[0][1])

    # ---- widget lookups -------------------------------------------------------------------
    @staticmethod
    def nav_buttons(root):
        return [w for w in _walk(root) if isinstance(w, tk.Button)]

    @staticmethod
    def theme_cards(root):
        cards = [w for w in _walk(root) if isinstance(w, tk.Canvas)
                 and int(w.cget("width")) == 188 and int(w.cget("height")) == 122]
        return cards

    @staticmethod
    def ttk_button(root, text):
        found = [w for w in _walk(root) if isinstance(w, ttk.Button) and str(w.cget("text")) == text]
        if not found:
            raise AssertionError("button not found: " + text)
        return found[0]

    @staticmethod
    def hex_entry(root):
        """The accent hex field: the Entry that shares a row with the 'Hex colour' label."""
        label = [w for w in _walk(root) if isinstance(w, ttk.Label)
                 and str(w.cget("text")) == m.T["accent_hex"][0]][0]
        return [c for c in label.master.winfo_children() if isinstance(c, ttk.Entry)][0]

    def click(self, widget):
        widget.event_generate("<Button-1>", x=5, y=5)
        widget.update()

    def expect_palette(self, root, appearance):
        pal = m.ui_palette(appearance)
        style = ttk.Style()
        self.assertEqual(style.lookup("TFrame", "background").lower(), pal["background"])
        self.assertEqual(style.lookup("TLabel", "foreground").lower(), pal["text_primary"])
        self.assertEqual(style.lookup("Treeview", "background").lower(), pal["card"])
        self.assertEqual(style.lookup("TuneAccent.TLabel", "foreground").lower(), pal["accent_text"])
        self.assertEqual(root.cget("background").lower(), pal["background"])
        navs = self.nav_buttons(root)
        sidebar = navs[0].master
        self.assertEqual(sidebar.cget("bg").lower(), pal["sidebar"])
        self.assertEqual(navs[0].cget("bg").lower(), pal["nav_selected"])     # dashboard is active
        self.assertEqual(navs[0].cget("fg").lower(), pal["accent_text"])
        self.assertEqual(navs[1].cget("bg").lower(), pal["sidebar"])
        self.assertEqual(navs[1].cget("fg").lower(), pal["text_secondary"])
        gauge = [w for w in _walk(root) if isinstance(w, tk.Canvas) and int(w.cget("height")) == 260][0]
        self.assertEqual(gauge.cget("bg").lower(), pal["card"])
        texts = [w for w in _walk(root) if isinstance(w, tk.Text)]
        self.assertTrue(texts)
        for text in texts:
            self.assertEqual(text.cget("bg").lower(), pal["input_background"])
            self.assertEqual(text.cget("fg").lower(), pal["input_foreground"])
        for combo in [w for w in _walk(root) if isinstance(w, ttk.Combobox)]:
            popdown = root.tk.call("ttk::combobox::PopdownWindow", combo)
            bg = root.tk.call(f"{popdown}.f.l", "cget", "-background")
            self.assertEqual(str(bg).lower(), pal["input_background"])

    # ---- tests ----------------------------------------------------------------------------
    def test_default_start_uses_midnight_cyan_palette(self):
        def driver(root):
            self.expect_palette(root, {"theme": "midnight_cyan", "mode": "night", "custom_accent": None})
        self.run_app(driver)

    def test_all_eight_theme_cards_switch_live_without_rebuilding_the_window(self):
        timings = []

        def driver(root):
            before = list(_walk(root))               # strong references: ids cannot be recycled
            cards = self.theme_cards(root)
            self.assertEqual(len(cards), 8)
            for theme_id, card in zip(m.THEME_PRESETS, cards):
                started = time.perf_counter()
                self.click(card)
                timings.append(time.perf_counter() - started)
                appearance = {"theme": theme_id, "mode": m.THEME_PRESETS[theme_id]["mode"],
                              "custom_accent": None}
                self.expect_palette(root, appearance)
                after = list(_walk(root))
                self.assertEqual({id(w) for w in before}, {id(w) for w in after},
                                 "widgets were created or destroyed while switching to " + theme_id)
                saved = json.loads(self.ui_json.read_text(encoding="utf-8"))
                self.assertEqual(saved["theme"], theme_id)
        self.run_app(driver)
        self.assertLess(max(timings), 2.0, timings)
        print("\n  live theme switch timings (s): " + ", ".join("%.3f" % t for t in timings))

    def test_custom_accent_invalid_entry_reset_and_swatches(self):
        def driver(root):
            before = {id(w) for w in list(_walk(root))}
            keep = list(_walk(root))                 # noqa: F841 - keeps ids unique
            entry = self.hex_entry(root)
            var_name = str(entry.cget("textvariable"))
            # invalid input: nothing changes, an error is shown, nothing is saved
            root.setvar(var_name, "zzz")
            self.ttk_button(root, "Apply").invoke()
            root.update()
            self.expect_palette(root, {"theme": "midnight_cyan", "mode": "night", "custom_accent": None})
            self.assertFalse(self.ui_json.exists())
            # valid input (short form) is normalised and applied everywhere
            root.setvar(var_name, "#F80")
            self.ttk_button(root, "Apply").invoke()
            root.update()
            applied = {"theme": "midnight_cyan", "mode": "night", "custom_accent": "#ff8800"}
            self.expect_palette(root, applied)
            self.assertEqual(json.loads(self.ui_json.read_text(encoding="utf-8"))["custom_accent"], "#ff8800")
            self.assertEqual(root.getvar(var_name).lower(), "#ff8800")
            self.assertEqual({id(w) for w in _walk(root)}, before)
            # the accent survives a theme change (user override), reset clears it
            self.click(self.theme_cards(root)[3])
            self.expect_palette(root, {"theme": "emerald", "mode": "night", "custom_accent": "#ff8800"})
            self.ttk_button(root, "Reset to Theme Default").invoke()
            root.update()
            self.expect_palette(root, {"theme": "emerald", "mode": "night", "custom_accent": None})
            saved = json.loads(self.ui_json.read_text(encoding="utf-8"))
            self.assertEqual(saved, {"theme": "emerald", "mode": "night", "custom_accent": None})
        self.run_app(driver)

    def test_day_night_toggle_and_reset_restores_theme_mode(self):
        def driver(root):
            self.ttk_button(root, "Day").invoke()          # night -> day
            root.update()
            self.expect_palette(root, {"theme": "midnight_cyan", "mode": "day", "custom_accent": None})
            self.ttk_button(root, "Night")                 # label flipped
            self.ttk_button(root, "Reset to Theme Default").invoke()
            root.update()
            self.expect_palette(root, {"theme": "midnight_cyan", "mode": "night", "custom_accent": None})
            self.ttk_button(root, "Day")
        self.run_app(driver)

    def test_selection_is_restored_after_restart(self):
        def first(root):
            self.click(self.theme_cards(root)[5])          # amber_truck
            root.setvar(str(self.hex_entry(root).cget("textvariable")), "#12ab34")
            self.ttk_button(root, "Apply").invoke()
            root.update()
        self.run_app(first)
        self.assertEqual(json.loads(self.ui_json.read_text(encoding="utf-8")),
                         {"theme": "amber_truck", "mode": "night", "custom_accent": "#12ab34"})

        def second(root):
            self.expect_palette(root, {"theme": "amber_truck", "mode": "night", "custom_accent": "#12ab34"})
            self.assertEqual(root.getvar(str(self.hex_entry(root).cget("textvariable"))), "#12ab34")
        self.run_app(second)

    def test_language_switch_keeps_the_chosen_theme(self):
        def driver(root):
            self.click(self.theme_cards(root)[7])          # light_professional (day)
            lang_button = [w for w in _walk(root) if isinstance(w, ttk.Button)
                           and str(w.cget("text")) == m.T["lang_btn"][0]][0]
            lang_button.invoke()                           # rebuild into Persian
            root.update()
            self.expect_palette(root, {"theme": "light_professional", "mode": "day", "custom_accent": None})
        self.run_app(driver)

    def test_theme_change_updates_a_window_opened_later_and_wheel_scrolls_settings(self):
        game = self.home / "Documents" / "Euro Truck Simulator 2"
        (game / "game.log.txt").write_text("00:00:00.000 : Version: 1.50.1.0s\nline two\n", encoding="utf-8")

        def driver(root):
            navs = self.nav_buttons(root)
            navs[4].invoke()                                   # Extras
            root.update()
            before = {w for w in _walk(root) if isinstance(w, tk.Toplevel)}
            self.ttk_button(root, m.T["ex_log_view"][0]).invoke()
            root.update()
            windows = [w for w in _walk(root) if isinstance(w, tk.Toplevel) and w not in before]
            self.assertEqual(len(windows), 1, "the game.log window did not open")
            log_text = [w for w in _walk(windows[0]) if isinstance(w, tk.Text)][0]
            navs[6].invoke()                                   # Settings
            root.update()
            self.click(self.theme_cards(root)[1])              # racing_red, window still open
            pal = m.ui_palette({"theme": "racing_red", "mode": "night", "custom_accent": None})
            self.assertEqual(log_text.cget("bg").lower(), pal["input_background"])
            self.assertEqual(log_text.cget("fg").lower(), pal["input_foreground"])
            self.assertEqual(windows[0].cget("bg").lower(), pal["background"])
            windows[0].destroy()                               # it would cover the pointer below
            root.update()
            # mouse wheel over the (taller than the window) settings page scrolls it
            canvas = [w for w in _walk(root) if isinstance(w, tk.Canvas) and w.winfo_ismapped()
                      and w.cget("yscrollcommand")][0]
            self.assertEqual(canvas.yview()[0], 0.0)
            x, y = canvas.winfo_rootx() + 40, canvas.winfo_rooty() + 40
            if sys.platform.startswith("win") or sys.platform == "darwin":
                canvas.event_generate("<MouseWheel>", delta=-120, x=40, y=40, rootx=x, rooty=y)
            else:
                canvas.event_generate("<Button-5>", x=40, y=40, rootx=x, rooty=y)
            root.update()
            self.assertGreater(canvas.yview()[0], 0.0, "settings page did not scroll")
        self.run_app(driver)

    def test_corrupt_or_legacy_ui_json_never_crashes(self):
        self.ui_json.parent.mkdir(parents=True, exist_ok=True)
        cases = (("{not json", {"theme": "midnight_cyan", "mode": "night", "custom_accent": None}),
                 (json.dumps({"theme": "nope", "mode": 7, "custom_accent": "red"}),
                  {"theme": "midnight_cyan", "mode": "night", "custom_accent": None}),
                 (json.dumps({"theme": "emerald", "accent": "Rose"}),
                  {"theme": "emerald", "mode": "night", "custom_accent": "#fb7185"}),
                 (json.dumps([1, 2, 3]), {"theme": "midnight_cyan", "mode": "night", "custom_accent": None}))
        for raw, expected in cases:
            self.ui_json.write_text(raw, encoding="utf-8")
            self.run_app(lambda root, e=expected: self.expect_palette(root, e))

    def test_unwritable_settings_file_degrades_gracefully(self):
        def driver(root):
            real_replace = os.replace

            def broken(src, dst):
                raise OSError("disk full")
            with mock.patch("os.replace", broken):
                self.click(self.theme_cards(root)[2])
            os.replace = real_replace
            # the UI still switched for this session
            self.expect_palette(root, {"theme": "arctic_blue", "mode": "night", "custom_accent": None})
            self.assertFalse(self.ui_json.exists())
        self.run_app(driver)


if __name__ == "__main__":
    unittest.main()
