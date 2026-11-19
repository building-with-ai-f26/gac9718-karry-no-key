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

Your ACRCloud keys live in a file called .env in the project folder.
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
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "app", "static")
PORT = 8000
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB is plenty for ~15 seconds of audio


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
    # Real environment variables win over the file.
    for key in ("ACR_HOST", "ACR_ACCESS_KEY", "ACR_ACCESS_SECRET"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


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

    def do_GET(self):
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

    def do_POST(self):
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

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self.send_json(400, {"ok": False, "matches": [], "message": "Recording is empty or too big."})
            return
        audio = self.rfile.read(length)

        try:
            result = simplify_result(identify_with_acrcloud(audio, env))
        except urllib.error.URLError as e:
            result = {"ok": False, "matches": [], "message": f"Could not reach ACRCloud: {e}"}
        print("ACRCloud result:", json.dumps(result))
        if self.path == "/identify-watch":
            self.send_watch_text(result)
        else:
            self.send_json(200, result)


def to_plain_ascii(text):
    """Turn 'Beyoncé' into 'Beyonce' etc., because the watch's basic font only has plain letters."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    return text.replace("\n", " ").strip()


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
    print(f"Karry-No-Key is running. Open http://localhost:{PORT} in Chrome. Press Ctrl+C to stop.")
    print(f"For the watch: this laptop's address is {laptop_ip_address()}")
    # "0.0.0.0" lets the watch reach the server from the same Wi-Fi, not just Chrome on this laptop.
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
