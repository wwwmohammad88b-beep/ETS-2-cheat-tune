#!/usr/bin/env python3
"""
ETS2 Save Editor  -  single-player Euro Truck Simulator 2 save editor.

Edits: money, XP, skills (ADR + 5 skill trees) and visited cities.
Reads all three .sii formats the game uses: encrypted (ScsC), binary (BSII)
and plain text (SiiNunit).  Changed files are written as unencrypted files,
which the game loads fine.  A backup is made before every write.

Single-player only. Do not use on online/multiplayer profiles.
"""
import os
import re
import sys
import struct
import zlib
import shutil
import time
import subprocess
import json
import math
import hashlib
import zipfile
import tempfile
import uuid
import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import PurePosixPath
from pathlib import Path

APP = "TR-EURO-EDITOR"
VERSION = "2.6"


def configure_error_logging():
    """Set up a per-user rotating log for startup and Tk callback failures."""
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) if local else Path.home() / ".local" / "share"
    log_dir = root / APP / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "application.log"
        handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3,
                                      encoding="utf-8")
    except OSError:
        log_dir = Path(tempfile.gettempdir()) / APP / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "application.log"
        handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3,
                                      encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(message)s", force=True)
    return log_path

TUNING_TRUCKS = {
    "Mercedes Actros 2014": "mercedes.actros2014",
    "Volvo FH 2024": "volvo.fh_2024",
    "Scania S 2024e": "scania.s_2024e",
}


def tuning_template_root():
    """Find bundled engine definitions in source and PyInstaller builds."""
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / "tuning_templates"


def _scale_scs_number(value, factor, integer=False):
    scaled = float(value) * factor
    if integer:
        if scaled == 0:
            return "0"
        return str(max(1, int(round(scaled))))
    return ("%.4f" % scaled).rstrip("0").rstrip(".")


def tune_engine_definition(text, target_hp, target_torque, target_rpm):
    """Apply absolute engine targets; no arbitrary gameplay caps are imposed."""
    mtorque = re.search(r"(?m)^\s*torque:\s*([\d.]+)", text)
    mhp = re.search(r'info\[\]:\s*"(\d+(?:@@dg@@\d{3})?).*?@@hp@@', text)
    mrpm = re.search(r"(?m)^\s*rpm_limit:\s*(\d+)", text)
    base_torque = float(mtorque.group(1)) if mtorque else target_torque
    base_hp = float(mhp.group(1).replace("@@dg@@", "")) if mhp else target_hp
    base_rpm = float(mrpm.group(1)) if mrpm and int(mrpm.group(1)) else target_rpm
    torque_factor = target_torque / base_torque
    rpm_factor = target_rpm / base_rpm
    curve_factor = (target_hp / base_hp) / (torque_factor * rpm_factor)
    out = []
    info_line = 0
    for line in text.splitlines(keepends=True):
        if re.match(r"\s*torque:\s*", line):
            line = re.sub(r"(\btorque:\s*)([-+]?\d+(?:\.\d+)?)",
                          lambda m: m.group(1) + _scale_scs_number(target_torque, 1),
                          line, count=1)
        elif re.match(r"\s*(?:secondary_)?torque_curve\[\]:", line):
            def scale_curve(match):
                rpm = _scale_scs_number(match.group(1), rpm_factor, integer=True)
                curve = _scale_scs_number(match.group(2), curve_factor)
                return "(" + rpm + ", " + curve + ")"
            line = re.sub(r"\(\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*\)",
                          scale_curve, line, count=1)
        elif re.match(r"\s*rpm_\w+\s*:", line):
            line = re.sub(r"([-+]?\d+(?:\.\d+)?)",
                lambda m: _scale_scs_number(m.group(1), rpm_factor, integer=True),
                          line, flags=re.I)
        elif re.match(r"\s*info\[\]:\s*\"", line):
            line_index = info_line
            info_line += 1
            replacements = [target_hp, int(round(target_hp * 0.7457))] if line_index == 0 else (
                [target_torque] if line_index == 1 else None)
            replacement_i = [0]
            def scale_display(match):
                old = int(match.group(1) + (match.group(2) or ""))
                if replacements is not None and replacement_i[0] < len(replacements):
                    new = int(round(replacements[replacement_i[0]]))
                    replacement_i[0] += 1
                else:
                    new = 0 if old == 0 else max(1, int(round(old * rpm_factor)))
                digits = str(new)
                if len(digits) > 3:
                    return digits[:-3] + "@@dg@@" + digits[-3:]
                return digits
            line = re.sub(r"(\d+)(?:@@dg@@(\d{3}))?", scale_display, line)
        out.append(line)
    return "".join(out)


def _engine_template_for(truck_id):
    source = tuning_template_root() / "def" / "vehicle" / "truck" / truck_id / "engine"
    if not source.is_dir():
        raise FileNotFoundError("Engine templates are missing for " + truck_id)
    candidates = []
    for path in source.glob("*.sii"):
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r'info\[\]:\s*"(\d+(?:@@dg@@\d{3})?).*?@@hp@@', text)
        hp = int(match.group(1).replace("@@dg@@", "")) if match else 0
        if hp:
            candidates.append((hp, path, text))
    if not candidates:
        raise ValueError("No usable engine definition found for " + truck_id)
    return max(candidates, key=lambda item: item[0])[2]


def tune_engine_accessory(text, truck_id, part_id, target_hp, target_torque,
                          target_rpm, engine_brake):
    """Create one new purchasable engine accessory without replacing stock engines."""
    text = tune_engine_definition(text, target_hp, target_torque, target_rpm)
    unit_id = f"ets2se_{part_id}.{truck_id}.engine"
    text, count = re.subn(r"(?m)^(\s*accessory_engine_data\s*:\s*)[^\s{]+",
                          lambda m: m.group(1) + unit_id, text, count=1)
    if count != 1:
        raise ValueError("Could not identify the engine accessory unit.")
    text = re.sub(r'(?m)^(\s*name:\s*)"[^"\r\n]*"',
                  lambda m: m.group(1) + '"ETS2 Save Editor %s hp / %s Nm"' %
                  (int(round(target_hp)), int(round(target_torque))), text, count=1)
    text = re.sub(r"(?m)^(\s*price:\s*)\d+",
                  lambda m: m.group(1) + "25000", text, count=1)
    text = re.sub(r"(?m)^\s*part_type:\s*\w+\s*\n", "", text)
    text = re.sub(r"(?m)^(\s*icon:\s*.*)$", r"\1\n\tpart_type: aftermarket", text, count=1)
    text = re.sub(r"(?m)^(\s*engine_brake:\s*)[-+\d.]+",
                  lambda m: m.group(1) + ("%.3f" % engine_brake).rstrip("0").rstrip("."),
                  text, count=1)
    return text


def create_transmission_accessory(truck_id, part_id, gear_count, top_speed_kmh,
                                  rpm_limit, differential, retarder_level):
    """Build a new purchasable transmission accessory with derived gear ratios."""
    if not 6 <= int(gear_count) <= 18:
        raise ValueError("Gear count must be between 6 and 18.")
    if min(top_speed_kmh, rpm_limit, differential) <= 0:
        raise ValueError("Transmission targets must be positive.")
    if not 0 <= retarder_level <= 3:
        raise ValueError("Retarder level must be between 0 and 3.")
    wheel_radius = 0.53  # Approximation for a common 22.5-inch truck tire.
    wheel_speed_ratio = rpm_limit * (2 * math.pi * wheel_radius) * 60 / (top_speed_kmh * 1000)
    top_gear = wheel_speed_ratio / differential
    if not math.isfinite(top_gear) or top_gear <= 0:
        raise ValueError("Could not derive a valid top gear ratio.")
    # Spread the remaining gears over a practical heavy-truck reduction range.
    low_gear = top_gear * 14.0
    ratios = [low_gear * ((top_gear / low_gear) ** (i / (int(gear_count) - 1)))
              for i in range(int(gear_count))]
    unit_id = f"ets2se_{part_id}.{truck_id}.transmission"
    lines = [
        "SiiNunit", "{",
        f"accessory_transmission_data : {unit_id} {{",
        f'\tname: "ETS2 Save Editor {gear_count}-speed / {top_speed_kmh:.0f} km/h"',
        "\tshort_name: \"ETS2 SE\"",
        "\tprice: 18000",
        "\tunlock: 0",
        '\ticon: "transmission_generic"',
        "\tpart_type: aftermarket",
        f"\tdifferential_ratio: {differential:.4f}",
    ]
    # SCS transmission arrays use indexed ratios_reverse and ratios_forward.
    lines.append("\tratios_reverse[0]: -11.0")
    for i, ratio in enumerate(ratios):
        lines.append(f"\tratios_forward[{i}]: {ratio:.5f}")
    lines.extend([f"\tretarder: {retarder_level}"])
    if retarder_level:
        for i in range(retarder_level):
            strength = (i + 1) / retarder_level
            lines.append(f"\tauxiliary_brakes[]: ({strength:.3f}, 1, 0)")
    lines.extend(["}", "}", ""])
    return "\n".join(lines)


def estimate_tuning_performance(hp, torque, rpm_limit, target_speed_kmh,
                                differential, mass_kg):
    """Return rough physics estimates; never presented as guaranteed in-game results."""
    wheel_radius = 0.53
    top_gear = (rpm_limit * (2 * math.pi * wheel_radius) * 60 /
                (target_speed_kmh * 1000 * differential))
    gear_limited = (rpm_limit * (2 * math.pi * wheel_radius) * 60 /
                    (top_gear * differential * 1000))
    power_w = hp * 745.7 * 0.84
    rho, cda, rolling = 1.2, 6.0, 0.008
    low, high = 0.0, 150.0
    for _ in range(48):
        speed = (low + high) / 2
        drag_power = 0.5 * rho * cda * speed ** 3
        rolling_power = rolling * mass_kg * 9.81 * speed
        if drag_power + rolling_power <= power_w:
            low = speed
        else:
            high = speed
    power_limited = low * 3.6
    top_speed = min(gear_limited, power_limited)
    zero_to_100_ms = 100 / 3.6
    kinetic_energy = 0.5 * mass_kg * zero_to_100_ms ** 2
    resistance_power = (rolling * mass_kg * 9.81 * (zero_to_100_ms / 2) +
                        0.5 * rho * cda * (zero_to_100_ms / 2) ** 3)
    usable_power = max(1.0, power_w - resistance_power)
    accel_seconds = kinetic_energy / usable_power
    return {"top_speed": top_speed, "zero_to_100": accel_seconds,
            "gear_limited": gear_limited, "power_limited": power_limited,
            "top_gear": top_gear}


TUNING_PRESET_FIELDS = ("hp", "torque", "rpm", "target_speed", "target_accel",
                        "mass", "gear_count", "differential", "engine_brake",
                        "retarder", "trucks")


