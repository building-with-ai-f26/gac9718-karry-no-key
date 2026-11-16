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

## 3. Non-Negotiable Engineering Rules
1. **Plan before coding:** Before touching multiple files, propose a brief plan and wait for approval.
2. **Small batches:** Make small, focused changes that can be reviewed and explained by a human in a single Pull Request.
3. **Explainability:** Avoid unnecessary abstractions or dependencies. Help the human builder understand the main components, what changed, why it matters, what could break, and how it was checked.
4. **Verify changes:** Run relevant checks. For a reproducible logic bug, use a failing test to demonstrate the fix when practical. Record manual checks and known limitations; follow the selected Build 4 evaluation track.
5. **Zero secrets in code:** Never hardcode API keys, tokens, or credentials. Always read from environment variables (`.env`) and update `.env.example` when new variables are needed.
6. **Stop when looping:** If a fix fails twice in a row, stop, summarize what failed, and ask the human before trying a third approach.
