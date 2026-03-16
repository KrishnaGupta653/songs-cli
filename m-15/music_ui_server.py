#!/usr/bin/env python3
"""
music_ui_server.py — bridge server between the m CLI (mpv IPC) and music_ui.html

Connects to mpv's Unix domain socket, exposes HTTP endpoints:
  GET  /              → serves music_ui.html
  GET  /api/state     → full player state JSON (title, pos, dur, paused, volume, queue, lyrics…)
  GET  /api/events    → Server-Sent Events stream for state changes
  POST /api/cmd       → send a command (body: {"cmd": "pause"} or {"cmd": "seek +10"})
  POST /api/play      → play by query (body: {"query": "..."})

Lyrics are fetched from lrclib.net and cached per track. /api/state never blocks on lyrics;
returns cached or "loading" state. Background prefetcher keeps cache warm.
"""

import http.server
import json
import os
import re
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

SOCKET_PATH = os.path.expanduser("~/music_system/socket/mpv.sock")
HTML_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7700

# ── mpv IPC (with request_id for multi-line response handling) ─────────────────

_mpv_request_id = 0
_mpv_request_id_lock = threading.Lock()


def _next_request_id():
    global _mpv_request_id
    with _mpv_request_id_lock:
        _mpv_request_id += 1
        return _mpv_request_id


def mpv_command(cmd_list):
    """
    Send a JSON command to mpv IPC socket, return parsed response.
    mpv can emit multiple JSON lines (event notifications) before the response.
    Read lines in a loop until finding one with request_id or error matching our request.
    """
    req_id = _next_request_id()
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(SOCKET_PATH)
        payload = json.dumps({"command": cmd_list, "request_id": req_id}) + "\n"
        sock.sendall(payload.encode())

        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Response has request_id; events typically don't
                if obj.get("request_id") == req_id:
                    sock.close()
                    return obj
                # Error response may have request_id
                if "error" in obj and obj.get("request_id") == req_id:
                    sock.close()
                    return obj
        sock.close()
        return {"error": "mpv no response"}
    except (socket.error, OSError, ConnectionRefusedError) as e:
        return {"error": "mpv unreachable", "detail": str(e)}
    except Exception as e:
        return {"error": "mpv error", "detail": str(e)}


def mpv_get(prop):
    """Get a single mpv property."""
    resp = mpv_command(["get_property", prop])
    if "error" in resp and resp.get("error") not in ("success", None):
        return None
    return resp.get("data")


def mpv_set(prop, value):
    resp = mpv_command(["set_property", prop, value])
    return resp


def mpv_alive():
    return os.path.exists(SOCKET_PATH)


# ── Lyrics cache ─────────────────────────────────────────────────────────────

_lyrics_cache = {}  # {title: {"synced": bool, "lines": [...]} or sentinel}
_lyrics_lock = threading.Lock()

LYRICS_LOADING = "loading"      # fetch in progress
LYRICS_NOT_FOUND = "not_found"  # fetch completed, nothing found


def _parse_lrc(lrc_text):
    """Parse LRC format into list of {t: seconds, text: str}."""
    lines = []
    for raw_line in lrc_text.split("\n"):
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)", raw_line)
        if m:
            mins, secs, text = m.groups()
            t = int(mins) * 60 + float(secs)
            lines.append({"t": round(t, 2), "text": text.strip()})
    lines.sort(key=lambda x: x["t"])
    return lines


