"""ETS2 music folder / live radio helpers for TR-EURO-EDITOR.

Everything here works on files under the user's ETS2 Documents folder:
  <game>/music/            the in-game "player's music" folder
  <game>/live_streams.sii  the in-game radio station list
It never touches the save files and never changes audio *content* except the optional,
backed-up "strip heavy ID3 tag" fix.

Facts used (and where they come from):
  * folder / live_streams.sii format: SCS "read me" for ETS2 (stream_data[]: "url|name|...").
  * the in-game player recognises MP3 and OGG (community reports); WMA/M4A/AAC are not
    recognised; FLAC is reported by some players to work -> only a warning.
  * the game reads the folder when it starts (community report) -> restart hint.
Every diagnostic carries a `basis` so the UI can show how sure we are:
  "fact"  - verifiable on disk (missing folder, zero-byte file, bad header...)
  "docs"  - stated in SCS documentation
  "report"- community report, not verified by this tool
"""
from __future__ import annotations

import http.server
import json
import mimetypes
import os
import re
import secrets
import shutil
import socketserver
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

PLAYABLE_EXT = (".mp3", ".ogg")                 # recognised by the in-game player
MAYBE_EXT = (".flac", ".wav")                   # reports differ -> warning only
KNOWN_BAD_EXT = (".wma", ".m4a", ".aac", ".opus", ".ape", ".mid", ".midi", ".wv")
PLAYLIST_EXT = (".m3u", ".m3u8", ".pls")
BROWSER_EXT = (".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac", ".opus")
MAX_TRACKS = 3000
MAX_DEPTH = 3
BIG_ID3_BYTES = 512 * 1024
STREAM_BAD_SUFFIX = (".pls", ".m3u", ".m3u8", ".asx", ".afx")


# ----------------------------------------------------------------------------- scanning
def music_dir(game):
    return Path(game) / "music"


def _walk(root, depth=MAX_DEPTH):
    root = Path(root)
    stack = [(root, 0)]
    seen = 0
    while stack:
        folder, level = stack.pop()
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name.casefold())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if level < depth:
                        stack.append((entry, level + 1))
                elif entry.is_file():
                    seen += 1
                    yield entry
                    if seen >= MAX_TRACKS:
                        return
            except OSError:
                continue


def _synchsafe(raw):
    return ((raw[0] & 0x7F) << 21) | ((raw[1] & 0x7F) << 14) | ((raw[2] & 0x7F) << 7) | (raw[3] & 0x7F)


def _decode_text(data):
    if not data:
        return ""
    enc, body = data[0], data[1:]
    try:
        if enc == 0:
            text = body.decode("latin-1")
        elif enc == 1:
            text = body.decode("utf-16")
        elif enc == 2:
            text = body.decode("utf-16-be")
        else:
            text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("latin-1", "replace")
    return text.split("\x00")[0].strip()


def read_id3(path):
    """Return (tags, id3v2_total_size). Never raises; tags are best effort."""
    tags = {"title": "", "artist": "", "album": ""}
    try:
        with open(path, "rb") as handle:
            head = handle.read(10)
            if len(head) == 10 and head[:3] == b"ID3":
                size = _synchsafe(head[6:10])
                total = size + 10
                body = handle.read(min(size, 256 * 1024))
                major = head[3]
                pos = 0
                names = {b"TIT2": "title", b"TPE1": "artist", b"TALB": "album"} if major >= 3 else \
                        {b"TT2": "title", b"TP1": "artist", b"TAL": "album"}
                step = 10 if major >= 3 else 6
                while pos + step <= len(body):
                    fid = body[pos:pos + (4 if major >= 3 else 3)]
                    if not fid.strip(b"\x00"):
                        break
                    if major >= 3:
                        raw = body[pos + 4:pos + 8]
                        fsize = _synchsafe(raw) if major == 4 else int.from_bytes(raw, "big")
                    else:
                        fsize = int.from_bytes(body[pos + 3:pos + 6], "big")
                    if fsize <= 0 or pos + step + fsize > len(body):
                        break
                    key = names.get(fid)
                    if key and not tags[key]:
                        tags[key] = _decode_text(body[pos + step:pos + step + fsize])[:120]
                    pos += step + fsize
                return tags, total
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            if end >= 128:
                handle.seek(end - 128)
                tail = handle.read(128)
                if tail[:3] == b"TAG":
                    tags["title"] = tail[3:33].split(b"\x00")[0].decode("latin-1").strip()
                    tags["artist"] = tail[33:63].split(b"\x00")[0].decode("latin-1").strip()
                    tags["album"] = tail[63:93].split(b"\x00")[0].decode("latin-1").strip()
    except OSError:
        pass
    return tags, 0


