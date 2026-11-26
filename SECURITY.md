# SECURITY.md — Karry-No-Key Security Audit (Build 3)

**Audited:** 22–23 Nov 2026, by me (Genny) with Claude as my coding agent
**Live app:** https://karry-no-key.onrender.com (Render, free tier)
**Repo:** https://github.com/building-with-ai-f26/gac9718-karry-no-key

## What kind of app this is

**No sign-in, no database.** Karry-No-Key doesn't use an AI model; it calls a **paid API** (ACRCloud song recognition) and **writes data to Spotify** (it adds songs to my shared "Sung It" playlist). So I ran the tests that fit both of those: a rate-limit test on the route that calls ACRCloud, and a test that strangers can't write to Spotify through the save routes.

| Where it runs | What it holds |
|---|---|
| Web page (`app/static/index.html`) | Only the PostHog `phc_` key, which is public by design |
| Server (`app/server.py`, on Render) | ACRCloud keys, Spotify client ID, client secret and refresh token, all as Render environment variables |
| Watch (`watch/KarryNoKey_Watch/`) | Wi-Fi details and the server address in `secrets.h`, which is git-ignored and only on my laptop and the watch |

## 1. Access controls

**Paid route (`POST /identify`), tested on the live site from the browser console:**

| Test | Expected | Result |
|---|---|---|
| 32 sings in a row from one visitor (empty recordings, so no ACRCloud calls were used up) | Calls 1–30 are allowed, then `429` | ✅ 30 × `400` ("Recording is empty"), then `429` on calls 31 and 32 |
| The same, while faking a different address in the `X-Forwarded-For` header | Still `429` | ❌ **Got past the limit** (`400` instead of `429`). **Fixed** (section 7) and **re-tested live on 23 Nov:** faked addresses now get `429` on calls 31 and 32 ✅ |

**Write routes (saving to Spotify), tested on the live site:**

