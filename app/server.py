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
import time
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


def simplify_result(acr):
    """Turn ACRCloud's big reply into a short list of matches for the page."""
    status = acr.get("status", {})
    code = status.get("code")
    if code == 1001:
        return {"ok": True, "matches": [], "message": "No match found. Try singing the chorus a bit louder."}
    if code != 0:
        return {"ok": False, "matches": [], "message": f"ACRCloud error {code}: {status.get('msg')}"}

    metadata = acr.get("metadata", {})
    # "humming" = sung/hummed matches; "music" = matches against recorded songs.
    raw = metadata.get("humming", []) + metadata.get("music", [])
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

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/identify":
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
        self.send_json(200, result)


if __name__ == "__main__":
    print(f"Karry-No-Key is running. Open http://localhost:{PORT} in Chrome. Press Ctrl+C to stop.")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