def _clean_lyrics_title(title):
    """Aggressively clean a YouTube title for lyrics search."""
    t = title
    # Strip common YouTube suffixes (parenthetical and bracketed)
    for pat in [
        r'\(Official[^)]*\)', r'\(Lyrics[^)]*\)', r'\(Audio[^)]*\)',
        r'\(Video[^)]*\)', r'\(Visuali[sz]er[^)]*\)', r'\(Full Song[^)]*\)',
        r'\(HD[^)]*\)', r'\(HQ[^)]*\)',
        r'\[Official[^]]*\]', r'\[Lyrics[^]]*\]', r'\[Audio[^]]*\]',
        r'\[Video[^]]*\]', r'\[HD[^]]*\]', r'\[HQ[^]]*\]',
    ]:
        t = re.sub(pat, '', t, flags=re.IGNORECASE)
    # Replace underscores with spaces (common in Bollywood YouTube titles)
    t = t.replace('_', ' ')
    # Remove year patterns like (1971) or standalone 4-digit years
    t = re.sub(r'\(\d{4}\)', '', t)
    t = re.sub(r'\b(19|20)\d{2}\b', '', t)
    # Remove everything after pipe
    t = t.split('|')[0]
    # Remove hashtags
    t = re.sub(r'#\S+', '', t)
    # Remove "a trib..." suffixes, "full movie/song"
    t = re.sub(r'\ba trib\w*\b.*', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\bfull movie\b.*', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\bfull song\b.*', '', t, flags=re.IGNORECASE)
    # Collapse whitespace
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _lrclib_request(url):
    """Make a request to lrclib.net. Returns parsed JSON or None.
    Falls back to curl if urllib hits SSL cert issues (common on macOS)."""
    # Try urllib first
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "m-cli/6.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read())
    except ssl.SSLError:
        pass
    except urllib.error.URLError as e:
        if "SSL" not in str(e) and "CERTIFICATE" not in str(e).upper():
            return None
    except Exception:
        return None
    # Fallback: use curl (uses system cert store, works reliably on macOS)
    try:
        result = subprocess.run(
            ["curl", "-sf", "--max-time", "8", "-H", "User-Agent: m-cli/6.0", url],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception:
        pass
    return None


def _try_lyrics_search(query, artist=""):
    """Try lyrics search on lrclib.net. Returns (synced_lrc, plain_lrc) or (None, None)."""
    enc_q = urllib.parse.quote(query)

    # Try search endpoint
    api_url = f"https://lrclib.net/api/search?q={enc_q}"
    if artist:
        api_url += f"&artist_name={urllib.parse.quote(artist)}"
    data = _lrclib_request(api_url)
    if data and isinstance(data, list) and len(data) > 0:
        synced = data[0].get("syncedLyrics") or ""
        plain = data[0].get("plainLyrics") or ""
        if synced or plain:
            return synced, plain

    # Try direct-get endpoint (exact match, often more reliable)
    if artist:
        get_url = f"https://lrclib.net/api/get?artist_name={urllib.parse.quote(artist)}&track_name={enc_q}"
        data = _lrclib_request(get_url)
        if data and isinstance(data, dict):
            synced = data.get("syncedLyrics") or ""
            plain = data.get("plainLyrics") or ""
            if synced or plain:
                return synced, plain

    return None, None


def fetch_lyrics(title):
    """
    Fetch lyrics from lrclib.net with multi-attempt title cleaning. Cache result.
    Never blocks /api/state — call only from background thread.
    """
    with _lyrics_lock:
        if title in _lyrics_cache:
            return _lyrics_cache[title]

    if not title:
        return None

    cleaned = _clean_lyrics_title(title)

    # Split "Artist - Title" if present
    artist = ""
    track = cleaned
    if " - " in cleaned:
        parts = cleaned.split(" - ", 1)
        artist = parts[0].strip()
        track = parts[1].strip()

    # Multi-attempt search: progressively simpler queries
    synced_lrc, plain_lrc = None, None
    attempts = [
        (track, artist),
        (cleaned, ""),
    ]
    words = cleaned.split()
    if len(words) > 8:
        attempts.append((' '.join(words[:8]), ""))
    if cleaned != title:
        attempts.append((title, ""))

    for q, a in attempts:
        synced_lrc, plain_lrc = _try_lyrics_search(q, a)
        if synced_lrc or plain_lrc:
            break

    result = LYRICS_NOT_FOUND
    if synced_lrc:
        lines = _parse_lrc(synced_lrc)
        if lines:
            result = {"synced": True, "lines": lines}
    if result == LYRICS_NOT_FOUND and plain_lrc:
        result = {
            "synced": False,
            "lines": [{"t": 0, "text": line} for line in plain_lrc.split("\n") if line.strip()]
        }

    with _lyrics_lock:
        _lyrics_cache[title] = result
    return result


def get_lyrics_cached(title):
    """Return cached lyrics for title, or LYRICS_LOADING sentinel. Never blocks."""
    with _lyrics_lock:
        if title in _lyrics_cache:
            return _lyrics_cache[title]
    return LYRICS_LOADING


# ── Background lyrics prefetcher ─────────────────────────────────────────────

_last_lyrics_title = ""
_lyrics_retry_count = {}  # {title: int} — how many retries for LYRICS_NOT_FOUND


def _lyrics_bg_fetch():
    """Runs in background thread — prefetches lyrics when track changes."""
    global _last_lyrics_title
    while True:
        try:
            if mpv_alive():
                title = mpv_get("media-title") or ""
                if title:
                    cached = get_lyrics_cached(title)
                    title_changed = (title != _last_lyrics_title)
                    # Retry if not_found and we haven't retried too many times
                    retries = _lyrics_retry_count.get(title, 0)
                    should_retry = (cached == LYRICS_NOT_FOUND and retries < 3)
                    if title_changed or should_retry:
                        if title_changed:
                            with _lyrics_lock:
                                _lyrics_cache.pop(title, None)
                            _lyrics_retry_count[title] = 0
                        else:
                            _lyrics_retry_count[title] = retries + 1
                            with _lyrics_lock:
                                _lyrics_cache.pop(title, None)
                        _last_lyrics_title = title
                        fetch_lyrics(title)
        except Exception:
            pass
        time.sleep(3)


threading.Thread(target=_lyrics_bg_fetch, daemon=True).start()


# ── Build full state (never blocks on lyrics) ─────────────────────────────────

def get_full_state():
    """
    Return a dict with the full player state for the UI.
    Lyrics: returns cached value or LYRICS_LOADING — never blocks on fetch.
    """
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

    # Never block: use cache or loading sentinel
    lyrics_data = get_lyrics_cached(title) if title else None

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


# ── Command whitelist and rate limiting ──────────────────────────────────────

ALLOWED_CMD_ACTIONS = frozenset([
    "pause", "pp", "next", "mn", "prev", "mb", "stop", "seek", "vol", "volume",
    "speed", "repeat", "rp", "repeat-one", "ro", "shuffle", "playlist-play-index",
    "clear", "norm", "like", "autodj", "eq", "play", "add", "m",
])

RATE_LIMIT_REQUESTS = 10
RATE_LIMIT_WINDOW = 1.0  # seconds

_cmd_timestamps = []
_cmd_timestamps_lock = threading.Lock()


def _check_rate_limit():
    """Return True if request is allowed, False if rate limited."""
    now = time.monotonic()
    with _cmd_timestamps_lock:
        _cmd_timestamps[:] = [t for t in _cmd_timestamps if now - t < RATE_LIMIT_WINDOW]
        if len(_cmd_timestamps) >= RATE_LIMIT_REQUESTS:
            return False
        _cmd_timestamps.append(now)
    return True


def _validate_cmd(cmd_str):
    """Return (valid, error_msg). Valid means cmd action is whitelisted."""
    cmd_str = (cmd_str or "").strip()
    if not cmd_str:
        return False, "empty command"
    parts = cmd_str.split()
    action = parts[0]
    if action not in ALLOWED_CMD_ACTIONS:
        return False, f"command not allowed: {action}"
    return True, None


# ── Handle commands from the UI ──────────────────────────────────────────────

def handle_cmd(cmd_str):
    """Execute an m-style command string against mpv."""
    valid, err = _validate_cmd(cmd_str)
    if not valid:
        return {"ok": False, "msg": err}

    parts = cmd_str.strip().split()
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
        os.system("m like >/dev/null 2>&1 &")
        return {"ok": True, "msg": "liked"}

    elif action == "autodj":
        os.system("m autodj >/dev/null 2>&1 &")
        return {"ok": True, "msg": "toggled autodj"}

    elif action == "eq":
        preset = parts[1] if len(parts) > 1 else "flat"
        presets = {
            "flat": "af set ''",
            "bass": 'af set "superequalizer=1b=6:2b=5:3b=4:4b=2"',
            "treble": 'af set "superequalizer=14b=5:15b=6:16b=4:17b=3"',
            "vocal": 'af set "superequalizer=6b=3:7b=5:8b=5:9b=3"',
            "loud": 'af set "loudnorm=I=-16:TP=-1.5:LRA=11"',
        }
        if preset in presets:
            mpv_command(["af", "set", ""])
            if preset != "flat":
                os.system(f"m eq {preset} >/dev/null 2>&1 &")
        return {"ok": True, "msg": f"eq {preset}"}

    # For "m <args>" or other whitelisted passthrough (play, add), shell out to m CLI
    if action == "m":
        rest = cmd_str[len(action):].strip()
        to_run = f"m {rest}" if rest else "m"
    else:
        to_run = f"m {cmd_str}"
    os.system(f"{to_run} >/dev/null 2>&1 &")
    return {"ok": True, "msg": to_run}


# ── SSE: state change notifications ───────────────────────────────────────────

_sse_clients = []
_sse_clients_lock = threading.Lock()
_last_state_json = None
_state_poll_interval = 0.5


def _sse_broadcast(data):
    """Send JSON to all connected SSE clients."""
    msg = f"data: {json.dumps(data)}\n\n"
    with _sse_clients_lock:
        dead = []
        for wfile in _sse_clients:
            try:
                wfile.write(msg.encode())
                wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                dead.append(wfile)
        for w in dead:
            _sse_clients.remove(w)


def _sse_poll_loop():
    """Background thread: poll state, broadcast on change."""
    global _last_state_json
    while True:
        try:
            state = get_full_state()
            state_json = json.dumps(state, sort_keys=True)
            if _last_state_json is not None and state_json != _last_state_json:
                _sse_broadcast(state)
            _last_state_json = state_json
        except Exception:
            pass
        time.sleep(_state_poll_interval)


threading.Thread(target=_sse_poll_loop, daemon=True).start()


# ── HTTP Server (ThreadingHTTPServer) ────────────────────────────────────────

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class UXIHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass  # silent

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._serve_html()
        elif path == "/api/state":
            self._json_response(get_full_state())
        elif path == "/api/events":
            self._serve_sse()
        else:
            self.send_error(404)

    def _serve_sse(self):
        """Serve Server-Sent Events stream."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        with _sse_clients_lock:
            _sse_clients.append(self.wfile)

        # Send initial state immediately so client doesn't wait for first change
        try:
            state = get_full_state()
            self.wfile.write(f"data: {json.dumps(state)}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            with _sse_clients_lock:
                if self.wfile in _sse_clients:
                    _sse_clients.remove(self.wfile)
            return

        try:
            # Keep connection open; client may disconnect
            while True:
                time.sleep(30)
                # Send keepalive comment
                try:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _sse_clients_lock:
                if self.wfile in _sse_clients:
                    _sse_clients.remove(self.wfile)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/cmd":
            if not _check_rate_limit():
                self._json_response({"ok": False, "msg": "rate limit exceeded"}, 429)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode(errors="replace") if length else "{}"
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {}
            cmd_str = data.get("cmd", "")
            result = handle_cmd(cmd_str)
            self._json_response(result)
        elif path == "/api/play":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode(errors="replace") if length else "{}"
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

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
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
    server = ThreadedHTTPServer(("127.0.0.1", PORT), UXIHandler)
    print(f"m uxi server running → http://127.0.0.1:{PORT}")
    print(f"  mpv socket: {SOCKET_PATH}")
    print(f"  html: {os.path.join(HTML_DIR, 'music_ui.html')}")
    print(f"  SSE: GET /api/events")
    print(f"  press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down…")
        server.shutdown()


if __name__ == "__main__":
    main()