def _has_mp3_frame(window):
    for i in range(len(window) - 1):
        if window[i] == 0xFF and (window[i + 1] & 0xE0) == 0xE0 and (window[i + 1] & 0x18) != 0x08:
            return True
    return False


def sniff_audio(path):
    """Inspect the first bytes. Returns {"kind", "ok", "size", "id3_size", "reason"}."""
    result = {"kind": "unknown", "ok": False, "size": 0, "id3_size": 0, "reason": ""}
    try:
        size = Path(path).stat().st_size
        result["size"] = size
        if size == 0:
            result.update(kind="empty", reason="empty")
            return result
        with open(path, "rb") as handle:
            head = handle.read(10)
            ext = Path(path).suffix.lower()
            if head[:3] == b"ID3" and len(head) == 10:
                result["id3_size"] = _synchsafe(head[6:10]) + 10
                handle.seek(result["id3_size"])
                window = handle.read(65536)
                result["kind"] = "mp3"
            elif head[:4] == b"OggS":
                result.update(kind="ogg", ok=True)
                return result
            elif head[:4] == b"fLaC":
                result.update(kind="flac", ok=True)
                return result
            elif head[:4] == b"RIFF":
                result.update(kind="wav", ok=True)
                return result
            else:
                handle.seek(0)
                window = handle.read(65536)
                result["kind"] = "mp3" if ext == ".mp3" else "unknown"
            if result["kind"] == "mp3":
                result["ok"] = _has_mp3_frame(window)
                if not result["ok"]:
                    result["reason"] = "no_frame"
    except OSError as exc:
        result.update(kind="unreadable", reason=str(exc)[:80])
    return result


def scan_music(game):
    """List audio-like files below <game>/music (depth-limited, no symlink following)."""
    folder = music_dir(game)
    info = {"folder": str(folder), "exists": folder.is_dir(), "tracks": [], "other": [], "shortcuts": 0,
            "truncated": False}
    if not info["exists"]:
        return info
    for entry in _walk(folder):
        ext = entry.suffix.lower()
        rel = entry.relative_to(folder).as_posix()
        if ext == ".lnk":
            info["shortcuts"] += 1
            continue
        if ext in PLAYABLE_EXT + MAYBE_EXT + KNOWN_BAD_EXT + PLAYLIST_EXT:
            sniff = sniff_audio(entry)
            tags, id3 = read_id3(entry) if ext == ".mp3" else ({"title": "", "artist": "", "album": ""}, 0)
            info["tracks"].append({"rel": rel, "name": entry.stem, "ext": ext, "size": sniff["size"],
                                   "kind": sniff["kind"], "ok": sniff["ok"], "reason": sniff["reason"],
                                   "id3_size": max(id3, sniff["id3_size"]), "title": tags["title"],
                                   "artist": tags["artist"], "album": tags["album"]})
        else:
            info["other"].append(rel)
        if len(info["tracks"]) >= MAX_TRACKS:
            info["truncated"] = True
            break
    return info


# ----------------------------------------------------------------------------- radio list
def parse_live_streams(game):
    """Parse <game>/live_streams.sii. Returns {"exists", "stations": [...], "problems": [...]}"""
    path = Path(game) / "live_streams.sii"
    out = {"path": str(path), "exists": path.is_file(), "stations": [], "problems": []}
    if not out["exists"]:
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        out["problems"].append({"code": "m_streams_unreadable", "params": {"err": str(exc)[:80]}})
        return out
    for number, line in enumerate(text.splitlines(), 1):
        match = re.match(r'^\s*stream_data\[\d*\]\s*:\s*"(.*)"\s*$', line)
        if not match:
            continue
        parts = match.group(1).split("|")
        url = parts[0].strip()
        name = (parts[1].strip() if len(parts) > 1 else "") or url
        station = {"line": number, "url": url, "name": name[:80], "genre": parts[2].strip()[:40] if len(parts) > 2 else "",
                   "bitrate": parts[4].strip() if len(parts) > 4 else ""}
        scheme = urlsplit(url).scheme.lower()
        if scheme not in ("http", "https"):
            station["issue"] = "scheme"
            out["problems"].append({"code": "m_stream_scheme", "params": {"name": station["name"], "url": url[:80]}})
        elif urlsplit(url).path.lower().endswith(STREAM_BAD_SUFFIX):
            station["issue"] = "playlist"
            out["problems"].append({"code": "m_stream_playlist", "params": {"name": station["name"]}})
        out["stations"].append(station)
        if len(out["stations"]) >= 1000:
            break
    return out


