"""
Spotify for Karry-No-Key: log in once, then save songs to a "Sung It" playlist.

How it works:
  1. You visit http://127.0.0.1:8000/spotify/login once and approve the app in Spotify.
  2. Spotify sends you back to /spotify/callback with a one-time code, which we swap
     for a "refresh token" saved in .spotify_token.json (git-ignored, stays on this laptop).
  3. From then on, save_song() uses that token to find the song and add it to "Sung It",
     creating the playlist the first time.

Your Spotify Client ID and Client secret live in .env (also git-ignored).
"""

import base64
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(ROOT, ".spotify_token.json")
REDIRECT_URI = "http://127.0.0.1:8000/spotify/callback"
SCOPES = "playlist-modify-private playlist-modify-public playlist-read-private"
PLAYLIST_NAME = "Sung It"
PLAYLIST_DESCRIPTION = "Songs I sang into Karry-No-Key"

_login_state = None  # a random value that proves the callback came from our own login


class SpotifyError(Exception):
    pass


_token_cache = {}   # kept in memory, so the cloud version works without a file


def _read_token():
    """Your Spotify login. On your laptop it's saved in .spotify_token.json; in the cloud
    (where files get wiped on restart) the refresh token comes from SPOTIFY_REFRESH_TOKEN."""
    if not _token_cache:
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE) as f:
                _token_cache.update(json.load(f))
        elif os.environ.get("SPOTIFY_REFRESH_TOKEN"):
            _token_cache["refresh_token"] = os.environ["SPOTIFY_REFRESH_TOKEN"]
    return dict(_token_cache)


def _write_token(data):
    _token_cache.clear()
    _token_cache.update(data)
    if os.environ.get("RENDER"):
        return  # in the cloud, memory is enough (and files would be wiped anyway)
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f)
    try:
        os.chmod(TOKEN_FILE, 0o600)  # only you can read it
    except OSError:
        pass


def is_connected():
    return bool(_read_token().get("refresh_token"))


def login_url(env):
    """The Spotify page where you approve Karry-No-Key."""
    global _login_state
    _login_state = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode({
        "client_id": env["SPOTIFY_CLIENT_ID"],
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": _login_state,
    })
    return "https://accounts.spotify.com/authorize?" + query


