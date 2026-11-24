"""
Karry-No-Key laptop version (Build 2 prototype).

What it does:
  1. Serves a web page (app/static/index.html) that records you singing
     through your laptop mic.
  2. Receives that recording and sends it to ACRCloud.
  3. Sends ACRCloud's best matches back to the page to show you.

How to run (from the project folder):
  python3 app/server.py
Then open http://localhost:8000 in Chrome.

For the watch it also: receives recordings from the watch (/identify-watch), saves the
matched song to your Spotify "Sung It" playlist (/save-watch), and sends watch events to PostHog.
Connect Spotify once by opening http://127.0.0.1:8000/spotify/login on this laptop.

Your ACRCloud and Spotify keys live in a file called .env in the project folder.
That file is listed in .gitignore, so it is NEVER pushed to GitHub.
Uses only Python's built-in libraries, so there is nothing to install.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import spotify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "app", "static")
# Render (the cloud host) tells the app which port to use; on your laptop it's 8000.
PORT = int(os.environ.get("PORT", 8000))
ON_CLOUD = bool(os.environ.get("RENDER"))   # Render sets this automatically
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB is plenty for ~15 seconds of audio

# PostHog project key: public by design (the same one is in the web pages), so it's fine in code.
POSTHOG_KEY = "phc_CHj8esjkwBFXBDRnBq6GFavcuJAyKmhJJ9stAEoG4KNJ"
POSTHOG_URL = "https://us.i.posthog.com/i/v0/e/"

# The watch's most recent match, so pressing BOOT can save it to Spotify.
last_watch_match = {}


def track(event, properties=None):
    """Send an event to PostHog in the background, so a slow network never holds up the watch."""
    def send():
        body = json.dumps({
            "api_key": POSTHOG_KEY,
            "event": event,
            "distinct_id": "karry-no-key-watch",
            "properties": dict(properties or {}, source="watch"),
        }).encode()
        try:
            req = urllib.request.Request(POSTHOG_URL, data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:  # analytics must never break the app
            print("PostHog event not sent:", e)
    threading.Thread(target=send, daemon=True).start()


def load_env():
    """Read KEY=value lines from .env into a dict (no extra library needed)."""
    env = {}
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env[key.strip()] = value.strip().strip('"').strip("'")
    # Real environment variables win over the file. On Render, the keys live in the service's
    # "Environment" settings (there is no .env file in the cloud).
    for key in ("ACR_HOST", "ACR_ACCESS_KEY", "ACR_ACCESS_SECRET",
                "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REFRESH_TOKEN"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


# ---------- Protecting the public app ----------
# Anyone with the link can use the app, so limit how often one person can sing (each sing uses
# one ACRCloud recognition from the trial).
SINGS_PER_HOUR = 30
# A backstop for everyone together, in case someone fakes lots of different addresses.
TOTAL_SINGS_PER_HOUR = 300
# Saves and "Not my song" taps from the watch routes, per visitor.
WATCH_ACTIONS_PER_HOUR = 30
_recent_sings = {}   # visitor address -> times they sang in the last hour
_recent_watch_actions = {}
_all_sings = []      # times anyone sang in the last hour
_rate_lock = threading.Lock()


def _under_limit(log, key, limit):
    """Record one more use for this key; False if it has already hit the hourly limit."""
    now = time.time()
    with _rate_lock:
        times = [t for t in log.get(key, []) if now - t < 3600]
        if len(times) >= limit:
            log[key] = times
            return False
        times.append(now)
        log[key] = times
        return True


def allowed_watch_action(visitor):
    return _under_limit(_recent_watch_actions, visitor, WATCH_ACTIONS_PER_HOUR)


def allowed_to_sing(visitor):
    if not _under_limit(_recent_sings, visitor, SINGS_PER_HOUR):
        return False
    now = time.time()
    with _rate_lock:
        _all_sings[:] = [t for t in _all_sings if now - t < 3600]
        if len(_all_sings) >= TOTAL_SINGS_PER_HOUR:
            return False
        _all_sings.append(now)
        return True


# The web page can only save a song the server has just identified for it: each match gets a
# one-time "save ticket", so nobody can add random songs to the playlist.
_save_tickets = {}   # ticket -> (time issued, song)
SAVE_TICKET_MINUTES = 15


def issue_save_ticket(song):
    ticket = uuid.uuid4().hex
    now = time.time()
    with _rate_lock:
        for old in [t for t, (issued, _) in _save_tickets.items() if now - issued > SAVE_TICKET_MINUTES * 60]:
            del _save_tickets[old]
        _save_tickets[ticket] = (now, song)
    return ticket


def use_save_ticket(ticket):
    with _rate_lock:
        issued, song = _save_tickets.pop(ticket, (0, None))
    if song and time.time() - issued <= SAVE_TICKET_MINUTES * 60:
        return song
    return None


def identify_with_acrcloud(audio_bytes, env):
    """Send a recording to ACRCloud's /v1/identify endpoint and return its JSON reply."""
    host = env["ACR_HOST"]
    access_key = env["ACR_ACCESS_KEY"]
    secret = env["ACR_ACCESS_SECRET"]

    timestamp = str(int(time.time()))
    string_to_sign = "\n".join(["POST", "/v1/identify", access_key, "audio", "1", timestamp])
    signature = base64.b64encode(
        hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha1).digest()
    ).decode()

    fields = {
        "access_key": access_key,
        "data_type": "audio",
        "signature_version": "1",
        "signature": signature,
        "sample_bytes": str(len(audio_bytes)),
        "timestamp": timestamp,
    }

    # Build a multipart/form-data body by hand (the same thing a web form upload sends).
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="sample"; filename="sample.wav"\r\n'
        f"Content-Type: audio/wav\r\n\r\n".encode()
        + audio_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        f"https://{host}/v1/identify",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


