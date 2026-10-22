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
import zipfile
from pathlib import Path

APP = "ETS2 Save Editor"
VERSION = "2.1"

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


def tune_engine_definition(text, torque_factor, rpm_factor):
    """Adjust engine definition values while preserving the Sii file layout."""
    out = []
    info_line = 0
    for line in text.splitlines(keepends=True):
        if re.match(r"\s*torque:\s*", line):
            line = re.sub(r"(\btorque:\s*)([-+]?\d+(?:\.\d+)?)",
                          lambda m: m.group(1) + _scale_scs_number(m.group(2), torque_factor),
                          line, count=1)
        elif re.match(r"\s*(?:secondary_)?torque_curve\[\]:", line):
            def scale_curve(match):
                rpm = _scale_scs_number(match.group(1), rpm_factor, integer=True)
                return "(" + rpm + ", " + match.group(2) + ")"
            line = re.sub(r"\(\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*\)",
                          scale_curve, line, count=1)
        elif re.match(r"\s*rpm_\w+\s*:", line):
            line = re.sub(r"([-+]?\d+(?:\.\d+)?)",
                          lambda m: _scale_scs_number(m.group(1), rpm_factor, integer=True),
                          line, flags=re.I)
        elif re.match(r"\s*info\[\]:\s*\"", line):
            factor = (torque_factor * rpm_factor) if info_line == 0 else (
                torque_factor if info_line == 1 else rpm_factor)
            info_line += 1
            def scale_display(match):
                old = int(match.group(1) + (match.group(2) or ""))
                new = 0 if old == 0 else max(1, int(round(old * factor)))
                digits = str(new)
                if len(digits) > 3:
                    return digits[:-3] + "@@dg@@" + digits[-3:]
                return digits
            line = re.sub(r"(\d+)(?:@@dg@@(\d{3}))?", scale_display, line)
        out.append(line)
    return "".join(out)


def create_tuning_mod(destination, selected_trucks, torque_factor, rpm_factor):
    """Create a ZIP-format SCS mod containing only selected truck engines."""
    template_root = tuning_template_root()
    entries = []
    for display_name, truck_id in TUNING_TRUCKS.items():
        if display_name not in selected_trucks:
            continue
        source = template_root / "def" / "vehicle" / "truck" / truck_id / "engine"
        if not source.is_dir():
            raise FileNotFoundError("Engine templates are missing for " + display_name)
        for path in sorted(source.glob("*.sii")):
            rel = path.relative_to(template_root).as_posix()
            original = path.read_text(encoding="utf-8", errors="replace")
            entries.append((rel, tune_engine_definition(original, torque_factor, rpm_factor)))
    if not entries:
        raise ValueError("Select at least one truck.")

    package_id = Path(destination).stem
    manifest = '''SiiNunit\n{\nmod_package : .%s {\n package_version: "1.0"\n display_name: "ETS2 Save Editor Performance Tune"\n author: "ETS2 Save Editor"\n category[]: "truck"\n compatible_versions[]: "1.60.*"\n}\n}\n''' % package_id
    description = (
        "Performance tuning for selected ETS2 truck models.\n"
        "Torque multiplier: %.2fx\nEngine RPM multiplier: %.2fx\n\n"
        "This changes engine definitions. Maximum road speed still depends on gearbox, "
        "truck configuration, load, and game physics.\n" % (torque_factor, rpm_factor)
    )
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.sii", manifest)
        archive.writestr("mod_description.txt", description)
        for name, contents in entries:
            archive.writestr(name, contents.encode("utf-8"))
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

    def apply(self, vals, unlock_cities=False, extras=()):
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

    def _repair_refuel(self, extras):
        nl_re = r"[^\r\n]*"

        def fix(m):
            kind, body = m.group(1), m.group(0)
            if "repair" in extras:
                for fn in REPAIR_SCALARS:
                    body = re.sub(r"^( %s: )%s" % (fn, nl_re), r"\g<1>0", body, flags=re.M)
                for fn in REPAIR_ARRAYS:
                    body = re.sub(r"^( %s\[\d+\]: )%s" % (fn, nl_re), r"\g<1>0", body, flags=re.M)
            if "refuel" in extras and kind == "vehicle":
                body = re.sub(r"^( fuel_relative: )%s" % nl_re, r"\g<1>1", body, flags=re.M)
            return body
        self.t = re.sub(r"^(vehicle|trailer) : [^\r\n]*\{[ \t]*\r?\n.*?^\}", fix, self.t,
                        flags=re.M | re.S)

    def verify(self):
        pass

    def apply(self, vals, unlock_cities=False, extras=()):
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
            self._repair_refuel(extras)

    def to_bytes(self):
        return self.t.encode("utf-8", "surrogateescape")


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

    def apply(self, vals, unlock_cities=False, extras=()):
        self.impl.apply(vals, unlock_cities, extras)

    def verify(self):
        self.impl.verify()

    def to_bytes(self):
        return self.impl.to_bytes()


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
    out = []
    for c in cands:
        if c.is_dir() and any((c / s).is_dir() for s in ("profiles", "steam_profiles")):
            if c not in out:
                out.append(c)
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