def _token_request(env, fields):
    auth = base64.b64encode(f'{env["SPOTIFY_CLIENT_ID"]}:{env["SPOTIFY_CLIENT_SECRET"]}'.encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise SpotifyError(f"Spotify login failed ({e.code}): {e.read().decode()[:200]}")


def finish_login(env, code, state):
    """Called when Spotify sends you back after approving. Saves the refresh token."""
    if not _login_state or state != _login_state:
        raise SpotifyError("This login link expired. Go to http://127.0.0.1:8000/spotify/login again.")
    data = _token_request(env, {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI})
    _write_token({
        "refresh_token": data["refresh_token"],
        "access_token": data["access_token"],
        "expires_at": time.time() + data.get("expires_in", 3600) - 60,
    })


def _access_token(env):
    token = _read_token()
    if not token.get("refresh_token"):
        raise SpotifyError("Spotify isn't connected yet. Open http://127.0.0.1:8000/spotify/login on this laptop.")
    if token.get("access_token") and token.get("expires_at", 0) > time.time():
        return token["access_token"]
    data = _token_request(env, {"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
    token["access_token"] = data["access_token"]
    token["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
    if data.get("refresh_token"):
        token["refresh_token"] = data["refresh_token"]
    _write_token(token)
    return token["access_token"]


def _api(env, method, path, body=None):
    req = urllib.request.Request(
        "https://api.spotify.com/v1" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + _access_token(env), "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise SpotifyError(f"Spotify said {e.code} for {method} {path}: {e.read().decode()[:200]}")


# Words in brackets or after a dash that mean "a different version", e.g. "(Remix)", "[Mixed]",
# "- Live", "(feat. X)". We strip these to get back to the plain song title.
_VERSION_WORDS = r"\b(?:remix|mix|mixed|cover|karaoke|instrumental|live|edit|version|remaster(?:ed)?|sped up|slowed|acoustic|feat|ft|with)\b"
_BRACKETS = re.compile(r"\s*[\(\[][^\)\]]*(?:" + _VERSION_WORDS + r")[^\)\]]*[\)\]]", re.IGNORECASE)
_DASH_SUFFIX = re.compile(r"\s+-\s+.*(?:" + _VERSION_WORDS + r").*$", re.IGNORECASE)


def clean_title(title):
    """'Gimme! Gimme! Gimme! (A Man After Midnight) [Remix] [Mixed]' -> 'Gimme! Gimme! Gimme! (A Man After Midnight)'"""
    t = _BRACKETS.sub("", title)
    t = _DASH_SUFFIX.sub("", t)
    return t.strip() or title.strip()


def _comparable(text):
    return re.sub(r"[^a-z0-9]", "", clean_title(text).lower())


def find_original(env, title):
    """ACRCloud matches the tune, so it often returns a cover or remix. Spotify ranks search
    results by popularity, so its top result with the same song title is usually the
    original by the famous artist. Returns {"title", "artist", "spotify_id"} or None."""
    plain = clean_title(title)
    q = urllib.parse.quote(f'track:"{plain}"')
    items = _api(env, "GET", f"/search?q={q}&type=track&limit=10").get("tracks", {}).get("items", [])
    wanted = _comparable(plain)
    for item in items:
        if item and _comparable(item["name"]) == wanted:
            return {
                "title": clean_title(item["name"]),
                "artist": ", ".join(a["name"] for a in item.get("artists", [])),
                "spotify_id": item["id"],
            }
    return None


def _find_track(env, title, artist, track_id=None):
    """Use ACRCloud's Spotify ID if it gave one, otherwise search Spotify by title and artist."""
    if track_id:
        return "spotify:track:" + track_id
    first_artist = artist.split(",")[0].strip()
    q = urllib.parse.quote(f"track:{title} artist:{first_artist}")
    results = _api(env, "GET", f"/search?q={q}&type=track&limit=1")
    items = results.get("tracks", {}).get("items", [])
    if not items:  # looser search, e.g. when the title has "(Remix)" in it
        q = urllib.parse.quote(f"{title} {first_artist}")
        items = _api(env, "GET", f"/search?q={q}&type=track&limit=1").get("tracks", {}).get("items", [])
    if not items:
        raise SpotifyError(f"Couldn't find '{title}' by {artist} on Spotify.")
    return items[0]["uri"]


def _playlist_id(env):
    """Find the "Sung It" playlist, or make it the first time."""
    token = _read_token()
    if token.get("playlist_id"):
        return token["playlist_id"]
    offset = 0
    while True:
        page = _api(env, "GET", f"/me/playlists?limit=50&offset={offset}")
        for p in page.get("items", []):
            if p and p.get("name") == PLAYLIST_NAME:
                token["playlist_id"] = p["id"]
                _write_token(token)
                return p["id"]
        if not page.get("next"):
            break
        offset += 50
    # Spotify's 2026 rules: new playlists are created with POST /me/playlists.
    created = _api(env, "POST", "/me/playlists",
                   {"name": PLAYLIST_NAME, "description": PLAYLIST_DESCRIPTION, "public": False})
    token["playlist_id"] = created["id"]
    _write_token(token)
    return created["id"]


def playlist_url():
    """Link to the "Sung It" playlist, if we know it."""
    pid = _read_token().get("playlist_id")
    return f"https://open.spotify.com/playlist/{pid}" if pid else None


def save_song(env, title, artist, track_id=None):
    """Add a song to the "Sung It" playlist. Returns the Spotify URI that was added."""
    uri = _find_track(env, title, artist, track_id)
    # Spotify's 2026 rules: songs are added with POST /playlists/{id}/items.
    _api(env, "POST", f"/playlists/{_playlist_id(env)}/items", {"uris": [uri]})
    return uri