# ----------------------------------------------------------------------------- diagnosis
def diagnose(game, log_text="", config=None, game_is_running=False):
    """Return {"findings": [...], "summary": {...}}. Each finding: level, code, params, basis."""
    findings = []

    def add(level, code, basis, **params):
        findings.append({"level": level, "code": code, "basis": basis, "params": params})

    info = scan_music(game)
    streams = parse_live_streams(game)
    if not info["exists"]:
        add("err", "m_no_folder", "fact", path=info["folder"])
    tracks = info["tracks"]
    playable = [t for t in tracks if t["ext"] in PLAYABLE_EXT and t["ok"] and t["size"] > 0]
    if info["exists"] and not tracks and not info["shortcuts"]:
        add("warn", "m_empty_folder", "fact", path=info["folder"])
    if info["shortcuts"]:
        add("info", "m_shortcuts", "docs", n=info["shortcuts"])
    for t in tracks:
        if t["ext"] in PLAYLIST_EXT:
            add("warn", "m_playlist_file", "report", file=t["rel"])
        elif t["ext"] in KNOWN_BAD_EXT:
            add("err", "m_bad_format", "report", file=t["rel"], ext=t["ext"])
        elif t["ext"] in MAYBE_EXT:
            add("warn", "m_maybe_format", "report", file=t["rel"], ext=t["ext"])
        elif t["size"] == 0:
            add("err", "m_zero_byte", "fact", file=t["rel"])
        elif t["kind"] == "unreadable":
            add("err", "m_unreadable", "fact", file=t["rel"], err=t["reason"])
        elif not t["ok"]:
            add("err", "m_no_frames", "fact", file=t["rel"])
        elif t["id3_size"] > BIG_ID3_BYTES:
            add("warn", "m_big_tag", "report", file=t["rel"], kb=t["id3_size"] // 1024)
        if t["ext"] == ".mp3" and t["ok"] and t["kind"] == "mp3" and t["rel"].lower().endswith(".mp3.mp3"):
            add("warn", "m_double_ext", "fact", file=t["rel"])
    for problem in streams["problems"]:
        add("warn", problem["code"], "docs" if problem["code"] == "m_stream_scheme" else "report",
            **problem["params"])
    if streams["exists"] and not streams["stations"]:
        add("info", "m_no_stations", "fact")
    # game.log.txt lines about audio
    wanted = re.compile(r"(music|radio|stream|audio|sound|mp3|ogg)", re.I)
    bad = re.compile(r"(error|fail|unable|could not|cannot|invalid|missing|unsupported)", re.I)
    hits = [ln.strip()[:200] for ln in (log_text or "").splitlines() if wanted.search(ln) and bad.search(ln)]
    if hits:
        add("warn", "m_log_lines", "fact", lines="\n".join(hits[:10]), n=len(hits))
    # config keys containing music/radio with value 0 (we do not assume key names)
    for key, value in sorted((config or {}).items()):
        if re.search(r"(music|radio)", key, re.I):
            add("warn" if str(value).strip() in ("0", "0.0", "0.000000") else "info", "m_cfg_key", "fact",
                key=key, value=str(value))
    add("info", "m_options_audio", "docs")
    add("info", "m_restart_hint", "report")
    if game_is_running:
        add("info", "m_game_running", "fact")
    errors = sum(1 for f in findings if f["level"] == "err")
    warns = sum(1 for f in findings if f["level"] == "warn")
    summary = {"tracks": len(tracks), "playable": len(playable), "errors": errors, "warnings": warns,
               "stations": len(streams["stations"]), "folder_exists": info["exists"]}
    return {"findings": findings, "summary": summary}


# ----------------------------------------------------------------------------- fixes (reversible)
def fix_root(storage_root):
    return Path(storage_root) / "music_fixes"


def _strip_id3v2(src, dst):
    with open(src, "rb") as handle:
        head = handle.read(10)
        if head[:3] != b"ID3" or len(head) < 10:
            raise ValueError("no ID3v2 tag")
        total = _synchsafe(head[6:10]) + 10
        handle.seek(total)
        rest = handle.read()
    if not _has_mp3_frame(rest[:65536]):
        raise ValueError("no audio frame after tag")
    with open(dst, "wb") as out:
        out.write(rest)


def run_fix(game, storage_root, action):
    """action: 'quarantine_bad' | 'strip_big_tags'. Returns {"stamp", "changed": [...]}.

    Originals are always kept: quarantined files are *moved* (not deleted) to
    <storage>/music_fixes/<stamp>/files/ and big-tag files are copied there before rewriting.
    """
    if action not in ("quarantine_bad", "strip_big_tags"):
        raise ValueError("Unknown music fix")
    info = scan_music(game)
    folder = music_dir(game).resolve()
    moment = time.time()
    while True:                                      # two fixes in the same second must not collide
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(moment))
        base = fix_root(storage_root) / stamp
        if not base.exists():
            break
        moment += 1
    changed = []
    for t in info["tracks"]:
        src = (folder / t["rel"]).resolve()
        if not src.is_relative_to(folder) or not src.is_file():
            continue
        if action == "quarantine_bad":
            bad = (t["ext"] in KNOWN_BAD_EXT or t["ext"] in PLAYLIST_EXT or t["size"] == 0
                   or (t["ext"] in PLAYABLE_EXT and not t["ok"]))
            if not bad:
                continue
            target = base / "files" / t["rel"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
            changed.append({"rel": t["rel"], "action": "moved"})
        else:
            if t["ext"] != ".mp3" or t["id3_size"] <= BIG_ID3_BYTES or not t["ok"]:
                continue
            backup = base / "files" / t["rel"]
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, backup)
            tmp = src.with_name(src.name + ".tmpfix")
            try:
                _strip_id3v2(src, tmp)                 # raises unless audio frames follow the tag
                os.replace(tmp, src)
                changed.append({"rel": t["rel"], "action": "stripped"})
            except (OSError, ValueError):
                if tmp.exists():
                    tmp.unlink()
                backup.unlink(missing_ok=True)
    if changed:
        base.mkdir(parents=True, exist_ok=True)
        (base / "manifest.json").write_text(json.dumps({"game": str(game), "action": action,
                                                         "changed": changed}, indent=1), encoding="utf-8")
    return {"stamp": stamp if changed else None, "changed": changed}


