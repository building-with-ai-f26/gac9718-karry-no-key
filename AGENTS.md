# AGENTS.md — Coding Agent Operating Manual

> Give this file to your coding agent and check that it is using these instructions. Keep the stack, commands, and ground rules current.

## 1. Project Overview
- **What this repo builds:** Karry-No-Key, a karaoke device that identifies the song you sing and helps you save it to Spotify. This build is the laptop version: a web page records you singing, a local Python server sends the clip to ACRCloud, and the page shows the top 3 matches.
- **Primary user & core workflow:** NYU students. Sing 10–15 seconds → see the identified song → save it to Spotify. Full PRD in `docs/index.html`.
- **Tech stack:** Python 3 standard library only (`app/server.py`), plain HTML/JS page (`app/static/index.html`), ACRCloud identification API (music + humming), PostHog analytics. Product page in `docs/` on GitHub Pages.

## 2. Commands
- **Install dependencies:** None. The server uses only Python's built-in libraries.
- **Set up keys (once):** `cp .env.example .env`, then fill in `ACR_HOST`, `ACR_ACCESS_KEY` and `ACR_ACCESS_SECRET` from the ACRCloud project. `.env` is git-ignored: never commit it.
- **Run locally:** `python3 app/server.py`, then open http://localhost:8000 in Chrome and allow the mic. Stop with Control + C.
- **Run deterministic tests:** Not yet. Manual check: sing a known chorus and confirm the right song is in the top 3.
- **Run model evals:** Not yet (Build 4).
- **Watch (Waveshare ESP32-S3 2.06" AMOLED):** code in `watch/KarryNoKey_Watch/`. Copy `secrets.example.h` to `secrets.h` (git-ignored) and fill in Wi-Fi name, password and the laptop's address (printed when `app/server.py` starts). In Arduino IDE: esp32 by Espressif board package, board **ESP32S3 Dev Module**, USB CDC On Boot Enabled, Flash 16MB, Partition "16M Flash (3MB APP/9.9MB FATFS)", PSRAM OPI PSRAM; library "GFX Library for Arduino". Laptop app must be running on the same 2.4 GHz Wi-Fi with no VPN. Press BOOT on the watch, sing 12 seconds; the watch posts the WAV to `/identify-watch` and shows the top match. Mics are on the ES7210 chip (I2C 0x40).

## 2b. Deployment (Render)
The app is deployed as a Render **Web Service** from this GitHub repo. Live URL: https://karry-no-key.onrender.com

**One-time setup**
1. Sign in at render.com with GitHub. New → **Web Service** → **Public Git Repository** → paste `https://github.com/building-with-ai-f26/gac9718-karry-no-key` → Connect.
2. Settings: Name `karry-no-key` · Language **Python 3** · Branch `main` · Root Directory blank · Build Command `pip install -r requirements.txt` · Start Command `python3 app/server.py` · Instance Type **Free**.
3. Environment Variables (never commit these): `ACR_HOST`, `ACR_ACCESS_KEY`, `ACR_ACCESS_SECRET`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN`. Easiest: "Add from .env" and paste the contents of your local `.env` (run the app locally and connect Spotify first so `.env` has `SPOTIFY_REFRESH_TOKEN`).
4. Deploy. Render sets `PORT` and `RENDER` automatically; `app/server.py` reads both.

**Updating:** push to `main`; Render redeploys automatically (Manual Deploy → Deploy latest commit if not).

**How it works in the cloud**
- No database: the only stored data is the Spotify refresh token (an environment variable) and the "Sung It" playlist on Spotify itself. Nothing is written to the server's disk, so restarts lose nothing.
- Visitors don't sign in. "Add to playlist" calls the server (`POST /save`), which saves to the owner's shared **Sung It** playlist with the server-side Spotify secret; the browser never talks to Spotify with a key. The server only saves songs it has just identified (one-time save tickets, 15 minutes).
- Abuse limits: 30 sings per visitor per hour (each sing uses one ACRCloud recognition).
- Free Render services sleep after ~15 minutes idle; the first visit after that takes up to a minute to wake.
- Rollback: Render → the service → Events → pick the previous deploy → **Rollback**.

## 3. Non-Negotiable Engineering Rules
1. **Plan before coding:** Before touching multiple files, propose a brief plan and wait for approval.
2. **Small batches:** Make small, focused changes that can be reviewed and explained by a human in a single Pull Request.
3. **Explainability:** Avoid unnecessary abstractions or dependencies. Help the human builder understand the main components, what changed, why it matters, what could break, and how it was checked.
4. **Verify changes:** Run relevant checks. For a reproducible logic bug, use a failing test to demonstrate the fix when practical. Record manual checks and known limitations; follow the selected Build 4 evaluation track.
5. **Zero secrets in code:** Never hardcode API keys, tokens, or credentials. Always read from environment variables (`.env`) and update `.env.example` when new variables are needed.
6. **Stop when looping:** If a fix fails twice in a row, stop, summarize what failed, and ask the human before trying a third approach.
