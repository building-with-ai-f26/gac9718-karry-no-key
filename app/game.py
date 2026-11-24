"""Finish the Lyric: a quick 5-round game using songs from the "Sung It" playlist.

Each round shows one line of a song with one word missing and three words to choose from.
Lyrics come from LRCLIB (lrclib.net), a free, open lyrics database that needs no key.
"""
import json
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

LRCLIB_SEARCH = "https://lrclib.net/api/search?"
USER_AGENT = "Karry-No-Key/0.1 (NYU Stern class project; https://karry-no-key.onrender.com)"

# Common little words make bad blanks ("the", "and"...), so the missing word is never one of these.
_SKIP_WORDS = set("""a an and are as at be but by can do for from get got had has have he her him his how
i i'm if in into is it it's its just me my no not now of oh on or our out over she so that the their them
then there they this to too up us was we were what when where who why will with yeah you you're your
all ooh ahh hey na la da gonna wanna don't can't ain't won't""".split())
_WORD = re.compile(r"[A-Za-z][A-Za-z']*")
_SPARE_WORDS = ["heart", "night", "dance", "dream", "light", "money", "summer", "forever", "baby", "fire"]

_lyrics_cache = {}   # (title, artist) -> (time fetched, lyrics text or "")
_cache_lock = threading.Lock()
CACHE_SECONDS = 6 * 3600


def fetch_lyrics(title, artist):
    """Plain lyrics for a song from LRCLIB, or "" if it has none."""
    key = (title.lower(), artist.lower())
    with _cache_lock:
        hit = _lyrics_cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    query = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
    req = urllib.request.Request(LRCLIB_SEARCH + query, headers={"User-Agent": USER_AGENT})
    text = ""
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            for record in json.loads(resp.read().decode()):
                if record.get("plainLyrics") and not record.get("instrumental"):
                    text = record["plainLyrics"]
                    break
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        print("LRCLIB lookup failed:", e)
        return ""   # don't cache failures, so it's retried next time
    with _cache_lock:
        _lyrics_cache[key] = (time.time(), text)
    return text


def _usable_lines(lyrics):
    """Lines that make a fair round: 5-14 words, not a [Chorus] label, not repeated filler."""
    lines = []
    for raw in lyrics.splitlines():
        line = raw.strip()
        if not line or line.startswith(("[", "(")):
            continue
        words = line.split()
        if 5 <= len(words) <= 14 and any(_is_good_blank(w) for w in words[1:]):
            lines.append(line)
    return list(dict.fromkeys(lines))   # drop exact repeats, keep order


def _clean(word):
    m = _WORD.search(word)
    return m.group(0) if m else ""


def _is_good_blank(word):
    w = _clean(word)
    return len(w) >= 3 and w.lower() not in _SKIP_WORDS


def make_round(title, artist, lyrics, rng=random):
    """Build one round from a song's lyrics, or None if the lyrics don't have a usable line."""
    lines = _usable_lines(lyrics)
    if not lines:
        return None
    line = rng.choice(lines)
    words = line.split()
    # Never blank the first word: it gives too little context.
    positions = [i for i, w in enumerate(words) if i > 0 and _is_good_blank(w)]
    if not positions:
        return None
    pos = rng.choice(positions)
    answer = _clean(words[pos])
    # Keep any punctuation around the missing word, e.g. "moon," -> "____,"
    blank = words[pos].replace(answer, "____", 1)
    before = " ".join(words[:pos])
    after = " ".join(words[pos + 1:])
    if blank != "____":
        after = blank.replace("____", "", 1) + (" " + after if after else "")

    # Wrong answers: other real words from the same song, a similar length, so it isn't too easy.
    sung = " ".join(l for l in lyrics.splitlines() if not l.strip().startswith(("[", "(")))
    pool = {_clean(w).lower() for w in sung.split() if _is_good_blank(w)}
    pool = [w for w in pool if w.lower() != answer.lower()]
    close = [w for w in pool if abs(len(w) - len(answer)) <= 2]
    choices_wrong = rng.sample(close, 2) if len(close) >= 2 else rng.sample(pool, min(2, len(pool)))
    spares = [w for w in _SPARE_WORDS if w != answer.lower() and w not in choices_wrong]
    while len(choices_wrong) < 2:
        choices_wrong.append(spares.pop(0))
    # Show every choice in lower case, so capital letters don't give the answer away.
    choices = [answer.lower()] + [w.lower() for w in choices_wrong]
    rng.shuffle(choices)
    return {"title": title, "artist": artist, "before": before, "after": after.strip(),
            "choices": choices, "answer": answer.lower()}


def next_round(songs, rng=random):
    """Pick a random song from the playlist that has lyrics and build a round from it."""
    songs = list(songs)
    rng.shuffle(songs)
    for song in songs[:6]:   # try a few songs at most, so a round never takes too long
        lyrics = fetch_lyrics(song["title"], song["artist"])
        if lyrics:
            r = make_round(song["title"], song["artist"], lyrics, rng)
            if r:
                return r
    return None