def backup_root():
    return Path.home() / "ETS2SaveEditor_Backups"


def make_backup(game_sii):
    game_sii = Path(game_sii)
    prof = game_sii.parent.parent.parent.name
    dest = backup_root() / prof / game_sii.parent.name / time.strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(game_sii, dest / "game.sii")
    return dest / "game.sii"


def latest_backup(game_sii):
    game_sii = Path(game_sii)
    d = backup_root() / game_sii.parent.parent.parent.name / game_sii.parent.name
    if not d.is_dir():
        return None
    subs = sorted(x for x in d.iterdir() if (x / "game.sii").is_file())
    return (subs[-1] / "game.sii") if subs else None


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


def edit_save(game_sii, vals, unlock_cities=False, extras=()):
    """Back up, edit, write and verify. Returns the backup path."""
    game_sii = Path(game_sii)
    sf = SaveFile(game_sii)
    sf.apply(vals, unlock_cities, extras)
    new = sf.to_bytes()
    bak = make_backup(game_sii)
    tmp = game_sii.with_name("game.sii.tmp")
    tmp.write_bytes(new)
    try:
        chk_file = SaveFile(tmp)
        chk_file.verify()
        chk = chk_file.read()
        for k, v in vals.items():
            if chk[k] != int(v):
                raise SiiError("verification failed for %s" % k)
        os.replace(tmp, game_sii)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    return bak


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
        data, _ = decode_container(self.path.read_bytes())
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
    ps = ProfileSii(p)
    ps.set_mods(items)
    new = ps.to_bytes()
    dest = backup_root() / Path(profile_dir).name / "profile_sii" / time.strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, dest / "profile.sii")
    tmp = p.with_name("profile.sii.tmp")
    tmp.write_bytes(new)
    try:
        if ProfileSii(tmp).get_mods() != list(items):
            raise SiiError("verification failed")
        os.replace(tmp, p)
    except Exception:
        if tmp.exists():
            tmp.unlink()
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
                m = re.search(r"[Vv]ersion[:\s]+(\d+\.\d+(?:\.\d+){0,2})", line)
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
            if bad:
                res.append(("err", "v_corrupt", {"name": bad}))
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
    raw = p.read_bytes().decode("utf-8", "surrogateescape")
    nl = "\r\n" if "\r\n" in raw else "\n"
    for k, v in changes.items():
        pat = r'^uset %s "[^"]*"' % re.escape(k)
        if re.search(pat, raw, re.M):
            raw = re.sub(pat, lambda m: 'uset %s "%s"' % (k, v), raw, count=1, flags=re.M)
        else:
            raw = raw.rstrip("\r\n") + nl + 'uset %s "%s"' % (k, v) + nl
    dest = backup_root() / "config_cfg" / time.strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, dest / "config.cfg")
    p.write_bytes(raw.encode("utf-8", "surrogateescape"))