# Whole words in a title that usually mean "not the original recording".
# (Whole words only, so a song called "Alive" or "Undercover" isn't caught by mistake.)
ALTERNATE_VERSION = re.compile(
    r"\b(remix|mixed|mix|cover|karaoke|instrumental|sped up|slowed|nightcore|tribute|live)\b",
    re.IGNORECASE,
)


def is_alternate_version(item):
    """True if the match looks like a remix/cover/karaoke version rather than the original."""
    return bool(ALTERNATE_VERSION.search(item.get("title", "")))


def simplify_result(acr):
    """Turn ACRCloud's big reply into a short list of matches for the page."""
    status = acr.get("status", {})
    code = status.get("code")
    if code == 1001:
        return {"ok": True, "matches": [], "message": "No match found. Try singing the chorus a bit louder."}
    if code == 2004:
        return {"ok": True, "matches": [], "message": "ACRCloud couldn't hear enough in that recording. Sing a bit louder and closer to your laptop, then try again."}
    if code != 0:
        return {"ok": False, "matches": [], "message": f"ACRCloud error {code}: {status.get('msg')}"}

    metadata = acr.get("metadata", {})
    # "humming" = sung/hummed matches; "music" = matches against recorded songs.
    raw = metadata.get("humming", []) + metadata.get("music", [])
    # ACRCloud often returns a remix or cover first. Put original versions ahead of them,
    # keeping ACRCloud's own order otherwise (sorted() is stable).
    raw = sorted(raw, key=is_alternate_version)
    matches = []
    for item in raw[:3]:  # top 3 guesses
        spotify = item.get("external_metadata", {}).get("spotify", {})
        track_id = spotify.get("track", {}).get("id")
        matches.append({
            "title": item.get("title", "Unknown title"),
            "artist": ", ".join(a.get("name", "") for a in item.get("artists", [])) or "Unknown artist",
            "score": item.get("score"),
            "spotify_url": f"https://open.spotify.com/track/{track_id}" if track_id else None,
            "spotify_id": track_id,
        })
    return {"ok": True, "matches": matches, "message": ""}


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # The only files the page is allowed to load, and what type each one is.
    STATIC_FILES = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/index.html": ("index.html", "text/html; charset=utf-8"),
        "/karry-no-key-logo-pink.svg": ("karry-no-key-logo-pink.svg", "image/svg+xml"),
    }

    def send_html(self, status, message):
        page = ("<!doctype html><meta charset='utf-8'><title>Karry-No-Key</title>"
                "<body style='font-family:sans-serif;background:#FFF3F8;color:#2A1F2D;"
                "text-align:center;padding:60px 20px'><h1 style='color:#B8336A'>Karry-No-Key</h1>"
                f"<p style='font-size:20px'>{message}</p></body>")
        data = page.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_spotify_pages(self, url):
        env = load_env()
        keys = (env.get("SPOTIFY_CLIENT_ID", ""), env.get("SPOTIFY_CLIENT_SECRET", ""))
        if not all(keys) or any(k.startswith("replace-with") for k in keys):
            self.send_html(500, "Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to your .env file, "
                                "then restart the app.")
            return
        if url.path == "/spotify/login":
            if self.headers.get("Host", "").split(":")[0] != "127.0.0.1":
                # Spotify only sends you back to 127.0.0.1, so start from there.
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{PORT}/spotify/login")
                self.end_headers()
                return
            self.send_response(302)
            self.send_header("Location", spotify.login_url(env))
            self.end_headers()
            return
        # /spotify/callback
        q = parse_qs(url.query)
        if "error" in q:
            self.send_html(400, "Spotify login was cancelled. Try again at /spotify/login.")
            return
        try:
            spotify.finish_login(env, q.get("code", [""])[0], q.get("state", [""])[0])
            self.send_html(200, "Spotify connected! &#127881; You can close this tab. "
                                "Songs you save from the watch go to your <b>Sung It</b> playlist.")
        except spotify.SpotifyError as e:
            self.send_html(400, str(e))

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/spotify/login", "/spotify/callback"):
            self.handle_spotify_pages(url)
            return
        if self.path not in self.STATIC_FILES:
            self.send_error(404)
            return
        filename, content_type = self.STATIC_FILES[self.path]
        with open(os.path.join(STATIC_DIR, filename), "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_watch_text(self, result):
        """Plain-text reply for the watch, which can't easily read JSON.
        Line 1: match / nomatch / error. Lines 2-4: title, artist, message (plain ASCII)."""
        top = result["matches"][0] if result.get("matches") else {}
        status = "match" if top else ("nomatch" if result.get("ok") else "error")
        lines = [status, top.get("title", ""), top.get("artist", ""), result.get("message", "")]
        text = "\n".join(to_plain_ascii(line) for line in lines)
        data = text.encode("ascii")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=ascii")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_text(self, text):
        data = to_plain_ascii_lines(text).encode("ascii")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=ascii")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def save_watch_match(self):
        """The watch's BOOT button was pressed on a match: save that song to Spotify."""
        if not last_watch_match:
            self.send_text("error\nNothing to save yet. Sing a song first.")
            return
        song = dict(last_watch_match)
        try:
            spotify.save_song(load_env(), song["title"], song["artist"], song.get("spotify_id"))
            print(f"Saved to Spotify: {song['title']} by {song['artist']}")
            track("watch_saved_to_spotify", {"title": song["title"], "artist": song["artist"]})
            self.send_text("saved\nSaved to Sung It")
        except (spotify.SpotifyError, urllib.error.URLError, KeyError) as e:
            print("Spotify save failed:", e)
            track("watch_save_failed", {"title": song["title"], "error": str(e)[:200]})
            message = str(e) if isinstance(e, spotify.SpotifyError) else "Couldn't reach Spotify."
            self.send_text("error\n" + message)

    def visitor(self):
        # On Render, the visitor's address arrives in a header. Anyone can add a fake address to
        # the start of X-Forwarded-For, so use the address Render's own network adds: Cloudflare's
        # CF-Connecting-IP if it's there, otherwise the LAST entry in X-Forwarded-For.
        real = self.headers.get("CF-Connecting-IP", "").strip()
        if real:
            return real
        forwarded = [a.strip() for a in self.headers.get("X-Forwarded-For", "").split(",") if a.strip()]
        return forwarded[-1] if forwarded else self.client_address[0]

    def save_from_web(self):
        """The web page's "Add to playlist" button. Saves only a song this server just identified."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            ticket = json.loads(self.rfile.read(min(length, 2000)) or b"{}").get("ticket", "")
        except (ValueError, json.JSONDecodeError):
            ticket = ""
        song = use_save_ticket(ticket)
        if not song:
            self.send_json(400, {"ok": False, "message": "That match has expired. Sing again to save it."})
            return
        try:
            spotify.save_song(load_env(), song["title"], song["artist"], song.get("spotify_id"))
            print(f"Saved to Spotify (web): {song['title']} by {song['artist']}")
            self.send_json(200, {"ok": True, "message": f"Saved {song['title']} to the Sung It playlist!",
                                 "playlist_url": spotify.playlist_url()})
        except (spotify.SpotifyError, urllib.error.URLError, KeyError) as e:
            print("Spotify save failed (web):", e)
            message = str(e) if isinstance(e, spotify.SpotifyError) else "Couldn't reach Spotify."
            self.send_json(502, {"ok": False, "message": message})

    def do_POST(self):
        global last_watch_match
        if self.path == "/save":
            self.save_from_web()
            return
        if self.path in ("/save-watch", "/reject-watch") and not allowed_watch_action(self.visitor()):
            self.send_text("error\nToo many taps. Try again in a little while.")
            return
        if self.path == "/save-watch":
            self.save_watch_match()
            last_watch_match = {}   # each match can only be saved once
            return
        if self.path == "/reject-watch":
            # "Not my song" on the watch: a wrong guess. Recorded so we can measure accuracy.
            song = last_watch_match or {}
            last_watch_match = {}
            track("watch_rejected_match", {"title": song.get("title", ""), "artist": song.get("artist", ""),
                                           "acr_title": song.get("acr_title", song.get("title", ""))})
            print(f"Not my song: {song.get('title', '?')} by {song.get('artist', '?')}")
            self.send_text("ok")
            return
        # /identify is used by the laptop web page; /identify-watch by the Karry-No-Key watch.
        if self.path not in ("/identify", "/identify-watch"):
            self.send_error(404)
            return

        env = load_env()
        missing = [k for k in ("ACR_HOST", "ACR_ACCESS_KEY", "ACR_ACCESS_SECRET") if not env.get(k)]
        if missing:
            self.send_json(500, {"ok": False, "matches": [],
                                 "message": "Missing in .env: " + ", ".join(missing)})
            return

        if not allowed_to_sing(self.visitor()):
            self.send_json(429, {"ok": False, "matches": [],
                                 "message": "That's a lot of singing! Take a break and try again in a little while."})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self.send_json(400, {"ok": False, "matches": [], "message": "Recording is empty or too big."})
            return
        audio = self.rfile.read(length)

        try:
            result = simplify_result(identify_with_acrcloud(audio, env))
        except urllib.error.URLError as e:
            result = {"ok": False, "matches": [], "message": f"Could not reach ACRCloud: {e}"}
        print("ACRCloud result:", json.dumps(result))
        prefer_original_version(result)
        for m in result.get("matches", []):
            m["save_ticket"] = issue_save_ticket({k: m.get(k) for k in ("title", "artist", "spotify_id")})
        if self.path == "/identify-watch":
            top = result["matches"][0] if result.get("matches") else {}
            last_watch_match = top
            track("watch_sang")
            if top:
                track("watch_song_matched", {"title": top["title"], "artist": top["artist"],
                                             "score": top.get("score"),
                                             "acr_title": top.get("acr_title", top["title"]),
                                             "acr_artist": top.get("acr_artist", top["artist"])})
            else:
                track("watch_no_match" if result.get("ok") else "watch_error",
                      {"message": result.get("message", "")})
            self.send_watch_text(result)
        else:
            self.send_json(200, result)


def prefer_original_version(result):
    """Swap ACRCloud's top match for the original recording on Spotify, if Spotify is connected.
    Keeps what ACRCloud actually heard in acr_title / acr_artist so we can measure it."""
    if not result.get("matches") or not spotify.is_connected():
        return
    top = result["matches"][0]
    try:
        original = spotify.find_original(load_env(), top["title"])
    except (spotify.SpotifyError, urllib.error.URLError) as e:
        print("Couldn't look up the original version:", e)
        return
    if not original:
        return
    top["acr_title"], top["acr_artist"] = top["title"], top["artist"]
    top.update(original)
    top["spotify_url"] = f"https://open.spotify.com/track/{original['spotify_id']}"
    if (top["acr_title"], top["acr_artist"]) != (top["title"], top["artist"]):
        print(f"Original version: {top['title']} by {top['artist']} "
              f"(ACRCloud heard: {top['acr_title']} by {top['acr_artist']})")


def to_plain_ascii(text):
    """Turn 'Beyoncé' into 'Beyonce' etc., because the watch's basic font only has plain letters."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    return text.replace("\n", " ").strip()


def to_plain_ascii_lines(text):
    return "\n".join(to_plain_ascii(line) for line in str(text).split("\n"))


def laptop_ip_address():
    """This laptop's address on the Wi-Fi, which the watch needs in order to reach the server."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # doesn't send anything; just picks the Wi-Fi connection
        return s.getsockname()[0]
    except OSError:
        return "unknown (are you connected to Wi-Fi?)"
    finally:
        s.close()


if __name__ == "__main__":
    if ON_CLOUD:
        print(f"Karry-No-Key is running on Render (port {PORT}).")
        if not spotify.is_connected():
            print("Spotify not connected: add SPOTIFY_REFRESH_TOKEN in Render's Environment settings.")
    else:
        print(f"Karry-No-Key is running. Open http://localhost:{PORT} in Chrome. Press Ctrl+C to stop.")
        print(f"For the watch: this laptop's address is {laptop_ip_address()}")
        if not spotify.is_connected():
            print(f"Spotify not connected yet: open http://127.0.0.1:{PORT}/spotify/login on this laptop.")
    # "0.0.0.0" lets the watch reach the server from the same Wi-Fi, not just Chrome on this laptop.
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
