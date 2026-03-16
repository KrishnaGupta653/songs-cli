#!/usr/bin/env python3
"""
music_ui_server.py — bridge server between the m CLI (mpv IPC) and music_ui.html

Connects to mpv's Unix domain socket, exposes HTTP endpoints:
  GET  /              → serves music_ui.html
  GET  /api/state     → full player state JSON (title, pos, dur, paused, volume, queue, lyrics…)
  POST /api/cmd       → send a command (body: {"cmd": "pause"} or {"cmd": "seek +10"})

Lyrics are fetched from lrclib.net and cached per track.
"""

import http.server
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request

SOCKET_PATH = os.path.expanduser("~/music_system/socket/mpv.sock")
HTML_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7700

# ── mpv IPC ──────────────────────────────────────────────────────────────────

def mpv_command(cmd_list):
    """Send a JSON command to mpv IPC socket, return parsed response."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(SOCKET_PATH)
        payload = json.dumps({"command": cmd_list}) + "\n"
        sock.sendall(payload.encode())
        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
        sock.close()
        line = buf.split(b"\n")[0]
        return json.loads(line)
    except Exception:
        return {"error": "mpv unreachable"}


def mpv_get(prop):
    """Get a single mpv property."""
    resp = mpv_command(["get_property", prop])
    return resp.get("data")


def mpv_set(prop, value):
    resp = mpv_command(["set_property", prop, value])
    return resp


def mpv_alive():
    return os.path.exists(SOCKET_PATH)


# ── Lyrics cache ─────────────────────────────────────────────────────────────

_lyrics_cache = {}  # {title: {"synced": bool, "lines": [{"t":float,"text":str}]}}
_lyrics_lock = threading.Lock()


def _parse_lrc(lrc_text):
    """Parse LRC format into list of {t: seconds, text: str}."""
    import re
    lines = []
    for raw_line in lrc_text.split("\n"):
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)", raw_line)
        if m:
            mins, secs, text = m.groups()
            t = int(mins) * 60 + float(secs)
            lines.append({"t": round(t, 2), "text": text.strip()})
    lines.sort(key=lambda x: x["t"])
    return lines


def fetch_lyrics(title):
    """Fetch lyrics from lrclib.net, cache result. Returns dict or None."""
    with _lyrics_lock:
        if title in _lyrics_cache:
            return _lyrics_cache[title]

    if not title:
        return None

    artist = ""
    track = title
    if " - " in title:
        parts = title.split(" - ", 1)
        artist = parts[0].strip()
        track = parts[1].strip()
    # Strip common junk suffixes
    for suffix in ["(Official Video)", "(Official Audio)", "(Lyrics)", "(Official Music Video)",
                   "[Official Video]", "[Official Audio]", "[Lyrics]", "(Audio)", "(Video)",
                   "(Visualizer)", "(Official Visualizer)"]:
        track = track.replace(suffix, "").strip()
        artist = artist.replace(suffix, "").strip()

    enc_track = urllib.parse.quote(track)
    enc_artist = urllib.parse.quote(artist)
    api_url = f"https://lrclib.net/api/search?q={enc_track}"
    if enc_artist:
        api_url += f"&artist_name={enc_artist}"

    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "m-music-cli/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None

    if not data or not isinstance(data, list) or len(data) == 0:
        with _lyrics_lock:
            _lyrics_cache[title] = None
        return None

    synced_lrc = data[0].get("syncedLyrics", "")
    plain_lrc = data[0].get("plainLyrics", "")

    result = None
    if synced_lrc:
        lines = _parse_lrc(synced_lrc)
        if lines:
            result = {"synced": True, "lines": lines}
    if not result and plain_lrc:
        result = {
            "synced": False,
            "lines": [{"t": 0, "text": line} for line in plain_lrc.split("\n") if line.strip()]
        }

    with _lyrics_lock:
        _lyrics_cache[title] = result
    return result


# ── Background lyrics prefetcher ─────────────────────────────────────────────

_last_lyrics_title = ""

def _lyrics_bg_fetch():
    """Runs in background thread — prefetches lyrics when track changes."""
    global _last_lyrics_title
    while True:
        try:
            if mpv_alive():
                title = mpv_get("media-title") or ""
                if title and title != _last_lyrics_title:
                    _last_lyrics_title = title
                    fetch_lyrics(title)
        except Exception:
            pass
        time.sleep(3)

threading.Thread(target=_lyrics_bg_fetch, daemon=True).start()


# ── Build full state ─────────────────────────────────────────────────────────

def get_full_state():
    """Return a dict with the full player state for the UI."""
    if not mpv_alive():
        return {
            "alive": False, "playing": False, "paused": True,
            "title": "nothing playing", "pos": 0, "dur": 0,
            "volume": 80, "speed": 1.0, "queue": [], "currentIdx": -1,
            "repeat": False, "loopOne": False, "autoDj": False,
            "lyrics": None,
        }

    title = mpv_get("media-title") or ""
    pos = mpv_get("time-pos") or 0
    dur = mpv_get("duration") or 0
    paused = mpv_get("pause")
    volume = mpv_get("volume") or 80
    speed = mpv_get("speed") or 1.0
    loop_playlist = mpv_get("loop-playlist") or "no"
    loop_file = mpv_get("loop-file") or "no"
    playlist_pos = mpv_get("playlist-playing-pos")

    # Queue
    pl_resp = mpv_command(["get_property", "playlist"])
    pl_data = pl_resp.get("data", []) if isinstance(pl_resp, dict) else []

    queue = []
    for i, item in enumerate(pl_data):
        t = item.get("title") or item.get("filename", "")
        queue.append({"title": t, "current": item.get("current", False)})

    try:
        pos = float(pos)
    except (TypeError, ValueError):
        pos = 0
    try:
        dur = float(dur)
    except (TypeError, ValueError):
        dur = 0
    try:
        volume = float(volume)
    except (TypeError, ValueError):
        volume = 80
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        speed = 1.0

    current_idx = -1
    if playlist_pos is not None:
        try:
            current_idx = int(playlist_pos)
        except (TypeError, ValueError):
            current_idx = -1

    is_paused = paused is True or paused == "true" or paused == "yes"
    is_playing = bool(title) and title != "nothing playing"

    autodj = os.path.exists(os.path.expanduser("~/music_system/data/autodj_enabled"))

    lyrics_data = None
    if title:
        lyrics_data = fetch_lyrics(title)

    return {
        "alive": True,
        "playing": is_playing,
        "paused": is_paused,
        "title": title or "nothing playing",
        "pos": round(pos, 1),
        "dur": round(dur, 1),
        "volume": round(volume),
        "speed": round(speed, 2),
        "queue": queue,
        "currentIdx": current_idx,
        "repeat": loop_playlist not in ("no", "", False),
        "loopOne": loop_file not in ("no", "", False),
        "autoDj": autodj,
        "lyrics": lyrics_data,
    }


# ── Handle commands from the UI ──────────────────────────────────────────────

def handle_cmd(cmd_str):
    """Execute an m-style command string against mpv."""
    parts = cmd_str.strip().split()
    if not parts:
        return {"ok": False, "msg": "empty command"}

    action = parts[0]

    if action in ("pause", "pp"):
        mpv_command(["cycle", "pause"])
        return {"ok": True, "msg": "toggled pause"}

    elif action in ("next", "mn"):
        mpv_command(["playlist-next"])
        return {"ok": True, "msg": "next track"}

    elif action in ("prev", "mb"):
        mpv_command(["playlist-prev"])
        return {"ok": True, "msg": "previous track"}

    elif action == "stop":
        mpv_command(["stop"])
        return {"ok": True, "msg": "stopped"}

    elif action == "seek":
        if len(parts) > 1:
            arg = parts[1]
            if arg.startswith("+") or arg.startswith("-"):
                mpv_command(["seek", arg, "relative"])
            else:
                mpv_command(["seek", arg, "absolute"])
            return {"ok": True, "msg": f"seek {arg}"}
        return {"ok": False, "msg": "seek needs argument"}

    elif action in ("vol", "volume"):
        if len(parts) > 1:
            arg = parts[1]
            if arg.startswith("+") or arg.startswith("-"):
                cur = mpv_get("volume") or 80
                try:
                    new_vol = max(0, min(150, float(cur) + float(arg)))
                except (TypeError, ValueError):
                    new_vol = 80
                mpv_set("volume", new_vol)
            else:
                try:
                    mpv_set("volume", max(0, min(150, float(arg))))
                except ValueError:
                    pass
            return {"ok": True, "msg": f"volume {arg}"}
        return {"ok": False, "msg": "vol needs argument"}

    elif action == "speed":
        if len(parts) > 1:
            try:
                s = max(0.25, min(4.0, float(parts[1])))
                mpv_set("speed", s)
                return {"ok": True, "msg": f"speed {s}"}
            except ValueError:
                pass
        return {"ok": False, "msg": "speed needs number"}

    elif action in ("repeat", "rp"):
        cur = mpv_get("loop-playlist") or "no"
        new_val = "no" if cur not in ("no", "", False) else "inf"
        mpv_set("loop-playlist", new_val)
        return {"ok": True, "msg": f"repeat {'on' if new_val == 'inf' else 'off'}"}

    elif action in ("repeat-one", "ro"):
        cur = mpv_get("loop-file") or "no"
        new_val = "no" if cur not in ("no", "", False) else "inf"
        mpv_set("loop-file", new_val)
        return {"ok": True, "msg": f"repeat-one {'on' if new_val == 'inf' else 'off'}"}

    elif action == "shuffle":
        mpv_command(["playlist-shuffle"])
        return {"ok": True, "msg": "shuffled"}

    elif action == "playlist-play-index":
        if len(parts) > 1:
            try:
                idx = int(parts[1])
                mpv_set("playlist-pos", idx)
                return {"ok": True, "msg": f"playing track {idx + 1}"}
            except ValueError:
                pass
        return {"ok": False, "msg": "need index"}

    elif action == "clear":
        mpv_command(["playlist-clear"])
        return {"ok": True, "msg": "queue cleared"}

    elif action in ("norm",):
        mpv_command(["af", "toggle", "dynaudnorm"])
        return {"ok": True, "msg": "toggled normalize"}

    elif action == "like":
        # Delegate to m CLI for file-based operations
        os.system("m like >/dev/null 2>&1 &")
        return {"ok": True, "msg": "liked"}

    elif action == "autodj":
        os.system("m autodj >/dev/null 2>&1 &")
        return {"ok": True, "msg": "toggled autodj"}

    elif action == "eq":
        preset = parts[1] if len(parts) > 1 else "flat"
        presets = {
            "flat":   "af set ''",
            "bass":   'af set "superequalizer=1b=6:2b=5:3b=4:4b=2"',
            "treble": 'af set "superequalizer=14b=5:15b=6:16b=4:17b=3"',
            "vocal":  'af set "superequalizer=6b=3:7b=5:8b=5:9b=3"',
            "loud":   'af set "loudnorm=I=-16:TP=-1.5:LRA=11"',
        }
        if preset in presets:
            mpv_command(["af", "set", ""])  # reset first
            if preset != "flat":
                os.system(f"m eq {preset} >/dev/null 2>&1 &")
        return {"ok": True, "msg": f"eq {preset}"}

    # For anything else, shell out to m CLI
    os.system(f"m {cmd_str} >/dev/null 2>&1 &")
    return {"ok": True, "msg": f"m {cmd_str}"}


# ── HTTP Server ──────────────────────────────────────────────────────────────

class UXIHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # silent

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_html()
        elif self.path == "/api/state":
            self._json_response(get_full_state())
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/cmd":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode() if length else "{}"
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {}
            cmd_str = data.get("cmd", "")
            result = handle_cmd(cmd_str)
            self._json_response(result)
        elif self.path == "/api/play":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode() if length else "{}"
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {}
            query = data.get("query", "")
            if query:
                os.system(f'm "{query}" >/dev/null 2>&1 &')
                self._json_response({"ok": True, "msg": f"playing: {query}"})
            else:
                self._json_response({"ok": False, "msg": "no query"})
        else:
            self.send_error(404)

    def _serve_html(self):
        html_path = os.path.join(HTML_DIR, "music_ui.html")
        try:
            with open(html_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(content))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404, "music_ui.html not found")

    def _json_response(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def main():
    server = http.server.HTTPServer(("127.0.0.1", PORT), UXIHandler)
    print(f"m uxi server running → http://127.0.0.1:{PORT}")
    print(f"  mpv socket: {SOCKET_PATH}")
    print(f"  html: {os.path.join(HTML_DIR, 'music_ui.html')}")
    print(f"  press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down…")
        server.shutdown()


if __name__ == "__main__":
    main()
