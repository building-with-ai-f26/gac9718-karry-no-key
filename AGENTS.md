# AGENTS.md — Coding Agent Operating Manual

> Give this file to your coding agent and check that it is using these instructions. Keep the stack, commands, and ground rules current.

## 1. Project Overview
- **What this repo builds:** Karry-No-Key, a karaoke watch that identifies the song you sing and saves it to Spotify. Two front ends share one Python server: a web page (live at https://karry-no-key.onrender.com) and the Waveshare watch. The server sends the clip to ACRCloud, swaps covers for originals via Spotify, and saves to the shared "Sung It" playlist.
- **Primary user & core workflow:** NYU students. Sing 10–15 seconds → see the identified song → save it to Spotify. Full PRD in `docs/index.html`.
- **Tech stack:** Python 3 standard library only (`app/server.py`), plain HTML/JS page (`app/static/index.html`), ACRCloud identification API (music + humming), PostHog analytics. Product page in `docs/` on GitHub Pages.

## 2. Commands
- **Install dependencies:** None. The server uses only Python's built-in libraries.
- **Set up keys (once):** `cp .env.example .env`, then fill in `ACR_HOST`, `ACR_ACCESS_KEY` and `ACR_ACCESS_SECRET` from the ACRCloud project. `.env` is git-ignored: never commit it.
- **Run locally:** `python3 app/server.py`, then open http://localhost:8000 in Chrome and allow the mic. Stop with Control + C.
- **Run deterministic tests:** Not yet. Manual check: sing a known chorus and confirm the right song is in the top 3.
- **Run model evals:** Not yet (Build 4).
- **Watch (Waveshare ESP32-S3 2.06" AMOLED):** code in `watch/KarryNoKey_Watch/`. Copy `secrets.example.h` to `secrets.h` (git-ignored) and fill in Wi-Fi name, password and the laptop's address (printed when `app/server.py` starts). In Arduino IDE: esp32 by Espressif board package, board **ESP32S3 Dev Module**, USB CDC On Boot Enabled, Flash 16MB, Partition "16M Flash (3MB APP/9.9MB FATFS)", PSRAM OPI PSRAM; library "GFX Library for Arduino". Laptop app must be running on the same 2.4 GHz Wi-Fi with no VPN. Press BOOT on the watch, sing 12 seconds; the watch posts the WAV to `/identify-watch` and shows the top match. Mics are on the ES7210 chip (I2C 0x40).

## 2b. Shipping (deploying to Render)
The app is deployed as a Render **Web Service** from this GitHub repo. Live URL: https://karry-no-key.onrender.com

**One-time setup**
1. Sign in at render.com with GitHub. New → **Web Service** → **Public Git Repository** → paste `https://github.com/building-with-ai-f26/gac9718-karry-no-key` → Connect.
2. Settings: Name `karry-no-key` · Language **Python 3** · Branch `main` · Root Directory blank · Build Command `pip install -r requirements.txt` · Start Command `python3 app/server.py` · Instance Type **Free**.
3. Environment Variables (never commit these): `ACR_HOST`, `ACR_ACCESS_KEY`, `ACR_ACCESS_SECRET`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN`. Easiest: "Add from .env" and paste the contents of your local `.env` (run the app locally and connect Spotify first so `.env` has `SPOTIFY_REFRESH_TOKEN`).
4. Deploy. Render sets `PORT` and `RENDER` automatically; `app/server.py` reads both.

**Shipping a change (the agent follows these steps every time)**
1. `python3 -m py_compile app/server.py app/spotify.py` must pass.
2. Run the app locally (`PORT=8765 python3 app/server.py`) and test every route you touched with `curl` (and the page in a browser if the page changed). Stop the server afterwards.
3. Secret check: `git grep -n -E "sb_secret_|service_role|sk-or-v1-[a-z0-9]{20}|sk-ant-|sk-proj-|sk_live_"` finds nothing, and `git status` shows no `.env`, `.spotify_token.json` or `secrets.h`.
4. Show the human the diff and the test results, then the human commits and pushes to `main`.
5. Render redeploys automatically. If the live site doesn't change within ~5 minutes: Render → the service → **Manual Deploy → Deploy latest commit**.
6. Check the live site: it loads, "Tap to sing" works on a phone, and PostHog shows the new events.

**How it works in the cloud**
- No database: the only stored data is the Spotify refresh token (an environment variable) and the "Sung It" playlist on Spotify itself. Nothing is written to the server's disk, so restarts lose nothing.
- Visitors don't sign in. "Add to playlist" calls the server (`POST /save`), which saves to the owner's shared **Sung It** playlist with the server-side Spotify secret; the browser never talks to Spotify with a key. The server only saves songs it has just identified (one-time save tickets, 15 minutes).
- Abuse limits: 30 sings per visitor per hour and 300 per hour for everyone together (each sing uses one ACRCloud recognition); 30 watch saves / "Not my song" taps per visitor per hour; each watch match can be saved once. The visitor's address comes from `CF-Connecting-IP` or the last `X-Forwarded-For` entry, because the first entry can be faked.
- Security audit results: see `SECURITY.md`.
- Free Render services sleep after ~15 minutes idle; the first visit after that takes up to a minute to wake.
- Rollback: Render → the service → Events → pick the previous deploy → **Rollback**.

## 3. Non-Negotiable Engineering Rules
1. **Plan before coding:** Before touching multiple files, propose a brief plan and wait for approval.
2. **Small batches:** Make small, focused changes that can be reviewed and explained by a human in a single Pull Request.
3. **Explainability:** Avoid unnecessary abstractions or dependencies. Help the human builder understand the main components, what changed, why it matters, what could break, and how it was checked.
4. **Verify changes:** Run relevant checks. For a reproducible logic bug, use a failing test to demonstrate the fix when practical. Record manual checks and known limitations; follow the selected Build 4 evaluation track.
5. **Zero secrets in code:** Never hardcode API keys, tokens, or credentials. Always read from environment variables (`.env`) and update `.env.example` when new variables are needed.
6. **Stop when looping:** If a fix fails twice in a row, stop, summarize what failed, and ask the human before trying a third approach.

## 4. Ask Me First (never do these without the human saying yes in chat)
- **Deleting data or changing storage:** deleting files, the "Sung It" playlist or songs in it, git history (`git reset --hard`, force push, rebasing pushed commits), or adding or changing a database.
- **Keys, secrets and permissions:** reading, printing, creating, rotating or moving any key or token (`.env`, Render environment variables, `secrets.h`, Spotify or ACRCloud dashboards); changing Spotify app settings, redirect URIs or scopes; changing who can access the repo or the Render service.
- **Anything that costs money:** upgrading Render, ACRCloud, PostHog or Spotify plans, adding a paid service, or raising the rate limits (each extra sing is an ACRCloud call).
- **Production:** changing Render settings, triggering a deploy, or rolling back.
- **Flashing the watch** with new firmware.

## 5. Rules Learned From Real Mistakes
1. **Compile and run it before handing it over.** The agent once gave the human a `server.py` that crashed on start (`SyntaxError: name 'last_watch_match' is used prior to global declaration`). Always run `python3 -m py_compile` and hit the changed routes locally before saying a change is ready.
2. **Check the API docs are current before using an endpoint.** The agent used Spotify's old `POST /users/{id}/playlists` and `/tracks` endpoints, which return 403 for new apps since Spotify's February 2026 changes. Use `POST /me/playlists` and `POST /playlists/{id}/items`, and check the current docs for any new endpoint.
3. **Never trust the first `X-Forwarded-For` entry.** The first version of the rate limit used it, and the security audit showed anyone could get past the limit by sending a fake address. Use the address the host adds.
4. **Never repeat secrets in chat or logs.** When checking whether a key leaked, compare values in a script and print only "found / not found".