| Test | Expected | Result |
|---|---|---|
| `POST /save` with a made-up save ticket | Rejected | ✅ `400` "That match has expired" and nothing saved |
| `POST /save` with no ticket | Rejected | ✅ `400` and nothing saved |
| `GET /spotify/callback?code=x&state=y` (trying to swap in someone else's Spotify account) | Rejected | ✅ `400`. The login "state" check blocks it, and on Render the login can't be started at all |
| `POST /save-watch` from a browser | Can only save the watch's last match | ⚠️ Worked, with no limit, and the same match could be saved again and again. **Fixed** (section 7) and **re-tested live on 23 Nov:** call 31 on the watch routes gets "Too many taps" ✅ |

## 2. Secret keys stay on the server

- **Live site:** I downloaded the live page and searched it for `sb_secret_`, `service_role`, `sk-or-`, `sk-ant-`, `sk-proj-`, `sk_live_`, `client_secret`, `refresh_token` and the ACRCloud key names. ✅ **Nothing found.** The only key present is PostHog's `phc_` key, which is meant to be public.
- **Repo, including every past commit:** I searched the whole git history for the same patterns. `sb_secret_`, `service_role`, `sk-ant-`, `sk-proj-` and `sk_live_` all came back with 0 hits. `sk-or-` was found only as the placeholder `sk-or-v1-replace-with-…` in `.env.example`, which is the course template and not a real key.
- **My own keys:** a script compared every real value in my `.env` (ACRCloud keys, Spotify client ID, client secret and refresh token) with every commit, printing only "found / not found". ✅ **None are in the repo or its history.**
- `.env`, `.spotify_token.json` and `watch/**/secrets.h` are all git-ignored, which I confirmed with `git check-ignore`.
- **Result: no key needs rotating.**

## 3. File storage

No file storage. Recordings are held in memory just long enough to send to ACRCloud, and are never saved to disk or to a storage bucket.

## 4. Protecting production from my agent

- There's no database, so there's nothing to back up or delete. The only "production data" is the **Sung It** playlist on Spotify.
- `AGENTS.md` now has an **Ask Me First** list: deleting data, git history rewrites, keys and secrets, permissions, anything that costs money, Render settings, deploys and rollbacks, and flashing the watch.
- The agent never sees key values: secret checks compare values in a script and print only "found / not found" (rule 4 in `AGENTS.md`).
- Rollback is one click: Render → Events → previous deploy → **Rollback** (written down in `AGENTS.md`).

## 5. Spending caps and rate limits

| Service | Cap |
|---|---|
| ACRCloud (the only per-use cost) | Free trial. The server allows 30 sings per visitor per hour, **plus a new limit of 300 per hour for everyone together**, so even someone faking lots of addresses can't run up more than 300 recognitions an hour. ✅ **Confirmed:** no payment card saved on ACRCloud and no auto-upgrade when the trial ends. |
| Render | ✅ **Confirmed:** Free instance type, no paid plan. |
| Spotify Web API | Free (development mode). |
| PostHog | ✅ **Confirmed:** free tier with a $0 billing limit. Session replay is on. |

The routes that call a paid API or write data are rate-limited per visitor: `/identify` and `/identify-watch` (30 per hour), and `/save-watch` and `/reject-watch` (30 per hour). `/save` needs a one-time ticket, and tickets only come from a rate-limited sing.

**PostHog check (live, 23 Nov):** PostHog loads on the live site, and session replay is recording ✅

## 6. Dependency audit

`pip-audit -r requirements.txt` → **"No known vulnerabilities found."** The app uses only Python's built-in libraries, so there are no third-party packages to audit. The watch uses the Arduino ESP32 core 3.3.13 and "GFX Library for Arduino" 1.6.9; there's no audit tool for these, so I keep them updated through the Arduino IDE.

## 7. Agent security review: what I found and fixed

I asked Claude to read the server code and try to break the live app. It found:

| # | Finding | Risk | Fix |
|---|---|---|---|
| 1 | **The rate limit could be bypassed by faking an address.** The server trusted the *first* entry in `X-Forwarded-For`, which anyone can write. | High: someone could use up my ACRCloud allowance | ✅ The server now uses `CF-Connecting-IP` or the **last** `X-Forwarded-For` entry (the one Render's network adds), plus an overall cap of 300 sings an hour. Tested locally: fake first entries now hit `429` at call 31. |
| 2 | **`/save-watch` had no limit, and one match could be saved over and over**, so anyone could spam the playlist with the watch's last song. | Medium | ✅ It's now rate-limited (30 per hour), and each watch match can only be saved or rejected once. Tested locally: call 31 gets "Too many taps". |
| 3 | **A broken `Content-Length` header crashed the request** (an unhandled error). | Low | ✅ It's now handled and returns `400`. Tested locally. |
| 4 | Could someone connect *their* Spotify through `/spotify/callback`? | Checked | ✅ Already safe: the random login "state" must match, and logins can't start on Render. |
| 5 | Is the PostHog key a leak? | Checked | ✅ No: `phc_` keys are public by design. |

## Remaining issues (known, accepted for now)

- **The watch routes don't know *which* watch is calling.** Any visitor could call `/identify-watch`, `/save-watch` and `/reject-watch` (with the limits above). Build 4 adds a 6-digit watch-linking code and sign-in, which fixes this properly.
- **Rate limits are kept in memory**, so they reset when Render restarts or the app wakes from sleep. The overall cap and the one-time tickets still apply. That's fine for a class prototype, but it needs a database (Supabase with Row Level Security) at launch.
- **Everyone saves to one shared Spotify playlist** on my account. Personal playlists need sign-in (Build 4).
- **The watch talks to my laptop over plain HTTP on my home Wi-Fi.** Moving it to the live HTTPS site is next.
- **Test noise in analytics:** the live re-test sent about 30 empty `watch_rejected_match` events to PostHog on 23 Nov. These should be filtered out when measuring accuracy.

## Update (24 Nov 2026): new features since the audit

| New route | What it does | Protection |
|---|---|---|
| `GET /game/round` | Finish the Lyric round (reads the Sung It playlist and LRCLIB) | 200 per visitor per hour. Lyrics are shown as plain text, never as HTML |
| `GET /recent-songs` | Recently Sung and Top Song lists | 120 per visitor per hour; read-only |
| `POST /watch/checkin` | The watch's "I'm here" (battery, signal, code) | Needs `WATCH_KEY`, otherwise `403` |
| `POST /find/status`, `POST /find/ring` | Find my page | Needs the 6-digit code shown on the watch; 30 tries per visitor per hour and 100 wrong codes per hour in total |
| `/identify-watch`, `/save-watch`, `/reject-watch` | Watch singing and saving | **Now need `WATCH_KEY`** once it's set, which fixes the earlier remaining issue "the watch routes don't know which watch is calling" |

- The watch now talks to the live site over **https** and checks the site's certificate against the ESP32's built-in list of trusted authorities, so the key can't be read or faked on the Wi-Fi.
- `WATCH_KEY` lives only in `.env`, Render's settings and the watch's git-ignored `secrets.h`.
- **New remaining issue:** someone who keeps guessing Find my codes can trigger the overall wrong-code limit, which locks the owner out for up to an hour.
- **New remaining issue:** LRCLIB lyrics are community-submitted, which is fine for a prototype, but a launch would need licensed lyrics.

## Update (26 Nov 2026): Play it and Sing it

| New route | What it does | Protection |
|---|---|---|
| `POST /watch/play` | Starts the watch's last matched song in Spotify on the owner's phone | Needs `WATCH_KEY`; only plays the song the watch just matched |
| `POST /watch/lyrics`, `POST /watch/progress` | Timed lyrics (LRCLIB) and where the song is up to | Needs `WATCH_KEY`; read-only |

- Spotify now has two extra permissions: control playback (`user-modify-playback-state`) and see what's playing (`user-read-playback-state`). The new refresh token went into `.env` and Render only, and was never shown on screen.
- **Remaining issue:** Play it always plays on the owner's Spotify, because there's no sign-in yet. Personal Spotify for each user comes with sign-in (Spotify development mode allows 5 users).