# --------------------------------------------------------------------------
# translations (en, fa)
# --------------------------------------------------------------------------
T = {
    "title": ("ETS2 Save Editor", "ادیتور سیو یورو تراک ۲"),
    "lang_btn": ("فارسی", "English"),
    "theme_day": ("Day", "روز"),
    "theme_night": ("Night", "شب"),
    "accent": ("Accent", "رنگ تأکیدی"),
    "tab_save": ("Save editor", "ادیت سیو"),
    "tab_extras": ("Extras", "امکانات بیشتر"),
    "tab_mods": ("Mods", "مدیریت ماد"),
    "tab_tune": ("Truck tuning", "تقویت کامیون"),
    "tab_settings": ("Game settings", "تنظیمات بازی"),
    "tune_title": ("Build a performance mod", "ساخت ماد تقویت موتور"),
    "tune_trucks": ("Truck models included", "مدل‌های کامیون در ماد"),
    "tune_torque": ("Torque multiplier", "ضریب گشتاور"),
    "tune_rpm": ("Engine RPM multiplier", "ضریب دور موتور"),
    "tune_build": ("Create and install mod", "ساخت و نصب ماد"),
    "tune_note": ("Choose the engine strength and RPM range. The mod changes engine definitions for the selected models. "
                  "Top speed is not a fixed target; the gearbox, truck setup, load, and game physics also affect it.",
                  "قدرت موتور و بازه‌ی دور موتور را انتخاب کن. ماد مشخصات موتور مدل‌های انتخابی را تغییر می‌دهد. "
                  "سرعت نهایی عدد تضمینی نیست؛ گیربکس، تنظیم کامیون، بار و فیزیک بازی هم اثر دارند."),
    "tune_success": ("Created {n} engine files and installed the mod:\n{path}\n\nEnable it in the game's Mod Manager.",
                     "{n} فایل موتور ساخته و ماد نصب شد:\n{path}\n\nآن را در Mod Manager بازی فعال کن."),
    "tune_bad_factor": ("Torque must be 1.0–3.0 and RPM must be 1.0–1.5.",
                        "ضریب گشتاور باید بین ۱ تا ۳ و دور موتور بین ۱ تا ۱٫۵ باشد."),
    "tune_gauge": ("SPEED ESTIMATE", "برآورد سرعت"),
    "tune_gauge_note": ("Visual estimate from RPM setting · gearbox and load change real speed",
                        "برآورد نمایشی بر پایه‌ی دور موتور · گیربکس و بار سرعت واقعی را تغییر می‌دهند"),
    "tune_selected": ("TRUCKS IN THIS BUILD", "کامیون‌های این ماد"),
    "tune_power": ("TORQUE OUTPUT", "قدرت گشتاور"),
    "tune_rpm_short": ("RPM RANGE", "بازه دور موتور"),
    "tune_live": ("TUNING PREVIEW", "پیش‌نمایش تیونینگ"),
    "tune_profile": ("PERFORMANCE PROFILE", "پروفایل عملکرد"),
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
    "cities_val": ("{v} visited / {t} in game", "{v} بازدیدشده از {t} شهر"),
    "unlock_cities": ("Unlock all cities (mark as visited)", "باز کردن همه‌ی شهرها (بازدیدشده)"),
    "max_all": ("Max everything", "همه‌چیز حداکثر"),
    "restore": ("Restore last backup", "بازگردانی آخرین بکاپ"),
    "apply": ("Apply changes", "اعمال تغییرات"),
    "st_start": ("Pick your ETS2 folder, then a profile and a save.",
                 "پوشه‌ی بازی، بعد پروفایل و یک سیو را انتخاب کن."),
    "st_noprof": ("No profiles found in that folder.", "پروفایلی در این پوشه پیدا نشد."),
    "st_saves": ("{n} save(s) found. Select one.", "{n} سیو پیدا شد. یکی را انتخاب کن."),
    "st_loaded": ("Loaded: {p}", "بارگذاری شد: {p}"),
    "st_maxed": ("Everything set to max. Click 'Apply changes' to write it.",
                 "همه‌چیز روی حداکثر گذاشته شد. برای ذخیره «اعمال تغییرات» را بزن."),
    "st_done": ("Done. Backup: {b}", "انجام شد. بکاپ: {b}"),
    "st_restored": ("Backup restored.", "بکاپ بازگردانی شد."),
    "no_folder": ("Could not find the folder automatically.\nClick Browse and pick Documents\\Euro Truck Simulator 2.",
                  "پوشه به‌صورت خودکار پیدا نشد.\nانتخاب را بزن و Documents\\Euro Truck Simulator 2 را انتخاب کن."),
    "pick_folder": ("Select the 'Euro Truck Simulator 2' folder", "پوشه‌ی «Euro Truck Simulator 2» را انتخاب کن"),
    "sel_save": ("Select a save first.", "اول یک سیو انتخاب کن."),
    "sel_profile": ("Select a profile first (Save editor tab).", "اول یک پروفایل انتخاب کن (تب ادیت سیو)."),
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
    "ex_repair": ("Repair all trucks and trailers (engine, gearbox, cabin, chassis, wheels, cargo)",
                  "تعمیر کامل همه‌ی کامیون‌ها و تریلرها (موتور، گیربکس، کابین، شاسی، چرخ، بار)"),
    "ex_refuel": ("Refuel all trucks (full tank)", "پر کردن باک همه‌ی کامیون‌ها"),
    "ex_note": ("Not included on purpose: buying garages / trucks and removing loans. "
                "Their values are not documented well enough to change them safely.",
                "عمداً شامل نیست: خرید گاراژ و کامیون و حذف وام. مقدارهای آنها آنقدر مستند نیست که ایمن تغییر کند."),
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
    "m_add": ("Add mod...", "افزودن ماد..."),
    "m_validate": ("Validate selected", "اعتبارسنجی انتخابی"),
    "m_validate_all": ("Validate all", "اعتبارسنجی همه"),
    "m_open": ("Open mod folder", "باز کردن پوشه‌ی ماد"),
    "ms_active": ("Active", "فعال"),
    "ms_off": ("Off", "خاموش"),
    "ms_missing": ("Active, file not in mod folder (Workshop?)", "فعال، فایل در پوشه‌ی mod نیست (Workshop؟)"),
    "m_order_note": ("Order is the one stored in profile.sii. Check the final order in the game's Mod Manager.",
                     "ترتیب همان است که در profile.sii ذخیره می‌شود. ترتیب نهایی را در Mod Manager خود بازی ببین."),
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
}
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
    root.geometry("1040x800")
    root.minsize(900, 700)
    style = ttk.Style()

    palette = [
        ("Sky", "#38bdf8"), ("Amber", "#f59e0b"), ("Emerald", "#34d399"),
        ("Violet", "#a78bfa"), ("Rose", "#fb7185"), ("Ice", "#e2e8f0"),
    ]
    theme_cfg = Path(os.environ.get("APPDATA", str(Path.home()))) / "ETS2 Save Editor" / "ui.json"
    try:
        saved_theme = json.loads(theme_cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        saved_theme = {}
    appearance = {"mode": saved_theme.get("mode", "night"),
                  "accent": saved_theme.get("accent", palette[0][0])}
    if appearance["mode"] not in ("day", "night"):
        appearance["mode"] = "night"
    if appearance["accent"] not in [x[0] for x in palette]:
        appearance["accent"] = palette[0][0]

    def save_appearance():
        try:
            theme_cfg.parent.mkdir(parents=True, exist_ok=True)
            theme_cfg.write_text(json.dumps(appearance), encoding="utf-8")
        except OSError:
            pass

    def apply_appearance():
        night = appearance["mode"] == "night"
        accent = dict(palette)[appearance["accent"]]
        bg, panel = ("#101923", "#182431") if night else ("#edf3f8", "#ffffff")
        fg = "#e7eef5" if night else "#17212b"
        muted = "#aab8c5" if night else "#526170"
        field = "#0c141c" if night else "#ffffff"
        style.theme_use("clam")
        style.configure(".", background=bg, foreground=fg)
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TLabelFrame", background=bg, foreground=accent, bordercolor="#344454")
        style.configure("TLabelFrame.Label", background=bg, foreground=accent)
        style.configure("TButton", background=panel, foreground=fg, bordercolor="#344454", padding=(10, 5))
        style.map("TButton", background=[("active", accent)], foreground=[("active", "#07111a")])
        style.configure("TCheckbutton", background=bg, foreground=fg)
        style.map("TCheckbutton", background=[("active", bg)])
        style.configure("TNotebook", background=bg, bordercolor="#344454")
        style.configure("TNotebook.Tab", background=panel, foreground=muted, padding=(12, 7))
        style.map("TNotebook.Tab", background=[("selected", accent)], foreground=[("selected", "#07111a")])
        style.configure("TEntry", fieldbackground=field, foreground=fg, insertcolor=fg)
        style.configure("TCombobox", fieldbackground=field, background=panel, foreground=fg, arrowcolor=accent)
        style.map("TCombobox", fieldbackground=[("readonly", field)], foreground=[("readonly", fg)])
        style.configure("Treeview", background=panel, fieldbackground=panel, foreground=fg,
                        rowheight=27, bordercolor="#344454")
        style.configure("Treeview.Heading", background=bg, foreground=accent, padding=6)
        style.map("Treeview", background=[("selected", accent)], foreground=[("selected", "#07111a")])
        style.configure("TSeparator", background="#344454")
        root.configure(background=bg)

    lang = {"i": 0}            # 0 = English, 1 = Persian

    def tr(key, **kw):
        s = T[key][lang["i"]]
        return s.format(**kw) if kw else s

    # persistent state (survives language switches)
    st = {"profiles": [], "saves": [], "path": None, "mods": [], "mods_other": {}}
    var_dir = tk.StringVar()
    var_money = tk.StringVar()
    var_xp = tk.StringVar()
    var_rank = tk.StringVar()
    var_cities = tk.StringVar(value="-")
    var_unlock = tk.BooleanVar(value=False)
    var_status = tk.StringVar()
    tune_truck_vars = {name: tk.BooleanVar(value=True) for name in TUNING_TRUCKS}
    var_torque_factor = tk.StringVar(value="1.5")
    var_rpm_factor = tk.StringVar(value="1.1")
    var_gauge_target = tk.DoubleVar(value=110.0)
    needle_speed = {"value": 0.0, "target": 99.0, "job": None}
    var_gver = tk.StringVar()
    skill_vars = {k: tk.IntVar(value=0) for k in SKILLS}
    adr_vars = [tk.BooleanVar(value=False) for _ in ADR_CLASSES]
    ex_vars = {k: tk.BooleanVar(value=False) for k in ("dealers", "repair", "refuel")}
    cfg_vars = {k: tk.BooleanVar(value=False) for k in CONFIG_KEYS}
    w = {}                     # widgets that get rebuilt
    cur = {"profile_idx": 0}

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

    # ---------------------------------------------------------- builders
    container = ttk.Frame(root)
    container.pack(fill="both", expand=True)

    def build():
        for c in container.winfo_children():
            c.destroy()
        root.title("%s %s" % (tr("title"), VERSION))
        font = ("Tahoma", 10) if lang["i"] else ("Segoe UI", 10)
        try:
            apply_appearance()
            style.configure(".", font=font)
            style.configure("Treeview", font=font, rowheight=24)
            style.configure("Treeview.Heading", font=font)
        except tk.TclError:
            pass
        bar = ttk.Frame(container)
        bar.pack(fill="x", padx=8, pady=(6, 0))
        ttk.Button(bar, text=tr("lang_btn"), command=switch_lang).pack(side="right")
        palette_names = [x[0] for x in palette]
        accent_box = ttk.Combobox(bar, values=palette_names, state="readonly", width=9)
        accent_box.set(appearance["accent"])
        accent_box.pack(side="right", padx=6)
        ttk.Label(bar, text=tr("accent")).pack(side="right")
        def choose_accent(_=None):
            appearance["accent"] = accent_box.get()
            save_appearance()
            apply_appearance()
        accent_box.bind("<<ComboboxSelected>>", choose_accent)
        ttk.Button(bar, text=tr("theme_day") if appearance["mode"] == "night" else tr("theme_night"),
                   command=toggle_theme).pack(side="right", padx=6)
        nb = ttk.Notebook(container)
        nb.pack(fill="both", expand=True, padx=8, pady=6)
        w["nb"] = nb
        t1, t2, t3, t4, t5 = (ttk.Frame(nb) for _ in range(5))
        nb.add(t1, text=tr("tab_save"))
        nb.add(t2, text=tr("tab_extras"))
        nb.add(t3, text=tr("tab_mods"))
        nb.add(t4, text=tr("tab_settings"))
        nb.add(t5, text=tr("tab_tune"))
        build_save(t1)
        build_extras(t2)
        build_mods(t3)
        build_settings(t4)
        build_tuning(t5)
        bt = ttk.Frame(container)
        bt.pack(fill="x", padx=8, pady=2)
        ttk.Button(bt, text=tr("max_all"), command=max_all).pack(side="left", padx=4)
        ttk.Button(bt, text=tr("restore"), command=restore).pack(side="left", padx=4)
        ttk.Button(bt, text=tr("apply"), command=apply).pack(side="right", padx=4)
        ttk.Label(container, textvariable=var_status, relief="sunken", anchor="w"
                  ).pack(fill="x", side="bottom")

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
        ttk.Label(ed, textvariable=var_rank, foreground="#555").grid(
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

    def build_extras(tab):
        fr = ttk.LabelFrame(tab, text=tr("ex_title"))
        fr.pack(fill="x", padx=10, pady=10)
        for k, key in (("dealers", "ex_dealers"), ("repair", "ex_repair"), ("refuel", "ex_refuel")):
            ttk.Checkbutton(fr, text=tr(key), variable=ex_vars[k]).pack(anchor="w", padx=10, pady=6)
        ttk.Label(tab, text=tr("ex_note"), wraplength=820, foreground="#555", justify="left"
                  ).pack(anchor="w", padx=14, pady=6)

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
        for key, cmd in (("m_refresh", refresh_mods), ("m_enable", lambda: toggle_mod(True)),
                         ("m_disable", lambda: toggle_mod(False)), ("m_up", lambda: move_mod(-1)),
                         ("m_down", lambda: move_mod(1)), ("m_add", add_mod)):
            ttk.Button(bt, text=tr(key), command=cmd).pack(side="left", padx=2)
        bt2 = ttk.Frame(tab)
        bt2.pack(fill="x", padx=8)
        for key, cmd in (("m_validate", validate_sel), ("m_validate_all", validate_all),
                         ("m_open", open_mod_dir)):
            ttk.Button(bt2, text=tr(key), command=cmd).pack(side="left", padx=2)
        ttk.Label(tab, text=tr("m_order_note"), foreground="#555", wraplength=860
                  ).pack(anchor="w", padx=10, pady=2)
        box = ttk.Frame(tab)
        box.pack(fill="both", expand=True, padx=8, pady=4)
        txt = tk.Text(box, height=10, wrap="word", state="disabled")
        sb = ttk.Scrollbar(box, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.tag_configure("ok", foreground="#1a7f37")
        txt.tag_configure("warn", foreground="#b35900")
        txt.tag_configure("err", foreground="#c62828")
        txt.tag_configure("head", font=("TkDefaultFont", 10, "bold"))
        w["report"] = txt

    def build_settings(tab):
        fr = ttk.LabelFrame(tab, text=tr("cfg_title"))
        fr.pack(fill="x", padx=10, pady=10)
        for k, key in (("developer", "cfg_dev"), ("console", "cfg_con"), ("savefmt", "cfg_fmt")):
            ttk.Checkbutton(fr, text=tr(key), variable=cfg_vars[k]).pack(anchor="w", padx=10, pady=6)
        row = ttk.Frame(fr)
        row.pack(anchor="w", padx=10, pady=8)
        ttk.Button(row, text=tr("cfg_load"), command=load_cfg).pack(side="left", padx=2)
        ttk.Button(row, text=tr("cfg_apply"), command=apply_cfg).pack(side="left", padx=2)
        ttk.Label(tab, text=tr("cfg_note"), foreground="#555").pack(anchor="w", padx=14)

    def build_tuning(tab):
        accent = dict(palette)[appearance["accent"]]
        bg = "#080d12" if appearance["mode"] == "night" else "#eaf0f5"
        panel = "#111a22" if appearance["mode"] == "night" else "#ffffff"
        ink = "#edf5fb" if appearance["mode"] == "night" else "#172330"
        muted = "#8494a3" if appearance["mode"] == "night" else "#647687"
        gold = "#f5a742"
        tab.configure(style="Tune.TFrame")
        style.configure("Tune.TFrame", background=bg)
        style.configure("TuneCard.TFrame", background=panel)
        style.configure("TuneTitle.TLabel", background=panel, foreground=ink,
                        font=("Segoe UI Semibold", 13))
        style.configure("TuneMuted.TLabel", background=panel, foreground=muted,
                        font=("Segoe UI", 9))
        style.configure("TuneValue.TLabel", background=panel, foreground=ink,
                        font=("Segoe UI Semibold", 18))
        style.configure("TuneAccent.TLabel", background=panel, foreground=accent,
                        font=("Segoe UI Semibold", 9))
        style.configure("TuneGold.TLabel", background=panel, foreground=gold,
                        font=("Segoe UI Semibold", 9))

        outer = ttk.Frame(tab, style="Tune.TFrame")
        outer.pack(fill="both", expand=True, padx=14, pady=12)
        header = ttk.Frame(outer, style="Tune.TFrame")
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="HAULER / PERFORMANCE LAB", style="TuneAccent.TLabel",
                  background=bg).pack(side="left")
        ttk.Label(header, text=tr("tune_title"), style="TuneTitle.TLabel",
                  background=bg).pack(side="right")

        row = ttk.Frame(outer, style="Tune.TFrame")
        row.pack(fill="both", expand=True)
        left = ttk.Frame(row, style="TuneCard.TFrame", padding=12)
        left.pack(side="left", fill="y", padx=(0, 10))
        right = ttk.Frame(row, style="Tune.TFrame")
        right.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text=tr("tune_trucks"), style="TuneAccent.TLabel").pack(anchor="w", pady=(2, 7))
        for name, variable in tune_truck_vars.items():
            ttk.Checkbutton(left, text=name, variable=variable,
                            command=refresh_tuning_dashboard).pack(anchor="w", pady=3)
        ttk.Separator(left).pack(fill="x", pady=10)
        ttk.Label(left, text=tr("tune_torque"), style="TuneMuted.TLabel").pack(anchor="w")
        ttk.Spinbox(left, from_=1.0, to=3.0, increment=0.1, width=13,
                    textvariable=var_torque_factor,
                    command=refresh_tuning_dashboard).pack(anchor="w", pady=(3, 9))
        ttk.Label(left, text=tr("tune_rpm"), style="TuneMuted.TLabel").pack(anchor="w")
        ttk.Spinbox(left, from_=1.0, to=1.5, increment=0.05, width=13,
                    textvariable=var_rpm_factor,
                    command=refresh_tuning_dashboard).pack(anchor="w", pady=(3, 9))
        ttk.Button(left, text=tr("tune_build"), command=build_tuning_mod).pack(fill="x", pady=(8, 3))

        hero = ttk.Frame(right, style="TuneCard.TFrame", padding=10)
        hero.pack(fill="both", expand=True)
        hero_head = ttk.Frame(hero, style="TuneCard.TFrame")
        hero_head.pack(fill="x")
        ttk.Label(hero_head, text=tr("tune_gauge"), style="TuneGold.TLabel").pack(side="left")
        ttk.Label(hero_head, text=tr("tune_live"), style="TuneMuted.TLabel").pack(side="right")
        gauge = tk.Canvas(hero, height=260, background=panel, highlightthickness=0)
        gauge.pack(fill="both", expand=True, pady=(0, 3))
        w["tune_gauge"] = gauge
        gauge.bind("<Configure>", lambda _e: draw_tuning_gauge())
        gauge.bind("<MouseWheel>", lambda e: set_gauge_target(var_gauge_target.get() + (5 if e.delta > 0 else -5)))

        stats = ttk.Frame(right, style="Tune.TFrame")
        stats.pack(fill="x", pady=(10, 0))
        cards = []
        for title, initial, suffix, color in (
                ("tune_power", "1.50×", "TORQUE", "TuneAccent.TLabel"),
                ("tune_rpm_short", "1.10×", "RPM", "TuneGold.TLabel"),
                ("tune_selected", "3", "TRUCKS", "TuneAccent.TLabel")):
            card = ttk.Frame(stats, style="TuneCard.TFrame", padding=(10, 8))
            card.pack(side="left", fill="x", expand=True, padx=(0, 8))
            ttk.Label(card, text=tr(title), style="TuneMuted.TLabel").pack(anchor="w")
            value = ttk.Label(card, text=initial, style="TuneValue.TLabel")
            value.pack(anchor="w", pady=2)
            ttk.Label(card, text=suffix, style=color).pack(anchor="w")
            cards.append(value)
        w["tune_cards"] = cards
        ttk.Label(right, text=tr("tune_gauge_note"), style="TuneMuted.TLabel",
                  wraplength=720, justify="left").pack(anchor="w", pady=(8, 2))
        ttk.Label(right, text=tr("tune_note"), style="TuneMuted.TLabel",
                  wraplength=720, justify="left").pack(anchor="w", pady=(2, 0))
        var_torque_factor.trace_add("write", refresh_tuning_dashboard)
        var_rpm_factor.trace_add("write", refresh_tuning_dashboard)
        draw_tuning_gauge()
        refresh_tuning_dashboard()

    def set_gauge_target(value):
        value = max(0.0, min(300.0, float(value)))
        var_gauge_target.set(value)
        draw_tuning_gauge()

    def draw_tuning_gauge():
        canvas = w.get("tune_gauge")
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        width = max(canvas.winfo_width(), 280)
        height = max(canvas.winfo_height(), 230)
        cx, cy = width / 2, height * 0.91
        radius = min(width * 0.34, height * 0.86)
        value = max(0.0, min(300.0, needle_speed["value"]))
        accent = dict(palette)[appearance["accent"]]
        start, extent = 225, 270
        bbox = (cx-radius, cy-radius, cx+radius, cy+radius)
        canvas.create_arc(bbox, start=start, extent=extent, style="arc", outline="#273540", width=13)
        active_extent = extent * value / 300.0
        canvas.create_arc(bbox, start=start, extent=active_extent, style="arc", outline=accent, width=13)
        for tick in range(0, 301, 20):
            angle = math.radians(start + extent * tick / 300.0)
            r1, r2 = radius - 8, radius - (22 if tick % 60 == 0 else 15)
            x1, y1 = cx + r1 * math.cos(angle), cy - r1 * math.sin(angle)
            x2, y2 = cx + r2 * math.cos(angle), cy - r2 * math.sin(angle)
            canvas.create_line(x1, y1, x2, y2, fill="#718391" if tick % 60 else "#d3dde5",
                               width=2 if tick % 60 == 0 else 1)
            if tick % 60 == 0:
                rt = radius - 37
                tx, ty = cx + rt * math.cos(angle), cy - rt * math.sin(angle)
                canvas.create_text(tx, ty, text=str(tick), fill="#aab8c5", font=("Segoe UI", 9))
        angle = math.radians(start + extent * value / 300.0)
        tipx, tipy = cx + (radius - 48) * math.cos(angle), cy - (radius - 48) * math.sin(angle)
        canvas.create_line(cx, cy, tipx, tipy, fill="#f5a742", width=4, capstyle="round")
        canvas.create_oval(cx-7, cy-7, cx+7, cy+7, fill="#f5a742", outline="")
        canvas.create_text(cx, cy - radius*0.37, text=f"{int(round(value))}", fill="#f4f8fb",
                           font=("Segoe UI Semibold", 32))
        canvas.create_text(cx, cy - radius*0.15, text="km/h  ·  ESTIMATE", fill="#90a0ad",
                           font=("Segoe UI Semibold", 9))

    def refresh_tuning_dashboard(*_):
        try:
            rpm = max(1.0, min(1.5, float(var_rpm_factor.get())))
            torque = max(1.0, min(3.0, float(var_torque_factor.get())))
        except (ValueError, tk.TclError):
            return
        target = 90.0 * rpm
        var_gauge_target.set(target)
        needle_speed["target"] = target
        animate_gauge()
        cards = w.get("tune_cards", [])
        if len(cards) == 3:
            cards[0].configure(text=f"{torque:.2f}×")
            cards[1].configure(text=f"{rpm:.2f}×")
            cards[2].configure(text=str(sum(v.get() for v in tune_truck_vars.values())))
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

    def build_tuning_mod():
        base = Path(var_dir.get().strip()) if var_dir.get().strip() else None
        if not base or not base.is_dir():
            warn("tune_no_dir")
            return
        if game_running():
            warn("tune_running")
            return
        selected = [name for name, variable in tune_truck_vars.items() if variable.get()]
        if not selected:
            messagebox.showwarning(tr("title"), tr("tune_trucks"))
            return
        try:
            torque_factor = float(var_torque_factor.get())
            rpm_factor = float(var_rpm_factor.get())
            if not (1.0 <= torque_factor <= 3.0 and 1.0 <= rpm_factor <= 1.5):
                raise ValueError
        except ValueError:
            messagebox.showwarning(tr("title"), tr("tune_bad_factor"))
            return
        mod_dir = mod_folder(base)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        filename = "ETS2SE_Tune_T%s_R%s_%s.scs" % (
            ("%.2f" % torque_factor).replace(".", "p"),
            ("%.2f" % rpm_factor).replace(".", "p"), stamp)
        destination = mod_dir / filename
        try:
            count = create_tuning_mod(destination, selected, torque_factor, rpm_factor)
            refresh_mods()
            var_status.set(tr("tune_success", n=count, path=destination))
            messagebox.showinfo(tr("title"), tr("tune_success", n=count, path=destination))
        except Exception as ex:
            if destination.exists():
                destination.unlink()
            err("fail", e=ex)

    # ---------------------------------------------------------- language
    def switch_lang():
        lang["i"] = 1 - lang["i"]
        build()
        scan(keep=True)
        refresh_mods()
        update_rank()
        status(tr("st_start"))

    def toggle_theme():
        appearance["mode"] = "day" if appearance["mode"] == "night" else "night"
        save_appearance()
        build()

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
            var_dir.set(d)
            scan()

    def detect():
        found = find_game_dirs()
        if not found:
            status(tr("no_folder").split("\n")[0])
            info("no_folder")
            return
        var_dir.set(str(found[0]))
        scan()

    def scan(keep=False):
        base = var_dir.get().strip()
        st["profiles"] = list_profiles(base) if base else []
        cmb, tree = w["cmb"], w["tree"]
        cmb["values"] = [p[0] for p in st["profiles"]]
        tree.delete(*tree.get_children())
        if not st["profiles"]:
            cmb.set("")
            st["path"] = None
            if base:
                status(tr("st_noprof"))
            return
        idx = cur["profile_idx"] if keep and cur["profile_idx"] < len(st["profiles"]) else 0
        cmb.current(idx)
        on_profile(keep_sel=keep)

    def on_profile(_=None, keep_sel=False):
        i = w["cmb"].current()
        if i < 0:
            return
        cur["profile_idx"] = i
        st["saves"] = list_saves(st["profiles"][i][1])
        tree = w["tree"]
        tree.delete(*tree.get_children())
        for n, p, mt in st["saves"]:
            tree.insert("", "end", values=(n, time.strftime("%Y-%m-%d %H:%M", time.localtime(mt))))
        if not keep_sel:
            st["path"] = None
            status(tr("st_saves", n=len(st["saves"])))
        refresh_mods()

    def on_save(_=None):
        sel = w["tree"].selection()
        if not sel:
            return
        path = st["saves"][w["tree"].index(sel[0])][1]
        try:
            v = SaveFile(path).read()
        except Exception as e:
            st["path"] = None
            err("read_fail", e=e)
            return
        st["path"] = path
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
        status(tr("st_loaded", p=path))

    def apply():
        if not st["path"]:
            warn("sel_save")
            return
        if game_running():
            warn("game_open")
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
        if game_running():
            warn("game_open")
            return
        b = latest_backup(st["path"])
        if not b:
            info("no_backup")
            return
        if messagebox.askyesno(tr("title"), tr("ask_restore", b=b)):
            shutil.copy2(b, st["path"])
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
            active = []
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
        if current_profile() is None:
            warn("sel_profile")
            return False
        if game_running():
            warn("game_open")
            return False
        return True

    def toggle_mod(on):
        i = sel_mod()
        if i is None or not guard():
            return
        e = st["mods"][i]
        if e["on"] == on:
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
        ents[i], ents[j] = ents[j], ents[i]
        try:
            save_active(ents)
        except Exception as ex:
            err("fail", e=ex)
        refresh_mods()
        w["mods"].selection_set(w["mods"].get_children()[j])

    def add_mod():
        base = var_dir.get().strip()
        d = mod_folder(base)
        if not d.is_dir():
            warn("m_nodir")
            return
        files = filedialog.askopenfilenames(
            title=tr("m_add"), filetypes=[("ETS2 mods", "*.scs *.zip"), ("All", "*.*")])
        n = 0
        for f in files:
            dest = d / Path(f).name
            if dest.exists() and not messagebox.askyesno(tr("title"), tr("m_overwrite", f=dest.name)):
                continue
            shutil.copy2(f, dest)
            n += 1
        if n:
            status(tr("m_added", n=n))
        refresh_mods()
        validate_files([d / Path(f).name for f in files])

    def enabled_file_sets(exclude=None):
        out = {}
        for e in st["mods"]:
            if e["on"] and e["path"] and mod_id(e["path"]) != exclude:
                kind, names, _ = mod_file_list(e["path"])
                out[mod_id(e["path"])] = {n.lower() for n in names if n.lower() != "manifest.sii"
                                          and not n.lower().startswith("mod_description")}
        return out

    def validate_files(paths):
        base = var_dir.get().strip()
        gv = var_gver.get().strip()
        others = enabled_file_sets()
        lines = []
        for p in paths:
            verdict, res = validate_mod(p, base, gv, others)
            lines.append(("head", tr("rep_head", name=mod_id(p))))
            for level, code, params in res:
                tag = {"ok": "ok", "warn": "warn", "err": "err"}.get(level, "")
                mark = {"ok": "[OK] ", "warn": "[!] ", "err": "[X] ", "info": "[i] "}[level]
                lines.append((tag, mark + tr(code, **params)))
            lines.append((verdict, tr("verdict_" + verdict)))
            lines.append(("", ""))
        lines.append(("", tr("rep_disclaimer")))
        report(lines)

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
    found = find_game_dirs()
    if found:
        var_dir.set(str(found[0]))
        scan()
        load_cfg_quiet = None
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
        sf = SaveFile(a.file)
        sf.apply(vals, a.all_cities, extras)
        Path(a.out).write_bytes(sf.to_bytes())
        chk = SaveFile(a.out)
        chk.verify()
        print(chk.read())
    else:
        print("backup:", edit_save(a.file, vals, a.all_cities, extras))
        print(SaveFile(a.file).read())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        _cli(sys.argv[2:])
    else:
        run_gui()
