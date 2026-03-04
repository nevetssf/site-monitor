# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run with Docker (recommended):**
```bash
docker-compose up -d        # Start
docker-compose build        # Rebuild after dependency changes
docker-compose logs -f      # View logs
```
App runs at http://localhost:8010 (host port 8010 → container port 8000).

**Run locally (requires Python 3.10+):**
```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

There are no tests in this project.

## Architecture

Site Monitor is a FastAPI app that periodically captures screenshots and HTML of monitored websites, detects changes via pixel diff, and sends push notifications via ntfy.sh.

**Core flow:**
1. On startup, `app/main.py` initializes the SQLite DB and starts APScheduler
2. `app/scheduler.py` creates one interval job per active site (job ID: `site_{id}`)
3. Each job calls `app/checker.py:check_site()`, which uses Playwright/Chromium to capture a screenshot + HTML, then diffs against the previous snapshot
4. Change detection: SHA256 hash comparison first (fast path), then pixel diff via Pillow if HTML changed
5. On change: saves diff image (`_diff.png`), sends ntfy alert, updates site status

**Key modules:**
- `app/checker.py` — all capture/diff/alert logic; Playwright runs with `domcontentloaded` + 3s wait
- `app/scheduler.py` — wraps APScheduler; jobs are recreated when a site's interval changes
- `app/models.py` — three SQLAlchemy models: `Site`, `Snapshot`, `Setting`
- `app/routers/` — three routers: `sites.py` (dashboard + CRUD), `snapshots.py` (history + image serving), `settings.py` (global config + ntfy test)
- `app/templates/` — Jinja2 templates using Alpine.js + Tailwind CSS; UTC→local time conversion in `base.html`

**Data storage:**
- DB: `data/monitor.db` (SQLite, async via aiosqlite)
- Screenshots: `data/screenshots/{site_id}/{uuid}.png`, diff at `{uuid}_diff.png`

**Environment variables** (set in `.env` or Docker Compose):
- `NTFY_SERVER`, `NTFY_TOPIC` — notification target
- `PIXEL_DIFF_THRESHOLD` — % changed pixels to trigger alert (default 1.0)
- `SCREENSHOT_RETENTION_DAYS` — auto-cleanup age (default 30)
- `REQUEST_TIMEOUT` — Playwright page load timeout (default 30s)

**Frontend:** Alpine.js components for dashboard polling, toggle/check-now actions, and lazy screenshot loading. No build step — Tailwind is loaded via CDN.