def list_fixes(storage_root):
    root = fix_root(storage_root)
    rows = []
    if root.is_dir():
        for folder in sorted(root.iterdir(), reverse=True):
            manifest = folder / "manifest.json"
            if manifest.is_file():
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                rows.append({"stamp": folder.name, "action": data.get("action"), "count": len(data.get("changed", []))})
    return rows[:30]


def undo_fix(game, storage_root, stamp):
    if not re.fullmatch(r"\d{8}-\d{6}", str(stamp)):
        raise ValueError("Invalid fix id")
    base = fix_root(storage_root) / stamp
    manifest = base / "manifest.json"
    if not manifest.is_file():
        raise ValueError("Fix not found")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    folder = music_dir(game).resolve()
    restored = 0
    for item in data.get("changed", []):
        saved = (base / "files" / item["rel"]).resolve()
        target = (folder / item["rel"]).resolve()
        if not saved.is_file() or not saved.is_relative_to(base.resolve()) or not target.is_relative_to(folder):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(saved, target)
        restored += 1
    return {"restored": restored}


# ----------------------------------------------------------------------------- loopback audio server
class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "TREditorAudio/1"

    def log_message(self, *_args):               # keep the console quiet
        return

    def _deny(self, code=404):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        srv = self.server
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{srv.server_port}", f"localhost:{srv.server_port}"):
            return self._deny(403)                # DNS-rebinding guard
        match = re.fullmatch(r"/t/([A-Za-z0-9_-]+)/(\d{1,5})", self.path.split("?")[0])
        if not match or not secrets.compare_digest(match.group(1), srv.token):
            return self._deny(404)
        index = int(match.group(2))
        path = srv.library.get(index)
        if path is None or not Path(path).is_file():
            return self._deny(404)
        size = Path(path).stat().st_size
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = handle.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (OSError, ConnectionError):
            pass


class AudioServer:
    """127.0.0.1-only, token-protected, read-only server for files found by scan_music()."""

    def __init__(self):
        self._srv = None
        self._thread = None
        self.token = secrets.token_urlsafe(18)

    def start(self):
        if self._srv is not None:
            return
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._srv.daemon_threads = True
        self._srv.token = self.token
        self._srv.library = {}
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self):
        return self._srv.server_port if self._srv else 0

    def set_library(self, mapping):
        if self._srv is not None:
            self._srv.library = dict(mapping)

    def base_url(self):
        return f"http://127.0.0.1:{self.port}/t/{self.token}/" if self._srv else ""

    def stop(self):
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
