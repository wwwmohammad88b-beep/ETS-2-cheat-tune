"""Narrow pywebview bridge for local ETS2 data.

The browser receives only fixed numeric selections; it never supplies save/profile/mod
paths to core write functions. Each target is re-resolved beneath the selected known root.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path


class EditorBridge:
    def __init__(self, core):
        self.core = core
        self.games = list(core.find_game_dirs())
        self.game_index = 0 if len(self.games) == 1 else -1
        self.profile_path = None
        self.profile_label = None
        self.save_slot = None
        # Keep the native window private; pywebview introspects public js_api attributes,
        # and traversing Window.native recurses through WinForms accessibility objects.
        self._window = None
        self.language = 0

    @staticmethod
    def _index(value, length, field):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < length:
            raise ValueError(f"Invalid {field} selection")
        return value

    def attach_window(self, window):
        self._window = window

    def _game(self):
        if not 0 <= self.game_index < len(self.games):
            raise ValueError("Select an ETS2 Documents folder first")
        return Path(self.games[self.game_index]).resolve()

    def _profiles(self):
        return self.core.list_profiles(self._game())

    def _profile(self):
        game = self._game()
        profiles = self._profiles()
        for label, path in profiles:
            resolved = Path(path).resolve()
            if resolved == self.profile_path and resolved.is_relative_to(game):
                return label, resolved
        raise ValueError("The selected profile is no longer available")

    def _saves(self):
        _label, profile = self._profile()
        return self.core.list_saves(profile)

    def _save(self):
        if not self.save_slot:
            raise ValueError("Select a save first")
        for slot, path, _modified in self._saves():
            if slot == self.save_slot and Path(path).resolve().is_relative_to(self.profile_path):
                return Path(path).resolve()
        raise ValueError("The selected save is no longer available")

    def _profile_sii(self):
        _label, path = self._profile()
        target = path / "profile.sii"
        if not target.is_file() or not target.resolve().is_relative_to(self._game()):
            raise ValueError("Profile data file is unavailable")
        return target

    def _text(self, value, field, maximum=120):
        if not isinstance(value, str):
            raise ValueError(f"Invalid {field}")
        value = value.strip()
        if not value or len(value) > maximum or "\x00" in value:
            raise ValueError(f"Invalid {field}")
        return value

    def bootstrap(self):
        appearance_path = self.core.editor_storage_root() / "settings" / "ui.json"
        try:
            saved = json.loads(appearance_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            saved = None
        appearance = self.core.sanitize_appearance(saved)
        return {"title": "TR-EURO-EDITOR", "translations": self.core.T,
                "themes": self.core.THEME_PRESETS, "theme_names_fa": self.core.THEME_NAMES_FA,
                "theme_fields": self.core.THEME_FIELDS, "appearance": appearance,
                "theme_css": self.theme_css(appearance), "tuning_trucks": self.core.TUNING_TRUCKS,
                "preset_fields": self.core.TUNING_PRESET_FIELDS, "skills": list(self.core.SKILLS), "skill_labels": self.core.SKILL_LABELS,
                "adr_classes": self.core.ADR_CLASSES, "adr_fa": self.core.ADR_FA, "config_keys": self.core.CONFIG_KEYS,
                "language": self.language}

    def state(self):
        discovered = list(self.core.find_game_dirs())
        known = {str(Path(p).resolve()).casefold() for p in self.games}
        self.games.extend(p for p in discovered if str(Path(p).resolve()).casefold() not in known)
        game_options = [str(Path(p)) for p in self.games]
        game = Path(self.games[self.game_index]) if 0 <= self.game_index < len(self.games) else None
        profiles = self.core.list_profiles(game) if game else []
        profile_rows = [{"index": i, "name": label} for i, (label, _path) in enumerate(profiles)]
        selected_profile_index = None
        selected_profile_name = None
        saves = []
        if self.profile_path is not None:
            for i, (label, path) in enumerate(profiles):
                if Path(path).resolve() == self.profile_path:
                    selected_profile_index, selected_profile_name = i, label
                    saves = self.core.list_saves(path)
                    break
            if selected_profile_index is None:
                self.profile_path = None
                self.profile_label = None
                self.save_slot = None
        save_rows = [{"index": i, "slot": slot,
                      "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(modified))}
                     for i, (slot, _path, modified) in enumerate(saves)]
        selected_save_index = next((i for i, (slot, _p, _m) in enumerate(saves)
                                    if slot == self.save_slot), None)
        summary, fleet, backup, config, mods = None, None, None, None, []
        selected_path = None
        errors = {}
        if selected_save_index is not None:
            selected_path = str(Path(saves[selected_save_index][1]))
            try:
                loaded = self.core.SaveFile(selected_path)
                values = loaded.read()
                rank = self.core.rank_for_xp(values.get("xp", 0))
                summary = {"format": loaded.kind, "encrypted": loaded.was_encrypted,
                           "writable": self.core.save_writes_supported(loaded.kind, loaded.was_encrypted),
                           "values": values,
                           "rank": {"level": rank[0], "name": rank[1]},
                           "modified": time.strftime("%Y-%m-%d %H:%M:%S",
                                                       time.localtime(Path(selected_path).stat().st_mtime))}
                try:
                    fleet = self.core.extract_fleet(loaded)
                except Exception as exc:
                    errors["fleet"] = str(exc)
                latest = self.core.latest_backup(selected_path)
                backup = {"path": str(latest), "exists": bool(latest and latest.is_file())}
                backup["history"] = [{"name": str(p.name), "modified": time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))}
                    for p in self.core.backup_history(selected_path)]
            except Exception as exc:
                errors["save"] = str(exc)
        if game:
            try:
                config = self.core.read_config(game)
            except Exception as exc:
                errors["config"] = str(exc)
        if selected_profile_index is not None:
            try:
                mods = self._mod_rows()
            except Exception as exc:
                errors["mods"] = str(exc)
        return {"game_options": game_options, "game_index": self.game_index if game else None,
                "game_path": str(game) if game else None, "profiles": profile_rows,
                "profile_index": selected_profile_index, "profile_name": selected_profile_name,
                "saves": save_rows, "save_index": selected_save_index,
                "save_path": selected_path, "summary": summary, "fleet": fleet,
                "backup": backup, "config": config, "mods": mods, "errors": errors}

    def select_game(self, index):
        self.game_index = self._index(index, len(self.games), "game folder")
        self.profile_path = None
        self.profile_label = None
        self.save_slot = None
        return self.state()

    def choose_game_folder(self):
        if self._window is None:
            raise RuntimeError("Native folder picker is unavailable")
        import webview
        selected = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not selected:
            return self.state()
        candidate = Path(selected[0]).resolve()
        if not candidate.is_dir() or not any((candidate / name).is_dir()
                                             for name in ("profiles", "steam_profiles")):
            raise ValueError("Selected folder is not an ETS2 Documents data folder")
        if str(candidate) not in [str(Path(g).resolve()) for g in self.games]:
            self.games.append(candidate)
        self.game_index = next(i for i, g in enumerate(self.games) if Path(g).resolve() == candidate)
        self.profile_path = None
        self.save_slot = None
        return self.state()

    def select_profile(self, index):
        profiles = self._profiles()
        index = self._index(index, len(profiles), "profile")
        self.profile_label, self.profile_path = profiles[index][0], Path(profiles[index][1]).resolve()
        self.save_slot = None
        return self.state()

    def select_save(self, index):
        saves = self._saves()
        index = self._index(index, len(saves), "save")
        self.save_slot = saves[index][0]
        return self.state()

    def set_language(self, index):
        if isinstance(index, bool) or index not in (0, 1):
            raise ValueError("Invalid language")
        self.language = index
        return {"language": self.language}

    def set_theme(self, appearance):
        if not isinstance(appearance, dict):
            raise ValueError("Invalid theme settings")
        clean = self.core.sanitize_appearance(appearance)
        path = self.core.editor_storage_root() / "settings" / "ui.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return clean

    def _save_edits(self, values, unlock_cities=False, extras=(), vehicle_ids=None):
        if not isinstance(values, dict) or not set(values).issubset(
                {"money", "xp", "adr", *self.core.SKILLS}):
            raise ValueError("Unsupported save fields")
        clean = {}
        ranges = {"money": (0, self.core.MAX_MONEY), "xp": (0, self.core.INT32_MAX),
                  "adr": (0, 63), **{k: (0, 6) for k in self.core.SKILLS}}
        for key, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"Invalid {key}")
            low, high = ranges[key]
            if not low <= value <= high:
                raise ValueError(f"{key} is outside supported range")
            clean[key] = value
        if not isinstance(unlock_cities, bool):
            raise ValueError("Invalid city option")
        if not isinstance(extras, (list, tuple)) or any(x not in ("dealers", "repair", "refuel") for x in extras):
            raise ValueError("Unsupported extra operation")
        save = self._save()
        if not self.core.save_writes_supported(self.core.SaveFile(save).kind,
                                               self.core.SaveFile(save).was_encrypted):
            raise ValueError("This save format is read-only")
        if self.core.game_running():
            raise ValueError("Close ETS2 before editing the save")
        backup = self.core.edit_save(save, clean, unlock_cities, extras, vehicle_ids)
        return {"backup": str(backup), "state": self.state()}

    def apply_save_edits(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"values", "unlock_cities", "extras"}:
            raise ValueError("Invalid edit request")
        return self._save_edits(payload.get("values", {}), payload.get("unlock_cities", False),
                                payload.get("extras", ()))

    def fleet_action(self, action, item_index=None, group=False, kind="truck"):
        if action not in ("repair", "refuel") or not isinstance(group, bool):
            raise ValueError("Invalid fleet operation")
        state = self.state()
        fleet = state.get("fleet")
        if not fleet:
            raise ValueError("Fleet data is unavailable")
        candidates = fleet["trucks"] + (fleet["trailers"] if action == "repair" and
                                       fleet.get("trailers_available") else [])
        if group:
            chosen = [item for item in candidates if item.get(
                "repair_supported" if action == "repair" else "refuel_supported")]
        else:
            if kind not in ("truck", "trailer") or (kind == "trailer" and action != "repair"):
                raise ValueError("Invalid vehicle type")
            typed = fleet["trucks"] if kind == "truck" else fleet["trailers"]
            item_index = self._index(item_index, len(typed), "vehicle")
            chosen = [typed[item_index]]
        if action == "refuel":
            chosen = [item for item in chosen if item in fleet["trucks"] and item.get("refuel_supported")]
        else:
            chosen = [item for item in chosen if item.get("repair_supported")]
        if not chosen:
            raise ValueError("No selected owned vehicle has supported fields")
        return self._save_edits({}, extras=[action], vehicle_ids=[x["id"] for x in chosen])

    def restore_backup(self, index=0):
        save = self._save()
        backups = self.core.backup_history(save)
        index = self._index(index, len(backups), "backup")
        if self.core.game_running():
            raise ValueError("Close ETS2 before restoring a backup")
        self.core.restore_save_backup(save, backups[index])
        return self.state()

    def _mod_rows(self):
        profile = self._profile_sii()
        ps = self.core.ProfileSii(profile)
        active = ps.get_mods()
        base = self._game()
        paths = self.core.list_mod_paths(base)
        by_id = {self.core.mod_id(p): p for p in paths}
        rows, seen = [], set()
        for name in active:
            ident = Path(name).stem
            path = by_id.get(ident)
            rows.append({"name": name, "active": True,
                         "profile_name": name, "file": path.name if path else None,
                         "size": path.stat().st_size if path and path.is_file() else None,
                         "available": bool(path)})
            seen.add(ident)
        for path in paths:
            ident = self.core.mod_id(path)
            if ident not in seen:
                rows.append({"name": ident, "active": False,
                             "file": path.name, "size": path.stat().st_size if path.is_file() else None,
                             "available": True})
        return rows

    def set_mod_active(self, index, active):
        if not isinstance(active, bool):
            raise ValueError("Invalid mod state")
        rows = self._mod_rows()
        index = self._index(index, len(rows), "mod")
        row = rows[index]
        _label, profile = self._profile()
        current = self.core.ProfileSii(profile / "profile.sii").get_mods()
        ident = Path(row["name"]).stem
        if row["active"] == active:
            return self.state()
        if active:
            available = {self.core.mod_id(p) for p in self.core.list_mod_paths(self._game())}
            if ident not in available:
                raise ValueError("Mod file is not present in the local mod folder")
            current.append(row["profile_name"])
        else:
            current = [name for name in current if Path(name).stem != ident]
        self.core.write_profile_mods(profile, current)
        return self.state()

    def move_mod(self, index, direction):
        if direction not in (-1, 1) or isinstance(direction, bool):
            raise ValueError("Invalid order change")
        rows = [row for row in self._mod_rows() if row["active"]]
        index = self._index(index, len(rows), "active mod")
        _label, profile = self._profile()
        current = self.core.ProfileSii(profile / "profile.sii").get_mods()
        new_index = index + direction
        if not 0 <= new_index < len(current):
            return self.state()
        current[index], current[new_index] = current[new_index], current[index]
        self.core.write_profile_mods(profile, current)
        return self.state()

    def add_mod(self):
        if self._window is None:
            raise RuntimeError("Native file picker is unavailable")
        import webview
        selected = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
                    file_types=("ETS2 mod archives (*.scs;*.zip)", "*.scs;*.zip"))
        if not selected:
            return self.state()
        source = Path(selected[0]).resolve()
        if not source.is_file() or source.suffix.lower() not in (".scs", ".zip"):
            raise ValueError("Choose an .scs or .zip mod archive")
        dest_dir = self.core.mod_folder(self._game())
        dest_dir.mkdir(parents=True, exist_ok=True)
        destination = dest_dir / source.name
        self.core.copy_mod_file(source, destination, overwrite=False)
        return self.state()

    def validate_mod(self, index):
        rows = self._mod_rows()
        index = self._index(index, len(rows), "mod")
        row = rows[index]
        if not row["available"]:
            raise ValueError("Mod file is unavailable locally")
        path = self.core.mod_folder(self._game()) / row["file"]
        if not path.resolve().is_relative_to(self.core.mod_folder(self._game()).resolve()):
            raise ValueError("Invalid mod location")
        verdict, details = self.core.validate_mod(path, self._game(), self.core.detect_game_version(self._game()))
        return {"verdict": verdict, "details": details}

    def save_game_settings(self, values):
        if not isinstance(values, dict) or set(values) - set(self.core.CONFIG_KEYS):
            raise ValueError("Unsupported game settings")
        clean = {}
        for key, value in values.items():
            if not isinstance(value, str) or value not in ("0", "1", "2"):
                raise ValueError("Invalid config value")
            if key != "savefmt" and value not in ("0", "1"):
                raise ValueError("Invalid config toggle")
            clean[self.core.CONFIG_KEYS[key]] = value
        self.core.write_config(self._game(), clean)
        return self.state()

    def log_tail(self):
        path = self.core.game_log_path(self._game())
        if not path.is_file():
            return {"path": str(path), "text": None}
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
        return {"path": str(path), "text": "\n".join(lines)}

    def tuning_presets(self):
        path = self.core.editor_storage_root() / "settings" / "tuning_presets.json"
        return self.core.load_tuning_presets(path)

    def save_tuning_preset(self, name, values):
        name = self._text(name, "preset name", 48)
        if not isinstance(values, dict) or set(values) != set(self.core.TUNING_PRESET_FIELDS):
            raise ValueError("Invalid tuning preset")
        # Round-trip through the existing preset estimator/range validation before persistence.
        numeric = [values.get(key) for key in self.core.TUNING_PRESET_FIELDS if key != "trucks"]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in numeric):
            raise ValueError("Invalid tuning preset values")
        if not isinstance(values.get("trucks"), list) or any(x not in self.core.TUNING_TRUCKS
                                                               for x in values["trucks"]):
            raise ValueError("Invalid tuning truck selection")
        presets = self.tuning_presets()
        presets[name] = values
        self.core.save_tuning_presets(self.core.editor_storage_root() / "settings" / "tuning_presets.json", presets)
        return presets

    def build_tuning_mod(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"name", "trucks", "hp", "torque", "rpm",
                "speed", "gears", "differential", "engine_brake", "retarder", "compatible_versions"}:
            raise ValueError("Invalid tuning request")
        trucks = payload.get("trucks")
        if not isinstance(trucks, list) or not trucks or any(x not in self.core.TUNING_TRUCKS for x in trucks):
            raise ValueError("Select supported truck definitions")
        def number(key, low, high):
            value = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"Invalid {key}")
            return float(value)
        hp = number("hp", 50, 5000)
        torque = number("torque", 100, 30000)
        rpm = number("rpm", 500, 10000)
        speed = number("speed", 20, 1000)
        diff = number("differential", 0.1, 20)
        engine_brake = number("engine_brake", 0, 20)
        gears = payload.get("gears")
        retarder = payload.get("retarder")
        if isinstance(gears, bool) or not isinstance(gears, int) or not 6 <= gears <= 18:
            raise ValueError("Invalid gear count")
        if isinstance(retarder, bool) or not isinstance(retarder, int) or not 0 <= retarder <= 3:
            raise ValueError("Invalid retarder")
        versions = payload.get("compatible_versions", [])
        if not isinstance(versions, list) or len(versions) > 20 or any(not isinstance(v, str) or len(v) > 40 for v in versions):
            raise ValueError("Invalid compatible versions")
        name = self._text(payload.get("name", "ETS2SE_Tune"), "mod name", 64)
        if not re.fullmatch(r"[A-Za-z0-9 _.-]+", name):
            raise ValueError("Mod name may use letters, numbers, spaces, dash, dot and underscore only")
        outdir = self.core.editor_storage_root() / "generated_mods"
        outdir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        destination = outdir / f"{name}_{stamp}.scs"
        count = self.core.create_tuning_mod(destination, trucks, hp, torque, rpm, gears, speed,
                    diff, engine_brake, retarder, versions)
        return {"path": str(destination), "files": count, "compatibility": "static_only"}

    def estimate_tuning(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Invalid estimate request")
        def positive(key, lo, hi):
            value=payload.get(key)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not lo<=value<=hi:
                raise ValueError("Invalid " + key)
            return float(value)
        return self.core.estimate_tuning_performance(positive("hp",50,5000), positive("torque",100,30000),
            positive("rpm",500,10000), positive("speed",20,1000), positive("differential",0.1,20),
            positive("mass",1000,200000))
    def theme_css(self, appearance=None):
        """Generate offline CSS tokens from the canonical Python theme palette."""
        if appearance is None:
            appearance = self.bootstrap()["appearance"]
        appearance = self.core.sanitize_appearance(appearance)
        roles = tuple(self.core.THEME_FIELDS) + ("accent_foreground", "accent_text", "nav_selected")
        def rule(selector, theme, mode, accent=None):
            palette = self.core.ui_palette({"theme": theme, "mode": mode,
                                            "custom_accent": accent})
            props = []
            for role in roles:
                if role in palette:
                    props.append(f"--{role.replace('_', '-')}: {palette[role]};")
            return selector + "{" + "".join(props) + "}"
        rules = [rule(":root", appearance["theme"], appearance["mode"], appearance["custom_accent"])]
        for theme_id in self.core.THEME_PRESETS:
            for mode in ("night", "day"):
                rules.append(rule(f'[data-theme="{theme_id}"][data-mode="{mode}"]', theme_id, mode))
        return "\n".join(rules)

    def set_all_mods_active(self, active):
        if not isinstance(active, bool):
            raise ValueError("Invalid bulk mod state")
        rows = self._mod_rows()
        _label, profile = self._profile()
        current = self.core.ProfileSii(profile / "profile.sii").get_mods()
        if active:
            available = self.core.list_mod_paths(self._game())
            current_ids = {Path(name).stem for name in current}
            current.extend(p.name for p in available if self.core.mod_id(p) not in current_ids)
        else:
            current = []
        self.core.write_profile_mods(profile, current)
        return self.state()

    def suggest_tuning(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Invalid tuning target")
        def positive(key, low, high):
            value = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("Invalid " + key)
            return float(value)
        return self.core.recommend_engine_for_targets(
            positive("speed", 20, 1000), positive("accel", 1, 1000),
            positive("mass", 1000, 200000), positive("rpm", 500, 10000))