def load_tuning_presets(path):
    """Load named local tuning setups; malformed files are treated as empty."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {name: values for name, values in data.items()
            if isinstance(name, str) and isinstance(values, dict)
            and all(key in values for key in TUNING_PRESET_FIELDS)}


def save_tuning_presets(path, presets):
    """Write local tuning setups atomically, preserving the prior file on failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(presets, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def estimate_tuning_preset(preset):
    return estimate_tuning_performance(
        float(preset["hp"]), float(preset["torque"]), float(preset["rpm"]),
        float(preset["target_speed"]), float(preset["differential"]), float(preset["mass"]))


def recommend_engine_for_targets(target_speed_kmh, zero_to_100_s, mass_kg, rpm_peak):
    """Suggest engine targets from the user's requested 0–100 time and mass."""
    v100 = 100 / 3.6
    vavg = v100 / 2
    kinetic_power = 0.5 * mass_kg * v100 ** 2 / max(1.0, zero_to_100_s)
    road_load = (0.008 * mass_kg * 9.81 * vavg +
                 0.5 * 1.2 * 6.0 * vavg ** 3)
    accel_w = kinetic_power + road_load
    target_speed = target_speed_kmh / 3.6
    top_speed_w = (0.008 * mass_kg * 9.81 * target_speed +
                   0.5 * 1.2 * 6.0 * target_speed ** 3)
    hp = max(accel_w, top_speed_w) / 0.84 / 745.7
    torque = hp * 7127 / max(1.0, rpm_peak)
    return {"hp": hp, "torque": torque, "rpm": rpm_peak,
            "target_speed": target_speed_kmh}


def validate_generated_tuning_mod(path, selected_trucks, part_id):
    """Check archive CRCs, safe member paths, required files, and generated IDs.

    This static package check cannot confirm that the game accepts the SCS units.
    """
    with zipfile.ZipFile(path, "r") as archive:
        infos = [item for item in archive.infolist() if not item.is_dir()]
        names = [item.filename for item in infos]
        if len(names) != len(set(names)):
            raise SiiError("generated archive contains duplicate member paths")
        for name in names:
            parsed = PurePosixPath(name)
            if "\\" in name or parsed.is_absolute() or ".." in parsed.parts or not parsed.parts:
                raise SiiError("generated archive contains an unsafe path: " + name)
        if "manifest.sii" not in names:
            raise SiiError("generated archive has no root manifest.sii")
        bad = archive.testzip()
        if bad:
            raise SiiError("generated archive failed CRC validation: " + bad)
        manifest_text = archive.read("manifest.sii").decode("utf-8", "strict")
        manifest = parse_manifest(manifest_text)
        if not manifest_text.startswith("SiiNunit") or any(
                not manifest.get(key) for key in ("package_version", "display_name", "author")):
            raise SiiError("generated manifest is incomplete")
        if not re.search(r"(?m)^mod_package\s*:\s*\.[A-Za-z0-9_]+\s*\{", manifest_text):
            raise SiiError("generated manifest package declaration is missing")
        for display_name, truck_id in TUNING_TRUCKS.items():
            if display_name not in selected_trucks:
                continue
            for folder, unit_class, suffix in (
                    ("engine", "accessory_engine_data", "engine"),
                    ("transmission", "accessory_transmission_data", "transmission")):
                member = f"def/vehicle/truck/{truck_id}/{folder}/ets2se_{part_id}.sii"
                if member not in names:
                    raise SiiError("generated archive is missing " + member)
                text = archive.read(member).decode("utf-8", "strict")
                expected = f"{unit_class} : ets2se_{part_id}.{truck_id}.{suffix}"
                if not text.startswith("SiiNunit") or expected not in text:
                    raise SiiError("generated definition failed static validation: " + member)
                if text.count("{") != text.count("}"):
                    raise SiiError("generated definition has unbalanced braces: " + member)
    return True


def create_tuning_mod(destination, selected_trucks, target_hp, target_torque, target_rpm,
                      gear_count=12, target_speed=120, differential=2.64,
                      engine_brake=1.5, retarder_level=3, compatible_versions=()):
    """Create a ZIP-format SCS mod with new selectable engines and transmissions."""
    template_root = tuning_template_root()
    entries = []
    part_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    for display_name, truck_id in TUNING_TRUCKS.items():
        if display_name not in selected_trucks:
            continue
        relbase = f"def/vehicle/truck/{truck_id}"
        engine = tune_engine_accessory(_engine_template_for(truck_id), truck_id, part_id,
                                       target_hp, target_torque, target_rpm, engine_brake)
        entries.append((f"{relbase}/engine/ets2se_{part_id}.sii", engine))
        transmission = create_transmission_accessory(
            truck_id, part_id, gear_count, target_speed, target_rpm, differential, retarder_level)
        entries.append((f"{relbase}/transmission/ets2se_{part_id}.sii", transmission))
    if not entries:
        raise ValueError("Select at least one truck.")

    package_id = re.sub(r"[^A-Za-z0-9_]", "_", Path(destination).stem)
    versions = [str(version).strip() for version in compatible_versions if str(version).strip()]
    if any(not re.fullmatch(r"\d+\.\d+(?:\.\d+){0,2}(?:\.\*)?", value)
           for value in versions):
        raise ValueError("Enter a numeric game version or wildcard such as 1.60.*")
    compatible_lines = "".join(' compatible_versions[]: "%s"\n' % version
                                for version in versions)
    manifest = ('''SiiNunit\n{\nmod_package : .%s {\n package_version: "1.0"\n'''
                ''' display_name: "ETS2 Save Editor Performance Tune"\n'''
                ''' author: "ETS2 Save Editor"\n category[]: "truck"\n%s}\n}\n''') % (
                    package_id, compatible_lines)
    description = (
        "Adds new purchasable engine and transmission accessories for selected ETS2 trucks.\n"
        "Target power: %.0f hp\nTarget torque: %.0f Nm\nTarget RPM limit: %.0f\n"
        "Gearbox: %d speeds, %.0f km/h target, %.2f differential\n\n"
        "The mod adds parts to the upgrade shop. It does not install them on a truck. "
        "Actual performance depends on vehicle, tires, cargo, road, and physics.\n"
        "Static package checks passed; in-game compatibility has not been confirmed.\n" %
        (target_hp, target_torque, target_rpm, gear_count, target_speed, differential)
    )
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            created = True
            archive.writestr("manifest.sii", manifest)
            archive.writestr("mod_description.txt", description)
            for name, contents in entries:
                archive.writestr(name, contents.encode("utf-8"))
        validate_generated_tuning_mod(destination, selected_trucks, part_id)
    except Exception:
        if created and destination.exists():
            destination.unlink()
        raise
    return len(entries)

# Public AES key used by the game for encrypted .sii files.
KEY = bytes([
    0x2a, 0x5f, 0xcb, 0x17, 0x91, 0xd2, 0x2f, 0xb6, 0x02, 0x45, 0xb3, 0xd8,
    0x36, 0x9e, 0xd0, 0xb2, 0xc2, 0x73, 0x71, 0x56, 0x3f, 0xbf, 0x1f, 0x3c,
    0x9e, 0xdf, 0x6b, 0x11, 0x82, 0x5a, 0x5d, 0x0a])

SKILLS = ["long_dist", "heavy", "fragile", "urgent", "mechanical"]
SKILL_LABELS = {
    "long_dist": "Long distance", "heavy": "High value cargo",
    "fragile": "Fragile cargo", "urgent": "Just-in-time delivery",
    "mechanical": "Eco driving"}
# NOTE: in the save file "heavy" is the High-value-cargo tree and "urgent"
# is Just-in-time; labels follow the in-game skill names.
ADR_CLASSES = [(1, "Class 1 - Explosives"), (2, "Class 2 - Gases"),
               (4, "Class 3 - Flammable liquids"),
               (8, "Class 4 - Flammable solids"),
               (16, "Class 6 - Toxic"), (32, "Class 8 - Corrosive")]

# (first level of tier, rank name, XP required) - from the ETS2 level table.
LEVEL_TIERS = [
    (0, "Newbie", 0), (5, "Enthusiast", 3400), (10, "Skilled Worker", 11900),
    (15, "Professional", 25400), (20, "Master", 44200),
    (25, "Instructor", 70200), (30, "Elite", 103700),
    (40, "King of the Road", 176700), (50, "Legend", 249700),
    (100, "Divine Champion", 575700)]
MAX_XP = 2000000
MAX_MONEY = 2000000000   # stays inside int32-safe range for the game UI
INT32_MAX = 2147483647


class SiiError(Exception):
    pass


# Central UI palettes.  UI colors are local assets: no network/theme service is
# used, and every preset provides the semantic roles consumed by ttk and the
# custom Tk/Canvas widgets.
THEME_PRESETS = {
    "midnight_cyan": {"name": "Midnight Cyan", "mode": "night", "background": "#080f18", "surface": "#101a25", "card": "#142230", "sidebar": "#0a141e", "text_primary": "#eef7fc", "text_secondary": "#a6bac8", "accent": "#00e5ff", "accent_hover": "#65d0ff", "border": "#263c4c", "success": "#34d399", "warning": "#fbbf24", "error": "#fb7185", "gauge_background": "#253644", "gauge_foreground": "#dff6ff", "input_background": "#0b1520", "input_foreground": "#eef7fc"},
    "racing_red": {"name": "Racing Red", "mode": "night", "background": "#100d10", "surface": "#1c171b", "card": "#241d22", "sidebar": "#151115", "text_primary": "#fff4f5", "text_secondary": "#c1adb1", "accent": "#ff3b30", "accent_hover": "#ff6975", "border": "#433038", "success": "#43d6a1", "warning": "#ffc65c", "error": "#ff6875", "gauge_background": "#3b2c32", "gauge_foreground": "#fff0f1", "input_background": "#181316", "input_foreground": "#fff4f5"},
    "arctic_blue": {"name": "Arctic Blue", "mode": "night", "background": "#09131c", "surface": "#101f2b", "card": "#152936", "sidebar": "#0b1924", "text_primary": "#f0f9ff", "text_secondary": "#a9c1ce", "accent": "#3b82f6", "accent_hover": "#72c7ff", "border": "#294354", "success": "#3dd6b0", "warning": "#f4c46c", "error": "#ff7885", "gauge_background": "#29404e", "gauge_foreground": "#e6f7ff", "input_background": "#0b1924", "input_foreground": "#f0f9ff"},
    "emerald": {"name": "Emerald", "mode": "night", "background": "#0a1210", "surface": "#111e19", "card": "#172720", "sidebar": "#0d1813", "text_primary": "#effaf3", "text_secondary": "#afc6b6", "accent": "#10b981", "accent_hover": "#63e0a5", "border": "#2b4638", "success": "#42d392", "warning": "#f4c66f", "error": "#ff7f83", "gauge_background": "#2c4035", "gauge_foreground": "#e7fff1", "input_background": "#0c1712", "input_foreground": "#effaf3"},
    "purple_neon": {"name": "Purple Neon", "mode": "night", "background": "#100d19", "surface": "#1a1526", "card": "#211b30", "sidebar": "#141020", "text_primary": "#f7f2ff", "text_secondary": "#baafcc", "accent": "#a855f7", "accent_hover": "#c49aff", "border": "#403253", "success": "#43d7a5", "warning": "#f5c568", "error": "#ff748d", "gauge_background": "#3a3049", "gauge_foreground": "#f2eaff", "input_background": "#151020", "input_foreground": "#f7f2ff"},
    "amber_truck": {"name": "Amber Truck", "mode": "night", "background": "#13100b", "surface": "#201a10", "card": "#292116", "sidebar": "#19140d", "text_primary": "#fff8ea", "text_secondary": "#c9b99c", "accent": "#f59e0b", "accent_hover": "#ffc468", "border": "#4a3a22", "success": "#42ce98", "warning": "#ffd166", "error": "#ff7880", "gauge_background": "#473822", "gauge_foreground": "#fff4d9", "input_background": "#18130c", "input_foreground": "#fff8ea"},
    "silver_steel": {"name": "Silver Steel", "mode": "night", "background": "#101519", "surface": "#1a2228", "card": "#222c33", "sidebar": "#151d22", "text_primary": "#f2f6f8", "text_secondary": "#b2c0c8", "accent": "#94a3b8", "accent_hover": "#b1d7ea", "border": "#3a4851", "success": "#48c99c", "warning": "#edc46e", "error": "#f27d83", "gauge_background": "#3b484f", "gauge_foreground": "#edf5f8", "input_background": "#151d22", "input_foreground": "#f2f6f8"},
    "light_professional": {"name": "Light Professional", "mode": "day", "background": "#edf2f6", "surface": "#f6f8fa", "card": "#ffffff", "sidebar": "#e4ebf1", "text_primary": "#192936", "text_secondary": "#586c7b", "accent": "#3b82f6", "accent_hover": "#086b9f", "border": "#cbd7df", "success": "#168452", "warning": "#a56800", "error": "#bb3544", "gauge_background": "#d5e0e7", "gauge_foreground": "#253c4b", "input_background": "#ffffff", "input_foreground": "#192936"},
}
THEME_FIELDS = ("background", "surface", "card", "sidebar", "text_primary",
                "text_secondary", "accent", "accent_hover", "border", "success",
                "warning", "error", "gauge_background", "gauge_foreground",
                "input_background", "input_foreground")


def theme_colors(theme_id, mode=None, custom_accent=None):
    """Return a complete, validated palette for a built-in or user accent theme."""
    if theme_id not in THEME_PRESETS:
        theme_id = "midnight_cyan"
    colors = dict(THEME_PRESETS[theme_id])
    if mode in ("day", "night"):
        colors["mode"] = mode
        if mode != THEME_PRESETS[theme_id]["mode"]:
            light = mode == "day"
            colors.update({
                "background": "#edf2f6" if light else "#0b1117",
                "surface": "#f6f8fa" if light else "#121b24",
                "card": "#ffffff" if light else "#17232d",
                "sidebar": "#e4ebf1" if light else "#0d151d",
                "text_primary": "#192936" if light else "#edf4fa",
                "text_secondary": "#586c7b" if light else "#a0b0bc",
                "border": "#cbd7df" if light else "#2d3c47",
                "gauge_background": "#d5e0e7" if light else "#35434d",
                "gauge_foreground": "#253c4b" if light else "#edf4fa",
                "input_background": "#ffffff" if light else "#0d151d",
                "input_foreground": "#192936" if light else "#edf4fa",
            })
    if isinstance(custom_accent, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", custom_accent):
        colors["accent"] = custom_accent.lower()
        colors["accent_hover"] = _accent_hover(custom_accent)
    colors["accent_foreground"] = best_text_on(colors["accent"])
    return colors


def _color_luminance(value):
    rgb = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
    return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def _accent_hover(value):
    rgb = [int(value[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{min(255, int(v + (255 - v) * 0.18)):02x}" for v in rgb)


def contrast_ratio(a, b):
    """WCAG contrast ratio between two #rrggbb colours (1.0 .. 21.0)."""
    la, lb = _color_luminance(a), _color_luminance(b)
    hi, lo = (la, lb) if la >= lb else (lb, la)
    return (hi + 0.05) / (lo + 0.05)


def best_text_on(background, dark="#07111a", light="#ffffff"):
    """Pick whichever of the dark/light text colours is more readable on `background`."""
    return dark if contrast_ratio(background, dark) >= contrast_ratio(background, light) else light


def normalize_hex_color(value):
    """Return '#rrggbb' (lower case) for '#rgb'/'#rrggbb'/'rrggbb' input, else None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(r"#?[0-9a-fA-F]{3}", text):
        text = text.lstrip("#")
        text = "".join(ch * 2 for ch in text)
    elif re.fullmatch(r"#?[0-9a-fA-F]{6}", text):
        text = text.lstrip("#")
    else:
        return None
    return "#" + text.lower()


def blend_colors(first, second, amount):
    """Linear mix: amount=0 -> first, amount=1 -> second (both #rrggbb)."""
    amount = max(0.0, min(1.0, float(amount)))
    a = [int(first[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(second[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b))


# Legacy named accents used by earlier builds (migrated to custom_accent on load).
LEGACY_ACCENTS = {"Sky": "#38bdf8", "Amber": "#f59e0b", "Emerald": "#34d399",
                  "Violet": "#a78bfa", "Rose": "#fb7185", "Ice": "#e2e8f0"}
DEFAULT_THEME = "midnight_cyan"


def sanitize_appearance(data):
    """Validate saved UI settings; unknown/invalid values fall back to safe defaults.

    Returns {"theme", "mode", "custom_accent"}; custom_accent is None when the theme's own
    accent is used and mode is always "day" or "night".
    """
    data = data if isinstance(data, dict) else {}
    theme = data.get("theme")
    if theme not in THEME_PRESETS:
        theme = DEFAULT_THEME
    mode = data.get("mode")
    if mode not in ("day", "night"):
        mode = THEME_PRESETS[theme]["mode"]
    accent = normalize_hex_color(data.get("custom_accent"))
    if accent is None:
        accent = LEGACY_ACCENTS.get(data.get("accent"))
    return {"theme": theme, "mode": mode, "custom_accent": accent}


def readable_on(color, background, minimum=4.5):
    """Nudge `color` toward black/white until it reaches `minimum` contrast on `background`."""
    if contrast_ratio(color, background) >= minimum:
        return color
    target = "#ffffff" if _color_luminance(background) < 0.4 else "#000000"
    for step in range(1, 21):
        candidate = blend_colors(color, target, step / 20)
        if contrast_ratio(candidate, background) >= minimum:
            return candidate
    return target


def ui_palette(appearance):
    """Palette used by the interface: theme colours plus derived, theme-aware roles."""
    c = theme_colors(appearance["theme"], appearance["mode"], appearance.get("custom_accent"))
    c["nav_selected"] = blend_colors(c["sidebar"], c["accent"], 0.22)
    c["brand_background"] = blend_colors(c["sidebar"], c["accent"], 0.18)
    c["text_dim"] = blend_colors(c["text_secondary"], c["background"], 0.32)
    c["accent_soft"] = blend_colors(c["card"], c["accent"], 0.16)
    # accent used as *text* (labels, headings) must stay readable on cards, sidebar and surface
    text_accent = c["accent"]
    for bg in (c["card"], c["surface"], c["sidebar"], c["background"]):
        text_accent = readable_on(text_accent, bg)
    c["accent_text"] = text_accent
    return c


def save_writes_supported(kind, encrypted):
    """Only plain-text saves have end-to-end write validation in this build."""
    return kind == "text" and not encrypted


# --------------------------------------------------------------------------
# container / encoding helpers
# --------------------------------------------------------------------------
def _aes():
    try:
        from Crypto.Cipher import AES
    except ImportError:
        try:
            from Cryptodome.Cipher import AES
        except ImportError:
            raise SiiError("pycryptodome is required to read encrypted saves "
                           "(pip install pycryptodome)")
    return AES


def decode_container(raw):
    """Return (payload, was_encrypted)."""
    if raw[:4] == b"ScsC":
        AES = _aes()
        iv = raw[36:52]
        enc = raw[56:]
        enc = enc[:len(enc) // 16 * 16]
        dec = AES.new(KEY, AES.MODE_CBC, iv).decrypt(enc)
        try:
            data = zlib.decompressobj().decompress(dec)
        except zlib.error as e:
            raise SiiError("could not decompress save: %s" % e)
        return data, True
    return raw, False


CH = "0123456789abcdefghijklmnopqrstuvwxyz_"


def tok_dec(v):
    s = ""
    while v:
        c = v % 38
        v //= 38
        s += CH[c - 1] if 1 <= c <= 37 else "?"
    return s


def tok_enc(s):
    v = 0
    for i, ch in enumerate(s):
        v += (CH.index(ch) + 1) * (38 ** i)
    return v


# --------------------------------------------------------------------------
# binary (BSII) reader
# --------------------------------------------------------------------------
class _R:
    def __init__(self, b):
        self.b = b
        self.p = 0

    def u8(self):
        v = self.b[self.p]
        self.p += 1
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.b, self.p)[0]
        self.p += 4
        return v

    def raw(self, n):
        v = bytes(self.b[self.p:self.p + n])
        self.p += n
        return v

    def str(self):
        return self.raw(self.u32())


FIX = {0x05: 4, 0x07: 8, 0x09: 12, 0x11: 12, 0x17: 16, 0x19: 32, 0x25: 4,
       0x27: 4, 0x29: 2, 0x2b: 2, 0x2f: 4, 0x31: 8, 0x33: 8, 0x35: 1,
       0x37: 4, 0x03: 8}
IDS = (0x39, 0x3b, 0x3d)          # id / link values
IDARR = (0x3a, 0x3c)              # arrays of ids


def _readid(r):
    c = r.u8()
    r.p += 8 if c == 0xff else 8 * c
    if r.p > len(r.b):
        raise SiiError("truncated id")


def _skip(r, t):
    st = r.p
    if t == 0x01:
        r.str()
    elif t == 0x02:
        for _ in range(r.u32()):
            r.str()
    elif t in IDS:
        _readid(r)
    elif t in IDARR:
        for _ in range(r.u32()):
            _readid(r)
    elif t in FIX:
        r.p += FIX[t]
    elif (t - 1) in FIX and t % 2 == 0:
        n = r.u32()
        r.p += n * FIX[t - 1]
    else:
        raise SiiError("unsupported value type 0x%x" % t)
    if r.p > len(r.b):
        raise SiiError("truncated value")
    return st, r.p


def _bsii_defs(d):
    """Parse BSII struct definitions. Returns (structs, offset of first data block)."""
    r = _R(d)
    r.raw(4)
    r.u32()
    structs = {}
    while r.p < len(d):
        save = r.p
        if r.u32() != 0:
            r.p = save
            break
        if r.u8() == 0:
            continue
        sid = r.u32()
        name = r.str().decode("latin1")
        fields = []
        while True:
            ft = r.u32()
            if ft == 0:
                break
            fn = r.str().decode("latin1")
            if ft in (0x37, 0x38):
                for _ in range(r.u32()):
                    r.u32()
                    r.str()
            fields.append((ft, fn))
        structs[sid] = (name, fields)
    return structs, r.p


REPAIR_SCALARS = ("engine_wear", "transmission_wear", "cabin_wear", "chassis_wear",
                  "engine_wear_unfixable", "transmission_wear_unfixable",
                  "cabin_wear_unfixable", "chassis_wear_unfixable",
                  "trailer_body_wear", "trailer_body_wear_unfixable", "cargo_damage")
REPAIR_ARRAYS = ("wheels_wear", "wheels_wear_unfixable")


class BsiiSave:
    NEED = ("bank", "garages", "experience_points", "adr", "long_dist",
            "heavy", "fragile", "urgent", "mechanical", "visited_cities",
            "visited_cities_count")
    OPT = ("unlocked_dealers", "unlocked_recruitments")

    def __init__(self, data):
        self.d = bytearray(data)
        self._locate()

    def _locate(self):
        self.structs, self.start = _bsii_defs(self.d)
        r = _R(self.d)
        r.p = self.start
        sid = r.u32()
        name, fields = self.structs[sid]
        if name != "economy":
            raise SiiError("unexpected save layout (first block is %s)" % name)
        _readid(r)
        self.sp = {}
        want = set(self.NEED) | set(self.OPT)
        for ft, fn in fields:
            s, e = _skip(r, ft)
            if fn in want:
                self.sp[fn] = (ft, s, e)
        self.econ_end = r.p
        missing = [k for k in self.NEED if k not in self.sp]
        if missing:
            raise SiiError("save is missing fields: %s" % ", ".join(missing))
        for k in ("experience_points", "adr") + tuple(SKILLS):
            if self.sp[k][0] != 0x27:
                raise SiiError("unexpected type for %s" % k)
        bank_ref = bytes(self.d[self.sp["bank"][1]:self.sp["bank"][2]])
        bank_sid = [k for k, v in self.structs.items() if v[0] == "bank"]
        if not bank_sid:
            raise SiiError("bank struct not found")
        pat = struct.pack("<I", bank_sid[0]) + bank_ref
        idx = bytes(self.d).find(pat, self.econ_end - 1)
        if idx < 0:
            raise SiiError("bank block not found")
        r2 = _R(self.d)
        r2.p = idx + 4
        _readid(r2)
        self.money = None
        for ft, fn in self.structs[bank_sid[0]][1]:
            s, e = _skip(r2, ft)
            if fn == "money_account":
                if ft != 0x31:
                    raise SiiError("unexpected type for money_account")
                self.money = (s, e)
                break
        if self.money is None:
            raise SiiError("money_account not found")
        self._idx = None

    # -- full index of vehicle / trailer blocks (for repair & refuel)
    def _index(self):
        if self._idx is not None:
            return self._idx
        r = _R(self.d)
        r.p = self.start
        out = []
        n = len(self.d)
        while r.p < n:
            sid = r.u32()
            if sid == 0:                      # end marker (type 0, valid 0)
                r.u8()
                continue
            if sid not in self.structs:
                raise SiiError("save structure check failed at byte %d" % r.p)
            name, fields = self.structs[sid]
            _readid(r)
            spans = {}
            for ft, fn in fields:
                s, e = _skip(r, ft)
                if name in ("vehicle", "trailer"):
                    spans[fn] = (ft, s, e)
            if name in ("vehicle", "trailer"):
                out.append((name, spans))
        if r.p != n:
            raise SiiError("save structure check failed (trailing data)")
        self._idx = out
        return out

    def verify(self):
        self._index()

    def _extra_reps(self, extras):
        reps = []
        if "repair" not in extras and "refuel" not in extras:
            return reps
        for name, spans in self._index():
            if "repair" in extras:
                for fn in REPAIR_SCALARS:
                    if fn in spans and spans[fn][0] == 0x05:
                        reps.append((spans[fn][1], spans[fn][2], struct.pack("<f", 0.0)))
                for fn in REPAIR_ARRAYS:
                    if fn in spans and spans[fn][0] == 0x06:
                        _, s, e = spans[fn]
                        cnt = struct.unpack_from("<I", self.d, s)[0]
                        reps.append((s, e, struct.pack("<I", cnt) + b"\x00" * (4 * cnt)))
            if "refuel" in extras and name == "vehicle" and "fuel_relative" in spans \
                    and spans["fuel_relative"][0] == 0x05:
                reps.append((spans["fuel_relative"][1], spans["fuel_relative"][2],
                             struct.pack("<f", 1.0)))
        return reps

    # -- helpers
    def _int(self, key):
        return struct.unpack_from("<i", self.d, self.sp[key][1])[0]

    def _garage_cities(self):
        ft, s, e = self.sp["garages"]
        r = _R(self.d)
        r.p = s
        n = r.u32()
        out = []
        for _ in range(n):
            c = r.u8()
            parts = [tok_dec(struct.unpack_from("<Q", self.d, r.p + 8 * k)[0])
                     for k in range(c)] if c != 0xff else []
            r.p += 8 if c == 0xff else 8 * c
            if len(parts) == 2 and parts[0] == "garage":
                out.append(parts[1])
        return list(dict.fromkeys(out))

    def _names(self, key):
        _, s, e = self.sp[key]
        n = struct.unpack_from("<I", self.d, s)[0]
        return [tok_dec(struct.unpack_from("<Q", self.d, s + 4 + 8 * i)[0])
                for i in range(n)]

    def _visited(self):
        names = self._names("visited_cities")
        _, s2, e2 = self.sp["visited_cities_count"]
        m = struct.unpack_from("<I", self.d, s2)[0]
        counts = [struct.unpack_from("<i", self.d, s2 + 4 + 4 * i)[0]
                  for i in range(m)]
        counts += [1] * (len(names) - len(counts))
        return names, counts[:len(names)]

    def read(self):
        v = {"money": struct.unpack_from("<q", self.d, self.money[0])[0],
             "xp": self._int("experience_points"),
             "adr": self._int("adr")}
        for k in SKILLS:
            v[k] = self._int(k)
        names, _ = self._visited()
        v["visited"] = len(names)
        v["total_cities"] = len(self._garage_cities())
        v["dealers"] = len(self._names("unlocked_dealers")) if "unlocked_dealers" in self.sp else 0
        return v

    def apply(self, vals, unlock_cities=False, extras=(), vehicle_ids=None):
        extras = set(extras)
        reps = []
        if "money" in vals:
            reps.append((self.money[0], self.money[1],
                         struct.pack("<q", int(vals["money"]))))
        for k in ("xp", "adr") + tuple(SKILLS):
            if k in vals:
                key = "experience_points" if k == "xp" else k
                s = self.sp[key][1]
                reps.append((s, s + 4, struct.pack("<i", int(vals[k]))))
        if unlock_cities:
            names, counts = self._visited()
            have = set(names)
            for c in self._garage_cities():
                if c not in have:
                    names.append(c)
                    counts.append(1)
            _, s, e = self.sp["visited_cities"]
            reps.append((s, e, struct.pack("<I", len(names)) +
                         b"".join(struct.pack("<Q", tok_enc(n)) for n in names)))
            _, s, e = self.sp["visited_cities_count"]
            reps.append((s, e, struct.pack("<I", len(counts)) +
                         b"".join(struct.pack("<i", c) for c in counts)))
        if "dealers" in extras:
            cities = self._garage_cities()
            for key in self.OPT:
                if key in self.sp and self.sp[key][0] == 0x04:
                    names = self._names(key)
                    for c in cities:
                        if c not in names:
                            names.append(c)
                    _, s, e = self.sp[key]
                    reps.append((s, e, struct.pack("<I", len(names)) +
                                 b"".join(struct.pack("<Q", tok_enc(n)) for n in names)))
        reps += self._extra_reps(extras)
        for s, e, b in sorted(reps, key=lambda x: -x[0]):
            self.d[s:e] = b
        self._locate()

    def to_bytes(self):
        return bytes(self.d)


# --------------------------------------------------------------------------
# text (SiiNunit) reader
# --------------------------------------------------------------------------
class TextSave:
    def __init__(self, data):
        self.t = data.decode("utf-8", "surrogateescape")
        self.nl = "\r\n" if "\r\n" in self.t[:2000] else "\n"

    def _span(self, name):
        m = re.search(r"^" + name + r" : [^\r\n]*\{[ \t]*\r?\n", self.t, re.M)
        if not m:
            raise SiiError("block '%s' not found" % name)
        e = re.compile(r"^\}", re.M).search(self.t, m.end())
        if not e:
            raise SiiError("block '%s' is not closed" % name)
        return m.end(), e.start()

    def _get(self, block, key):
        s, e = self._span(block)
        m = re.search(r"^ %s: (-?\d+)" % key, self.t[s:e], re.M)
        if not m:
            raise SiiError("field '%s' not found" % key)
        return int(m.group(1))

    def _garage_cities(self):
        s, e = self._span("economy")
        c = re.findall(r"^ garages\[\d+\]: garage\.(\S+?)\r?$", self.t[s:e], re.M)
        return list(dict.fromkeys(c))

    def _visited(self, block):
        names = re.findall(r"^ visited_cities\[\d+\]: (\S+?)\r?$", block, re.M)
        counts = [int(x) for x in re.findall(
            r"^ visited_cities_count\[\d+\]: (-?\d+)", block, re.M)]
        counts += [1] * (len(names) - len(counts))
        return names, counts[:len(names)]

    def read(self):
        v = {"money": self._get("bank", "money_account"),
             "xp": self._get("economy", "experience_points"),
             "adr": self._get("economy", "adr")}
        for k in SKILLS:
            v[k] = self._get("economy", k)
        s, e = self._span("economy")
        names, _ = self._visited(self.t[s:e])
        v["visited"] = len(names)
        v["total_cities"] = len(self._garage_cities())
        v["dealers"] = len(re.findall(r"^ unlocked_dealers\[\d+\]:", self.t[s:e], re.M))
        return v

    def _set_in_block(self, block, key, value):
        s, e = self._span(block)
        seg, n = re.subn(r"^( %s: )-?\d+" % key, lambda m: m.group(1) + str(int(value)),
                         self.t[s:e], count=1, flags=re.M)
        if n != 1:
            raise SiiError("field '%s' not found" % key)
        self.t = self.t[:s] + seg + self.t[e:]

    def _repair_refuel(self, extras, vehicle_ids=None):
        nl_re = r"[^\r\n]*"
        selected = set(vehicle_ids) if vehicle_ids is not None else None

        def fix(m):
            kind, unit_id, body = m.group(1), m.group(2).strip(), m.group(0)
            if selected is not None and unit_id not in selected:
                return body
            if "repair" in extras:
                for fn in REPAIR_SCALARS:
                    body = re.sub(r"^( %s: )%s" % (fn, nl_re), r"\g<1>0", body, flags=re.M)
                for fn in REPAIR_ARRAYS:
                    body = re.sub(r"^( %s\[\d+\]: )%s" % (fn, nl_re), r"\g<1>0", body, flags=re.M)
            if "refuel" in extras and kind == "vehicle":
                body = re.sub(r"^( fuel_relative: )%s" % nl_re, r"\g<1>1", body, flags=re.M)
            return body
        self.t = re.sub(r"^(vehicle|trailer) : ([^\r\n]+?)\s*\{[ \t]*\r?\n.*?^\}", fix, self.t,
                        flags=re.M | re.S)

    def verify(self):
        if not self.t.startswith("SiiNunit"):
            raise SiiError("text save header is missing")
        depth = 0
        quoted = escaped = comment = False
        for char in self.t:
            if comment:
                if char in "\r\n":
                    comment = False
                continue
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == "#":
                comment = True
            elif char == '"':
                quoted = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth < 0:
                    raise SiiError("text save contains an unmatched closing brace")
        if quoted or depth != 0:
            raise SiiError("text save has an unterminated string or incomplete block")
        values = self.read()
        if not all(isinstance(values[k], int) for k in
                   ("money", "xp", "adr") + tuple(SKILLS)):
            raise SiiError("text save contains invalid supported values")

    def apply(self, vals, unlock_cities=False, extras=(), vehicle_ids=None):
        extras = set(extras)
        if "money" in vals:
            self._set_in_block("bank", "money_account", vals["money"])
        for k in ("xp", "adr") + tuple(SKILLS):
            if k in vals:
                self._set_in_block("economy",
                                   "experience_points" if k == "xp" else k, vals[k])
        nl = self.nl
        if unlock_cities:
            s, e = self._span("economy")
            seg = self.t[s:e]
            names, counts = self._visited(seg)
            have = set(names)
            for c in self._garage_cities():
                if c not in have:
                    names.append(c)
                    counts.append(1)
            vtxt = " visited_cities: %d%s" % (len(names), nl) + "".join(
                " visited_cities[%d]: %s%s" % (i, n, nl) for i, n in enumerate(names))
            ctxt = " visited_cities_count: %d%s" % (len(counts), nl) + "".join(
                " visited_cities_count[%d]: %d%s" % (i, c, nl) for i, c in enumerate(counts))
            seg, n1 = re.subn(
                r"^ visited_cities: \d+\r?\n(?: visited_cities\[\d+\]: [^\r\n]*\r?\n)*",
                lambda m: vtxt, seg, count=1, flags=re.M)
            seg, n2 = re.subn(
                r"^ visited_cities_count: \d+\r?\n(?: visited_cities_count\[\d+\]: [^\r\n]*\r?\n)*",
                lambda m: ctxt, seg, count=1, flags=re.M)
            if n1 != 1 or n2 != 1:
                raise SiiError("visited cities section not found")
            self.t = self.t[:s] + seg + self.t[e:]
        if "dealers" in extras:
            cities = self._garage_cities()
            for key in ("unlocked_dealers", "unlocked_recruitments"):
                s, e = self._span("economy")
                seg = self.t[s:e]
                pat = r"^ %s: \d+\r?\n(?: %s\[\d+\]: [^\r\n]*\r?\n)*" % (key, key)
                m = re.search(pat, seg, re.M)
                if not m:
                    continue
                names = re.findall(r"^ %s\[\d+\]: (\S+?)\r?$" % key, m.group(0), re.M)
                for c in cities:
                    if c not in names:
                        names.append(c)
                txt = " %s: %d%s" % (key, len(names), nl) + "".join(
                    " %s[%d]: %s%s" % (key, i, n, nl) for i, n in enumerate(names))
                seg = seg[:m.start()] + txt + seg[m.end():]
                self.t = self.t[:s] + seg + self.t[e:]
        if "repair" in extras or "refuel" in extras:
            self._repair_refuel(extras, vehicle_ids)

    def to_bytes(self):
        return self.t.encode("utf-8", "surrogateescape")


_SII_UNIT_HEADER = re.compile(r"^([A-Za-z_][\w.]*) : ([^\s{]+) \{")
_SII_FIELD = re.compile(r"^ ([\w]+(?:\[\d+\])?): (.*)$")
_SII_ARRAY_KEY = re.compile(r"^([\w]+)\[(\d+)\]$")


def _brace_delta(line):
    depth = 0
    quoted = escaped = comment = False
    for char in line:
        if comment:
            break
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '#':
            comment = True
        elif char == '"':
            quoted = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
    return depth


def _parse_sii_units(text):
    """Index top-level SiiNunit objects without interpreting unknown fields."""
    units = {}
    unit = None
    body = []
    depth = 0
    for line in text.splitlines():
        if unit is None:
            match = _SII_UNIT_HEADER.match(line)
            if not match:
                continue
            unit = (match.group(1), match.group(2))
            body = []
            depth = _brace_delta(line)
            if depth != 1:
                raise SiiError("unsupported or malformed SiiNunit object header")
            continue
        depth += _brace_delta(line)
        if depth < 0:
            raise SiiError("SiiNunit object has an unmatched closing brace")
        if depth == 0:
            fields = {}
            for item in body:
                field_match = _SII_FIELD.match(item)
                if field_match:
                    key, value = field_match.groups()
                    fields[key] = value.strip()
            if unit[1] in units:
                raise SiiError("duplicate SiiNunit object identifier: " + unit[1])
            units[unit[1]] = (unit[0], fields)
            unit = None
            body = []
        else:
            body.append(line)
    if unit is not None or depth != 0:
        raise SiiError("SiiNunit object is incomplete")
    return units


def _sii_array(fields, name):
    if name not in fields:
        return None
    try:
        count = int(fields[name])
    except (TypeError, ValueError) as exc:
        raise SiiError("invalid array count for " + name) from exc
    if count < 0 or count > 100000:
        raise SiiError("unreasonable array count for " + name)
    values = []
    for index in range(count):
        key = f"{name}[{index}]"
        if key not in fields:
            raise SiiError("incomplete array " + name)
        values.append(fields[key])
    return values


def _sii_float(value):
    value = str(value).strip()
    match = re.fullmatch(r"&([0-9a-fA-F]{8})", value)
    if match:
        number = struct.unpack(">f", bytes.fromhex(match.group(1)))[0]
    else:
        number = float(value)
    return number if math.isfinite(number) else None


def _sii_fraction(fields, key):
    try:
        value = _sii_float(fields[key])
    except (KeyError, ValueError, OverflowError):
        return None
    return value if value is not None and 0.0 <= value <= 1.0 else None


def _sii_unquote(value):
    value = str(value).strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    return value


def _vehicle_details(unit_id, fields, units, garage_map):
    raw_accessories = _sii_array(fields, "accessories")
    model = None
    specs = {name: None for name in ("chassis", "cabin", "engine", "transmission")}
    if raw_accessories is not None:
        paths = []
        for ref in raw_accessories:
            accessory = units.get(ref)
            if accessory is None:
                continue
            _, accessory_fields = accessory
            path = _sii_unquote(accessory_fields.get("data_path", ""))
            if path:
                paths.append(path.replace("\\", "/"))
        for path in paths:
            match = re.fullmatch(r"/def/vehicle/truck/([^/]+)/data\.sii", path, re.I)
            if match:
                model = match.group(1)
                break
        if model:
            for path in paths:
                match = re.fullmatch(
                    r"/def/vehicle/truck/([^/]+)/(chassis|cabin|engine|transmission)/([^/]+\.sii)",
                    path, re.I)
                if match and match.group(1).casefold() == model.casefold():
                    key = match.group(2).lower()
                    if specs[key] is None:
                        specs[key] = match.group(3)
                    elif specs[key] != match.group(3):
                        specs[key] = "ambiguous"
    try:
        odometer = int(fields["odometer"])
        if odometer < 0:
            odometer = None
    except (KeyError, TypeError, ValueError):
        odometer = None
    wheels_raw = _sii_array(fields, "wheels_wear")
    wheels = None
    if wheels_raw is not None:
        try:
            parsed = [_sii_float(value) for value in wheels_raw]
            if all(x is not None and 0 <= x <= 1 for x in parsed):
                wheels = parsed
        except (ValueError, OverflowError):
            pass
    assignments = garage_map.get(unit_id, [])
    garage = assignments[0] if len(assignments) == 1 else None
    try:
        plate = _sii_unquote(fields.get("license_plate", "")) or None
    except Exception:
        plate = None
    damage = {key: _sii_fraction(fields, key) for key in
              ("engine_wear", "transmission_wear", "cabin_wear", "chassis_wear")}
    return {"id": unit_id, "model": model, "garage": garage,
            "garage_ambiguous": len(assignments) > 1, "odometer": odometer,
            "damage": damage, "wheels_wear": wheels,
            "fuel_relative": _sii_fraction(fields, "fuel_relative"),
            "fuel_liters": None, "license_plate": plate, "specs": specs,
            "repair_supported": any(k in fields for k in REPAIR_SCALARS + REPAIR_ARRAYS),
            "refuel_supported": _sii_fraction(fields, "fuel_relative") is not None}


def extract_fleet(source):
    """Read owned trucks, attached garage slots, owned trailers and known metrics from text SII."""
    if isinstance(source, SaveFile):
        save = source
    else:
        try:
            save = SaveFile(source)
        except Exception as exc:
            raise SiiError("save could not be parsed for fleet extraction: " + str(exc)) from exc
    if save.kind != "text" or not isinstance(save.impl, TextSave):
        raise SiiError("fleet extraction currently requires a readable SiiNunit text save; BSII layouts are not mapped yet")
    units = _parse_sii_units(save.impl.t)
    players = [(ident, fields) for ident, (kind, fields) in units.items() if kind == "player"]
    if len(players) != 1:
        raise SiiError("could not identify exactly one player unit in this save")
    _, player = players[0]
    truck_refs = _sii_array(player, "trucks")
    if truck_refs is None:
        raise SiiError("player truck ownership list is not present in this save")
    trailer_refs = _sii_array(player, "trailers")
    trailers_available = trailer_refs is not None
    if trailer_refs is None:
        trailer_refs = []
    truck_ids = []
    for ref in truck_refs:
        if ref in ("null", "nil"):
            continue
        if ref not in units or units[ref][0] != "vehicle":
            raise SiiError("player truck reference does not resolve to a vehicle unit")
        if ref not in truck_ids:
            truck_ids.append(ref)
    trailer_ids = []
    for ref in trailer_refs:
        if ref in ("null", "nil"):
            continue
        if ref not in units or units[ref][0] != "trailer":
            raise SiiError("player trailer reference does not resolve to a trailer unit")
        if ref not in trailer_ids:
            trailer_ids.append(ref)

    garage_map = {}
    garage_contents = {}
    for ident, (kind, fields) in units.items():
        if kind != "garage" or not ident.startswith("garage."):
            continue
        city = ident[len("garage."):]
        truck_slots = _sii_array(fields, "vehicles")
        trailer_slots = _sii_array(fields, "trailers")
        truck_slots = truck_slots if truck_slots is not None else []
        trailer_slots = trailer_slots if trailer_slots is not None else []
        owned_trucks = [ref for ref in truck_slots if ref in truck_ids]
        owned_trailers = [ref for ref in trailer_slots if ref in trailer_ids]
        if owned_trucks or owned_trailers:
            garage_contents[city] = {"trucks": owned_trucks, "trailers": owned_trailers}
            for ref in owned_trucks + owned_trailers:
                garage_map.setdefault(ref, []).append(city)

    trucks = [_vehicle_details(ref, units[ref][1], units, garage_map) for ref in truck_ids]
    trailers = []
    for ref in trailer_ids:
        _, fields = units[ref]
        try:
            odometer = int(fields["odometer"])
            if odometer < 0:
                odometer = None
        except (KeyError, TypeError, ValueError):
            odometer = None
        damage = {key: _sii_fraction(fields, key) for key in
                  ("trailer_body_wear", "chassis_wear", "cargo_damage")}
        definition = _sii_unquote(fields.get("trailer_definition", "")) or None
        assignments = garage_map.get(ref, [])
        wheels_raw = _sii_array(fields, "wheels_wear")
        wheels = None
        if wheels_raw is not None:
            try:
                parsed = [_sii_float(value) for value in wheels_raw]
                if all(x is not None and 0 <= x <= 1 for x in parsed):
                    wheels = parsed
            except (ValueError, OverflowError):
                pass
        trailers.append({"id": ref, "definition": definition,
                         "garage": assignments[0] if len(assignments) == 1 else None,
                         "garage_ambiguous": len(assignments) > 1,
                         "odometer": odometer, "damage": damage,
                         "wheels_wear": wheels,
                         "repair_supported": any(k in fields for k in REPAIR_SCALARS + REPAIR_ARRAYS)})
    garages = [{"city": city, **contents} for city, contents in sorted(garage_contents.items())]
    return {"trucks": trucks, "trailers": trailers,
            "trailers_available": trailers_available, "garages": garages,
            "garage_count": len(garages), "format": "text", "encrypted": save.was_encrypted,
            "limitations": ["Only garage locations containing a player-listed truck or trailer are counted.",
                            "Fuel is a relative tank fraction; liters require a verified capacity definition."]}


def load_sii(path):
    """Return (impl, kind, was_encrypted) for any .sii file."""
    raw = Path(path).read_bytes()
    data, enc = decode_container(raw)
    if data[:4] == b"SiiN":
        return TextSave(data), "text", enc
    if data[:4] == b"BSII":
        return BsiiSave(data), "binary", enc
    raise SiiError("unknown save format")


class SaveFile:
    def __init__(self, path):
        self.path = Path(path)
        self.impl, self.kind, self.was_encrypted = load_sii(path)

    def read(self):
        return self.impl.read()

    def apply(self, vals, unlock_cities=False, extras=(), vehicle_ids=None):
        self.impl.apply(vals, unlock_cities, extras, vehicle_ids)

    def verify(self):
        self.impl.verify()

    def to_bytes(self):
        return self.impl.to_bytes()


def inspect_save_file(path):
    """Return verified metadata and the values supported by the current reader."""
    path = Path(path)
    sf = SaveFile(path)
    values = sf.read()
    stat = path.stat()
    return {"path": path, "format": sf.kind, "encrypted": sf.was_encrypted,
            "size": stat.st_size, "modified": stat.st_mtime, "values": values}


# --------------------------------------------------------------------------
# folders, backups
# --------------------------------------------------------------------------
def documents_dir():
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort),
                            ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]
            fid = GUID(0xFDD39AD0, 0x238F, 0x46AF,
                       (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(fid), 0, None, ctypes.byref(out)) == 0:
                p = out.value
                ctypes.windll.ole32.CoTaskMemFree(out)
                if p:
                    return Path(p)
        except Exception:
            pass
    return Path.home() / "Documents"


def find_game_dirs():
    cands = [documents_dir() / "Euro Truck Simulator 2",
             Path.home() / "Documents" / "Euro Truck Simulator 2",
             Path.home() / "OneDrive" / "Documents" / "Euro Truck Simulator 2",
             Path.home() / ".local" / "share" / "Euro Truck Simulator 2"]
    out, seen = [], set()
    for c in cands:
        if c.is_dir() and any((c / s).is_dir() for s in ("profiles", "steam_profiles")):
            try:
                key = str(c.resolve()).casefold()
            except OSError:
                key = str(c).casefold()
            if key not in seen:
                out.append(c)
                seen.add(key)
    return out


def list_profiles(base):
    base = Path(base)
    res = []
    for sub in ("profiles", "steam_profiles"):
        d = base / sub
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if not (p / "save").is_dir():
                continue
            try:
                label = bytes.fromhex(p.name).decode("utf-8")
            except Exception:
                label = p.name
            res.append((("%s  (%s)" % (label, sub)), p))
    return res


def default_profile_index(profiles, preferred_path=None):
    """Auto-select only a sole profile, or preserve an explicit prior choice."""
    if preferred_path is not None:
        for index, (_, path) in enumerate(profiles):
            if Path(path) == Path(preferred_path):
                return index
    return 0 if len(profiles) == 1 else -1


def list_saves(profile):
    res = []
    sd = Path(profile) / "save"
    if not sd.is_dir():
        return res
    for s in sd.iterdir():
        g = s / "game.sii"
        if g.is_file():
            res.append((s.name, g, g.stat().st_mtime))
    res.sort(key=lambda x: -x[2])
    return res


def editor_storage_root(game_data=None):
    """Keep editor data beside the user's ETS2 documents data, not the game install."""
    base = Path(game_data) if game_data is not None else documents_dir() / "Euro Truck Simulator 2"
    return base / "TR-EURO-EDITOR"


def backup_root(game_data=None):
    return editor_storage_root(game_data) / "backups"


def legacy_backup_root():
    """Read-only fallback for backups created by older versions of the editor."""
    return Path.home() / "ETS2SaveEditor_Backups"


def _save_game_data_root(game_sii):
    game_sii = Path(game_sii)
    return game_sii.parent.parent.parent.parent.parent


def unique_backup_dir(*parts, game_data=None):
    parent = backup_root(game_data).joinpath(*parts)
    parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for suffix in range(1, 10000):
        name = stamp if suffix == 1 else "%s-%02d" % (stamp, suffix)
        candidate = parent / name
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise SiiError("could not allocate a unique backup directory")


def make_backup(game_sii):
    game_sii = Path(game_sii)
    prof = game_sii.parent.parent.parent.name
    backup = backup_root(_save_game_data_root(game_sii)) / prof / game_sii.parent.name / "game.sii"
    backup.parent.mkdir(parents=True, exist_ok=True)
    original = game_sii.read_bytes()
    tmp = None
    try:
        tmp = _write_temp_bytes(backup, original)
        if tmp.read_bytes() != original or game_sii.read_bytes() != original:
            raise SiiError("backup verification failed")
        os.replace(tmp, backup)
    except Exception:
        if tmp is not None and tmp.exists():
            tmp.unlink()
        raise
    return backup


def latest_backup(game_sii):
    game_sii = Path(game_sii)
    prof, slot = game_sii.parent.parent.parent.name, game_sii.parent.name
    current = backup_root(_save_game_data_root(game_sii)) / prof / slot / "game.sii"
    if current.is_file():
        return current
    old_dir = legacy_backup_root() / prof / slot
    old = [x / "game.sii" for x in old_dir.iterdir()
           if old_dir.is_dir() and (x / "game.sii").is_file()] if old_dir.is_dir() else []
    return max(old, key=lambda p: (p.stat().st_mtime_ns, p.parent.name)) if old else None


def backup_history(game_sii):
    game_sii = Path(game_sii)
    current = latest_backup(game_sii)
    return [current] if current and current.is_file() else []


def restore_save_backup(game_sii, backup_file):
    """Restore a readable backup after preserving the current file and verifying a temp copy."""
    game_sii, backup_file = Path(game_sii), Path(backup_file)
    current = SaveFile(game_sii)
    # Stage the selected backup first: it may be the single rolling backup file,
    # which make_backup() below will replace with the current save.
    staged = _write_temp_bytes(game_sii, backup_file.read_bytes())
    try:
        source = SaveFile(staged)
        if (not save_writes_supported(source.kind, source.was_encrypted) or
                not save_writes_supported(current.kind, current.was_encrypted)):
            raise SiiError("safe restore is currently enabled only for plain-text, unencrypted saves")
        source.verify()
        expected = source.read()
        current_fingerprint = save_fingerprint(game_sii)
        recovery_backup = make_backup(game_sii)
        if save_fingerprint(recovery_backup) != current_fingerprint:
            raise SiiError("recovery backup does not match the current save")
        restored = SaveFile(staged)
        restored.verify()
        if restored.read() != expected:
            raise SiiError("restored backup verification failed")
        if save_fingerprint(game_sii) != current_fingerprint:
            raise SiiError("current save changed during restore; no changes were written")
        os.replace(staged, game_sii)
    except Exception:
        if staged.exists():
            staged.unlink()
        raise
    return recovery_backup


def save_fingerprint(path):
    """Stable content fingerprint used to detect edits made outside the app."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_temp_bytes(target, data):
    """Write bytes to a unique same-directory temporary file and flush them."""
    target = Path(target)
    fd, name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        return tmp
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise


def _verify_text_extras(text, extras, unlock_cities=False, vehicle_ids=None):
    """Check only fields the current text-save editor explicitly transforms."""
    extras = set(extras)
    if unlock_cities:
        parsed_save = TextSave(text.encode("utf-8", "surrogateescape"))
        s, e = parsed_save._span("economy")
        visited, _ = parsed_save._visited(parsed_save.t[s:e])
        missing = set(parsed_save._garage_cities()) - set(visited)
        if missing:
            raise SiiError("visited-city verification failed")
    if "dealers" in extras:
        parsed_save = TextSave(text.encode("utf-8", "surrogateescape"))
        cities = set(parsed_save._garage_cities())
        s, e = parsed_save._span("economy")
        economy = parsed_save.t[s:e]
        for field in ("unlocked_dealers", "unlocked_recruitments"):
            section = re.search(r"^ " + field + r": \d+\r?\n"
                                r"(?: " + field + r"\[\d+\]: [^\r\n]*\r?\n)*",
                                economy, re.M)
            if section is None:
                if cities:
                    raise SiiError("could not verify " + field)
                continue
            unlocked = set(re.findall(r"^ " + field + r"\[\d+\]: (\S+?)\r?$",
                                      section.group(0), re.M))
            if not cities.issubset(unlocked):
                raise SiiError("dealer/recruitment verification failed for " + field)
    units = _parse_sii_units(text)
    selected = set(vehicle_ids) if vehicle_ids is not None else None
    blocks = [(ident, kind, fields) for ident, (kind, fields) in units.items()
              if kind in ("vehicle", "trailer") and (selected is None or ident in selected)]
    if selected is not None:
        found = {ident for ident, _, _ in blocks}
        if not selected.issubset(found):
            raise SiiError("selected vehicle references are missing; save was not written")
    if extras & {"repair", "refuel"} and not blocks:
        raise SiiError("no supported vehicle/trailer blocks were found; save was not written")
    repair_count = fuel_count = 0
    for _ident, kind, fields in blocks:
        if "repair" in extras:
            for field in REPAIR_SCALARS:
                if field in fields:
                    try:
                        value = _sii_float(fields[field])
                    except (ValueError, OverflowError) as ex:
                        raise SiiError("could not verify repaired field " + field) from ex
                    if value is None or value != 0:
                        raise SiiError("repair verification failed for " + field)
                    repair_count += 1
            for field in REPAIR_ARRAYS:
                values = _sii_array(fields, field)
                if values is not None:
                    try:
                        numbers = [_sii_float(value) for value in values]
                    except (ValueError, OverflowError) as ex:
                        raise SiiError("could not verify repaired field " + field) from ex
                    if any(number is None or number != 0 for number in numbers):
                        raise SiiError("repair verification failed for " + field)
                    repair_count += len(numbers)
        if "refuel" in extras and kind == "vehicle":
            if "fuel_relative" in fields:
                fuel = _sii_fraction(fields, "fuel_relative")
                if fuel is None:
                    raise SiiError("fuel verification failed")
                fuel_count += 1
                if abs(fuel - 1.0) > 1e-6:
                    raise SiiError("fuel verification failed")
    if "repair" in extras and repair_count == 0:
        raise SiiError("no recognized repair fields were found; save was not written")
    if "refuel" in extras and fuel_count == 0:
        raise SiiError("no recognized fuel fields were found; save was not written")


def game_running():
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq eurotrucks2.exe"], capture_output=True,
            text=True, creationflags=0x08000000).stdout.lower()
        return "eurotrucks2.exe" in out
    except Exception:
        return False


def edit_save(game_sii, vals, unlock_cities=False, extras=(), vehicle_ids=None):
    """Back up, edit, write and verify. Returns the backup path."""
    game_sii = Path(game_sii)
    original_fingerprint = save_fingerprint(game_sii)
    sf = SaveFile(game_sii)
    if not save_writes_supported(sf.kind, sf.was_encrypted):
        raise SiiError("safe writes are currently enabled only for plain-text SiiNunit saves; binary or encrypted saves are read-only")
    if vehicle_ids is not None and not vehicle_ids:
        raise SiiError("no owned vehicles were selected")
    sf.apply(vals, unlock_cities, extras, vehicle_ids)
    new = sf.to_bytes()
    if save_fingerprint(game_sii) != original_fingerprint:
        raise SiiError("save changed while it was being prepared; no changes were written")
    bak = make_backup(game_sii)
    tmp = None
    try:
        if save_fingerprint(bak) != original_fingerprint:
            raise SiiError("backup does not match the original save; no changes were written")
        tmp = _write_temp_bytes(game_sii, new)
        chk_file = SaveFile(tmp)
        if chk_file.kind != "text" or chk_file.was_encrypted:
            raise SiiError("temporary save format changed unexpectedly")
        chk_file.verify()
        chk = chk_file.read()
        for k, v in vals.items():
            if chk[k] != int(v):
                raise SiiError("verification failed for %s" % k)
        _verify_text_extras(chk_file.impl.t, extras, unlock_cities, vehicle_ids)
        if save_fingerprint(game_sii) != original_fingerprint:
            raise SiiError("save changed before replacement; no changes were written")
        os.replace(tmp, game_sii)
    except Exception:
        if tmp is not None and tmp.exists():
            tmp.unlink()
        raise
    return bak


def write_save_copy(source, destination, vals, unlock_cities=False, extras=(), vehicle_ids=None):
    """Create a new verified save copy without ever overwriting an existing path."""
    source, destination = Path(source), Path(destination)
    sf = SaveFile(source)
    if not save_writes_supported(sf.kind, sf.was_encrypted):
        raise SiiError("safe writes are currently enabled only for plain-text, unencrypted SiiNunit saves")
    original_fingerprint = save_fingerprint(source)
    original_values = sf.read()
    sf.apply(vals, unlock_cities, extras, vehicle_ids)
    new = sf.to_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    reservation = False
    tmp = None
    try:
        # Reserve the destination atomically so --out can never replace a file.
        fd = os.open(str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        reservation = True
        tmp = _write_temp_bytes(destination, new)
        candidate = SaveFile(tmp)
        candidate.verify()
        values = candidate.read()
        for key, value in vals.items():
            if values[key] != int(value):
                raise SiiError("verification failed for %s" % key)
        if unlock_cities and values["visited"] < original_values["visited"]:
            raise SiiError("visited-city verification failed")
        _verify_text_extras(candidate.impl.t, extras, unlock_cities, vehicle_ids)
        if save_fingerprint(source) != original_fingerprint:
            raise SiiError("source save changed while output was being prepared")
        os.replace(tmp, destination)
    except Exception:
        if tmp is not None and tmp.exists():
            tmp.unlink()
        if reservation and destination.exists() and destination.stat().st_size == 0:
            destination.unlink()
        raise


def rank_for_xp(xp):
    best = LEVEL_TIERS[0]
    for t in LEVEL_TIERS:
        if xp >= t[2]:
            best = t
    return best


# --------------------------------------------------------------------------
# mods: profile.sii active_mods, manifest reading, validation
# --------------------------------------------------------------------------
import zipfile
import fnmatch

EXEC_EXT = (".exe", ".dll", ".bat", ".cmd", ".ps1", ".vbs", ".scr", ".msi", ".com", ".jar")
CONTENT_DIRS = ("def", "vehicle", "map", "sound", "material", "model", "model2", "ui",
                "automat", "effect", "font", "locale", "prefab", "prefab2", "road",
                "sign", "system", "unit", "video", "action", "cutscene", "custom",
                "dlc", "env", "fx", "gui", "particle", "terrain", "texture")


class ProfileSii:
    """Reads/writes the active_mods list of a profile.sii (text, binary or encrypted)."""

    def __init__(self, path):
        self.path = Path(path)
        data, self.was_encrypted = decode_container(self.path.read_bytes())
        if data[:4] == b"SiiN":
            self.kind = "text"
            self.t = data.decode("utf-8", "surrogateescape")
            self.nl = "\r\n" if "\r\n" in self.t[:2000] else "\n"
        elif data[:4] == b"BSII":
            self.kind = "binary"
            self.d = bytearray(data)
            self._locate()
        else:
            raise SiiError("unknown profile format")

    def _locate(self):
        structs, start = _bsii_defs(self.d)
        r = _R(self.d)
        r.p = start
        sid = r.u32()
        name, fields = structs[sid]
        _readid(r)
        self.span = None
        for ft, fn in fields:
            s, e = _skip(r, ft)
            if fn == "active_mods":
                if ft != 0x02:
                    raise SiiError("unexpected type for active_mods")
                self.span = (s, e)
                break
        if self.span is None:
            raise SiiError("active_mods not found in profile")

    def get_mods(self):
        if self.kind == "text":
            out = []
            for m in re.finditer(r'^ active_mods\[\d+\]: "(.*)"\r?$', self.t, re.M):
                out.append(m.group(1).replace('\\"', '"'))
            return out
        r = _R(self.d)
        r.p = self.span[0]
        return [r.str().decode("utf-8", "replace") for _ in range(r.u32())]

    def set_mods(self, items):
        if self.kind == "text":
            nl = self.nl
            txt = " active_mods: %d%s" % (len(items), nl) + "".join(
                ' active_mods[%d]: "%s"%s' % (i, x.replace('"', '\\"'), nl)
                for i, x in enumerate(items))
            pat = r"^ active_mods: \d+\r?\n(?: active_mods\[\d+\]: [^\r\n]*\r?\n)*"
            self.t, n = re.subn(pat, lambda m: txt, self.t, count=1, flags=re.M)
            if n != 1:
                raise SiiError("active_mods section not found")
        else:
            blob = struct.pack("<I", len(items))
            for x in items:
                b = x.encode("utf-8")
                blob += struct.pack("<I", len(b)) + b
            self.d[self.span[0]:self.span[1]] = blob
            self._locate()

    def to_bytes(self):
        return self.t.encode("utf-8", "surrogateescape") if self.kind == "text" else bytes(self.d)


def write_profile_mods(profile_dir, items):
    p = Path(profile_dir) / "profile.sii"
    original = p.read_bytes()
    original_fingerprint = hashlib.sha256(original).hexdigest()
    ps = ProfileSii(p)
    if ps.kind != "text" or ps.was_encrypted:
        raise SiiError("safe profile writes are currently enabled only for plain-text, unencrypted profile.sii files")
    ps.set_mods(items)
    new = ps.to_bytes()
    dest = unique_backup_dir(Path(profile_dir).name, "profile_sii",
                             game_data=Path(profile_dir).parent.parent)
    backup = dest / "profile.sii"
    backup_tmp = None
    tmp = None
    try:
        backup_tmp = _write_temp_bytes(backup, original)
        if hashlib.sha256(backup_tmp.read_bytes()).hexdigest() != original_fingerprint:
            raise SiiError("profile backup verification failed")
        os.replace(backup_tmp, backup)
        tmp = _write_temp_bytes(p, new)
        candidate = ProfileSii(tmp)
        if candidate.get_mods() != list(items):
            raise SiiError("verification failed")
        if hashlib.sha256(p.read_bytes()).hexdigest() != original_fingerprint:
            raise SiiError("profile changed while it was being prepared; no changes were written")
        os.replace(tmp, p)
    except Exception:
        if tmp is not None and tmp.exists():
            tmp.unlink()
        if backup_tmp is not None and backup_tmp.exists():
            backup_tmp.unlink()
        raise


def mod_folder(base):
    return Path(base) / "mod"


def list_mod_paths(base):
    d = mod_folder(base)
    out = []
    if d.is_dir():
        for p in sorted(d.iterdir(), key=lambda x: x.name.lower()):
            if p.is_file() and p.suffix.lower() in (".scs", ".zip"):
                out.append(p)
            elif p.is_dir() and (p / "manifest.sii").is_file():
                out.append(p)
    return out


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_mod_file(source, destination, overwrite=False):
    """Copy a mod atomically; overwrite is an explicit caller decision."""
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        return False
    if destination.exists() and not overwrite:
        raise FileExistsError(destination)
    fd, name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp",
                                dir=destination.parent)
    os.close(fd)
    tmp = Path(name)
    reserved = False
    try:
        shutil.copyfile(source, tmp)
        shutil.copystat(source, tmp)
        if source.stat().st_size != tmp.stat().st_size or _file_sha256(source) != _file_sha256(tmp):
            raise SiiError("copied mod verification failed")
        if not overwrite:
            fd = os.open(str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            reserved = True
        os.replace(tmp, destination)
        return True
    except Exception:
        if tmp.exists():
            tmp.unlink()
        if reserved and destination.exists() and destination.stat().st_size == 0:
            destination.unlink()
        raise


def mod_id(path):
    p = Path(path)
    return p.name if p.is_dir() else p.stem


def parse_manifest(text):
    out = {"category": [], "compatible_versions": []}
    for m in re.finditer(r'^[ \t]*(\w+)(\[\])?[ \t]*:[ \t]*(.+?)[ \t]*$', text, re.M):
        k, arr, v = m.groups()
        v = v.strip()
        if v.startswith('"') and v.endswith('"') and len(v) >= 2:
            v = v[1:-1]
        if k in ("category", "compatible_versions") and arr:
            out[k].append(v)
        elif k in ("package_version", "display_name", "author", "mp_mod_optional",
                   "icon", "description_file") and not arr:
            out[k] = v
    return out


def mod_file_list(path):
    """Return (kind, names, error). kind: zip | dir | hashfs | bad"""
    p = Path(path)
    if p.is_dir():
        names = []
        for root, _, files in os.walk(p):
            for f in files:
                names.append(os.path.relpath(os.path.join(root, f), p).replace("\\", "/"))
                if len(names) > 200000:
                    break
        return "dir", names, None
    try:
        with open(p, "rb") as fh:
            head = fh.read(4)
    except OSError as e:
        return "bad", [], str(e)
    if head == b"SCS#":
        return "hashfs", [], None
    if not zipfile.is_zipfile(p):
        return "bad", [], "not a zip archive"
    try:
        with zipfile.ZipFile(p) as z:
            return "zip", [i.filename for i in z.infolist() if not i.filename.endswith("/")], None
    except Exception as e:
        return "bad", [], str(e)


def read_mod_manifest(path, names, kind):
    """Return (manifest dict or None, location, error)."""
    cand = [n for n in names if n.lower().endswith("manifest.sii")]
    if not cand:
        return None, None, "missing"
    root = [n for n in cand if n.lower() == "manifest.sii"]
    loc = root[0] if root else sorted(cand, key=len)[0]
    try:
        if kind == "dir":
            raw = (Path(path) / loc).read_bytes()
        else:
            with zipfile.ZipFile(path) as z:
                raw = z.read(loc)
        raw, _ = decode_container(raw)
        return parse_manifest(raw.decode("utf-8", "replace")), loc, None
    except Exception as e:
        return None, loc, str(e)


def game_log_path(base):
    return Path(base) / "game.log.txt"


def detect_game_version(base):
    p = game_log_path(base)
    if not p.is_file():
        return ""
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                # The broad word "version" also matches Windows and XInput, so
                # accept only the ETS2 pack-set version record.
                m = re.search(r"\[ufs\]\s+Loaded pack set version\s+"
                              r"(\d+\.\d+(?:\.\d+){0,2}[A-Za-z]?)", line, re.I)
                if m:
                    return m.group(1)
                if i > 400:
                    break
    except OSError:
        pass
    return ""


def scan_game_log(base, ident, display):
    """Return (mentioned, problem_lines) for a mod, from game.log.txt."""
    p = game_log_path(base)
    if not p.is_file():
        return None, []
    keys = [k.lower() for k in (ident, display) if k]
    mentioned, bad = False, []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                low = line.lower()
                if any(k in low for k in keys):
                    mentioned = True
                    if "error" in low or "warning" in low or "fail" in low or "unable" in low \
                            or "could not" in low or "missing" in low:
                        bad.append(line.strip()[:220])
    except OSError:
        return None, []
    return mentioned, bad[:12]


def validate_mod(path, base, game_version="", other_files=None):
    """Static checks + game log scan. Returns (verdict, [(level, code, params)])."""
    res = []
    kind, names, err = mod_file_list(path)
    ident = mod_id(path)
    display = ident
    if kind == "bad":
        res.append(("err", "v_notzip", {"err": err}))
        return "err", res
    if kind == "hashfs":
        res.append(("warn", "v_hashfs", {}))
    if kind == "zip":
        try:
            with zipfile.ZipFile(path) as z:
                bad = z.testzip()
                archive_names = [item.filename for item in z.infolist() if not item.is_dir()]
            if bad:
                res.append(("err", "v_corrupt", {"name": bad}))
            seen, duplicates, unsafe = set(), set(), []
            for name in archive_names:
                if name in seen:
                    duplicates.add(name)
                seen.add(name)
                posix = PurePosixPath(name)
                if ("\\" in name or posix.is_absolute() or re.match(r"^[A-Za-z]:", name)
                        or ".." in posix.parts):
                    unsafe.append(name)
            unsafe.extend(sorted(duplicates))
            if unsafe:
                res.append(("err", "v_unsafe_path", {"paths": ", ".join(unsafe[:8])}))
        except Exception as e:
            res.append(("err", "v_corrupt", {"name": str(e)}))
    man = None
    if kind in ("zip", "dir"):
        man, loc, merr = read_mod_manifest(path, names, kind)
        if man is None and merr == "missing":
            res.append(("err", "v_nomanifest", {}))
        elif man is None:
            res.append(("err", "v_manifest_bad", {"err": merr}))
        else:
            if loc.lower() != "manifest.sii":
                res.append(("err", "v_nested", {"path": loc}))
            display = man.get("display_name", ident)
            res.append(("info", "v_info", {"name": display, "ver": man.get("package_version", "?"),
                                            "author": man.get("author", "?")}))
            miss = [k for k in ("package_version", "display_name", "author") if not man.get(k)]
            if miss:
                res.append(("warn", "v_fields", {"fields": ", ".join(miss)}))
            declared = man.get("compatible_versions", [])
            if not declared:
                res.append(("warn", "v_compat_none", {}))
            elif not game_version:
                res.append(("info", "v_compat_unknown", {"declared": ", ".join(declared)}))
            else:
                ok = any(fnmatch.fnmatch(game_version, d) or fnmatch.fnmatch(game_version + ".0", d)
                         for d in declared)
                res.append(("ok" if ok else "err", "v_compat_ok" if ok else "v_compat_bad",
                            {"game": game_version, "declared": ", ".join(declared)}))
        execs = [n for n in names if n.lower().endswith(EXEC_EXT)]
        if execs:
            res.append(("err", "v_exe", {"files": ", ".join(execs[:5]), "n": len(execs)}))
        tops = {n.split("/")[0].lower() for n in names if "/" in n}
        if names and not (tops & set(CONTENT_DIRS)) and (man is not None):
            res.append(("warn", "v_nocontent", {}))
        if other_files:
            mine = {n.lower() for n in names if n.lower() not in ("manifest.sii",)
                    and not n.lower().startswith("mod_description")}
            hits = {}
            for oid, oset in other_files.items():
                if oid == ident:
                    continue
                inter = mine & oset
                if inter:
                    hits[oid] = sorted(inter)
            if hits:
                total = sum(len(v) for v in hits.values())
                sample = next(iter(hits.values()))[0]
                res.append(("warn", "v_conflict", {"n": total, "mods": ", ".join(list(hits)[:4]),
                                                    "sample": sample}))
    mentioned, lines = scan_game_log(base, ident, display)
    if mentioned is None:
        res.append(("info", "v_log_missing", {}))
    elif not mentioned:
        res.append(("info", "v_log_none", {}))
    elif lines:
        res.append(("warn", "v_log_err", {"lines": "\n    ".join(lines)}))
    else:
        res.append(("ok", "v_log_ok", {}))
    levels = [r[0] for r in res]
    verdict = "err" if "err" in levels else ("warn" if "warn" in levels else "ok")
    return verdict, res


# --------------------------------------------------------------------------
# config.cfg
# --------------------------------------------------------------------------
CONFIG_KEYS = {"developer": "g_developer", "console": "g_console", "savefmt": "g_save_format"}


def read_config(base):
    p = Path(base) / "config.cfg"
    if not p.is_file():
        raise SiiError("config.cfg not found")
    txt = p.read_text(encoding="utf-8", errors="replace")
    out = {}
    for m in re.finditer(r'^uset (\w+) "([^"]*)"', txt, re.M):
        out[m.group(1)] = m.group(2)
    return out


def write_config(base, changes):
    p = Path(base) / "config.cfg"
    original = p.read_bytes()
    original_fingerprint = hashlib.sha256(original).hexdigest()
    raw = original.decode("utf-8", "surrogateescape")
    nl = "\r\n" if "\r\n" in raw else "\n"
    for k, v in changes.items():
        pat = r'^uset %s "[^"]*"' % re.escape(k)
        if re.search(pat, raw, re.M):
            raw = re.sub(pat, lambda m: 'uset %s "%s"' % (k, v), raw, count=1, flags=re.M)
        else:
            raw = raw.rstrip("\r\n") + nl + 'uset %s "%s"' % (k, v) + nl
    dest = unique_backup_dir("config_cfg", game_data=base)
    backup = dest / "config.cfg"
    backup_tmp = tmp = None
    try:
        backup_tmp = _write_temp_bytes(backup, original)
        if hashlib.sha256(backup_tmp.read_bytes()).hexdigest() != original_fingerprint:
            raise SiiError("config.cfg backup verification failed")
        os.replace(backup_tmp, backup)
        tmp = _write_temp_bytes(p, raw.encode("utf-8", "surrogateescape"))
        check = tmp.read_bytes().decode("utf-8", "surrogateescape")
        values = {m.group(1): m.group(2) for m in re.finditer(r'^uset (\w+) "([^"]*)"', check, re.M)}
        if any(values.get(k) != str(v) for k, v in changes.items()):
            raise SiiError("config.cfg verification failed")
        if hashlib.sha256(p.read_bytes()).hexdigest() != original_fingerprint:
            raise SiiError("config.cfg changed during update; no changes were written")
        os.replace(tmp, p)
    except Exception:
        if backup_tmp is not None and backup_tmp.exists():
            backup_tmp.unlink()
        if tmp is not None and tmp.exists():
            tmp.unlink()
        raise


# --------------------------------------------------------------------------
# translations (en, fa)
# --------------------------------------------------------------------------
T = {
    "title": ("TR-EURO-EDITOR", "TR-EURO-EDITOR | ادیتور سیو یورو تراک ۲"),
    "lang_btn": ("فارسی", "English"),
    "theme_day": ("Day", "روز"),
    "theme_night": ("Night", "شب"),
    "accent": ("Accent", "رنگ تأکیدی"),
    "tab_save": ("Save editor", "ادیت سیو"),
    "tab_extras": ("Extras", "امکانات بیشتر"),
    "tab_mods": ("Mods", "مدیریت ماد"),
    "tab_tune": ("Truck tuning", "تقویت کامیون"),
    "tab_settings": ("Game settings", "تنظیمات بازی"),
    "tab_dashboard": ("Dashboard", "داشبورد"),
    "tab_profiles": ("Save profiles", "پروفایل‌ها و سیوها"),
    "tab_garage": ("Garage", "گاراژ"),
    "dashboard_title": ("Career overview", "نمای کلی مسیر رانندگی"),
    "dashboard_subtitle": ("Live values read from the selected local save.",
                           "اطلاعات خوانده‌شده از سیو محلی انتخاب‌شده."),
    "dash_profile_count": ("PROFILES", "پروفایل‌ها"),
    "dash_save_count": ("SAVES IN PROFILE", "سیوهای پروفایل"),
    "dash_balance": ("ACCOUNT BALANCE", "موجودی حساب"),
    "dash_driver_level": ("CAREER TIER", "ردهٔ پیشرفت"),
    "dash_experience": ("EXPERIENCE", "امتیاز تجربه"),
    "dash_city_progress": ("VISITED CITIES", "شهرهای بازدیدشده"),
    "dash_profile_value": ("No profile selected", "پروفایلی انتخاب نشده"),
    "dash_save_value": ("No save selected", "سیوی انتخاب نشده"),
    "dash_mtime_unavailable": ("last modified time unavailable", "زمان آخرین تغییر در دسترس نیست"),
    "dash_save_details": ("{path}\nLast modified: {modified}", "{path}\nآخرین تغییر: {modified}"),
    "dash_profile_label": ("ACTIVE PROFILE", "پروفایل فعال"),
    "dash_save_label": ("ACTIVE SAVE", "سیو فعال"),
    "dash_unavailable": ("Unavailable in the selected save", "در سیو انتخاب‌شده در دسترس نیست"),
    "dash_fleet": ("Fleet snapshot", "نمای ناوگان"),
    "dash_fleet_note": ("Owned trucks and garage slots are linked through the save's player and garage references. "
                        "Only verified text saves are listed.",
                        "کامیون‌های مالکیت‌شده و جایگاه گاراژ از ارجاع‌های واقعی بازیکن و گاراژ استخراج می‌شوند؛ فقط سیو متنیِ پشتیبانی‌شده نمایش داده می‌شود."),
    "dash_backup": ("Latest backup", "آخرین نسخهٔ پشتیبان"),
    "dash_no_backup": ("No backup for the selected save", "برای سیو انتخاب‌شده بکاپی نیست"),
    "dash_open_tuning": ("Open tuning studio", "رفتن به بخش تیونینگ"),
    "garage_title": ("Garage & vehicles", "گاراژ و خودروها"),
    "garage_unavailable": ("Select a save to inspect garage data.", "برای بررسی گاراژ، یک سیو انتخاب کن."),
    "garage_limit": ("This save is binary or its ownership references are not supported. Fleet data is unavailable.",
                     "این سیو باینری است یا ارجاع‌های مالکیت آن پشتیبانی نمی‌شوند؛ اطلاعات ناوگان در دسترس نیست."),
    "fleet_empty": ("No owned trucks or trailers are listed in this save.", "در این سیو کامیون یا تریلر مالکیت‌شده‌ای ثبت نشده است."),
    "fleet_empty_trucks": ("No owned trucks are listed in this save.", "در این سیو کامیونی برای بازیکن ثبت نشده است."),
    "fleet_summary": ("{trucks} trucks · {trailers} trailers · {garages} garages with listed vehicles",
                      "{trucks} کامیون · {trailers} تریلر · {garages} گاراژ دارای وسیلهٔ ثبت‌شده"),
    "fleet_trailer_unavailable": ("Trailer ownership unavailable", "مالکیت تریلر در دسترس نیست"),
    "fleet_failed": ("Fleet data could not be read: {reason}", "خواندن اطلاعات ناوگان ممکن نشد: {reason}"),
    "fleet_type_truck": ("Owned truck", "کامیون مالکیت‌شده"),
    "fleet_type_trailer": ("Owned trailer", "تریلر مالکیت‌شده"),
    "fleet_list": ("Vehicles linked to this save", "وسایل نقلیهٔ متصل به این سیو"),
    "fleet_details": ("Selected vehicle details", "مشخصات وسیلهٔ انتخاب‌شده"),
    "fleet_column_type": ("Type", "نوع"),
    "fleet_column_model": ("Model / definition", "مدل / شناسه"),
    "fleet_column_garage": ("Garage", "گاراژ"),
    "dash_open_garage": ("Open garage", "رفتن به گاراژ"),
    "fleet_model": ("Truck definition", "شناسهٔ مدل کامیون"),
    "fleet_trailer_def": ("Trailer definition", "شناسهٔ مدل تریلر"),
    "fleet_garage": ("Garage city", "شهر گاراژ"),
    "fleet_odometer": ("Odometer value", "مقدار کیلومترشمار"),
    "fleet_engine_wear": ("Engine wear", "فرسودگی موتور"),
    "fleet_transmission_wear": ("Transmission wear", "فرسودگی گیربکس"),
    "fleet_cabin_wear": ("Cabin wear", "فرسودگی کابین"),
    "fleet_chassis_wear": ("Chassis wear", "فرسودگی شاسی"),
    "fleet_trailer_wear": ("Trailer body wear", "فرسودگی بدنهٔ تریلر"),
    "fleet_cargo_damage": ("Cargo damage", "خسارت بار"),
    "fleet_wheels_wear": ("Wheel wear values", "مقادیر فرسودگی چرخ‌ها"),
    "fleet_fuel": ("Fuel level (relative)", "سطح سوخت (نسبی)"),
    "fleet_fuel_liters_note": ("Exact liters unavailable: verified tank capacity is not in the save.",
                                "لیتر دقیق در دسترس نیست؛ ظرفیت باک از سیو قابل‌تأیید نیست."),
    "fleet_chassis": ("Chassis part", "قطعهٔ شاسی"),
    "fleet_cabin": ("Cabin part", "قطعهٔ کابین"),
    "fleet_engine": ("Engine part", "قطعهٔ موتور"),
    "fleet_transmission": ("Transmission part", "قطعهٔ گیربکس"),
    "fleet_na": ("Information unavailable", "اطلاعات در دسترس نیست"),
    "fleet_ambiguous_garage": ("Ambiguous garage reference", "ارجاع گاراژ مبهم است"),
    "fleet_repair_selected": ("Repair selected", "تعمیر مورد انتخاب‌شده"),
    "fleet_refuel_selected": ("Refuel selected truck", "سوخت‌گیری کامیون انتخاب‌شده"),
    "fleet_repair_group": ("Repair listed owned vehicles", "تعمیر وسایل مالکیت‌شدهٔ فهرست‌شده"),
    "fleet_refuel_group": ("Refuel listed owned trucks", "سوخت‌گیری کامیون‌های مالکیت‌شدهٔ فهرست‌شده"),
    "fleet_write_limit": ("Writing is disabled for encrypted or binary saves. Open a supported plain-text save to enable these controls.",
                         "نوشتن روی سیو رمزگذاری‌شده یا باینری غیرفعال است؛ برای فعال‌شدن این دکمه‌ها یک سیو متنیِ پشتیبانی‌شده باز کن."),
    "fleet_select_note": ("Select a truck or trailer to inspect values. Missing fields are shown as unavailable.",
                          "برای دیدن اطلاعات کامیون یا تریلر را انتخاب کن. فیلدهای موجودنبودن با «اطلاعات در دسترس نیست» مشخص می‌شوند."),
    "fleet_confirm": ("{action} for {count} selected vehicle(s)?\n\nThe game must be closed. A verified rolling backup will replace the previous backup for this save.",
                      "{action} برای {count} وسیلهٔ انتخاب‌شده انجام شود؟\n\nبازی باید بسته باشد. بکاپ چرخشیِ تأییدشده جای بکاپ قبلی همین سیو را می‌گیرد."),
    "tune_title": ("Build a performance mod", "ساخت ماد تقویت موتور"),
    "tune_trucks": ("Truck models included", "مدل‌های کامیون در ماد"),
    "tune_torque": ("Target torque (Nm)", "گشتاور هدف (نیوتن‌متر)"),
    "tune_rpm": ("RPM limit", "حد دور موتور"),
    "tune_hp": ("Target horsepower (hp)", "اسب‌بخار هدف (HP)"),
    "tune_build": ("Build selectable parts mod", "ساخت ماد قطعات قابل خرید"),
    "tune_tab_engine": ("Engine", "موتور"),
    "tune_tab_gearbox": ("Gearbox", "گیربکس"),
    "tune_tab_brakes": ("Brakes & handling", "ترمز و هندلینگ"),
    "tune_tab_weight": ("Truck weight", "وزن کامیون"),
    "tune_target_speed": ("Target top speed (km/h)", "سرعت نهایی هدف (km/h)"),
    "tune_target_accel": ("Target 0–100 time (seconds)", "زمان هدف صفر تا ۱۰۰ (ثانیه)"),
    "tune_suggest": ("Suggest engine + gearbox", "پیشنهاد موتور و گیربکس"),
    "tune_gear_count": ("Forward gears (6–18)", "تعداد دنده جلو (۶ تا ۱۸)"),
    "tune_diff": ("Differential ratio", "نسبت دیفرانسیل"),
    "tune_engine_brake": ("Engine brake strength", "قدرت ترمز موتوری"),
    "tune_retarder": ("Retarder levels (0–3)", "سطح ریتاردر (۰ تا ۳)"),
    "tune_mass": ("Truck + cargo mass (kg)", "وزن کامیون به‌همراه بار (کیلوگرم)"),
    "tune_weight_note": ("Mass affects the estimate only. Real chassis-weight upgrades need each truck's chassis definition.",
                         "وزن فقط روی تخمین اثر دارد. تغییر واقعی وزن به فایل شاسی هر کامیون نیاز دارد."),
    "tune_brake_note": ("This tunes engine braking and gearbox retarder. Service-brake and steering chassis parts are not changed.",
                         "این بخش ترمز موتوری و ریتاردر گیربکس را تنظیم می‌کند؛ ترمز اصلی و قطعات فرمان/شاسی تغییر نمی‌کنند."),
    "tune_performance": ("ESTIMATED PERFORMANCE", "عملکرد تخمینی"),
    "tune_estimated_accel": ("0–100 estimate", "تخمین صفر تا ۱۰۰"),
    "tune_estimated_speed": ("Top-speed estimate", "تخمین سرعت نهایی"),
    "tune_estimate_note": ("Approximation only: tires, gearbox setup, cargo, road, traction and game physics affect results.",
                           "فقط تخمین است؛ لاستیک، گیربکس، بار، جاده، چسبندگی و فیزیک بازی نتیجه را تغییر می‌دهند."),
    "tune_suggest_success": ("Suggested: {hp:.0f} hp, {torque:.0f} Nm, {gears} gears, diff {diff:.2f}.",
                             "پیشنهاد: {hp:.0f} اسب‌بخار، {torque:.0f} نیوتن‌متر، {gears} دنده، دیفرانسیل {diff:.2f}."),
    "tune_note": ("Choose the engine strength and RPM range. The mod changes engine definitions for the selected models. "
                  "Top speed is not a fixed target; the gearbox, truck setup, load, and game physics also affect it.",
                  "قدرت موتور و بازه‌ی دور موتور را انتخاب کن. ماد مشخصات موتور مدل‌های انتخابی را تغییر می‌دهد. "
                  "سرعت نهایی عدد تضمینی نیست؛ گیربکس، تنظیم کامیون، بار و فیزیک بازی هم اثر دارند."),
    "tune_success": ("Static package checks passed; in-game compatibility is NOT confirmed.\nCreated {n} engine/gearbox definition files:\n{path}\n\nReview the archive, then install and test it in-game. Nothing is installed or activated automatically.",
                     "بررسی ایستای فایل‌ها موفق بود؛ سازگاری داخل بازی تأیید نشده است.\n{n} فایل تعریف موتور/گیربکس ساخته شد:\n{path}\n\nآرشیو را بررسی و سپس در بازی نصب و آزمایش کن. چیزی خودکار نصب یا فعال نمی‌شود."),
    "tune_output": ("Generated mod output folder", "پوشهٔ خروجی ماد ساخته‌شده"),
    "tune_compat": ("Manifest version pattern (optional)", "الگوی نسخهٔ سازگار در manifest (اختیاری)"),
    "tune_compat_hint": ("Use a version/pattern you have verified. Blank leaves compatibility undeclared.",
                         "الگوی نسخه را فقط در صورت اطمینان وارد کن. خالی بماند، سازگاری اعلام نمی‌شود."),
    "tune_detect_version": ("Read version from game.log", "خواندن نسخه از game.log"),
    "tune_browse_output": ("Browse...", "انتخاب پوشه..."),
    "tune_open_output": ("Open output folder", "بازکردن پوشهٔ خروجی"),
    "tune_presets": ("Saved setups", "تنظیمات ذخیره‌شده"),
    "tune_preset_name": ("Setup name", "نام تنظیم"),
    "tune_preset_save": ("Save current setup", "ذخیرهٔ تنظیم فعلی"),
    "tune_preset_load": ("Load selected setup", "بارگذاری تنظیم انتخابی"),
    "tune_preset_delete": ("Delete", "حذف"),
    "tune_compare_a": ("Setup A", "تنظیم A"),
    "tune_compare_b": ("Setup B", "تنظیم B"),
    "tune_compare": ("Compare setups", "مقایسهٔ تنظیم‌ها"),
    "tune_compare_empty": ("Save at least two named setups to compare them.",
                           "برای مقایسه، دست‌کم دو تنظیم نام‌گذاری‌شده ذخیره کن."),
    "tune_compare_result": ("A: {a_hp:.0f} hp · {a_tq:.0f} Nm · {a_accel:.1f} s · {a_speed:.0f} km/h · {a_gears} gears\n"
                            "B: {b_hp:.0f} hp · {b_tq:.0f} Nm · {b_accel:.1f} s · {b_speed:.0f} km/h · {b_gears} gears",
                            "A: {a_hp:.0f} اسب · {a_tq:.0f} Nm · {a_accel:.1f} ثانیه · {a_speed:.0f} km/h · {a_gears} دنده\n"
                            "B: {b_hp:.0f} اسب · {b_tq:.0f} Nm · {b_accel:.1f} ثانیه · {b_speed:.0f} km/h · {b_gears} دنده"),
    "tune_preset_exists": ("A setup with this name already exists. Replace it?",
                           "تنظیمی با این نام وجود دارد؛ جایگزین شود؟"),
    "tune_preset_delete_confirm": ("Delete this saved setup?", "این تنظیم ذخیره‌شده حذف شود؟"),
    "tune_preset_bad": ("Enter a name and valid positive tuning values first.",
                        "اول نام و مقادیر معتبر و مثبت برای تیونینگ وارد کن."),
    "tune_preset_saved": ("Saved setup: {name}", "تنظیم «{name}» ذخیره شد."),
    "tune_preset_loaded": ("Loaded setup: {name}", "تنظیم «{name}» بارگذاری شد."),
    "tune_bad_factor": ("Enter positive finite values for horsepower, torque, and RPM.",
                        "برای اسب‌بخار، گشتاور و دور موتور عدد مثبت و معتبر وارد کن."),
    "tune_gauge": ("SPEED ESTIMATE", "برآورد سرعت"),
    "tune_gauge_note": ("Visual estimate from RPM setting · gearbox and load change real speed",
                        "برآورد نمایشی بر پایه‌ی دور موتور · گیربکس و بار سرعت واقعی را تغییر می‌دهند"),
    "tune_selected": ("TRUCKS IN THIS BUILD", "کامیون‌های این ماد"),
    "tune_power": ("TORQUE OUTPUT", "قدرت گشتاور"),
    "tune_rpm_short": ("RPM RANGE", "بازه دور موتور"),
    "tune_live": ("TUNING PREVIEW", "پیش‌نمایش تیونینگ"),
    "tune_profile": ("PERFORMANCE PROFILE", "پروفایل عملکرد"),
    "tune_card_hp": ("TARGET POWER", "قدرت هدف"),
    "tune_card_torque": ("TARGET TORQUE", "گشتاور هدف"),
    "tune_card_models": ("SELECTED TRUCKS", "کامیون انتخابی"),
    "tune_no_dir": ("Choose your Documents\\Euro Truck Simulator 2 folder in the Save editor tab first.",
                    "اول در تب ادیت سیو، پوشه‌ی Documents\\Euro Truck Simulator 2 را انتخاب کن."),
    "tune_running": ("Close ETS2 before creating a tuning mod.", "قبل از ساخت ماد، بازی ETS2 را ببند."),
    "sec_folder": ("1. ETS2 folder", "۱. پوشه‌ی بازی"),
    "browse": ("Browse...", "انتخاب..."),
    "autodetect": ("Auto-detect", "پیدا کردن خودکار"),
    "sec_profile": ("2. Profile and save", "۲. پروفایل و سیو"),
    "col_save": ("Save", "سیو"),
    "col_date": ("Last modified", "آخرین تغییر"),
    "sec_edit": ("3. Edit", "۳. ویرایش"),
    "money": ("Money (EUR)", "پول (یورو)"),
    "set100m": ("Set 100M", "۱۰۰ میلیون"),
    "xp": ("Experience (XP)", "تجربه (XP)"),
    "preset": ("Quick level preset...", "انتخاب سریع لول..."),
    "max_xp": ("Max ({xp} XP)", "حداکثر ({xp} XP)"),
    "rank": ("Rank: {name} (level {lv} or higher)", "رتبه: {name} (لول {lv} یا بالاتر)"),
    "skills": ("Skills (0-6)", "مهارت‌ها (۰ تا ۶)"),
    "max_skills": ("Max skills", "همه‌ی مهارت‌ها حداکثر"),
    "adr_box": ("ADR licences", "مجوزهای ADR"),
    "cities": ("Cities", "شهرها"),
    "cities_val": ("{v} recorded as visited (game total unavailable)",
                   "{v} شهر به‌عنوان بازدیدشده ثبت شده؛ تعداد کل بازی در دسترس نیست"),
    "unlock_cities": ("Unlock all cities (mark as visited)", "باز کردن همه‌ی شهرها (بازدیدشده)"),
    "max_all": ("Max everything", "همه‌چیز حداکثر"),
    "restore": ("Restore last backup", "بازگردانی آخرین بکاپ"),
    "apply": ("Apply changes", "اعمال تغییرات"),
    "ask_max": ("Stage all supported maximum values? This only changes the form; the save is not written yet.",
                "مقادیر حداکثریِ پشتیبانی‌شده در فرم قرار بگیرند؟ هنوز چیزی روی سیو نوشته نمی‌شود."),
    "ask_discard": ("There are staged changes for the current save. Discard them and switch?",
                    "برای سیو فعلی تغییرات اعمال‌نشده داری. دور ریخته شوند و جابه‌جا شوی؟"),
    "dirty_label": ("UNSAVED CHANGES", "تغییر اعمال‌نشده"),
    "preview_title": ("Review these save changes before writing:", "پیش از نوشتن، تغییرات سیو را بررسی کن:"),
    "preview_context": ("Profile: {profile}\nSave: {path}\nLast modified: {modified}\n\n",
                        "پروفایل: {profile}\nسیو: {path}\nآخرین تغییر: {modified}\n\n"),
    "no_changes": ("There are no changes to apply.", "تغییری برای اعمال وجود ندارد."),
    "stale_save": ("The save changed outside this editor after it was loaded. Reload it and review your edits before applying.",
                   "سیو پس از بارگذاری بیرون از برنامه تغییر کرده است. دوباره بارگذاری کن و پیش از اعمال، تغییرات را مرور کن."),
    "st_start": ("Pick your ETS2 folder, then a profile and a save.",
                 "پوشه‌ی بازی، بعد پروفایل و یک سیو را انتخاب کن."),
    "st_noprof": ("No profiles found in that folder.", "پروفایلی در این پوشه پیدا نشد."),
    "st_saves": ("{n} save(s) found. Select one.", "{n} سیو پیدا شد. یکی را انتخاب کن."),
    "select_profile_explicit": ("{n} profiles found. Select the intended profile explicitly before editing.",
                                 "{n} پروفایل پیدا شد. پیش از ویرایش، پروفایل موردنظر را خودت انتخاب کن."),
    "multiple_game_dirs": ("Multiple ETS2 save folders were found. Choose the exact folder to use.",
                           "چند پوشهٔ سیو ETS2 پیدا شد. پوشهٔ موردنظر را دقیقاً انتخاب کن."),
    "invalid_game_dir": ("The selected folder does not contain profiles or steam_profiles.",
                         "پوشهٔ انتخاب‌شده profiles یا steam_profiles ندارد."),
    "st_loaded": ("Loaded: {p}", "بارگذاری شد: {p}"),
    "st_loaded_readonly": ("Loaded read-only: this save format has not been validated for writing. {p}",
                           "سیو فقط‌خواندنی بارگذاری شد؛ نوشتن روی این قالب هنوز اعتبارسنجی نشده است. {p}"),
    "save_readonly_format": ("This save format is read-only in this build. Convert/save it as plain text in ETS2 and load it again before editing.",
                             "این قالب سیو در این نسخه فقط‌خواندنی است. در ETS2 آن را به‌صورت متن ساده ذخیره کن و دوباره بارگذاری کن."),
    "st_maxed": ("Everything set to max. Click 'Apply changes' to write it.",
                 "همه‌چیز روی حداکثر گذاشته شد. برای ذخیره «اعمال تغییرات» را بزن."),
    "st_done": ("Done. Backup: {b}", "انجام شد. بکاپ: {b}"),
    "st_restored": ("Backup restored.", "بکاپ بازگردانی شد."),
    "no_folder": ("Could not find the folder automatically.\nClick Browse and pick Documents\\Euro Truck Simulator 2.",
                  "پوشه به‌صورت خودکار پیدا نشد.\nانتخاب را بزن و Documents\\Euro Truck Simulator 2 را انتخاب کن."),
    "pick_folder": ("Select the 'Euro Truck Simulator 2' folder", "پوشه‌ی «Euro Truck Simulator 2» را انتخاب کن"),
    "sel_save": ("Select a save first.", "اول یک سیو انتخاب کن."),
    "sel_profile": ("Select a profile first (Save editor tab).", "اول یک پروفایل انتخاب کن (تب ادیت سیو)."),
    "m_profile_readonly": ("Mod-list changes are disabled unless profile.sii is plain-text and unencrypted. The detected profile remains read-only.",
                           "تغییر فهرست ماد فقط برای profile.sii متن ساده و بدون رمز فعال است؛ پروفایل تشخیص‌داده‌شده فقط‌خواندنی می‌ماند."),
    "game_open": ("Euro Truck Simulator 2 is running.\nClose the game first, then try again.",
                  "بازی در حال اجراست.\nاول بازی را ببند و دوباره امتحان کن."),
    "read_fail": ("Could not read this save:\n{e}", "این سیو خوانده نشد:\n{e}"),
    "fail": ("Failed, nothing was changed:\n{e}", "ناموفق بود، چیزی تغییر نکرد:\n{e}"),
    "applied": ("Changes applied.\nBackup saved to:\n{b}\n\nStart the game and load this save.",
                "تغییرات اعمال شد.\nبکاپ اینجا ذخیره شد:\n{b}\n\nبازی را باز کن و همین سیو را لود کن."),
    "no_backup": ("No backup found for this save.", "برای این سیو بکاپی پیدا نشد."),
    "ask_restore": ("Restore this backup?\n{b}", "این بکاپ بازگردانی شود؟\n{b}"),
    "num_err": ("{n} must be a whole number", "{n} باید عدد صحیح باشد"),
    "ex_title": ("Extra options (applied together with 'Apply changes')",
                 "امکانات بیشتر (همراه «اعمال تغییرات» اعمال می‌شوند)"),
    "ex_dealers": ("Unlock all truck dealers and recruitment agencies",
                   "باز کردن همه‌ی نمایندگی‌های کامیون و آژانس‌های استخدام"),
    "ex_repair": ("Repair recognized fields on all saved vehicles/trailers", "تعمیر فیلدهای شناسایی‌شده در همهٔ خودروها/تریلرهای سیو"),
    "ex_refuel": ("Refuel all vehicles with a recognized fuel field", "سوخت‌گیری همهٔ خودروهایی که فیلد سوخت شناخته‌شده دارند"),
    "ex_note": ("Buying garages / trucks and removing loans are not supported. Garage-page actions target player-listed vehicles only; encrypted and binary saves remain read-only.",
                "خرید گاراژ/کامیون و حذف وام پشتیبانی نمی‌شود. عملیات صفحهٔ گاراژ فقط وسایل ثبت‌شده برای بازیکن را هدف می‌گیرد؛ سیو رمزگذاری‌شده و باینری فقط‌خواندنی است."),
    "ex_inspector": ("Save inspector", "بازرس سیو"),
    "ex_inspector_refresh": ("Inspect selected save", "بررسی سیو انتخاب‌شده"),
    "ex_inspector_none": ("Select a save on the Save Profiles page first.",
                          "ابتدا از صفحهٔ پروفایل‌ها یک سیو انتخاب کن."),
    "ex_inspector_ok": ("Readable by the current editor. This is not a guarantee of in-game compatibility.",
                        "خوانشگر فعلی توانست سیو را بخواند؛ این به‌تنهایی سازگاری کامل در بازی را تضمین نمی‌کند."),
    "ex_backup_history": ("Save backup", "بکاپ سیو"),
    "ex_backup_refresh": ("Refresh history", "بازخوانی تاریخچه"),
    "ex_backup_restore": ("Restore selected backup", "بازگردانی بکاپ انتخابی"),
    "ex_backup_empty": ("No backups found for the selected save.", "برای سیو انتخاب‌شده بکاپی پیدا نشد."),
    "ex_backup_date": ("Created", "زمان بکاپ"),
    "ex_backup_size": ("Size", "حجم"),
    "ex_log_view": ("View game log", "نمایش لاگ بازی"),
    "ex_log_missing": ("game.log.txt was not found in the selected ETS2 folder.",
                       "game.log.txt در پوشهٔ انتخاب‌شدهٔ ETS2 پیدا نشد."),
    "mods_ver": ("Game version:", "نسخه‌ی بازی:"),
    "mods_detect": ("Detect", "تشخیص"),
    "mc_on": ("On", "فعال"),
    "mc_name": ("Name", "نام"),
    "mc_file": ("File", "فایل"),
    "mc_ver": ("Version", "نسخه"),
    "mc_status": ("Status", "وضعیت"),
    "m_refresh": ("Refresh", "بازخوانی"),
    "m_enable": ("Enable", "فعال کردن"),
    "m_disable": ("Disable", "غیرفعال کردن"),
    "m_up": ("Move up", "بالا"),
    "m_down": ("Move down", "پایین"),
    "m_confirm_toggle": ("Change active mod list for {profile}?\n{action}: {name}",
                         "فهرست ماد فعال پروفایل {profile} تغییر کند؟\n{action}: {name}"),
    "m_confirm_move": ("Change load order for {profile}?\n{name}",
                       "ترتیب بارگذاری پروفایل {profile} تغییر کند؟\n{name}"),
    "m_add": ("Add mod...", "افزودن ماد..."),
    "m_validate": ("Validate selected", "اعتبارسنجی انتخابی"),
    "m_validate_all": ("Validate all", "اعتبارسنجی همه"),
    "m_open": ("Open mod folder", "باز کردن پوشه‌ی ماد"),
    "ms_active": ("Active", "فعال"),
    "ms_off": ("Off", "خاموش"),
    "ms_missing": ("Active, file not in mod folder (Workshop?)", "فعال، فایل در پوشه‌ی mod نیست (Workshop؟)"),
    "m_order_note": ("Order is written to profile.sii only when it is plain-text and unencrypted. Check the final order in the game's Mod Manager.",
                     "ترتیب فقط وقتی profile.sii متن ساده و بدون رمز باشد نوشته می‌شود. ترتیب نهایی را در Mod Manager بازی بررسی کن."),
    "validation_running": ("Validating mod files in the background…", "اعتبارسنجی مادها در پس‌زمینه در حال انجام است…"),
    "validation_done": ("Static mod validation finished. It does not prove in-game compatibility.",
                        "بررسی ایستای مادها تمام شد؛ سازگاری واقعی داخل بازی را ثابت نمی‌کند."),
    "copying_mods": ("Copying mod files safely in the background…", "کپی امن فایل‌های ماد در پس‌زمینه…"),
    "m_added": ("Copied {n} file(s) to the mod folder. They are NOT enabled yet.",
                "{n} فایل به پوشه‌ی mod کپی شد. هنوز فعال نشده‌اند."),
    "m_overwrite": ("{f} already exists in the mod folder. Replace it?", "{f} از قبل در پوشه‌ی mod هست. جایگزین شود؟"),
    "m_nomod": ("Select a mod first.", "اول یک ماد انتخاب کن."),
    "m_nofile": ("This entry has no file in the mod folder, so it cannot be checked.",
                 "این مورد فایلی در پوشه‌ی mod ندارد، پس بررسی نمی‌شود."),
    "m_nodir": ("No 'mod' folder found in the ETS2 folder.", "پوشه‌ی mod در پوشه‌ی بازی پیدا نشد."),
    "rep_head": ("=== {name} ===", "=== {name} ==="),
    "rep_disclaimer": ("Note: these are file and log checks. Only the game itself can prove a mod works: "
                       "enable it, start the game, load your profile, quit, then press Validate again "
                       "to read game.log.txt.",
                       "توجه: اینها بررسی فایل و لاگ است. فقط خود بازی می‌تواند ثابت کند ماد کار می‌کند: "
                       "ماد را فعال کن، بازی را باز کن، پروفایل را لود کن، خارج شو و دوباره اعتبارسنجی بزن "
                       "تا game.log.txt خوانده شود."),
    "verdict_ok": ("RESULT: no problems found", "نتیجه: مشکلی پیدا نشد"),
    "verdict_warn": ("RESULT: works probably, but check the warnings", "نتیجه: احتمالاً کار می‌کند، ولی هشدارها را ببین"),
    "verdict_err": ("RESULT: problems found - this mod will likely not work", "نتیجه: مشکل پیدا شد - احتمالاً ماد کار نمی‌کند"),
    "v_notzip": ("The file is not a readable zip archive ({err}).", "فایل یک آرشیو zip قابل‌خواندن نیست ({err})."),
    "v_hashfs": ("This is a HashFS (SCS#) archive; its contents cannot be inspected.",
                 "این آرشیو HashFS است؛ محتوایش قابل بررسی نیست."),
    "v_corrupt": ("The archive is damaged (bad file: {name}). Download it again.",
                  "آرشیو خراب است (فایل خراب: {name}). دوباره دانلودش کن."),
    "v_unsafe_path": ("The archive contains unsafe or duplicate paths: {paths}",
                      "آرشیو مسیر ناامن یا تکراری دارد: {paths}"),
    "v_nomanifest": ("No manifest.sii found. The game ignores mods without it.",
                     "manifest.sii پیدا نشد. بازی ماد بدون آن را نادیده می‌گیرد."),
    "v_manifest_bad": ("manifest.sii could not be read ({err}).", "manifest.sii خوانده نشد ({err})."),
    "v_nested": ("manifest.sii is inside a sub-folder ({path}). It must be at the archive root - re-pack the mod.",
                 "manifest.sii داخل یک زیرپوشه است ({path}). باید در ریشه‌ی آرشیو باشد - ماد را دوباره بسته‌بندی کن."),
    "v_info": ("Name: {name} | Version: {ver} | Author: {author}", "نام: {name} | نسخه: {ver} | سازنده: {author}"),
    "v_fields": ("manifest.sii is missing: {fields}", "در manifest.sii این موارد نیست: {fields}"),
    "v_compat_none": ("No compatible_versions declared; the game may still load it.",
                      "compatible_versions اعلام نشده؛ بازی شاید باز هم لودش کند."),
    "v_compat_unknown": ("Declared for game version(s): {declared}. Enter your game version above to compare.",
                         "برای نسخه‌ی(های) {declared} اعلام شده. نسخه‌ی بازی‌ات را بالا وارد کن تا مقایسه شود."),
    "v_compat_ok": ("Compatible with game version {game} (declared: {declared}).",
                    "با نسخه‌ی بازی {game} سازگار است (اعلام‌شده: {declared})."),
    "v_compat_bad": ("NOT compatible: game is {game}, mod declares {declared}. The game will mark it outdated or refuse it.",
                     "ناسازگار: نسخه‌ی بازی {game} است، ماد {declared} اعلام کرده. بازی آن را قدیمی می‌داند یا قبول نمی‌کند."),
    "v_exe": ("Contains {n} executable/script file(s): {files}. Mods should never contain these - do not use it unless you trust the source.",
              "شامل {n} فایل اجرایی/اسکریپت است: {files}. مادها نباید این را داشته باشند - اگر به منبع اعتماد نداری استفاده نکن."),
    "v_nocontent": ("No known game folders (def, vehicle, map, sound...) found inside.",
                    "هیچ پوشه‌ی شناخته‌شده‌ی بازی (def، vehicle، map، sound ...) داخلش نیست."),
    "v_conflict": ("Overlaps {n} file(s) with other mods ({mods}), e.g. {sample}. The one loaded later wins.",
                   "{n} فایل با مادهای دیگر ({mods}) تداخل دارد، مثلاً {sample}. آن که دیرتر لود شود برنده است."),
    "v_log_missing": ("game.log.txt not found yet (start the game once).", "game.log.txt هنوز نیست (یک بار بازی را اجرا کن)."),
    "v_log_none": ("Not mentioned in game.log.txt: the game has not loaded it yet.",
                   "در game.log.txt اسمی از آن نیست: بازی هنوز آن را لود نکرده."),
    "v_log_err": ("The game log reports problems for this mod:\n    {lines}",
                  "لاگ بازی برای این ماد مشکل گزارش کرده:\n    {lines}"),
    "v_log_ok": ("The game loaded it and the log shows no errors for it.",
                 "بازی آن را لود کرده و لاگ خطایی برایش ندارد."),
    "cfg_title": ("config.cfg options (close the game first)", "گزینه‌های config.cfg (اول بازی را ببند)"),
    "cfg_dev": ("Developer mode (g_developer)", "حالت توسعه‌دهنده (g_developer)"),
    "cfg_con": ("In-game console (g_console)", "کنسول بازی (g_console)"),
    "cfg_fmt": ("Save files as editable text (g_save_format = 2)", "ذخیره‌ی سیو به‌صورت متن قابل‌ویرایش (g_save_format = 2)"),
    "cfg_load": ("Reload", "بازخوانی"),
    "cfg_apply": ("Apply settings", "اعمال تنظیمات"),
    "cfg_done": ("config.cfg updated (backup made).", "config.cfg به‌روز شد (بکاپ گرفته شد)."),
    "cfg_note": ("Tip: with 'save as text' on, the game writes plain-text saves.",
                 "نکته: با روشن بودن «سیو متنی»، بازی سیوها را متنی می‌نویسد."),
    "appearance_title": ("Appearance & Themes", "ظاهر و تم‌ها"),
    "appearance_note": ("Choose a cockpit-inspired palette. Changes apply across the workspace and are saved on this PC.",
                         "یک پالت الهام‌گرفته از داشبورد کامیون انتخاب کن. تغییرات در محیط برنامه اعمال و روی همین رایانه ذخیره می‌شود."),
    "theme_selected": ("SELECTED", "انتخاب‌شده"),
    "theme_accent_custom": ("Choose custom accent…", "انتخاب رنگ تأکیدی دلخواه…"),
    "theme_reset_accent": ("Reset to theme default", "بازگشت به رنگ پیش‌فرض تم"),
    "theme_color_title": ("Choose an accent color", "رنگ تأکیدی را انتخاب کن"),
}
T.update({
    "set_theme_hint": ("Pick a theme. It applies instantly (no restart) and is remembered next time.",
                       "یک تم انتخاب کن. بلافاصله (بدون راه‌اندازی دوباره) اعمال می‌شود و دفعه‌ی بعد هم یادش می‌ماند."),
    "set_accent": ("Accent colour", "رنگ تأکیدی"),
    "accent_hex": ("Hex colour", "کد رنگ"),
    "accent_apply": ("Apply", "اعمال"),
    "accent_reset": ("Reset to Theme Default", "بازگشت به پیش‌فرض تم"),
    "accent_theme_default": ("Theme default", "پیش‌فرض تم"),
    "accent_custom": ("Custom", "دلخواه"),
    "accent_invalid": ("Enter a colour like #38bdf8 (3 or 6 hex digits).",
                       "یک رنگ مثل #38bdf8 وارد کن (۳ یا ۶ رقم هگز)."),
    "accent_saved": ("Accent colour: {c}", "رنگ تأکیدی: {c}"),
    "theme_applied": ("Theme: {name}", "تم: {name}"),
    "theme_reset": ("Accent colour and day/night mode reset to the theme default.",
                    "رنگ تأکیدی و حالت روز/شب به پیش‌فرض تم برگشت."),
    "theme_mode_night": ("NIGHT", "شب"),
    "theme_mode_day": ("DAY", "روز"),
    "set_appearance_note": ("Saved automatically in {p}", "به‌صورت خودکار ذخیره می‌شود در {p}"),
    "settings_save_failed": ("Could not save the appearance settings; they apply for this session only.",
                             "ذخیره‌ی تنظیمات ظاهر ممکن نشد؛ فقط برای همین نشست اعمال می‌شوند."),
})


THEME_NAMES_FA = {"midnight_cyan": "فیروزه‌ای نیمه‌شب", "racing_red": "قرمز مسابقه",
                  "arctic_blue": "آبی قطبی", "emerald": "زمرد", "purple_neon": "بنفش نئونی",
                  "amber_truck": "کهربایی کامیون", "silver_steel": "فولاد نقره‌ای",
                  "light_professional": "روشن حرفه‌ای"}
T.update({
    "web_workspace": ("WORKSPACE", "فضای کار"),
    "web_game_data": ("ETS2 Documents", "اسناد یورو تراک"),
    "web_choose_game": ("Choose Documents folder", "انتخاب پوشه Documents"),
    "web_no_profile": ("No profile selected", "پروفایلی انتخاب نشده"),
    "web_no_save": ("No save selected", "سیوی انتخاب نشده"),
    "web_overview": ("A clear snapshot of your selected career save.", "نمایی از اطلاعات واقعی سیو انتخاب‌شده."),
    "web_career_snapshot": ("Career snapshot", "نمای کلی مسیر"),
    "web_fleet_snapshot": ("Fleet overview", "نمای ناوگان"),
    "web_account_balance": ("Account balance", "موجودی حساب"),
    "web_driver_level": ("Driver level", "سطح راننده"),
    "web_experience": ("Experience points", "امتیاز تجربه"),
    "web_owned_trucks": ("Owned trucks", "کامیون‌های مالکیت‌شده"),
    "web_owned_trailers": ("Owned trailers", "تریلرهای مالکیت‌شده"),
    "web_garages": ("Populated garages", "گاراژهای دارای وسیله"),
    "web_distance": ("Known truck distance", "مسافت ثبت‌شدهٔ کامیون‌ها"),
    "web_activity": ("Save details", "جزئیات سیو"),
    "web_progress": ("Verified values", "مقادیر قابل‌استخراج"),
    "web_no_fabricated": ("Only values found in this save are shown. Unavailable fields are not estimated.", "فقط داده‌های موجود در سیو نمایش داده می‌شوند؛ برای موارد ناموجود عدد تخمینی ساخته نمی‌شود."),
    "web_unavailable": ("Information unavailable", "اطلاعات در دسترس نیست"),
    "web_profile": ("Profile", "پروفایل"),
    "web_save": ("Save", "سیو"),
    "web_select_profile": ("Select a profile", "انتخاب پروفایل"),
    "web_select_save": ("Select a save", "انتخاب سیو"),
    "web_modified": ("Last modified", "آخرین تغییر"),
    "web_save_format": ("Save format", "قالب سیو"),
    "web_read_only": ("Read only", "فقط‌خواندنی"),
    "web_writable": ("Editable text save", "سیو متنی قابل‌ویرایش"),
    "web_apply": ("Apply changes", "اعمال تغییرات"),
    "web_backup": ("Rolling backup", "بکاپ چرخشی"),
    "web_restore": ("Restore backup", "بازیابی بکاپ"),
    "web_restore_confirm": ("Restore the selected rolling backup? The current save will be backed up first.", "بکاپ چرخشی بازیابی شود؟ ابتدا از سیو فعلی بکاپ گرفته می‌شود."),
    "web_money": ("Money (€)", "پول (€)"),
    "web_xp": ("Experience (XP)", "تجربه (XP)"),
    "web_level": ("Level", "سطح"),
    "web_skills": ("Skills", "مهارت‌ها"),
    "web_adr": ("ADR classes", "کلاس‌های ADR"),
    "web_unlock_cities": ("Unlock known cities", "بازکردن شهرهای شناخته‌شده"),
    "web_select_truck": ("Select truck", "انتخاب کامیون"),
    "web_truck_info": ("Truck information", "اطلاعات کامیون"),
    "web_tuning_estimate": ("Estimated performance · not live telemetry", "عملکرد تخمینی · تله‌متری زنده نیست"),
    "web_rpm": ("RPM", "دور موتور"),
    "web_speed": ("Speed", "سرعت"),
    "web_power": ("Power", "توان"),
    "web_torque": ("Torque", "گشتاور"),
    "web_accel": ("0–100 estimate", "تخمین صفر تا ۱۰۰"),
    "web_top_speed": ("Top speed estimate", "تخمین سرعت نهایی"),
    "web_gear_count": ("Forward gears", "دنده‌های جلو"),
    "web_differential": ("Differential ratio", "نسبت دیفرانسیل"),
    "web_engine_brake": ("Engine brake", "ترمز موتوری"),
    "web_retarder": ("Retarder", "ریتاردر"),
    "web_mass": ("Truck + cargo mass (kg)", "وزن کامیون و بار (کیلوگرم)"),
    "web_suggest": ("Suggest setup", "پیشنهاد تنظیمات"),
    "web_build_mod": ("Build selectable parts mod", "ساخت ماد قطعات قابل‌خرید"),
    "web_save_preset": ("Save setup", "ذخیره تنظیمات"),
    "web_load_preset": ("Load setup", "بارگذاری تنظیمات"),
    "web_preset_name": ("Setup name", "نام تنظیمات"),
    "web_compatibility": ("Compatibility versions (optional)", "نسخه‌های سازگار (اختیاری)"),
    "web_garage_city": ("Garage / city", "گاراژ / شهر"),
    "web_model": ("Truck definition", "شناسهٔ کامیون"),
    "web_chassis": ("Chassis", "شاسی"),
    "web_cabin": ("Cabin", "کابین"),
    "web_engine_part": ("Engine part", "قطعهٔ موتور"),
    "web_transmission": ("Transmission", "گیربکس"),
    "web_transmission_wear": ("Transmission wear", "فرسودگی گیربکس"),
    "web_cabin_wear": ("Cabin wear", "فرسودگی کابین"),
    "web_chassis_wear": ("Chassis wear", "فرسودگی شاسی"),
    "web_wheels_wear": ("Wheel wear", "فرسودگی چرخ‌ها"),
    "web_license_plate": ("License plate", "پلاک"),
    "web_odometer": ("Odometer", "کیلومترشمار"),
    "web_damage": ("Wear / damage", "فرسودگی / خسارت"),
    "web_fuel": ("Relative fuel", "سطح نسبی سوخت"),
    "web_repair": ("Repair", "تعمیر"),
    "web_refuel": ("Refuel", "سوخت‌گیری"),
    "web_group_repair": ("Repair owned fleet", "تعمیر ناوگان مالکیت‌شده"),
    "web_group_refuel": ("Refuel owned trucks", "سوخت‌گیری کامیون‌های مالکیت‌شده"),
    "web_group_confirm": ("Run {action} for {count} listed owned vehicles? The game must be closed. The existing rolling backup policy will run first.", "{action} روی {count} وسیلهٔ فهرست‌شده اجرا شود؟ بازی باید بسته باشد و سیاست فعلی بکاپ چرخشی پیش از نوشتن اجرا می‌شود."),
    "web_engine": ("Engine", "موتور"),
    "web_gearbox": ("Gearbox", "گیربکس"),
    "web_brakes": ("Brakes & handling", "ترمز و هندلینگ"),
    "web_weight": ("Weight", "وزن"),
    "web_profiles_saves": ("Profiles & saves", "پروفایل‌ها و سیوها"),
    "web_save_details": ("Save details", "جزئیات سیو"),
    "web_game_settings": ("Game settings", "تنظیمات بازی"),
    "web_install_path": ("ETS2 Documents path", "مسیر اسناد ETS2"),
    "web_dev_mode": ("Developer mode", "حالت توسعه‌دهنده"),
    "web_console": ("In-game console", "کنسول داخل بازی"),
    "web_text_saves": ("Editable text saves", "سیو متنی قابل‌ویرایش"),
    "web_text_mode_title": ("Change save mode", "تغییر حالت سیو"),
    "web_text_mode_button": ("Enable editable text saves", "فعال‌کردن سیو متنی قابل‌ویرایش"),
    "web_text_mode_explanation": ("This changes the game's setting for future saves. It does not convert or edit the selected save immediately.", "این گزینه تنظیم بازی را برای سیوهای بعدی تغییر می‌دهد؛ سیوی که الان انتخاب شده فوراً تبدیل یا ویرایش نمی‌شود."),
    "web_text_mode_confirm": ("Close ETS2 first. The game setting will be changed so that future saves can be plain text. Continue?", "ابتدا بازی ETS2 را ببند. تنظیم بازی طوری عوض می‌شود که سیوهای بعدی متنی باشند. ادامه می‌دهی؟"),
    "web_text_mode_steps": ("Setting updated with a config backup. Open ETS2, load this profile, save in-game, exit the game, then reload that new save here. The current encrypted save remains unchanged.", "تنظیم با تهیهٔ بکاپ config تغییر کرد. ETS2 را باز کن، همین پروفایل را بارگذاری و داخل بازی سیو کن؛ بعد بازی را ببند و سیو جدید را دوباره اینجا بارگذاری کن. سیو رمزگذاری‌شدهٔ فعلی دست‌نخورده می‌ماند."),
    "web_save_settings": ("Save game settings", "ذخیره تنظیمات بازی"),
    "web_mod_name": ("Mod name", "نام ماد"),
    "web_mod_status": ("Status", "وضعیت"),
    "web_mod_order": ("Load order", "ترتیب بارگذاری"),
    "web_mod_size": ("Size", "حجم"),
    "web_active": ("Active", "فعال"),
    "web_inactive": ("Disabled", "غیرفعال"),
    "web_missing_file": ("File unavailable locally", "فایل در پوشهٔ محلی موجود نیست"),
    "web_add_mod": ("Add mod…", "افزودن ماد…"),
    "web_enable_all": ("Enable all", "فعال‌سازی همه"),
    "web_disable_all": ("Disable all", "غیرفعال‌سازی همه"),
    "web_validate": ("Validate", "اعتبارسنجی"),
    "web_validate_static": ("Static validation does not prove in-game compatibility.", "اعتبارسنجی ایستا سازگاری داخل بازی را ثابت نمی‌کند."),
    "web_mod_details": ("Mod details", "جزئیات ماد"),
    "web_log": ("Game log", "لاگ بازی"),
    "web_backup_history": ("Backup history", "تاریخچهٔ بکاپ"),
    "web_dealers": ("Unlock dealers", "بازکردن نمایندگی‌ها"),
    "web_open_log": ("View game log", "نمایش لاگ بازی"),
    "web_appearance": ("Appearance & themes", "ظاهر و تم‌ها"),
    "web_dashboard_kicker": ("CAREER / PROFILE SNAPSHOT", "نمای کلی مسیر / پروفایل"),
    "web_tuning_kicker": ("TUNING LAB / INPUT SIMULATOR", "آزمایشگاه تیونینگ / شبیه‌ساز ورودی"),
    "web_tuning_versions_hint": ("Optional; verify against game.log", "اختیاری؛ با game.log بررسی کن"),
    "web_off": ("Off", "خاموش"),
    "web_on": ("On", "روشن"),
    "web_binary_save": ("Binary save", "سیو باینری"),
    "web_text_save": ("Text save", "سیو متنی"),
    "web_theme_previews": ("Theme previews", "پیش‌نمایش تم‌ها"),
    "web_custom_accent": ("Custom accent color", "رنگ تأکیدی دلخواه"),
    "web_reset_accent": ("Reset to Theme Default", "بازگشت به رنگ پیش‌فرض تم"),
    "web_choose_color": ("Choose color…", "انتخاب رنگ…"),
    "web_day_night": ("Day / night", "روز / شب"),
    "web_theme_saved": ("Theme preference saved", "تنظیم تم ذخیره شد"),
    "web_confirm_edit": ("Apply these edits to the selected save? A verified rolling backup will replace the previous one first.", "تغییرها روی سیو انتخاب‌شده اعمال شوند؟ ابتدا بکاپ چرخشیِ تأییدشده جایگزین نسخهٔ قبلی می‌شود."),
    "web_success": ("Completed successfully", "با موفقیت انجام شد"),
    "web_error": ("Operation failed", "عملیات ناموفق بود"),
    "web_no_profiles": ("No profiles found in this folder.", "در این پوشه پروفایلی پیدا نشد."),
    "web_no_saves": ("No saves found in this profile.", "در این پروفایل سیوی پیدا نشد."),
    "web_no_trucks": ("No owned trucks found in the selected save.", "در سیو انتخاب‌شده کامیون مالکیت‌شده‌ای پیدا نشد."),
    "web_select_save_first": ("Choose a profile and save first.", "ابتدا پروفایل و سیو را انتخاب کن."),
    "web_saved_to": ("Saved to {path}", "در این مسیر ذخیره شد: {path}"),
    "web_static_only": ("Built successfully; in-game compatibility is not verified.", "ساخت فایل انجام شد؛ سازگاری داخل بازی تأیید نشده است."),
    "web_estimate_note": ("Performance values are estimates from the selected inputs, not live game data.", "مقادیر عملکرد بر اساس ورودی‌ها تخمینی هستند و دادهٔ زندهٔ بازی نیستند."),
    "web_backup_not_found": ("No rolling backup is available for this save.", "برای این سیو بکاپ چرخشی موجود نیست."),
    "web_no_log": ("The game log is unavailable.", "لاگ بازی در دسترس نیست."),
    "web_appearance_note": ("Choose a cockpit-inspired palette. Theme changes apply immediately and are saved on this PC.", "یک پالت الهام‌گرفته از داشبورد کامیون انتخاب کن؛ تغییر تم فوراً اعمال و روی همین رایانه ذخیره می‌شود."),
    "web_theme_colors": ("Eight themed palettes", "هشت پالت رنگی"),
    "web_game_folder_required": ("Choose the Euro Truck Simulator 2 Documents folder to continue.", "برای ادامه پوشه Documents بازی Euro Truck Simulator 2 را انتخاب کن."),
    "web_garage_summary": ("Owned vehicles grouped by verified garage references.", "وسایل مالکیت‌شده بر اساس ارجاع‌های معتبر گاراژ گروه‌بندی شده‌اند."),
    "web_data_unavailable": ("Not available in this save", "در این سیو موجود نیست"),
    "web_confirm_bulk_mods": ("Change the active mod list for this profile?", "فهرست مادهای فعال این پروفایل تغییر کند؟"),
    "web_shortcut_hint": ("Keyboard: Ctrl+1…9 switches pages · Tab navigates controls", "صفحه‌کلید: Ctrl+۱ تا ۹ برای جابه‌جایی صفحه · Tab برای کنترل‌ها"),
})

LEVEL_FA = {"Newbie": "تازه‌کار", "Enthusiast": "علاقه‌مند", "Skilled Worker": "کارگر ماهر",
            "Professional": "حرفه‌ای", "Master": "استاد", "Instructor": "مربی", "Elite": "نخبه",
            "King of the Road": "پادشاه جاده", "Legend": "افسانه", "Divine Champion": "قهرمان الهی"}
SKILL_FA = {"long_dist": "مسافت طولانی", "heavy": "محموله‌ی باارزش", "fragile": "محموله‌ی شکستنی",
            "urgent": "تحویل به‌موقع", "mechanical": "رانندگی اقتصادی"}
ADR_FA = ["کلاس ۱ - مواد منفجره", "کلاس ۲ - گازها", "کلاس ۳ - مایعات قابل‌اشتعال",
          "کلاس ۴ - جامدات قابل‌اشتعال", "کلاس ۶ - سمی", "کلاس ۸ - خورنده"]


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    def log_tk_callback_error(exc, value, tb):
        logging.error("Unhandled Tk callback exception: %s", value,
                      exc_info=(exc, value, tb))
        try:
            messagebox.showerror(APP, "An unexpected error occurred. Details were saved to:\n" +
                                 str(configure_error_logging()))
        except Exception:
            pass
    root.report_callback_exception = log_tk_callback_error
    root.geometry("1360x880")
    root.minsize(1080, 740)
    style = ttk.Style()

    theme_cfg = editor_storage_root() / "settings" / "ui.json"
    preset_file = theme_cfg.parent / "tuning_presets.json"
    old_user_data = Path(os.environ.get("APPDATA", str(Path.home()))) / "ETS2 Save Editor"
    for old_file, new_file in ((old_user_data / "ui.json", theme_cfg),
                               (old_user_data / "tuning_presets.json", preset_file)):
        if not new_file.exists() and old_file.is_file():
            try:
                new_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old_file, new_file)
            except OSError:
                pass
    tune_presets = load_tuning_presets(preset_file)
    try:
        saved_theme = json.loads(theme_cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        saved_theme = {}
    appearance = sanitize_appearance(saved_theme)       # theme / mode / custom_accent
    themed_widgets = []     # (widget, {option: palette role or callable(palette)})
    redraw_hooks = []       # callables that repaint canvases / text tags after a theme change
    scroll_areas = []       # scrollable settings areas (mouse-wheel routing)

    def colors():
        return ui_palette(appearance)

    def accent_color():
        return colors()["accent"]

    def save_appearance():
        """Persist theme, mode and accent atomically. Returns False if the file can't be written."""
        data = sanitize_appearance(appearance)
        try:
            theme_cfg.parent.mkdir(parents=True, exist_ok=True)
            tmp = theme_cfg.with_name(theme_cfg.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, theme_cfg)
            return True
        except OSError:
            return False

    def apply_appearance():
        """(Re)define every ttk style from the central palette. ttk widgets update instantly."""
        c = colors()
        bg, panel, card = c["background"], c["surface"], c["card"]
        fg, muted, accent = c["text_primary"], c["text_secondary"], c["accent"]
        atext, afg, border = c["accent_text"], c["accent_foreground"], c["border"]
        field, field_fg = c["input_background"], c["input_foreground"]
        base_font = ("Tahoma", 10) if lang["i"] else ("Segoe UI", 10)
        if style.theme_use() != "clam":
            style.theme_use("clam")
        style.configure(".", background=bg, foreground=fg, font=base_font, bordercolor=border,
                        troughcolor=bg, focuscolor=accent, lightcolor=panel, darkcolor=panel)
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("Muted.TLabel", background=bg, foreground=muted)
        style.configure("Bar.TLabel", background=panel, foreground=muted)
        style.configure("Error.TLabel", background=bg, foreground=c["error"])
        style.configure("Success.TLabel", background=bg, foreground=c["success"])
        for name in ("TLabelframe", "TLabelFrame"):
            style.configure(name, background=bg, foreground=atext, bordercolor=border,
                            lightcolor=border, darkcolor=border, relief="solid", borderwidth=1)
            style.configure(name + ".Label", background=bg, foreground=atext,
                            font=("Segoe UI Semibold", 10))
        style.configure("TButton", background=panel, foreground=fg, bordercolor=border,
                        lightcolor=border, darkcolor=border, padding=(12, 7), relief="flat")
        style.map("TButton",
                  background=[("disabled", panel), ("pressed", accent), ("active", c["accent_hover"])],
                  foreground=[("disabled", c["text_dim"]), ("pressed", afg), ("active", afg)])
        style.configure("TCheckbutton", background=bg, foreground=fg, indicatorbackground=field,
                        indicatorforeground=fg)
        style.map("TCheckbutton", background=[("active", bg)], foreground=[("disabled", c["text_dim"])],
                  indicatorbackground=[("selected", accent), ("!selected", field)],
                  indicatorforeground=[("selected", afg)])
        style.configure("TNotebook", background=bg, bordercolor=border)
        style.configure("TNotebook.Tab", background=panel, foreground=muted, padding=(14, 9),
                        font=("Segoe UI Semibold", 9))
        style.map("TNotebook.Tab", background=[("selected", accent), ("active", c["accent_soft"])],
                  foreground=[("selected", afg), ("active", fg)])
        style.configure("TEntry", fieldbackground=field, foreground=field_fg, insertcolor=field_fg,
                        padding=7, bordercolor=border)
        style.map("TEntry", fieldbackground=[("disabled", panel)], foreground=[("disabled", c["text_dim"])])
        style.configure("TCombobox", fieldbackground=field, background=panel, foreground=field_fg,
                        arrowcolor=atext, padding=6, bordercolor=border)
        style.map("TCombobox", fieldbackground=[("readonly", field)], foreground=[("readonly", field_fg)],
                  selectbackground=[("readonly", field)], selectforeground=[("readonly", field_fg)])
        style.configure("TSpinbox", fieldbackground=field, background=panel, foreground=field_fg,
                        arrowcolor=atext, padding=5, bordercolor=border)
        style.configure("TScrollbar", background=panel, troughcolor=bg, bordercolor=border,
                        arrowcolor=muted, lightcolor=panel, darkcolor=panel)
        style.map("TScrollbar", background=[("active", c["accent_soft"])])
        style.configure("TPanedwindow", background=bg)
        style.configure("Sash", background=border, sashthickness=6)
        style.configure("Treeview", background=card, fieldbackground=card, foreground=fg,
                        rowheight=24, bordercolor=border, relief="flat", font=base_font)
        style.configure("Treeview.Heading", background=panel, foreground=atext, padding=(8, 9),
                        font=("Segoe UI Semibold", 9) if not lang["i"] else base_font)
        style.map("Treeview", background=[("selected", accent)], foreground=[("selected", afg)])
        style.configure("TSeparator", background=border)
        style.configure("DashCard.TFrame", background=card, bordercolor=border, borderwidth=1,
                        relief="solid")
        style.configure("DashTitle.TLabel", background=card, foreground=fg, font=("Segoe UI Semibold", 15))
        style.configure("DashMuted.TLabel", background=card, foreground=muted)
        style.configure("DashAccent.TLabel", background=card, foreground=atext,
                        font=("Segoe UI Semibold", 9))
        # tuning workspace
        style.configure("Tune.TFrame", background=bg)
        style.configure("TuneCard.TFrame", background=card, bordercolor=border, borderwidth=1, relief="solid")
        style.configure("TuneInner.TFrame", background=card)
        style.configure("TuneTitle.TLabel", background=card, foreground=fg, font=("Segoe UI Semibold", 13))
        style.configure("TuneMuted.TLabel", background=card, foreground=muted, font=("Segoe UI", 9))
        style.configure("TuneValue.TLabel", background=card, foreground=fg, font=("Segoe UI Semibold", 18))
        style.configure("TuneAccent.TLabel", background=card, foreground=atext, font=("Segoe UI Semibold", 9))
        style.configure("TuneGold.TLabel", background=card, foreground=c["warning"],
                        font=("Segoe UI Semibold", 9))
        root.configure(background=bg)
        # popdown lists of comboboxes created later
        root.option_add("*TCombobox*Listbox.background", field)
        root.option_add("*TCombobox*Listbox.foreground", field_fg)
        root.option_add("*TCombobox*Listbox.selectBackground", accent)
        root.option_add("*TCombobox*Listbox.selectForeground", afg)

    def _apply_roles(widget, roles, pal):
        widget.configure(**{opt: (role(pal) if callable(role) else pal[role])
                            for opt, role in roles.items()})

    def reg(widget, **roles):
        """Register a plain Tk widget so retheme() can recolour it; applies the current palette now."""
        themed_widgets.append((widget, roles))
        try:
            _apply_roles(widget, roles, colors())
        except tk.TclError:
            pass
        return widget

    def tk_frame(parent, bg, **kw):
        return reg(tk.Frame(parent, **kw), bg=bg)

    def tk_label(parent, bg, fg, **kw):
        return reg(tk.Label(parent, **kw), bg=bg, fg=fg)

    def reg_text(widget):
        """Style a tk.Text/Entry-like widget with the input colours of the palette."""
        return reg(widget, bg="input_background", fg="input_foreground",
                   insertbackground="input_foreground", selectbackground="accent",
                   selectforeground="accent_foreground", highlightbackground="border",
                   highlightcolor="accent")

    def iter_widgets(widget):
        yield widget
        for child in widget.winfo_children():
            yield from iter_widgets(child)

    def refresh_combobox_popdowns(pal):
        for widget in iter_widgets(root):
            if isinstance(widget, ttk.Combobox):
                try:
                    popdown = root.tk.call("ttk::combobox::PopdownWindow", widget)
                    root.tk.call(f"{popdown}.f.l", "configure", "-background", pal["input_background"],
                                 "-foreground", pal["input_foreground"], "-selectbackground", pal["accent"],
                                 "-selectforeground", pal["accent_foreground"])
                except tk.TclError:
                    pass

    def retheme():
        """Switch the whole UI to the current appearance in place: no widget is destroyed."""
        pal = colors()
        apply_appearance()
        alive = []
        for widget, roles in themed_widgets:
            try:
                if not widget.winfo_exists():
                    continue
                _apply_roles(widget, roles, pal)
            except tk.TclError:
                continue
            alive.append((widget, roles))
        themed_widgets[:] = alive
        refresh_combobox_popdowns(pal)
        for hook in list(redraw_hooks):
            try:
                hook()
            except tk.TclError:
                pass
        root.update_idletasks()

    lang = {"i": 0}            # 0 = English, 1 = Persian

    def tr(key, **kw):
        s = T[key][lang["i"]]
        return s.format(**kw) if kw else s

    # persistent state (survives language switches)
    st = {"profiles": [], "saves": [], "path": None, "mods": [], "mods_other": {},
          "loaded_values": None, "loaded_fingerprint": None, "backup_items": {},
          "validation_running": False, "mod_copy_running": False, "save_writable": False,
          "fleet": None, "fleet_error": None, "fleet_selected": None}
    var_dir = tk.StringVar()
    var_money = tk.StringVar()
    var_xp = tk.StringVar()
    var_rank = tk.StringVar()
    var_cities = tk.StringVar(value="-")
    var_unlock = tk.BooleanVar(value=False)
    var_status = tk.StringVar()
    var_dirty = tk.StringVar(value="")
    tune_truck_vars = {name: tk.BooleanVar(value=True) for name in TUNING_TRUCKS}
    var_torque_factor = tk.StringVar(value="3500")
    var_rpm_factor = tk.StringVar(value="3000")
    var_hp_target = tk.StringVar(value="750")
    var_target_speed = tk.StringVar(value="200")
    var_target_accel = tk.StringVar(value="30")
    var_mass = tk.StringVar(value="28000")
    var_gear_count = tk.StringVar(value="12")
    var_differential = tk.StringVar(value="2.64")
    var_engine_brake = tk.StringVar(value="1.5")
    var_retarder = tk.StringVar(value="3")
    var_mod_output = tk.StringVar(value=str(editor_storage_root() / "generated_mods"))
    var_tune_compat = tk.StringVar()
    var_preset_name = tk.StringVar()
    var_compare_result = tk.StringVar(value="")
    var_gauge_target = tk.DoubleVar(value=110.0)
    needle_speed = {"value": 0.0, "target": 99.0, "job": None}
    var_gver = tk.StringVar()
    var_accent_hex = tk.StringVar()
    var_accent_msg = tk.StringVar()
    skill_vars = {k: tk.IntVar(value=0) for k in SKILLS}
    adr_vars = [tk.BooleanVar(value=False) for _ in ADR_CLASSES]
    ex_vars = {k: tk.BooleanVar(value=False) for k in ("dealers", "repair", "refuel")}
    cfg_vars = {k: tk.BooleanVar(value=False) for k in CONFIG_KEYS}
    w = {}                     # widgets that get rebuilt
    cur = {"profile_idx": -1}
    active_page = {"key": "dashboard"}
    dash_vars = {k: tk.StringVar(value="—") for k in
                 ("profiles", "saves", "balance", "level", "xp", "cities", "profile", "save", "backup", "garage")}

    def status(msg):
        var_status.set(msg)
        root.update_idletasks()

    def info(key, **kw):
        messagebox.showinfo(tr("title"), tr(key, **kw))

    def warn(key, **kw):
        messagebox.showwarning(tr("title"), tr(key, **kw))

    def err(key, **kw):
        messagebox.showerror(tr("title"), tr(key, **kw))

    def skill_name(k):
        return SKILL_FA[k] if lang["i"] else SKILL_LABELS[k]

    def tier_name(n):
        return LEVEL_FA.get(n, n) if lang["i"] else n

    def num(s, lo, hi, name):
        try:
            v = int(str(s).replace(",", "").replace(" ", "").strip())
        except ValueError:
            raise ValueError(tr("num_err", n=name))
        return max(lo, min(hi, v))

    def current_profile():
        i = cur["profile_idx"]
        if 0 <= i < len(st["profiles"]):
            return st["profiles"][i][1]
        return None

    def selected_save_details():
        path = st.get("path")
        if not path:
            return tr("dash_save_value")
        try:
            modified = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(Path(path).stat().st_mtime))
        except OSError:
            modified = tr("dash_mtime_unavailable")
        return tr("dash_save_details", path=path, modified=modified)

    def has_pending_edits():
        original = st.get("loaded_values")
        if not st.get("path") or original is None:
            return False
        try:
            money = int(var_money.get().replace(",", "").strip())
            xp = int(var_xp.get().replace(",", "").strip())
            adr = sum(bit for (bit, _), flag in zip(ADR_CLASSES, adr_vars) if flag.get())
            if money != original["money"] or xp != original["xp"] or adr != original["adr"]:
                return True
            if any(skill_vars[k].get() != original[k] for k in SKILLS):
                return True
            if var_unlock.get() or any(flag.get() for flag in ex_vars.values()):
                return True
            return False
        except (ValueError, tk.TclError, KeyError):
            return True

    def refresh_dirty_indicator(*_):
        var_dirty.set(tr("dirty_label") if has_pending_edits() else "")

    for tracked_var in (var_money, var_xp, var_unlock, *skill_vars.values(),
                        *adr_vars, *ex_vars.values()):
        tracked_var.trace_add("write", refresh_dirty_indicator)

    def refresh_dashboard():
        dash_vars["profiles"].set(str(len(st["profiles"])))
        dash_vars["saves"].set(str(len(st["saves"])))
        profile = current_profile()
        dash_vars["profile"].set(st["profiles"][cur["profile_idx"]][0] if profile is not None
                                  else tr("dash_profile_value"))
        dash_vars["save"].set(selected_save_details())
        fleet = st.get("fleet")
        if not st["path"]:
            dash_vars["garage"].set(tr("garage_unavailable"))
        elif st.get("fleet_error"):
            dash_vars["garage"].set(tr("garage_limit"))
        elif fleet is None:
            dash_vars["garage"].set(tr("garage_limit"))
        else:
            trailer_count = (len(fleet["trailers"]) if fleet["trailers_available"]
                             else tr("fleet_trailer_unavailable"))
            dash_vars["garage"].set(tr("fleet_summary", trucks=len(fleet["trucks"]),
                                       trailers=trailer_count,
                                       garages=fleet["garage_count"]))
        backup = latest_backup(st["path"]) if st["path"] else None
        dash_vars["backup"].set(time.strftime("%Y-%m-%d %H:%M", time.localtime(backup.stat().st_mtime))
                                 if backup and backup.is_file() else tr("dash_no_backup"))
        dash_vars["balance"].set(tr("dash_unavailable"))
        dash_vars["level"].set(tr("dash_unavailable"))
        dash_vars["xp"].set(tr("dash_unavailable"))
        dash_vars["cities"].set(tr("dash_unavailable"))
        if st["path"]:
            try:
                values = SaveFile(st["path"]).read()
                level, rank, _ = rank_for_xp(values["xp"])
                dash_vars["balance"].set(f"€ {values['money']:,}")
                dash_vars["level"].set(f"{tier_name(rank)} · level {level}+" if not lang["i"]
                                        else f"{tier_name(rank)} · حداقل سطح {level}")
                dash_vars["xp"].set(f"{values['xp']:,} XP")
                dash_vars["cities"].set(f"{values['visited']} visited" if not lang["i"]
                                         else f"{values['visited']} بازدیدشده")
            except Exception:
                pass
        refresh_dirty_indicator()

    # ---------------------------------------------------------- builders
    container = ttk.Frame(root)
    container.pack(fill="both", expand=True)

    # ---------------------------------------------------------- appearance actions
    def accent_choices():
        return [tr("accent_theme_default")] + list(LEGACY_ACCENTS)

    def accent_label():
        custom = appearance.get("custom_accent")
        if custom is None:
            return tr("accent_theme_default")
        for name, value in LEGACY_ACCENTS.items():
            if value == custom:
                return name
        return tr("accent_custom")

    def theme_display_name(theme_id):
        return THEME_NAMES_FA.get(theme_id, THEME_PRESETS[theme_id]["name"]) if lang["i"] \
            else THEME_PRESETS[theme_id]["name"]

    def commit_appearance(message=None):
        saved = save_appearance()
        retheme()
        if not saved:
            status(tr("settings_save_failed"))
        elif message:
            status(message)

    def set_theme(theme_id):
        if theme_id not in THEME_PRESETS:
            return
        appearance["theme"] = theme_id
        appearance["mode"] = THEME_PRESETS[theme_id]["mode"]      # a theme starts in its native mode
        commit_appearance(tr("theme_applied", name=theme_display_name(theme_id)))

    def set_accent(value):
        """value: '#rrggbb' (also '#rgb'/'rrggbb') or None for the theme default."""
        if value is None:
            appearance["custom_accent"] = None
            commit_appearance(tr("accent_saved", c=tr("accent_theme_default")))
            return True
        normalized = normalize_hex_color(value)
        if normalized is None:
            return False
        appearance["custom_accent"] = normalized
        commit_appearance(tr("accent_saved", c=normalized))
        return True

    def reset_theme_defaults():
        appearance["custom_accent"] = None
        appearance["mode"] = THEME_PRESETS[appearance["theme"]]["mode"]
        commit_appearance(tr("theme_reset"))

    def toggle_theme():
        appearance["mode"] = "day" if appearance["mode"] == "night" else "night"
        commit_appearance()

    def choose_accent_from_box(_=None):
        box = w.get("accent_box")
        if box is None:
            return
        name = box.get()
        if name == tr("accent_theme_default"):
            set_accent(None)
        elif name in LEGACY_ACCENTS:
            set_accent(LEGACY_ACCENTS[name])
        else:
            sync_topbar_controls()

    def sync_topbar_controls():
        box, btn = w.get("accent_box"), w.get("theme_btn")
        if box is not None and box.winfo_exists():
            box.set(accent_label())
        if btn is not None and btn.winfo_exists():
            btn.configure(text=tr("theme_day") if appearance["mode"] == "night" else tr("theme_night"))

    def sync_accent_controls():
        var_accent_hex.set(colors()["accent"])
        swatch = w.get("accent_swatch")
        if swatch is not None and swatch.winfo_exists():
            swatch.delete("all")
            swatch.create_rectangle(1, 1, 33, 33, fill=colors()["accent"], outline=colors()["border"], width=2)
        var_accent_msg.set("")

    def apply_accent_from_entry(_=None):
        if not set_accent(var_accent_hex.get()):
            var_accent_msg.set(tr("accent_invalid"))

    def pick_accent_dialog():
        from tkinter import colorchooser
        try:
            _rgb, chosen = colorchooser.askcolor(color=colors()["accent"], parent=root,
                                                 title=tr("theme_color_title"))
        except tk.TclError:
            return
        if chosen:
            set_accent(chosen)

    def paint_theme_card(cv, theme_id):
        current = theme_id == appearance["theme"]
        if current:
            p = ui_palette(appearance)
        else:
            p = ui_palette({"theme": theme_id, "mode": THEME_PRESETS[theme_id]["mode"], "custom_accent": None})
        now = colors()
        cw, ch = 188, 122
        cv.delete("all")
        cv.create_rectangle(0, 0, cw, ch, fill=p["background"], outline="")
        cv.create_rectangle(0, 0, 40, 92, fill=p["sidebar"], outline="")
        cv.create_rectangle(8, 10, 32, 22, fill=p["accent"], outline="")
        for k in range(3):
            cv.create_rectangle(8, 32 + k * 14, 32, 40 + k * 14,
                                fill=p["accent_soft"] if k == 0 else p["text_dim"], outline="")
        cv.create_rectangle(48, 8, cw - 8, 24, fill=p["surface"], outline=p["border"])
        cv.create_rectangle(48, 32, 112, 84, fill=p["card"], outline=p["border"])
        cv.create_rectangle(120, 32, cw - 8, 84, fill=p["card"], outline=p["border"])
        cv.create_rectangle(54, 38, 90, 42, fill=p["accent"], outline="")
        cv.create_rectangle(54, 50, 100, 53, fill=p["text_secondary"], outline="")
        cv.create_rectangle(54, 58, 92, 61, fill=p["text_dim"], outline="")
        cv.create_rectangle(126, 38, 150, 52, fill=p["accent"], outline="")
        cv.create_rectangle(126, 58, 170, 61, fill=p["text_secondary"], outline="")
        cv.create_rectangle(0, 92, cw, ch, fill=p["surface"], outline="")
        cv.create_text(10, 107, text=theme_display_name(theme_id), anchor="w", fill=p["text_primary"],
                       font=("Segoe UI Semibold", 9))
        cv.create_text(cw - 10, 107, text=tr("theme_mode_day" if p["mode"] == "day" else "theme_mode_night"),
                       anchor="e", fill=p["text_secondary"], font=("Segoe UI", 7))
        cv.create_rectangle(1, 1, cw - 1, ch - 1, outline=now["accent"] if current else now["border"],
                            width=3 if current else 1)
        if root.focus_get() is cv:
            cv.create_rectangle(5, 5, cw - 5, ch - 5, outline=now["accent_text"], dash=(3, 3))
        if current:
            cv.create_rectangle(cw - 74, 4, cw - 5, 19, fill=now["accent"], outline="")
            cv.create_text(cw - 39, 11, text=tr("theme_selected"), fill=now["accent_foreground"],
                           font=("Segoe UI Semibold", 7))

    def repaint_theme_cards():
        for theme_id, cv in list(w.get("theme_cards", {}).items()):
            if cv.winfo_exists():
                paint_theme_card(cv, theme_id)

    def _wheel(event):
        if event.num == 4:
            step = -1
        elif event.num == 5:
            step = 1
        else:
            step = -1 if event.delta > 0 else 1
        try:
            under = root.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return
        for area, canvas in list(scroll_areas):
            try:
                inside = under is not None and (str(under) == str(area) or str(under).startswith(str(area) + "."))
                if inside and canvas.winfo_exists():
                    canvas.yview_scroll(step, "units")
                    return
            except tk.TclError:
                continue

    for _seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        root.bind_all(_seq, _wheel, add="+")

    def scrolled_frame(parent):
        holder = ttk.Frame(parent)
        holder.pack(fill="both", expand=True)
        canvas = reg(tk.Canvas(holder, highlightthickness=0, bd=0), bg="background")
        vbar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))
        scroll_areas.append((holder, canvas))
        return inner

    # ---------------------------------------------------------- shell
    def build():
        for c in container.winfo_children():
            c.destroy()
        themed_widgets.clear()
        redraw_hooks.clear()
        scroll_areas.clear()
        root.title("%s %s" % (tr("title"), VERSION))
        try:
            apply_appearance()
        except tk.TclError:
            pass
        shell = ttk.Frame(container)
        shell.pack(fill="both", expand=True)
        sidebar = tk_frame(shell, "sidebar", width=220)
        sidebar.pack(side="right" if lang["i"] else "left", fill="y")
        sidebar.pack_propagate(False)
        brand = tk_frame(sidebar, "sidebar")
        brand.pack(fill="x", padx=16, pady=(18, 24))
        tk_label(brand, "brand_background", "accent_text", text="T",
                 font=("Segoe UI Semibold", 17), width=2, height=1).pack(side="left", padx=(0, 9))
        brand_text = tk_frame(brand, "sidebar")
        brand_text.pack(side="left", fill="x", expand=True)
        tk_label(brand_text, "sidebar", "text_primary", text="TR-EURO-EDITOR",
                 justify="left", anchor="w", font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk_label(brand_text, "sidebar", "text_dim", text="SAVE STUDIO  /  ETS2",
                 justify="left", anchor="w", font=("Segoe UI Semibold", 7)).pack(anchor="w", pady=(2, 0))
        tk_label(sidebar, "sidebar", "text_dim", text="WORKSPACE" if not lang["i"] else "فضای کار",
                 font=("Segoe UI Semibold", 8)).pack(anchor="w", padx=18, pady=(0, 8))
        content = ttk.Frame(shell)
        content.pack(side="left", fill="both", expand=True)
        bar = reg(tk.Frame(content, highlightthickness=1), bg="surface", highlightbackground="border")
        bar.pack(fill="x", padx=18, pady=(12, 8), ipady=5)
        ttk.Button(bar, text=tr("lang_btn"), command=switch_lang).pack(side="right")
        accent_box = ttk.Combobox(bar, values=accent_choices(), state="readonly", width=14)
        accent_box.pack(side="right", padx=6)
        ttk.Label(bar, text=tr("accent"), style="Bar.TLabel").pack(side="right")
        accent_box.bind("<<ComboboxSelected>>", choose_accent_from_box)
        theme_btn = ttk.Button(bar, command=toggle_theme)
        theme_btn.pack(side="right", padx=6)
        w["accent_box"], w["theme_btn"] = accent_box, theme_btn
        tk_label(bar, "surface", "text_secondary", text="ETS2   /   SINGLE PLAYER",
                 font=("Segoe UI Semibold", 9)).pack(side="left", padx=(12, 10))
        tk_label(bar, "surface", "text_primary", textvariable=dash_vars["profile"],
                 font=("Segoe UI Semibold", 9)).pack(side="left", padx=8)
        tk_label(bar, "surface", "text_dim", text="·").pack(side="left")
        tk_label(bar, "surface", "text_secondary", textvariable=dash_vars["save"],
                 font=("Segoe UI", 9)).pack(side="left", padx=8)
        tk_label(bar, "surface", "text_dim", text="BACKUP",
                 font=("Segoe UI Semibold", 8)).pack(side="left", padx=(12, 3))
        tk_label(bar, "surface", "text_secondary", textvariable=dash_vars["backup"],
                 font=("Segoe UI", 8)).pack(side="left", padx=3)
        tk_label(bar, "surface", "warning", textvariable=var_dirty,
                 font=("Segoe UI Semibold", 8)).pack(side="left", padx=(9, 2))
        sync_topbar_controls()
        redraw_hooks.append(sync_topbar_controls)
        page_host = ttk.Frame(content)
        page_host.pack(fill="both", expand=True, padx=16, pady=(2, 8))
        page_defs = [
            ("dashboard", tr("tab_dashboard"), "⌂", build_dashboard),
            ("tune", tr("tab_tune"), "◉", build_tuning),
            ("save", tr("tab_profiles"), "▤", build_save),
            ("garage", tr("tab_garage"), "▣", build_garage),
            ("extras", tr("tab_extras"), "✦", build_extras),
            ("mods", tr("tab_mods"), "⬡", build_mods),
            ("settings", tr("tab_settings"), "⚙", build_settings),
        ]
        pages = {}
        nav_buttons = {}
        for key, label, icon, builder in page_defs:
            panel = ttk.Frame(page_host)
            panel.place(relx=0, rely=0, relwidth=1, relheight=1)
            builder(panel)
            pages[key] = panel
            button_text = f"  {icon}     {label}" if not lang["i"] else f"{label}     {icon}  "
            button = reg(tk.Button(sidebar, text=button_text, anchor="e" if lang["i"] else "w",
                                   relief="flat", bd=0, padx=12, pady=11, cursor="hand2",
                                   font=("Segoe UI Semibold", 10), highlightthickness=1,
                                   command=lambda k=key: show_page(k)),
                         highlightbackground="sidebar", highlightcolor="accent_text")
            button.pack(fill="x", padx=10, pady=2)
            nav_buttons[key] = button
        w["pages"], w["nav_buttons"] = pages, nav_buttons
        tk_label(content, "surface", "text_secondary", textvariable=var_status,
                 anchor="w", padx=10, pady=5, font=("Segoe UI", 9)).pack(fill="x", side="bottom")
        show_page(active_page["key"])
        redraw_hooks.append(lambda: show_page(active_page["key"]))

    def show_page(key):
        active_page["key"] = key
        pages = w.get("pages", {})
        if key in pages:
            pages[key].tkraise()
        c = colors()
        for name, button in w.get("nav_buttons", {}).items():
            selected = name == key
            bg = c["nav_selected"] if selected else c["sidebar"]
            fg = c["accent_text"] if selected else c["text_secondary"]
            button.configure(bg=bg, fg=fg, activebackground=c["nav_selected"],
                             activeforeground=c["accent_text"])

    def build_save(tab):
        pad = {"padx": 8, "pady": 4}
        top = ttk.LabelFrame(tab, text=tr("sec_folder"))
        top.pack(fill="x", **pad)
        ttk.Entry(top, textvariable=var_dir).pack(side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(top, text=tr("browse"), command=browse).pack(side="left", padx=2)
        ttk.Button(top, text=tr("autodetect"), command=detect).pack(side="left", padx=6)
        mid = ttk.LabelFrame(tab, text=tr("sec_profile"))
        mid.pack(fill="both", **pad)
        cmb = ttk.Combobox(mid, state="readonly")
        cmb.pack(fill="x", padx=6, pady=(6, 3))
        tree = ttk.Treeview(mid, columns=("save", "date"), show="headings", height=5,
                            selectmode="browse")
        tree.heading("save", text=tr("col_save"))
        tree.heading("date", text=tr("col_date"))
        tree.column("save", width=300)
        tree.column("date", width=200)
        tree.pack(fill="x", padx=6, pady=(3, 6))
        w["cmb"], w["tree"] = cmb, tree
        cmb.bind("<<ComboboxSelected>>", on_profile)
        tree.bind("<<TreeviewSelect>>", on_save)

        ed = ttk.LabelFrame(tab, text=tr("sec_edit"))
        ed.pack(fill="both", expand=True, **pad)
        r = 0
        ttk.Label(ed, text=tr("money")).grid(row=r, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(ed, textvariable=var_money, width=16).grid(row=r, column=1, sticky="w")
        f = ttk.Frame(ed)
        f.grid(row=r, column=2, columnspan=3, sticky="w")
        for txt, amt in (("+1M", 1000000), ("+10M", 10000000)):
            ttk.Button(f, text=txt, width=6, command=lambda a=amt: add_money(a)).pack(side="left", padx=2)
        ttk.Button(f, text=tr("set100m"), command=lambda: var_money.set("100000000")
                   ).pack(side="left", padx=2)
        r += 1
        ttk.Label(ed, text=tr("xp")).grid(row=r, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(ed, textvariable=var_xp, width=16).grid(row=r, column=1, sticky="w")
        presets = ["%d - %s (%s XP)" % (lv, tier_name(nm), format(xp, ","))
                   for lv, nm, xp in LEVEL_TIERS[1:]]
        presets.append(tr("max_xp", xp=format(MAX_XP, ",")))
        pcf = ttk.Frame(ed)
        pcf.grid(row=r, column=2, columnspan=3, sticky="w")
        pc = ttk.Combobox(pcf, values=presets, state="readonly", width=36)
        pc.pack(side="left", padx=2)
        pc.set(tr("preset"))

        def on_preset(_=None):
            s = pc.get()
            m = re.search(r"\(([\d,]+) XP\)", s)
            if m:
                var_xp.set(m.group(1).replace(",", ""))
        pc.bind("<<ComboboxSelected>>", on_preset)
        ttk.Label(ed, textvariable=var_rank, style="Muted.TLabel").grid(
            row=r + 1, column=1, columnspan=4, sticky="w")
        r += 2
        ttk.Separator(ed).grid(row=r, column=0, columnspan=5, sticky="ew", pady=6)
        r += 1
        ttk.Label(ed, text=tr("skills")).grid(row=r, column=0, sticky="nw", padx=8)
        sf = ttk.Frame(ed)
        sf.grid(row=r, column=1, columnspan=2, sticky="w")
        for i, k in enumerate(SKILLS):
            ttk.Label(sf, text=skill_name(k)).grid(row=i, column=0, sticky="w", pady=1)
            ttk.Spinbox(sf, from_=0, to=6, width=4, textvariable=skill_vars[k]
                        ).grid(row=i, column=1, padx=8)
        ttk.Button(sf, text=tr("max_skills"), command=max_skills).grid(row=5, column=0, pady=4, sticky="w")
        af = ttk.LabelFrame(ed, text=tr("adr_box"))
        af.grid(row=r, column=3, columnspan=2, sticky="nw", padx=12)
        for i, ((bit, nm), v) in enumerate(zip(ADR_CLASSES, adr_vars)):
            ttk.Checkbutton(af, text=ADR_FA[i] if lang["i"] else nm, variable=v).pack(anchor="w", padx=6)
        r += 1
        ttk.Separator(ed).grid(row=r, column=0, columnspan=5, sticky="ew", pady=6)
        r += 1
        ttk.Label(ed, text=tr("cities")).grid(row=r, column=0, sticky="w", padx=8)
        ttk.Label(ed, textvariable=var_cities).grid(row=r, column=1, sticky="w")
        ttk.Checkbutton(ed, text=tr("unlock_cities"), variable=var_unlock
                        ).grid(row=r, column=2, columnspan=3, sticky="w")
        actions = ttk.Frame(tab)
        actions.pack(fill="x", padx=8, pady=(3, 6))
        ttk.Button(actions, text=tr("max_all"), command=max_all).pack(side="left", padx=(0, 5))
        ttk.Button(actions, text=tr("restore"), command=restore).pack(side="left", padx=5)
        ttk.Button(actions, text=tr("apply"), command=apply).pack(side="right")

    def build_dashboard(tab):
        outer = ttk.Frame(tab)
        outer.pack(fill="both", expand=True, padx=14, pady=12)
        intro = ttk.Frame(outer, style="DashCard.TFrame", padding=(18, 14))
        intro.pack(fill="x", pady=(0, 12))
        ttk.Label(intro, text="TR-EURO-EDITOR  /  PROFILE ANALYTICS",
                  style="DashAccent.TLabel").pack(anchor="w")
        ttk.Label(intro, text=tr("dashboard_title"), style="DashTitle.TLabel").pack(anchor="w", pady=(4, 2))
        ttk.Label(intro, text=tr("dashboard_subtitle"), style="DashMuted.TLabel").pack(anchor="w")

        stats = ttk.Frame(outer)
        stats.pack(fill="x", pady=(0, 12))
        metrics = [
            ("dash_profile_count", "profiles", "#38bdf8"),
            ("dash_save_count", "saves", "#34d399"),
            ("dash_balance", "balance", "#f59e0b"),
            ("dash_driver_level", "level", "#a78bfa"),
            ("dash_experience", "xp", "#fb7185"),
            ("dash_city_progress", "cities", "#22d3ee"),
        ]
        for index, (label_key, value_key, _color) in enumerate(metrics):
            card = ttk.Frame(stats, style="DashCard.TFrame", padding=(14, 12))
            card.grid(row=index // 3, column=index % 3, sticky="nsew",
                      padx=(0 if index % 3 == 0 else 5, 5), pady=5)
            stats.columnconfigure(index % 3, weight=1, uniform="dashmetric")
            ttk.Label(card, text=tr(label_key), style="DashAccent.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=dash_vars[value_key], style="DashTitle.TLabel",
                      wraplength=250).pack(anchor="w", pady=(8, 1))

        lower = ttk.Frame(outer)
        lower.pack(fill="both", expand=True)
        fleet = ttk.Frame(lower, style="DashCard.TFrame", padding=16)
        fleet.pack(side="left", fill="both", expand=True, padx=(0, 6))
        ttk.Label(fleet, text=tr("dash_fleet"), style="DashTitle.TLabel").pack(anchor="w")
        ttk.Separator(fleet).pack(fill="x", pady=10)
        ttk.Label(fleet, text=tr("dash_fleet_note"), style="DashMuted.TLabel",
                  wraplength=520, justify="left").pack(anchor="w", pady=(4, 12))
        ttk.Label(fleet, textvariable=dash_vars["garage"], style="DashAccent.TLabel",
                  wraplength=520, justify="left").pack(anchor="w", pady=(0, 12))
        ttk.Label(fleet, text=tr("dash_profile_label"), style="DashAccent.TLabel").pack(anchor="w")
        ttk.Label(fleet, textvariable=dash_vars["profile"], style="DashMuted.TLabel",
                  wraplength=520).pack(anchor="w", pady=(2, 10))
        ttk.Label(fleet, text=tr("dash_save_label"), style="DashAccent.TLabel").pack(anchor="w")
        ttk.Label(fleet, textvariable=dash_vars["save"], style="DashMuted.TLabel").pack(anchor="w", pady=(2, 10))
        ttk.Button(fleet, text=tr("dash_open_tuning"), command=lambda: show_page("tune")
                   ).pack(anchor="w", pady=(8, 0))
        ttk.Button(fleet, text=tr("dash_open_garage"), command=lambda: show_page("garage")
                   ).pack(anchor="w", pady=(6, 0))

        backup_card = ttk.Frame(lower, style="DashCard.TFrame", padding=16)
        backup_card.pack(side="left", fill="y", padx=(6, 0))
        ttk.Label(backup_card, text=tr("dash_backup"), style="DashTitle.TLabel",
                  wraplength=260).pack(anchor="w")
        ttk.Separator(backup_card).pack(fill="x", pady=10)
        ttk.Label(backup_card, textvariable=dash_vars["backup"], style="DashAccent.TLabel",
                  wraplength=250, justify="left").pack(anchor="w", pady=(2, 12))
        ttk.Label(backup_card, text=tr("dashboard_subtitle"), style="DashMuted.TLabel",
                  wraplength=260, justify="left").pack(anchor="w")
        ttk.Button(backup_card, text=tr("restore"), command=restore).pack(anchor="w", pady=(18, 0))

    def build_garage(tab):
        outer = ttk.Frame(tab)
        outer.pack(fill="both", expand=True, padx=14, pady=12)
        head = ttk.Frame(outer, style="DashCard.TFrame", padding=(18, 14))
        head.pack(fill="x", pady=(0, 12))
        ttk.Label(head, text="TR-EURO-EDITOR  /  VEHICLE WORKSPACE",
                  style="DashAccent.TLabel").pack(anchor="w")
        ttk.Label(head, text=tr("garage_title"), style="DashTitle.TLabel").pack(anchor="w", pady=(4, 2))
        ttk.Label(head, textvariable=dash_vars["save"], style="DashMuted.TLabel").pack(anchor="w")
        ttk.Label(outer, textvariable=dash_vars["garage"], style="DashAccent.TLabel").pack(anchor="w", pady=(0, 8))
        split = ttk.Panedwindow(outer, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.LabelFrame(split, text=tr("fleet_list"), padding=8)
        right = ttk.LabelFrame(split, text=tr("fleet_details"), padding=10)
        split.add(left, weight=1)
        split.add(right, weight=2)
        tree = ttk.Treeview(left, columns=("type", "model", "garage"), show="headings",
                            selectmode="browse", height=14)
        for key, width in (("type", 105), ("model", 220), ("garage", 130)):
            tree.heading(key, text=tr("fleet_column_" + key))
            tree.column(key, width=width, stretch=True)
        tree.pack(fill="both", expand=True)
        st["fleet_tree"] = tree
        detail = reg_text(tk.Text(right, height=15, wrap="word", state="disabled",
                                  relief="flat", borderwidth=0, padx=10, pady=8, highlightthickness=1))
        detail.pack(fill="both", expand=True)
        ttk.Label(right, text=tr("fleet_select_note"), style="Muted.TLabel",
                  wraplength=550).pack(anchor="w", pady=(7, 4))
        action_row = ttk.Frame(right)
        action_row.pack(fill="x", pady=(6, 2))
        selected_repair = ttk.Button(action_row, text=tr("fleet_repair_selected"))
        selected_refuel = ttk.Button(action_row, text=tr("fleet_refuel_selected"))
        group_repair = ttk.Button(action_row, text=tr("fleet_repair_group"))
        group_refuel = ttk.Button(action_row, text=tr("fleet_refuel_group"))
        for button in (selected_repair, selected_refuel, group_repair, group_refuel):
            button.pack(side="left", padx=2, pady=2)
        ttk.Label(right, text=tr("fleet_write_limit") if not st.get("save_writable") else "",
                  style="Muted.TLabel", wraplength=550).pack(anchor="w", pady=(5, 0))

        rows = {}

        def write_details(lines):
            detail.configure(state="normal")
            detail.delete("1.0", "end")
            detail.insert("end", "\n".join(lines) if lines else tr("fleet_select_note"))
            detail.configure(state="disabled")

        def fmt_percent(value):
            return tr("fleet_na") if value is None else f"{value * 100:.1f}%"

        def selected_record():
            if not tree.selection():
                return None
            return rows.get(tree.selection()[0])

        def render_record(_event=None):
            selected = selected_record()
            if selected is None:
                st["fleet_selected"] = None
                current_fleet = st.get("fleet")
                if st.get("fleet_error"):
                    empty_lines = [tr("fleet_failed", reason=st["fleet_error"])]
                elif current_fleet is not None and not rows:
                    empty_lines = [tr("fleet_empty") if current_fleet["trailers_available"]
                                   else tr("fleet_empty_trucks"),
                                   *([] if current_fleet["trailers_available"] else
                                     [tr("fleet_trailer_unavailable")])]
                else:
                    empty_lines = []
                write_details(empty_lines)
                selected_repair.configure(state="disabled")
                selected_refuel.configure(state="disabled")
                return
            kind, item = selected
            st["fleet_selected"] = (kind, item["id"])
            lines = [tr("fleet_type_" + kind)]
            if kind == "truck":
                lines.append(f"{tr('fleet_model')}: {item['model'] or tr('fleet_na')}")
                specs = item["specs"]
                for key in ("chassis", "cabin", "engine", "transmission"):
                    part = specs.get(key)
                    value = part if part not in (None, "ambiguous") else tr("fleet_na")
                    lines.append(f"{tr('fleet_' + key)}: {value}")
            else:
                lines.append(f"{tr('fleet_trailer_def')}: {item['definition'] or tr('fleet_na')}")
            garage = item["garage"] or (tr("fleet_ambiguous_garage") if item["garage_ambiguous"]
                                         else tr("fleet_na"))
            lines.append(f"{tr('fleet_garage')}: {garage}")
            odo = item["odometer"]
            lines.append(f"{tr('fleet_odometer')}: {odo:,}" if odo is not None
                         else f"{tr('fleet_odometer')}: {tr('fleet_na')}")
            damage = item["damage"]
            for key in (("engine_wear", "transmission_wear", "cabin_wear", "chassis_wear")
                        if kind == "truck" else ("trailer_body_wear", "chassis_wear", "cargo_damage")):
                label = "fleet_" + ("trailer_wear" if key == "trailer_body_wear" else key)
                lines.append(f"{tr(label)}: {fmt_percent(damage.get(key))}")
            wheels = item.get("wheels_wear")
            lines.append(f"{tr('fleet_wheels_wear')}: " +
                         (", ".join(fmt_percent(x) for x in wheels) if wheels is not None else tr("fleet_na")))
            if kind == "truck":
                lines.append(f"{tr('fleet_fuel')}: {fmt_percent(item['fuel_relative'])}")
                lines.append(tr("fleet_fuel_liters_note"))
            write_details(lines)
            writable = bool(st.get("save_writable"))
            selected_repair.configure(state="normal" if writable and item.get("repair_supported") else "disabled")
            selected_refuel.configure(state="normal" if writable and kind == "truck" and
                                      item.get("refuel_supported") else "disabled")

        tree.bind("<<TreeviewSelect>>", render_record)

        def run_fleet_action(action, group=False):
            current_fleet = st.get("fleet")
            if not st.get("path") or not current_fleet:
                warn("sel_save")
                return
            if not st.get("save_writable"):
                warn("save_readonly_format")
                return
            if game_running():
                warn("game_open")
                return
            if group:
                candidates = current_fleet["trucks"] + (
                    current_fleet["trailers"] if action == "repair" and
                    current_fleet["trailers_available"] else [])
                ids = [x["id"] for x in candidates if x.get("repair_supported" if action == "repair"
                                                                 else "refuel_supported")]
            else:
                selected = selected_record()
                if selected is None:
                    warn("sel_save")
                    return
                kind, item = selected
                if action == "refuel" and kind != "truck":
                    warn("save_readonly_format")
                    return
                if not item.get("repair_supported" if action == "repair" else "refuel_supported"):
                    warn("save_readonly_format")
                    return
                ids = [item["id"]]
            if not ids:
                messagebox.showinfo(tr("title"), tr("fleet_na"))
                return
            if save_fingerprint(st["path"]) != st.get("loaded_fingerprint"):
                info("stale_save")
                return
            action_label = tr("fleet_repair_group" if action == "repair" and group else
                              "fleet_refuel_group" if action == "refuel" and group else
                              "fleet_repair_selected" if action == "repair" else
                              "fleet_refuel_selected")
            if not messagebox.askyesno(tr("title"), tr("fleet_confirm", action=action_label,
                                                        count=len(ids))):
                return
            try:
                backup = edit_save(st["path"], {}, extras=(action,), vehicle_ids=ids)
            except Exception as exc:
                err("fail", e=exc)
                return
            on_save()
            status(tr("st_done", b=backup))
            info("applied", b=backup)

        def refresh_fleet_view():
            tree.delete(*tree.get_children())
            rows.clear()
            current_fleet = st.get("fleet")
            if current_fleet:
                for truck in current_fleet["trucks"]:
                    iid = "truck|" + truck["id"]
                    rows[iid] = ("truck", truck)
                    tree.insert("", "end", iid=iid, values=(
                        tr("fleet_type_truck"), truck["model"] or tr("fleet_na"),
                        truck["garage"] or (tr("fleet_ambiguous_garage") if truck["garage_ambiguous"]
                                             else tr("fleet_na"))))
                for trailer in current_fleet["trailers"]:
                    iid = "trailer|" + trailer["id"]
                    rows[iid] = ("trailer", trailer)
                    tree.insert("", "end", iid=iid, values=(
                        tr("fleet_type_trailer"), trailer["definition"] or tr("fleet_na"),
                        trailer["garage"] or (tr("fleet_ambiguous_garage") if trailer["garage_ambiguous"]
                                              else tr("fleet_na"))))
            group_repair.configure(state="normal" if st.get("save_writable") and current_fleet and any(
                x.get("repair_supported") for x in current_fleet["trucks"] + (
                    current_fleet["trailers"] if current_fleet["trailers_available"] else []))
                                   else "disabled")
            group_refuel.configure(state="normal" if st.get("save_writable") and current_fleet and any(
                x.get("refuel_supported") for x in current_fleet["trucks"])
                                   else "disabled")
            if st.get("fleet_selected"):
                wanted = st["fleet_selected"][0] + "|" + st["fleet_selected"][1]
                if wanted in rows:
                    tree.selection_set(wanted)
            if not tree.selection() and tree.get_children():
                tree.selection_set(tree.get_children()[0])
            render_record()

        selected_repair.configure(command=lambda: run_fleet_action("repair"))
        selected_refuel.configure(command=lambda: run_fleet_action("refuel"))
        group_repair.configure(command=lambda: run_fleet_action("repair", group=True))
        group_refuel.configure(command=lambda: run_fleet_action("refuel", group=True))
        tree.configure(selectmode="browse")
        st["refresh_fleet_view"] = refresh_fleet_view
        refresh_fleet_view()

    def build_extras(tab):
        fr = ttk.LabelFrame(tab, text=tr("ex_title"))
        fr.pack(fill="x", padx=10, pady=10)
        for k, key in (("dealers", "ex_dealers"), ("repair", "ex_repair"), ("refuel", "ex_refuel")):
            ttk.Checkbutton(fr, text=tr(key), variable=ex_vars[k]).pack(anchor="w", padx=10, pady=6)
        ttk.Label(tab, text=tr("ex_note"), wraplength=820, style="Muted.TLabel", justify="left"
                  ).pack(anchor="w", padx=14, pady=6)
        tools = ttk.Frame(tab)
        tools.pack(fill="both", expand=True, padx=10, pady=(4, 8))
        inspector = ttk.LabelFrame(tools, text=tr("ex_inspector"), padding=8)
        inspector.pack(side="left", fill="both", expand=True, padx=(0, 5))
        ttk.Button(inspector, text=tr("ex_inspector_refresh"), command=refresh_extra_tools
                   ).pack(anchor="w", pady=(0, 6))
        inspect_text = reg_text(tk.Text(inspector, height=10, wrap="word", state="disabled",
                                        relief="flat", borderwidth=0, padx=8, pady=7, highlightthickness=1))
        inspect_text.pack(fill="both", expand=True)
        w["extras_inspector"] = inspect_text

        history = ttk.LabelFrame(tools, text=tr("ex_backup_history"), padding=8)
        history.pack(side="left", fill="both", expand=True, padx=(5, 0))
        backup_tree = ttk.Treeview(history, columns=("date", "size"), show="headings", height=8,
                                   selectmode="browse")
        backup_tree.heading("date", text=tr("ex_backup_date"))
        backup_tree.heading("size", text=tr("ex_backup_size"))
        backup_tree.column("date", width=150, anchor="w")
        backup_tree.column("size", width=90, anchor="e")
        backup_tree.pack(fill="both", expand=True, pady=(0, 6))
        w["extras_backups"] = backup_tree
        actions = ttk.Frame(history)
        actions.pack(fill="x")
        ttk.Button(actions, text=tr("ex_backup_refresh"), command=refresh_extra_tools
                   ).pack(side="left", padx=(0, 4))
        ttk.Button(actions, text=tr("ex_backup_restore"), command=restore_selected_backup
                   ).pack(side="left", padx=4)
        ttk.Button(actions, text=tr("ex_log_view"), command=show_game_log
                   ).pack(side="right")
        refresh_extra_tools()

    def refresh_extra_tools():
        inspector = w.get("extras_inspector")
        tree = w.get("extras_backups")
        if inspector is None or not inspector.winfo_exists():
            return
        inspector.configure(state="normal")
        inspector.delete("1.0", "end")
        if not st["path"]:
            inspector.insert("end", tr("ex_inspector_none"))
        else:
            try:
                data = inspect_save_file(st["path"])
                values = data["values"]
                status_line = tr("ex_inspector_ok")
                details = [
                    status_line,
                    "",
                    "Path: %s" % data["path"],
                    "Format: %s%s%s" % (data["format"], " · encrypted" if data["encrypted"] else " · plain",
                                        " · read-only" if not save_writes_supported(
                                            data["format"], data["encrypted"]) else ""),
                    "Size: %s bytes" % format(data["size"], ","),
                    "Modified: %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(data["modified"])),
                    "Money: € %s" % format(values["money"], ","),
                    "Experience: %s XP" % format(values["xp"], ","),
                    "Recorded visited cities: %s" % values["visited"],
                ]
                inspector.insert("end", "\n".join(details))
            except Exception as ex:
                inspector.insert("end", tr("read_fail", e=ex))
        inspector.configure(state="disabled")
        if tree is not None and tree.winfo_exists():
            tree.delete(*tree.get_children())
            st["backup_items"] = {}
            if st["path"]:
                backups = backup_history(st["path"])
                for backup in backups:
                    try:
                        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(backup.stat().st_mtime))
                        size = format(backup.stat().st_size, ",") + " B"
                        iid = tree.insert("", "end", values=(stamp, size))
                        st["backup_items"][iid] = backup
                    except OSError:
                        continue
                if not backups:
                    tree.insert("", "end", iid="no-backups", values=(tr("ex_backup_empty"), ""))

    def restore_selected_backup():
        tree = w.get("extras_backups")
        selection = tree.selection() if tree is not None else ()
        if not st["path"]:
            warn("sel_save")
            return
        if not st["save_writable"]:
            warn("save_readonly_format")
            return
        if not selection:
            info("no_backup")
            return
        backup = st.get("backup_items", {}).get(selection[0])
        if not backup or not backup.is_file():
            info("no_backup")
            return
        if game_running():
            warn("game_open")
            return
        if save_fingerprint(st["path"]) != st.get("loaded_fingerprint"):
            info("stale_save")
            return
        if not messagebox.askyesno(tr("title"), tr("ask_restore", b=backup)):
            return
        try:
            restore_save_backup(st["path"], backup)
        except Exception as ex:
            err("fail", e=ex)
            return
        on_save()
        refresh_extra_tools()
        status(tr("st_restored"))

    def show_game_log():
        base = var_dir.get().strip()
        path = game_log_path(base) if base else None
        if path is None or not path.is_file():
            info("ex_log_missing")
            return
        try:
            content = path.read_bytes()[-1_500_000:].decode("utf-8", "replace")
        except OSError as ex:
            err("fail", e=ex)
            return
        window = reg(tk.Toplevel(root), bg="background")
        window.title("ETS2 game.log.txt")
        window.geometry("900x600")
        frame = ttk.Frame(window, padding=8)
        frame.pack(fill="both", expand=True)
        text = reg_text(tk.Text(frame, wrap="none", state="normal", highlightthickness=1))
        ybar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        xbar = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        ybar.pack(side="right", fill="y")
        xbar.pack(side="bottom", fill="x")
        text.pack(side="left", fill="both", expand=True)
        text.insert("1.0", content)
        text.configure(state="disabled")

    def build_mods(tab):
        top = ttk.Frame(tab)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text=tr("mods_ver")).pack(side="left")
        ttk.Entry(top, textvariable=var_gver, width=14).pack(side="left", padx=4)
        ttk.Button(top, text=tr("mods_detect"), command=detect_ver).pack(side="left", padx=2)
        tv = ttk.Treeview(tab, columns=("on", "name", "file", "ver", "status"), show="headings",
                          height=9, selectmode="browse")
        for c, k, wd in (("on", "mc_on", 60), ("name", "mc_name", 260), ("file", "mc_file", 200),
                         ("ver", "mc_ver", 70), ("status", "mc_status", 240)):
            tv.heading(c, text=tr(k))
            tv.column(c, width=wd, anchor="center" if c == "on" else "w")
        tv.pack(fill="x", padx=8)
        w["mods"] = tv
        bt = ttk.Frame(tab)
        bt.pack(fill="x", padx=8, pady=4)
        add_button = None
        for key, cmd in (("m_refresh", refresh_mods), ("m_enable", lambda: toggle_mod(True)),
                         ("m_disable", lambda: toggle_mod(False)), ("m_up", lambda: move_mod(-1)),
                         ("m_down", lambda: move_mod(1)), ("m_add", add_mod)):
            button = ttk.Button(bt, text=tr(key), command=cmd)
            button.pack(side="left", padx=2)
            if key == "m_add":
                add_button = button
        w["mod_add_button"] = add_button
        if st["mod_copy_running"] and add_button is not None:
            add_button.configure(state="disabled")
        bt2 = ttk.Frame(tab)
        bt2.pack(fill="x", padx=8)
        validation_buttons = []
        for key, cmd in (("m_validate", validate_sel), ("m_validate_all", validate_all),
                         ("m_open", open_mod_dir)):
            button = ttk.Button(bt2, text=tr(key), command=cmd)
            button.pack(side="left", padx=2)
            if key in ("m_validate", "m_validate_all"):
                validation_buttons.append(button)
        w["validation_buttons"] = validation_buttons
        if st["validation_running"]:
            for button in validation_buttons:
                button.configure(state="disabled")
        ttk.Label(tab, text=tr("m_order_note"), style="Muted.TLabel", wraplength=860
                  ).pack(anchor="w", padx=10, pady=2)
        box = ttk.Frame(tab)
        box.pack(fill="both", expand=True, padx=8, pady=4)
        txt = reg_text(tk.Text(box, height=10, wrap="word", state="disabled",
                               relief="flat", borderwidth=0, padx=10, pady=8, highlightthickness=1))
        sb = ttk.Scrollbar(box, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        def recolor_report_tags(widget=txt):
            if not widget.winfo_exists():
                return
            pal = colors()
            widget.tag_configure("ok", foreground=pal["success"])
            widget.tag_configure("warn", foreground=pal["warning"])
            widget.tag_configure("err", foreground=pal["error"])
            widget.tag_configure("info", foreground=pal["accent_text"])
            widget.tag_configure("head", font=("TkDefaultFont", 10, "bold"))
        recolor_report_tags()
        redraw_hooks.append(recolor_report_tags)
        w["report"] = txt

    def build_settings(tab):
        body = scrolled_frame(tab)
        # ---- appearance
        ap = ttk.LabelFrame(body, text=tr("appearance_title"), padding=8)
        ap.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Label(ap, text=tr("appearance_note") + "  " + tr("set_theme_hint"), style="Muted.TLabel",
                  wraplength=780, justify="left").pack(anchor="w", padx=4, pady=(0, 4))
        grid = ttk.Frame(ap)
        grid.pack(anchor="w", padx=2, pady=2)
        w["theme_cards"] = {}
        for index, theme_id in enumerate(THEME_PRESETS):
            cv = reg(tk.Canvas(grid, width=188, height=122, bd=0, highlightthickness=0,
                               takefocus=1, cursor="hand2"), bg="background")
            cv.grid(row=index // 4, column=index % 4, padx=6, pady=6)
            cv.bind("<Button-1>", lambda _e, t=theme_id: set_theme(t))
            for key in ("<Return>", "<space>"):
                cv.bind(key, lambda _e, t=theme_id: set_theme(t))
            for key in ("<FocusIn>", "<FocusOut>"):
                cv.bind(key, lambda _e, c=cv, t=theme_id: paint_theme_card(c, t), add="+")
            w["theme_cards"][theme_id] = cv
        repaint_theme_cards()
        redraw_hooks.append(repaint_theme_cards)

        ttk.Separator(ap).pack(fill="x", pady=8)
        ttk.Label(ap, text=tr("set_accent"), style="TLabel").pack(anchor="w", padx=4)
        accent_row = ttk.Frame(ap)
        accent_row.pack(anchor="w", padx=4, pady=6)
        swatch = reg(tk.Canvas(accent_row, width=34, height=34, bd=0, highlightthickness=0), bg="surface")
        swatch.pack(side="left", padx=(0, 10))
        w["accent_swatch"] = swatch
        ttk.Label(accent_row, text=tr("accent_hex")).pack(side="left", padx=(0, 6))
        hex_entry = ttk.Entry(accent_row, textvariable=var_accent_hex, width=10)
        hex_entry.pack(side="left")
        hex_entry.bind("<Return>", apply_accent_from_entry)
        ttk.Button(accent_row, text=tr("accent_apply"), command=apply_accent_from_entry
                   ).pack(side="left", padx=(6, 2))
        ttk.Button(accent_row, text=tr("theme_accent_custom"), command=pick_accent_dialog
                   ).pack(side="left", padx=2)
        quick = ttk.Frame(ap)
        quick.pack(anchor="w", padx=4, pady=(0, 6))
        for name, value in LEGACY_ACCENTS.items():
            chip = reg(tk.Canvas(quick, width=26, height=26, bd=0, highlightthickness=1, cursor="hand2"),
                       bg=lambda _p, v=value: v, highlightbackground="border")
            chip.pack(side="left", padx=3)
            chip.bind("<Button-1>", lambda _e, v=value: set_accent(v))
        ttk.Button(ap, text=tr("accent_reset"), command=reset_theme_defaults).pack(anchor="w", padx=4, pady=(2, 4))
        ttk.Label(ap, textvariable=var_accent_msg, style="Error.TLabel").pack(anchor="w", padx=4)
        ttk.Label(ap, text=tr("set_appearance_note", p=str(theme_cfg)), style="Muted.TLabel",
                  wraplength=780, justify="left").pack(anchor="w", padx=4, pady=(4, 2))
        sync_accent_controls()
        redraw_hooks.append(sync_accent_controls)
        # ---- game config
        fr = ttk.LabelFrame(body, text=tr("cfg_title"), padding=8)
        fr.pack(fill="x", padx=10, pady=(6, 10))
        for k, key in (("developer", "cfg_dev"), ("console", "cfg_con"), ("savefmt", "cfg_fmt")):
            ttk.Checkbutton(fr, text=tr(key), variable=cfg_vars[k]).pack(anchor="w", padx=10, pady=4)
        row = ttk.Frame(fr)
        row.pack(anchor="w", padx=10, pady=8)
        ttk.Button(row, text=tr("cfg_load"), command=load_cfg).pack(side="left", padx=2)
        ttk.Button(row, text=tr("cfg_apply"), command=apply_cfg).pack(side="left", padx=2)
        ttk.Label(fr, text=tr("cfg_note"), style="Muted.TLabel").pack(anchor="w", padx=10)

    def build_tuning(tab):
        tab.configure(style="Tune.TFrame")

        outer = ttk.Frame(tab, style="Tune.TFrame")
        outer.pack(fill="both", expand=True, padx=14, pady=12)
        header = ttk.Frame(outer, style="TuneCard.TFrame", padding=(14, 10))
        header.pack(fill="x", pady=(0, 10))
        header_copy = ttk.Frame(header, style="TuneCard.TFrame")
        header_copy.pack(side="left", fill="x", expand=True)
        ttk.Label(header_copy, text="TR-EURO-EDITOR   /   PERFORMANCE LAB", style="TuneAccent.TLabel"
                  ).pack(anchor="w")
        ttk.Label(header_copy, text=tr("tune_title"), style="TuneTitle.TLabel"
                  ).pack(anchor="w", pady=(3, 0))
        ttk.Label(header_copy, text=tr("tune_gauge_note"), style="TuneMuted.TLabel",
                  wraplength=580, justify="left").pack(anchor="w", pady=(2, 0))
        ttk.Label(header, text="ETS2  •  SINGLE PLAYER", style="TuneGold.TLabel"
                  ).pack(side="right", anchor="ne", padx=(8, 2))

        row = ttk.Frame(outer, style="Tune.TFrame")
        row.pack(fill="both", expand=True)
        left = ttk.Frame(row, style="TuneCard.TFrame", padding=12)
        left.pack(side="left", fill="y", padx=(0, 10))
        right = ttk.Frame(row, style="Tune.TFrame")
        right.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text=tr("tune_output"), style="TuneAccent.TLabel").pack(anchor="w", pady=(2, 4))
        output_row = ttk.Frame(left, style="TuneCard.TFrame")
        output_row.pack(fill="x", pady=(0, 8))
        ttk.Entry(output_row, textvariable=var_mod_output, width=20).pack(side="left", fill="x", expand=True)
        ttk.Button(output_row, text=tr("tune_browse_output"), command=browse_tune_output
                   ).pack(side="left", padx=(5, 0))
        ttk.Button(left, text=tr("tune_open_output"), command=open_tune_output
                   ).pack(fill="x", pady=(0, 8))
        ttk.Label(left, text=tr("tune_compat"), style="TuneAccent.TLabel").pack(anchor="w", pady=(2, 3))
        ttk.Entry(left, textvariable=var_tune_compat, width=20).pack(fill="x", pady=(0, 3))
        ttk.Label(left, text=tr("tune_compat_hint"), style="TuneMuted.TLabel",
                  wraplength=230, justify="left").pack(anchor="w", pady=(0, 3))
        ttk.Button(left, text=tr("tune_detect_version"), command=detect_tune_version
                   ).pack(fill="x", pady=(0, 8))
        ttk.Label(left, text=tr("tune_trucks"), style="TuneAccent.TLabel").pack(anchor="w", pady=(2, 7))
        for name, variable in tune_truck_vars.items():
            ttk.Checkbutton(left, text=name, variable=variable,
                            command=refresh_tuning_dashboard).pack(anchor="w", pady=3)
        ttk.Separator(left).pack(fill="x", pady=10)
        sections = ttk.Notebook(left)
        sections.pack(fill="both", expand=True, pady=(0, 8))
        engine_tab = ttk.Frame(sections, padding=8, style="TuneInner.TFrame")
        gearbox_tab = ttk.Frame(sections, padding=8, style="TuneInner.TFrame")
        brakes_tab = ttk.Frame(sections, padding=8, style="TuneInner.TFrame")
        weight_tab = ttk.Frame(sections, padding=8, style="TuneInner.TFrame")
        preset_tab = ttk.Frame(sections, padding=8, style="TuneInner.TFrame")
        sections.add(engine_tab, text=tr("tune_tab_engine"))
        sections.add(gearbox_tab, text=tr("tune_tab_gearbox"))
        sections.add(brakes_tab, text=tr("tune_tab_brakes"))
        sections.add(weight_tab, text=tr("tune_tab_weight"))
        sections.add(preset_tab, text=tr("tune_presets"))

        def tuning_field(parent, key, variable):
            ttk.Label(parent, text=tr(key), style="TuneMuted.TLabel").pack(anchor="w", pady=(4, 0))
            ttk.Entry(parent, width=19, textvariable=variable).pack(anchor="w", fill="x", pady=(2, 4))

        tuning_field(engine_tab, "tune_hp", var_hp_target)
        tuning_field(engine_tab, "tune_torque", var_torque_factor)
        tuning_field(engine_tab, "tune_rpm", var_rpm_factor)
        tuning_field(engine_tab, "tune_target_speed", var_target_speed)
        tuning_field(engine_tab, "tune_target_accel", var_target_accel)
        ttk.Button(engine_tab, text=tr("tune_suggest"), command=suggest_tuning_setup
                   ).pack(fill="x", pady=(8, 3))

        tuning_field(gearbox_tab, "tune_gear_count", var_gear_count)
        tuning_field(gearbox_tab, "tune_diff", var_differential)
        ttk.Label(gearbox_tab, text=tr("tune_estimate_note"), style="TuneMuted.TLabel",
                  wraplength=230, justify="left").pack(anchor="w", pady=(6, 0))

        tuning_field(brakes_tab, "tune_engine_brake", var_engine_brake)
        tuning_field(brakes_tab, "tune_retarder", var_retarder)
        ttk.Label(brakes_tab, text=tr("tune_brake_note"), style="TuneMuted.TLabel",
                  wraplength=230, justify="left").pack(anchor="w", pady=(6, 0))

        tuning_field(weight_tab, "tune_mass", var_mass)
        ttk.Label(weight_tab, text=tr("tune_weight_note"), style="TuneMuted.TLabel",
                  wraplength=230, justify="left").pack(anchor="w", pady=(6, 0))
        ttk.Label(preset_tab, text=tr("tune_preset_name"), style="TuneMuted.TLabel").pack(anchor="w")
        ttk.Entry(preset_tab, textvariable=var_preset_name, width=22).pack(fill="x", pady=(3, 5))
        ttk.Button(preset_tab, text=tr("tune_preset_save"), command=save_current_tuning_preset
                   ).pack(fill="x", pady=(0, 8))
        ttk.Label(preset_tab, text=tr("tune_presets"), style="TuneMuted.TLabel").pack(anchor="w")
        preset_box = ttk.Combobox(preset_tab, state="readonly", width=20)
        preset_box.pack(fill="x", pady=(3, 5))
        preset_actions = ttk.Frame(preset_tab)
        preset_actions.pack(fill="x")
        ttk.Button(preset_actions, text=tr("tune_preset_load"), command=load_selected_tuning_preset
                   ).pack(side="left", fill="x", expand=True, padx=(0, 3))
        ttk.Button(preset_actions, text=tr("tune_preset_delete"), command=delete_selected_tuning_preset
                   ).pack(side="left", padx=(3, 0))
        ttk.Separator(preset_tab).pack(fill="x", pady=8)
        compare_row = ttk.Frame(preset_tab)
        compare_row.pack(fill="x")
        compare_a = ttk.Combobox(compare_row, state="readonly", width=8)
        compare_a.pack(side="left", fill="x", expand=True, padx=(0, 3))
        compare_b = ttk.Combobox(compare_row, state="readonly", width=8)
        compare_b.pack(side="left", fill="x", expand=True, padx=(3, 0))
        compare_a.set(tr("tune_compare_a"))
        compare_b.set(tr("tune_compare_b"))
        ttk.Button(preset_tab, text=tr("tune_compare"), command=compare_tuning_presets
                   ).pack(fill="x", pady=(5, 3))
        ttk.Label(preset_tab, textvariable=var_compare_result, style="TuneMuted.TLabel",
                  wraplength=240, justify="left").pack(anchor="w", pady=(3, 0))
        w["preset_box"], w["compare_a"], w["compare_b"] = preset_box, compare_a, compare_b
        refresh_preset_widgets()
        ttk.Button(left, text=tr("tune_build"), command=build_tuning_mod).pack(fill="x", pady=(8, 3))

        hero = ttk.Frame(right, style="TuneCard.TFrame", padding=10)
        hero.pack(fill="both", expand=True)
        hero_head = ttk.Frame(hero, style="TuneCard.TFrame")
        hero_head.pack(fill="x")
        ttk.Label(hero_head, text=tr("tune_gauge"), style="TuneGold.TLabel").pack(side="left")
        ttk.Label(hero_head, text=tr("tune_live"), style="TuneMuted.TLabel").pack(side="right")
        gauge = reg(tk.Canvas(hero, height=260, highlightthickness=0), bg="card")
        gauge.pack(fill="both", expand=True, pady=(0, 3))
        w["tune_gauge"] = gauge
        redraw_hooks.append(draw_tuning_gauge)
        gauge.bind("<Configure>", lambda _e: draw_tuning_gauge())
        gauge.bind("<MouseWheel>", lambda e: set_gauge_target(var_gauge_target.get() + (5 if e.delta > 0 else -5)))

        stats = ttk.Frame(right, style="Tune.TFrame")
        stats.pack(fill="x", pady=(10, 0))
        cards = []
        for title, initial, suffix, color in (
                ("tune_card_hp", "750 hp", "HORSEPOWER", "TuneAccent.TLabel"),
                ("tune_card_torque", "3500 Nm", "NEWTON-METRES", "TuneGold.TLabel"),
                ("tune_estimated_accel", "30 s", "0–100 KM/H", "TuneGold.TLabel"),
                ("tune_estimated_speed", "200 km/h", "TOP SPEED", "TuneAccent.TLabel")):
            card = ttk.Frame(stats, style="TuneCard.TFrame", padding=(10, 8))
            card.pack(side="left", fill="x", expand=True, padx=(0, 8))
            ttk.Label(card, text=tr(title), style="TuneMuted.TLabel").pack(anchor="w")
            value = ttk.Label(card, text=initial, style="TuneValue.TLabel")
            value.pack(anchor="w", pady=2)
            ttk.Label(card, text=suffix, style=color).pack(anchor="w")
            cards.append(value)
        w["tune_cards"] = cards
        w["tune_sections"] = sections
        ttk.Label(right, text=tr("tune_gauge_note"), style="TuneMuted.TLabel",
                  wraplength=720, justify="left").pack(anchor="w", pady=(8, 2))
        ttk.Label(right, text=tr("tune_estimate_note"), style="TuneMuted.TLabel",
                  wraplength=720, justify="left").pack(anchor="w", pady=(2, 0))
        for variable, token in w.get("tune_trace_ids", []):
            try:
                variable.trace_remove("write", token)
            except tk.TclError:
                pass
        trace_ids = []
        for variable in (var_torque_factor, var_rpm_factor, var_hp_target,
                         var_target_speed, var_target_accel, var_mass,
                         var_gear_count, var_differential, var_engine_brake, var_retarder):
            trace_ids.append((variable, variable.trace_add("write", refresh_tuning_dashboard)))
        w["tune_trace_ids"] = trace_ids
        draw_tuning_gauge()
        refresh_tuning_dashboard()

    def suggest_tuning_setup():
        try:
            speed = float(var_target_speed.get())
            accel = float(var_target_accel.get())
            mass = float(var_mass.get())
            rpm = float(var_rpm_factor.get())
            if min(speed, accel, mass, rpm) <= 0 or not all(
                    math.isfinite(v) for v in (speed, accel, mass, rpm)):
                raise ValueError
            suggestion = recommend_engine_for_targets(speed, accel, mass,
                                                      max(900.0, min(rpm * 0.65, 1800.0)))
            var_hp_target.set(f"{suggestion['hp']:.0f}")
            var_torque_factor.set(f"{suggestion['torque']:.0f}")
            var_gear_count.set("12")
            var_differential.set("2.64")
            refresh_tuning_dashboard()
            info("tune_suggest_success", hp=suggestion["hp"],
                 torque=suggestion["torque"], gears=12, diff=2.64)
        except (ValueError, tk.TclError, ZeroDivisionError):
            messagebox.showwarning(tr("title"), tr("tune_bad_factor"))

    def set_gauge_target(value):
        value = max(0.0, float(value))
        var_gauge_target.set(value)
        draw_tuning_gauge()

    def draw_tuning_gauge():
        canvas = w.get("tune_gauge")
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        width = max(canvas.winfo_width(), 280)
        height = max(canvas.winfo_height(), 230)
        radius = min(width * 0.34, (height - 24) / 1.75)
        cx = width / 2
        cy = (height - 1.707 * radius) / 2 + radius
        value = max(0.0, needle_speed["value"])
        scale_max = max(300, int(math.ceil(value / 100.0) * 100))
        pal = colors()
        accent = pal["accent"]
        left_end, extent = 225, 270          # 0 km/h sits at the lower left, the scale runs clockwise
        bbox = (cx-radius, cy-radius, cx+radius, cy+radius)
        canvas.create_arc(bbox, start=left_end - extent, extent=extent, style="arc",
                          outline=pal["gauge_background"], width=13)
        active_extent = extent * min(value, scale_max) / scale_max
        if active_extent > 0:
            canvas.create_arc(bbox, start=left_end - active_extent, extent=active_extent, style="arc",
                              outline=accent, width=13)
        step = scale_max // 5
        for tick in range(0, scale_max + 1, step):
            angle = math.radians(left_end - extent * tick / scale_max)
            r1, r2 = radius - 8, radius - (22 if tick % 60 == 0 else 15)
            x1, y1 = cx + r1 * math.cos(angle), cy - r1 * math.sin(angle)
            x2, y2 = cx + r2 * math.cos(angle), cy - r2 * math.sin(angle)
            canvas.create_line(x1, y1, x2, y2, fill=pal["text_secondary"] if tick % 60 else pal["gauge_foreground"],
                               width=2 if tick % 60 == 0 else 1)
            if tick % (step * 2) == 0:
                rt = radius - 37
                tx, ty = cx + rt * math.cos(angle), cy - rt * math.sin(angle)
                canvas.create_text(tx, ty, text=str(tick), fill=pal["text_secondary"], font=("Segoe UI", 9))
        angle = math.radians(left_end - extent * min(value, scale_max) / scale_max)
        tipx, tipy = cx + (radius - 48) * math.cos(angle), cy - (radius - 48) * math.sin(angle)
        canvas.create_line(cx, cy, tipx, tipy, fill=pal["warning"], width=4, capstyle="round")
        canvas.create_oval(cx-7, cy-7, cx+7, cy+7, fill=pal["warning"], outline="")
        canvas.create_text(cx, cy - radius*0.37, text=f"{int(round(value))}", fill=pal["gauge_foreground"],
                           font=("Segoe UI Semibold", 32))
        canvas.create_text(cx, cy - radius*0.15, text="km/h  ·  ESTIMATE", fill=pal["text_secondary"],
                           font=("Segoe UI Semibold", 9))

    def refresh_tuning_dashboard(*_):
        try:
            rpm = float(var_rpm_factor.get())
            torque = float(var_torque_factor.get())
            hp = float(var_hp_target.get())
            speed = float(var_target_speed.get())
            differential = float(var_differential.get())
            mass = float(var_mass.get())
            if min(rpm, torque, hp) <= 0 or not all(math.isfinite(v) for v in (rpm, torque, hp)):
                return
            if min(speed, differential, mass) <= 0 or not all(
                    math.isfinite(v) for v in (speed, differential, mass)):
                return
            estimate = estimate_tuning_performance(hp, torque, rpm, speed, differential, mass)
        except (ValueError, tk.TclError):
            return
        target = estimate["top_speed"]
        var_gauge_target.set(target)
        needle_speed["target"] = target
        if needle_speed["job"] is None:
            animate_gauge()
        cards = w.get("tune_cards", [])
        if len(cards) == 4:
            cards[0].configure(text=f"{hp:,.0f} hp")
            cards[1].configure(text=f"{torque:,.0f} Nm")
            cards[2].configure(text=f"{estimate['zero_to_100']:.1f} s")
            cards[3].configure(text=f"{estimate['top_speed']:.0f} km/h")
        draw_tuning_gauge()

    def animate_gauge():
        needle_speed["job"] = None
        delta = needle_speed["target"] - needle_speed["value"]
        if abs(delta) < 0.4:
            needle_speed["value"] = needle_speed["target"]
            draw_tuning_gauge()
            return
        needle_speed["value"] += delta * 0.16
        draw_tuning_gauge()
        needle_speed["job"] = root.after(16, animate_gauge)

    def current_tuning_preset():
        values = {
            "hp": float(var_hp_target.get()), "torque": float(var_torque_factor.get()),
            "rpm": float(var_rpm_factor.get()), "target_speed": float(var_target_speed.get()),
            "target_accel": float(var_target_accel.get()), "mass": float(var_mass.get()),
            "gear_count": int(var_gear_count.get()), "differential": float(var_differential.get()),
            "engine_brake": float(var_engine_brake.get()), "retarder": int(var_retarder.get()),
            "trucks": [name for name, flag in tune_truck_vars.items() if flag.get()],
        }
        numeric = [v for k, v in values.items() if k != "trucks"]
        if not values["trucks"] or not all(math.isfinite(v) for v in numeric):
            raise ValueError
        if min(values[k] for k in ("hp", "torque", "rpm", "target_speed", "target_accel",
                                  "mass", "differential")) <= 0:
            raise ValueError
        if not 6 <= values["gear_count"] <= 18 or not 0 <= values["retarder"] <= 3 \
                or values["engine_brake"] < 0:
            raise ValueError
        return values

    def refresh_preset_widgets():
        names = sorted(tune_presets, key=str.casefold)
        box = w.get("preset_box")
        if box is not None and box.winfo_exists():
            box["values"] = names
            if box.get() not in names:
                box.set(names[0] if names else "")
        for key in ("compare_a", "compare_b"):
            combo = w.get(key)
            if combo is not None and combo.winfo_exists():
                previous = combo.get()
                combo["values"] = names
                if previous in names:
                    combo.set(previous)
                elif names:
                    combo.set(names[0 if key == "compare_a" else min(1, len(names)-1)])
                else:
                    combo.set("")

    def save_current_tuning_preset():
        name = var_preset_name.get().strip()
        try:
            values = current_tuning_preset()
        except (ValueError, tk.TclError):
            warn("tune_preset_bad")
            return
        if not name:
            warn("tune_preset_bad")
            return
        if name in tune_presets and not messagebox.askyesno(tr("title"), tr("tune_preset_exists")):
            return
        tune_presets[name] = values
        try:
            save_tuning_presets(preset_file, tune_presets)
        except Exception as ex:
            err("fail", e=ex)
            return
        refresh_preset_widgets()
        if w.get("preset_box") is not None:
            w["preset_box"].set(name)
        status(tr("tune_preset_saved", name=name))

    def load_selected_tuning_preset():
        box = w.get("preset_box")
        name = box.get() if box is not None else ""
        values = tune_presets.get(name)
        if values is None:
            warn("tune_compare_empty")
            return
        try:
            var_hp_target.set(str(values["hp"]))
            var_torque_factor.set(str(values["torque"]))
            var_rpm_factor.set(str(values["rpm"]))
            var_target_speed.set(str(values["target_speed"]))
            var_target_accel.set(str(values["target_accel"]))
            var_mass.set(str(values["mass"]))
            var_gear_count.set(str(values["gear_count"]))
            var_differential.set(str(values["differential"]))
            var_engine_brake.set(str(values["engine_brake"]))
            var_retarder.set(str(values["retarder"]))
            selected = set(values["trucks"])
            for truck, flag in tune_truck_vars.items():
                flag.set(truck in selected)
        except (KeyError, TypeError, tk.TclError):
            warn("tune_preset_bad")
            return
        refresh_tuning_dashboard()
        status(tr("tune_preset_loaded", name=name))

    def delete_selected_tuning_preset():
        box = w.get("preset_box")
        name = box.get() if box is not None else ""
        if name not in tune_presets:
            warn("tune_compare_empty")
            return
        if not messagebox.askyesno(tr("title"), tr("tune_preset_delete_confirm")):
            return
        del tune_presets[name]
        try:
            save_tuning_presets(preset_file, tune_presets)
        except Exception as ex:
            err("fail", e=ex)
            return
        refresh_preset_widgets()
        var_compare_result.set("")

    def compare_tuning_presets():
        a_name, b_name = w["compare_a"].get(), w["compare_b"].get()
        if a_name not in tune_presets or b_name not in tune_presets:
            var_compare_result.set(tr("tune_compare_empty"))
            return
        try:
            a, b = tune_presets[a_name], tune_presets[b_name]
            ea, eb = estimate_tuning_preset(a), estimate_tuning_preset(b)
            var_compare_result.set(tr("tune_compare_result",
                a_hp=float(a["hp"]), a_tq=float(a["torque"]), a_accel=ea["zero_to_100"],
                a_speed=ea["top_speed"], a_gears=int(a["gear_count"]),
                b_hp=float(b["hp"]), b_tq=float(b["torque"]), b_accel=eb["zero_to_100"],
                b_speed=eb["top_speed"], b_gears=int(b["gear_count"])))
        except (ValueError, TypeError, KeyError, ZeroDivisionError):
            var_compare_result.set(tr("tune_preset_bad"))

    def browse_tune_output():
        initial = Path(var_mod_output.get().strip())
        start = initial if initial.is_dir() else documents_dir()
        chosen = filedialog.askdirectory(title=tr("tune_output"), initialdir=str(start))
        if chosen:
            var_mod_output.set(chosen)

    def detect_tune_version():
        version = detect_game_version(var_dir.get().strip()) if var_dir.get().strip() else ""
        if not version:
            messagebox.showwarning(tr("title"), tr("v_log_missing"))
            return
        var_tune_compat.set(version)
        status(tr("mods_ver") + " " + version)

    def open_tune_output():
        folder = Path(var_mod_output.get().strip()) if var_mod_output.get().strip() else None
        if folder is None:
            warn("tune_bad_factor")
            return
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(folder))  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as ex:
            err("fail", e=ex)

    def build_tuning_mod():
        output_dir = Path(var_mod_output.get().strip()) if var_mod_output.get().strip() else None
        if output_dir is None:
            warn("tune_bad_factor")
            return
        selected = [name for name, variable in tune_truck_vars.items() if variable.get()]
        if not selected:
            messagebox.showwarning(tr("title"), tr("tune_trucks"))
            return
        try:
            target_torque = float(var_torque_factor.get())
            target_rpm = float(var_rpm_factor.get())
            target_hp = float(var_hp_target.get())
            target_speed = float(var_target_speed.get())
            differential = float(var_differential.get())
            engine_brake = float(var_engine_brake.get())
            gear_count = int(var_gear_count.get())
            retarder = int(var_retarder.get())
            values = (target_torque, target_rpm, target_hp, target_speed,
                      differential, engine_brake)
            if min(values[:5]) <= 0 or engine_brake < 0 or not all(
                    math.isfinite(v) for v in values):
                raise ValueError
            if not 6 <= gear_count <= 18 or not 0 <= retarder <= 3:
                raise ValueError
            compatible_versions = [v.strip() for v in var_tune_compat.get().split(",") if v.strip()]
        except ValueError:
            messagebox.showwarning(tr("title"), tr("tune_bad_factor"))
            return
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as ex:
            err("fail", e=ex)
            return
        stamp = time.strftime("%Y%m%d_%H%M%S")
        stem = "ETS2SE_Tune_%sHP_%sNm_%sG_%s" % (
            int(target_hp), int(target_torque), gear_count, stamp)
        destination = output_dir / (stem + ".scs")
        collision = 2
        while destination.exists():
            destination = output_dir / (stem + "_%02d.scs" % collision)
            collision += 1
        try:
            count = create_tuning_mod(destination, selected, target_hp, target_torque, target_rpm,
                                      gear_count, target_speed, differential,
                                      engine_brake, retarder, compatible_versions)
            var_status.set(tr("tune_success", n=count, path=destination))
            messagebox.showinfo(tr("title"), tr("tune_success", n=count, path=destination))
        except Exception as ex:
            err("fail", e=ex)

    # ---------------------------------------------------------- language
    def switch_lang():
        lang["i"] = 1 - lang["i"]
        build()
        scan(keep=True)
        refresh_mods()
        update_rank()
        refresh_dirty_indicator()
        status(tr("st_start"))

    # ---------------------------------------------------------- save tab logic
    def add_money(a):
        try:
            cur_m = num(var_money.get() or 0, 0, MAX_MONEY, tr("money"))
        except ValueError:
            cur_m = 0
        var_money.set(str(min(MAX_MONEY, cur_m + a)))

    def update_rank(*_):
        try:
            xp = num(var_xp.get() or 0, 0, INT32_MAX, "XP")
        except ValueError:
            var_rank.set("")
            return
        lv, nm, _ = rank_for_xp(xp)
        var_rank.set(tr("rank", name=tier_name(nm), lv=lv))

    var_xp.trace_add("write", update_rank)

    def max_skills():
        for v in skill_vars.values():
            v.set(6)
        for v in adr_vars:
            v.set(True)

    def max_all():
        if not messagebox.askyesno(tr("title"), tr("ask_max")):
            return
        var_money.set("100000000")
        var_xp.set(str(MAX_XP))
        max_skills()
        var_unlock.set(True)
        for v in ex_vars.values():
            v.set(True)
        status(tr("st_maxed"))

    def browse():
        d = filedialog.askdirectory(title=tr("pick_folder"))
        if d:
            if d != var_dir.get().strip() and has_pending_edits() and not messagebox.askyesno(
                    tr("title"), tr("ask_discard")):
                return
            var_dir.set(d)
            scan()

    def detect():
        found = find_game_dirs()
        if not found:
            status(tr("no_folder").split("\n")[0])
            info("no_folder")
            return
        if len(found) > 1:
            chosen = filedialog.askdirectory(
                title=tr("multiple_game_dirs"), initialdir=str(documents_dir()))
            if not chosen:
                return
            selected = Path(chosen)
            if not any((selected / folder).is_dir() for folder in ("profiles", "steam_profiles")):
                warn("invalid_game_dir")
                return
        else:
            selected = found[0]
        if str(selected) != var_dir.get().strip() and has_pending_edits() and not messagebox.askyesno(
                tr("title"), tr("ask_discard")):
            return
        var_dir.set(str(selected))
        scan()

    def scan(keep=False):
        previous_profile = current_profile() if keep else None
        base = var_dir.get().strip()
        if base and not var_tune_compat.get().strip():
            detected_version = detect_game_version(base)
            if detected_version:
                var_tune_compat.set(detected_version)
        st["profiles"] = list_profiles(base) if base else []
        st["saves"] = []
        refresh_dashboard()
        cmb, tree = w["cmb"], w["tree"]
        cmb["values"] = [p[0] for p in st["profiles"]]
        tree.delete(*tree.get_children())
        if not st["profiles"]:
            cmb.set("")
            cur["profile_idx"] = -1
            st["path"] = None
            st["fleet"] = st["fleet_error"] = st["fleet_selected"] = None
            st["loaded_values"] = None
            st["loaded_fingerprint"] = None
            st["save_writable"] = False
            refresh_dashboard()
            if st.get("refresh_fleet_view"):
                st["refresh_fleet_view"]()
            refresh_extra_tools()
            if base:
                status(tr("st_noprof"))
            return
        idx = default_profile_index(st["profiles"], previous_profile if keep else None)
        if idx < 0:
            cur["profile_idx"] = -1
            st["path"] = None
            st["fleet"] = st["fleet_error"] = st["fleet_selected"] = None
            st["loaded_values"] = None
            st["loaded_fingerprint"] = None
            st["save_writable"] = False
            cmb.set("")
            refresh_dashboard()
            if st.get("refresh_fleet_view"):
                st["refresh_fleet_view"]()
            refresh_extra_tools()
            status(tr("select_profile_explicit", n=len(st["profiles"])))
            return
        cmb.current(idx)
        on_profile(keep_sel=keep)

    def on_profile(_=None, keep_sel=False):
        i = w["cmb"].current()
        if i < 0:
            return
        if i != cur["profile_idx"] and has_pending_edits():
            if not messagebox.askyesno(tr("title"), tr("ask_discard")):
                w["cmb"].current(cur["profile_idx"])
                return
        cur["profile_idx"] = i
        st["saves"] = list_saves(st["profiles"][i][1])
        tree = w["tree"]
        tree.delete(*tree.get_children())
        for n, p, mt in st["saves"]:
            tree.insert("", "end", values=(n, time.strftime("%Y-%m-%d %H:%M", time.localtime(mt))))
        if not keep_sel:
            st["path"] = None
            st["fleet"] = st["fleet_error"] = st["fleet_selected"] = None
            st["loaded_values"] = None
            st["loaded_fingerprint"] = None
            st["save_writable"] = False
            status(tr("st_saves", n=len(st["saves"])))
        refresh_dashboard()
        if st.get("refresh_fleet_view"):
            st["refresh_fleet_view"]()
        refresh_extra_tools()
        refresh_mods()

    def on_save(_=None):
        sel = w["tree"].selection()
        if not sel:
            return
        path = st["saves"][w["tree"].index(sel[0])][1]
        if st.get("path") != path and has_pending_edits():
            if not messagebox.askyesno(tr("title"), tr("ask_discard")):
                old_path = st.get("path")
                for index, (_, candidate, _) in enumerate(st["saves"]):
                    if candidate == old_path:
                        w["tree"].selection_set(w["tree"].get_children()[index])
                        break
                return
        try:
            loaded_save = SaveFile(path)
            v = loaded_save.read()
            fingerprint = save_fingerprint(path)
        except Exception as e:
            st["path"] = None
            st["fleet"] = None
            st["fleet_error"] = str(e)
            st["fleet_selected"] = None
            st["loaded_values"] = None
            st["loaded_fingerprint"] = None
            st["save_writable"] = False
            refresh_dashboard()
            if st.get("refresh_fleet_view"):
                st["refresh_fleet_view"]()
            refresh_extra_tools()
            err("read_fail", e=e)
            return
        if st.get("path") != path:
            st["fleet_selected"] = None
        st["path"] = path
        st["loaded_values"] = dict(v)
        st["loaded_fingerprint"] = fingerprint
        st["save_writable"] = save_writes_supported(loaded_save.kind, loaded_save.was_encrypted)
        try:
            st["fleet"] = extract_fleet(loaded_save)
            st["fleet_error"] = None
        except Exception as fleet_exc:
            st["fleet"] = None
            st["fleet_error"] = str(fleet_exc)
        refresh_dashboard()
        if st.get("refresh_fleet_view"):
            st["refresh_fleet_view"]()
        refresh_extra_tools()
        var_money.set(str(v["money"]))
        var_xp.set(str(v["xp"]))
        for k in SKILLS:
            skill_vars[k].set(v[k])
        for (bit, _), bv in zip(ADR_CLASSES, adr_vars):
            bv.set(bool(v["adr"] & bit))
        var_cities.set(tr("cities_val", v=v["visited"], t=v["total_cities"]))
        var_unlock.set(False)
        for x in ex_vars.values():
            x.set(False)
        refresh_dirty_indicator()
        status(tr("st_loaded" if st["save_writable"] else "st_loaded_readonly", p=path))

    def apply():
        if not st["path"]:
            warn("sel_save")
            return
        if not st["save_writable"]:
            warn("save_readonly_format")
            return
        if game_running():
            warn("game_open")
            return
        if not Path(st["path"]).is_file():
            warn("sel_save")
            return
        try:
            if save_fingerprint(st["path"]) != st.get("loaded_fingerprint"):
                info("stale_save")
                return
            original = SaveFile(st["path"]).read()
        except Exception as e:
            err("fail", e=e)
            return
        try:
            vals = {"money": num(var_money.get(), 0, MAX_MONEY, tr("money")),
                    "xp": num(var_xp.get(), 0, INT32_MAX, "XP"),
                    "adr": sum(bit for (bit, _), v in zip(ADR_CLASSES, adr_vars) if v.get())}
            for k in SKILLS:
                vals[k] = num(skill_vars[k].get(), 0, 6, skill_name(k))
        except (ValueError, tk.TclError) as e:
            messagebox.showerror(tr("title"), str(e))
            return
        extras = [k for k, v in ex_vars.items() if v.get()]
        label = {"money": tr("money"), "xp": tr("xp"), "adr": tr("adr_box")}
        changes = []
        for key, value in vals.items():
            old = original[key]
            if old != value:
                name = label.get(key, skill_name(key))
                changes.append(f"{name}: {old:,} → {value:,}")
        if var_unlock.get():
            changes.append(tr("unlock_cities"))
        extra_labels = {"dealers": "ex_dealers", "repair": "ex_repair", "refuel": "ex_refuel"}
        changes.extend(tr(extra_labels[key]) for key in extras)
        if not changes:
            info("no_changes")
            return
        profile = (st["profiles"][cur["profile_idx"]][0] if current_profile()
                   else tr("dash_profile_value"))
        try:
            modified = time.strftime("%Y-%m-%d %H:%M:%S",
                                    time.localtime(Path(st["path"]).stat().st_mtime))
        except OSError:
            modified = tr("dash_mtime_unavailable")
        preview = (tr("preview_title") + "\n\n" +
                   tr("preview_context", profile=profile, path=st["path"], modified=modified) +
                   "\n".join("• " + item for item in changes))
        if not messagebox.askyesno(tr("title"), preview):
            return
        try:
            bak = edit_save(st["path"], vals, var_unlock.get(), extras)
        except Exception as e:
            err("fail", e=e)
            return
        on_save()
        status(tr("st_done", b=bak))
        info("applied", b=bak)

    def restore():
        if not st["path"]:
            warn("sel_save")
            return
        if not st["save_writable"]:
            warn("save_readonly_format")
            return
        if game_running():
            warn("game_open")
            return
        b = latest_backup(st["path"])
        if not b:
            info("no_backup")
            return
        if messagebox.askyesno(tr("title"), tr("ask_restore", b=b)):
            try:
                if save_fingerprint(st["path"]) != st.get("loaded_fingerprint"):
                    info("stale_save")
                    return
            except Exception as e:
                err("fail", e=e)
                return
            try:
                restore_save_backup(st["path"], b)
            except Exception as e:
                err("fail", e=e)
                return
            on_save()
            status(tr("st_restored"))

    # ---------------------------------------------------------- mods tab logic
    def report(lines):
        t = w["report"]
        t.configure(state="normal")
        t.delete("1.0", "end")
        for tag, text in lines:
            t.insert("end", text + "\n", tag)
        t.configure(state="disabled")

    def detect_ver():
        v = detect_game_version(var_dir.get())
        if v:
            var_gver.set(v)
        else:
            report([("warn", tr("v_log_missing"))])

    def refresh_mods():
        tv = w.get("mods")
        if tv is None:
            return
        tv.delete(*tv.get_children())
        st["mods"] = []
        base = var_dir.get().strip()
        prof = current_profile()
        if not base or prof is None:
            return
        if not var_gver.get():
            v = detect_game_version(base)
            if v:
                var_gver.set(v)
        local = {mod_id(p): p for p in list_mod_paths(base)}
        try:
            active = ProfileSii(Path(prof) / "profile.sii").get_mods()
        except Exception as e:
            report([("err", str(e))])
            return
        used = set()
        entries = []
        for a in active:
            ident, _, name = a.partition("|")
            used.add(ident)
            entries.append({"id": ident, "name": name or ident, "path": local.get(ident), "on": True})
        for ident, p in local.items():
            if ident not in used:
                entries.append({"id": ident, "name": ident, "path": p, "on": False})
        for e in entries:
            e["ver"] = ""
            if e["path"]:
                kind, names, _ = mod_file_list(e["path"])
                if kind in ("zip", "dir"):
                    man, _, _ = read_mod_manifest(e["path"], names, kind)
                    if man:
                        e["ver"] = man.get("package_version", "")
                        if not e["on"] and man.get("display_name"):
                            e["name"] = man["display_name"]
        st["mods"] = entries
        for e in entries:
            stt = tr("ms_active") if (e["on"] and e["path"]) else (
                tr("ms_missing") if e["on"] else tr("ms_off"))
            tv.insert("", "end", values=("✔" if e["on"] else "", e["name"],
                                          e["path"].name if e["path"] else "-", e["ver"], stt))

    def sel_mod():
        tv = w["mods"]
        s = tv.selection()
        if not s:
            warn("m_nomod")
            return None
        return tv.index(s[0])

    def save_active(entries):
        prof = current_profile()
        items = ["%s|%s" % (e["id"], e["name"]) for e in entries if e["on"]]
        write_profile_mods(prof, items)

    def guard():
        profile = current_profile()
        if profile is None:
            warn("sel_profile")
            return False
        if game_running():
            warn("game_open")
            return False
        try:
            parsed = ProfileSii(Path(profile) / "profile.sii")
        except Exception as ex:
            err("fail", e=ex)
            return False
        if parsed.kind != "text" or parsed.was_encrypted:
            warn("m_profile_readonly")
            return False
        return True

    def toggle_mod(on):
        i = sel_mod()
        if i is None or not guard():
            return
        e = st["mods"][i]
        if e["on"] == on:
            return
        action = tr("m_enable") if on else tr("m_disable")
        profile_name = st["profiles"][cur["profile_idx"]][0]
        if not messagebox.askyesno(tr("title"), tr("m_confirm_toggle",
                profile=profile_name, action=action, name=e["name"])):
            return
        e["on"] = on
        ents = st["mods"]
        if on:                      # move to the end of the active block
            ents.remove(e)
            last = max([j for j, x in enumerate(ents) if x["on"]], default=-1)
            ents.insert(last + 1, e)
        try:
            save_active(ents)
        except Exception as ex:
            err("fail", e=ex)
        refresh_mods()

    def move_mod(d):
        i = sel_mod()
        if i is None or not guard():
            return
        ents = st["mods"]
        j = i + d
        if not (0 <= j < len(ents)) or not ents[i]["on"] or not ents[j]["on"]:
            return
        profile_name = st["profiles"][cur["profile_idx"]][0]
        if not messagebox.askyesno(tr("title"), tr("m_confirm_move",
                profile=profile_name, name=ents[i]["name"])):
            return
        ents[i], ents[j] = ents[j], ents[i]
        try:
            save_active(ents)
        except Exception as ex:
            err("fail", e=ex)
        refresh_mods()
        w["mods"].selection_set(w["mods"].get_children()[j])

    def add_mod():
        if st["mod_copy_running"]:
            return
        base = var_dir.get().strip()
        d = mod_folder(base)
        if not d.is_dir():
            warn("m_nodir")
            return
        files = filedialog.askopenfilenames(
            title=tr("m_add"), filetypes=[("ETS2 mods", "*.scs *.zip"), ("All", "*.*")])
        plan = []
        for f in files:
            dest = d / Path(f).name
            overwrite = dest.exists()
            if overwrite and not messagebox.askyesno(tr("title"), tr("m_overwrite", f=dest.name)):
                continue
            plan.append((Path(f), dest, overwrite))
        if not plan:
            return
        st["mod_copy_running"] = True
        button = w.get("mod_add_button")
        if button is not None:
            button.configure(state="disabled")
        status(tr("copying_mods"))

        def copy_worker():
            copied, errors = [], []
            for source, destination, overwrite in plan:
                try:
                    if copy_mod_file(source, destination, overwrite=overwrite):
                        copied.append(destination)
                except Exception as ex:
                    errors.append((destination.name, str(ex)))

            def finish_copy():
                st["mod_copy_running"] = False
                button = w.get("mod_add_button")
                if button is not None:
                    try:
                        button.configure(state="normal")
                    except tk.TclError:
                        pass
                refresh_mods()
                if errors:
                    messagebox.showerror(tr("title"), "\n".join(
                        "%s: %s" % item for item in errors[:8]))
                if copied:
                    status(tr("m_added", n=len(copied)))
                    validate_files(copied)
                elif not errors:
                    status(tr("st_start"))

            try:
                root.after(0, finish_copy)
            except tk.TclError:
                pass

        threading.Thread(target=copy_worker, name="mod-copy", daemon=True).start()

    def enabled_file_sets(exclude=None):
        out = {}
        for e in st["mods"]:
            if e["on"] and e["path"] and mod_id(e["path"]) != exclude:
                kind, names, _ = mod_file_list(e["path"])
                out[mod_id(e["path"])] = {n.lower() for n in names if n.lower() != "manifest.sii"
                                          and not n.lower().startswith("mod_description")}
        return out

    def validate_files(paths):
        if st["validation_running"]:
            return
        base = var_dir.get().strip()
        gv = var_gver.get().strip()
        paths = list(paths)
        active_snapshot = [(e["id"], e["path"]) for e in st["mods"] if e["on"] and e["path"]]
        st["validation_running"] = True
        for button in w.get("validation_buttons", []):
            button.configure(state="disabled")
        status(tr("validation_running"))

        def validate_worker():
            try:
                others = {}
                for ident, path in active_snapshot:
                    kind, names, _ = mod_file_list(path)
                    others[ident] = {name.lower() for name in names
                                     if name.lower() != "manifest.sii"
                                     and not name.lower().startswith("mod_description")}
                results = [(path, *validate_mod(path, base, gv, others)) for path in paths]
                failure = None
            except Exception as ex:
                results, failure = [], ex

            def finish_validation():
                st["validation_running"] = False
                for button in w.get("validation_buttons", []):
                    try:
                        button.configure(state="normal")
                    except tk.TclError:
                        pass
                if failure is not None:
                    report([("err", tr("fail", e=failure))])
                    status(tr("fail", e=failure))
                    return
                lines = []
                for path, verdict, details in results:
                    lines.append(("head", tr("rep_head", name=mod_id(path))))
                    for level, code, params in details:
                        tag = {"ok": "ok", "warn": "warn", "err": "err"}.get(level, "")
                        mark = {"ok": "[OK] ", "warn": "[!] ", "err": "[X] ", "info": "[i] "}[level]
                        lines.append((tag, mark + tr(code, **params)))
                    lines.append((verdict, tr("verdict_" + verdict)))
                    lines.append(("", ""))
                lines.append(("", tr("rep_disclaimer")))
                report(lines)
                status(tr("validation_done"))

            try:
                root.after(0, finish_validation)
            except tk.TclError:
                pass

        threading.Thread(target=validate_worker, name="mod-validation", daemon=True).start()

    def validate_sel():
        i = sel_mod()
        if i is None:
            return
        e = st["mods"][i]
        if not e["path"]:
            warn("m_nofile")
            return
        validate_files([e["path"]])

    def validate_all():
        paths = [e["path"] for e in st["mods"] if e["path"]]
        if not paths:
            warn("m_nodir")
            return
        validate_files(paths)

    def open_mod_dir():
        d = mod_folder(var_dir.get().strip())
        if not d.is_dir():
            warn("m_nodir")
            return
        try:
            if os.name == "nt":
                os.startfile(str(d))      # noqa
            else:
                subprocess.Popen(["xdg-open", str(d)])
        except Exception:
            pass

    # ---------------------------------------------------------- settings tab
    def load_cfg():
        try:
            c = read_config(var_dir.get().strip())
        except Exception as e:
            err("fail", e=e)
            return
        cfg_vars["developer"].set(c.get("g_developer") == "1")
        cfg_vars["console"].set(c.get("g_console") == "1")
        cfg_vars["savefmt"].set(c.get("g_save_format") == "2")

    def apply_cfg():
        if game_running():
            warn("game_open")
            return
        try:
            write_config(var_dir.get().strip(), {
                "g_developer": "1" if cfg_vars["developer"].get() else "0",
                "g_console": "1" if cfg_vars["console"].get() else "0",
                "g_save_format": "2" if cfg_vars["savefmt"].get() else "0"})
        except Exception as e:
            err("fail", e=e)
            return
        status(tr("cfg_done"))
        info("cfg_done")

    # ---------------------------------------------------------- start
    status(tr("st_start"))
    build()
    refresh_dashboard()
    found = find_game_dirs()
    if len(found) == 1:
        var_dir.set(str(found[0]))
        scan()
        load_cfg_quiet = None
    elif len(found) > 1:
        status(tr("multiple_game_dirs"))
    status(tr("st_start"))
    root.mainloop()


# --------------------------------------------------------------------------
def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="ets2_save_editor.py --cli")
    ap.add_argument("file")
    ap.add_argument("--money", type=int)
    ap.add_argument("--xp", type=int)
    ap.add_argument("--skills", type=int, help="set all 5 skills to this value (0-6)")
    ap.add_argument("--adr", type=int, help="ADR bitmask (63 = all)")
    ap.add_argument("--all-cities", action="store_true")
    ap.add_argument("--dealers", action="store_true")
    ap.add_argument("--repair", action="store_true")
    ap.add_argument("--refuel", action="store_true")
    ap.add_argument("--out", help="write to this file instead of editing in place")
    a = ap.parse_args(argv)
    vals = {}
    if a.money is not None:
        vals["money"] = a.money
    if a.xp is not None:
        vals["xp"] = a.xp
    if a.adr is not None:
        vals["adr"] = a.adr
    if a.skills is not None:
        for k in SKILLS:
            vals[k] = a.skills
    extras = [n for n, f in (("dealers", a.dealers), ("repair", a.repair),
                             ("refuel", a.refuel)) if f]
    if not vals and not a.all_cities and not extras:
        print(SaveFile(a.file).read())
        return
    if a.out:
        write_save_copy(a.file, a.out, vals, a.all_cities, extras)
        print(SaveFile(a.out).read())
    else:
        print("backup:", edit_save(a.file, vals, a.all_cities, extras))
        print(SaveFile(a.file).read())


def run_web_ui():
    """Launch the offline WebView2 UI while keeping the existing Tk GUI available."""
    import webview
    from ets2_web_bridge import EditorBridge

    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    index = base / "ui" / "index.html"
    if not index.is_file():
        raise FileNotFoundError("The bundled UI files are missing: " + str(index))
    bridge = EditorBridge(sys.modules[__name__])
    window = webview.create_window(
        APP, url=index.as_uri(), js_api=bridge, width=1440, height=900,
        min_size=(1100, 700), background_color="#080f18", text_select=True)
    bridge.attach_window(window)
    webview.start(gui="edgechromium", debug=False)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        _cli(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "--classic":
        log_path = configure_error_logging()
        try:
            run_gui()
        except Exception as exc:
            logging.exception("Classic application startup failed")
            try:
                import tkinter.messagebox
                tkinter.messagebox.showerror(APP,
                    "The application could not start:\n%s\n\nLog file:\n%s" % (exc, log_path))
            except Exception:
                print("The application could not start: %s\nLog file: %s" % (exc, log_path),
                      file=sys.stderr)
    else:
        log_path = configure_error_logging()
        try:
            run_web_ui()
        except Exception as exc:
            logging.exception("WebView2 UI could not start; falling back to classic Tk UI")
            try:
                import tkinter.messagebox
                tkinter.messagebox.showwarning(APP,
                    "WebView2 interface could not start; opening the classic interface instead.\n\n"
                    "If the modern interface is needed, install Microsoft Edge WebView2 Runtime.\n\n"
                    "Details: %s\nLog file: %s" % (exc, log_path))
                run_gui()
            except Exception as fallback_exc:
                logging.exception("Classic fallback could not start")
                try:
                    import tkinter.messagebox
                    tkinter.messagebox.showerror(APP,
                        "The application could not start:\n%s\n\nLog file:\n%s" %
                        (fallback_exc, log_path))
                except Exception:
                    print("The application could not start: %s\nLog file: %s" %
                          (fallback_exc, log_path), file=sys.stderr)
