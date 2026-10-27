"""RPC server: real EditorBridge on a fake ETS2 folder; used by the jsdom end-to-end test."""
import json, os, sys, tempfile, struct, io, shutil
from pathlib import Path
from unittest import mock
ROOT = Path(sys.argv[1]); sys.path.insert(0, str(ROOT))
import ets2_save_editor as core
from ets2_web_bridge import EditorBridge

tmp = Path(tempfile.mkdtemp(prefix="e2e_"))
home = tmp / "home"; game = home / "Documents" / "Euro Truck Simulator 2"
prof = game / "profiles" / "447269766572"; (prof / "save" / "quicksave").mkdir(parents=True)
(game / "mod").mkdir(); (game / "music").mkdir()
shutil.copy(ROOT / "sample.sii", prof / "save" / "quicksave" / "game.sii")
(prof / "profile.sii").write_text('SiiNunit\n{\nuser_profile : _nameless.1 {\n profile_name: "Driver"\n active_mods: 0\n}\n}\n', encoding="utf-8")
(game / "config.cfg").write_text('uset g_developer "1"\nuset g_console "0"\nuset g_save_format "2"\nuset g_music_volume "0"\n', encoding="utf-8")
(game / "game.log.txt").write_text("00:00:00.000 : [ufs] Loaded pack set version 1.60.1.7s\n00:00:02.000 : [audio] error: could not open stream radio1\n", encoding="utf-8")
(game / "live_streams.sii").write_text('SiiNunit\n{\nlive_stream_def : .live_streams {\n stream_data[]: "http://example.invalid/a.mp3|Good FM|Pop|EN|128|0"\n stream_data[]: "mms://example.invalid/b|Old MMS|Pop|EN|64|0"\n stream_data[]: "http://example.invalid/list.pls|Playlist Radio|Rock|EN|64|0"\n}\n}\n', encoding="utf-8")
def mp3(path, id3_extra=0, frames=40):
    body = b"\xff\xfb\x90\x64" + b"\x00" * 413
    tag = b""
    if id3_extra:
        payload = b"\x00" * id3_extra
        size = len(payload); sync = bytes([(size >> 21) & 127, (size >> 14) & 127, (size >> 7) & 127, size & 127])
        tag = b"ID3\x03\x00\x00" + sync + payload
    else:
        t = b"\x00" + b"Test Song"; a = b"\x00" + b"Test Artist"
        fr = b"TIT2" + len(t).to_bytes(4, "big") + b"\x00\x00" + t + b"TPE1" + len(a).to_bytes(4, "big") + b"\x00\x00" + a
        size = len(fr); sync = bytes([(size >> 21) & 127, (size >> 14) & 127, (size >> 7) & 127, size & 127])
        tag = b"ID3\x03\x00\x00" + sync + fr
    Path(path).write_bytes(tag + body * frames)
mp3(game / "music" / "good.mp3"); mp3(game / "music" / "bigtag.mp3", id3_extra=700 * 1024)
(game / "music" / "empty.mp3").write_bytes(b""); (game / "music" / "song.wma").write_bytes(b"x" * 10)
(game / "music" / "fake.mp3").write_bytes(b"this is not audio" * 100)
(game / "music" / "list.m3u").write_text("a.mp3\n")
env = mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(tmp / "ad"), "LOCALAPPDATA": str(tmp / "la")})
env.start()
mock.patch.object(core, "documents_dir", lambda: home / "Documents").start()
mock.patch.object(core, "find_game_dirs", lambda: [game]).start()
mock.patch.object(core, "game_running", lambda: False).start()
bridge = EditorBridge(core)
bridge.select_game(0); bridge.select_profile(0); bridge.select_save(0)
print(json.dumps({"ready": True, "game": str(game), "tmp": str(tmp)}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    name = req["m"]
    try:
        if name.startswith("_") or not hasattr(bridge, name):
            raise ValueError("no such method: " + name)
        result = getattr(bridge, name)(*req.get("a", []))
        out = {"id": req["id"], "ok": True, "r": result}
    except Exception as exc:
        out = {"id": req["id"], "ok": False, "e": str(exc)}
    print(json.dumps(out, default=str), flush=True)
