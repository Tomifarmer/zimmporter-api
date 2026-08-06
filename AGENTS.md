# Zimmporter — Music Importer

## What it is
Python app that searches YouTube Music, downloads albums/playlists, converts to AAC, embeds metadata + cover art, and uploads to an S3-compatible bucket (defined in env vars). Exposed as a FastAPI + Celery API. Jobs and songs tracked in MariaDB.

## Structure
```
zimmporter/          — core library (cert config, search, download, yt-dlp postprocess)
api/                 — FastAPI routes (search, download, jobs, cookies)
db/                  — SQLAlchemy models + engine (MariaDB)
tasks/               — Celery tasks (download_album, download_playlist)
tests/               — pytest suite (128 tests across all modules)
```

## Usage

### API (primary)
```bash
# Local development
docker compose up -d

# Search albums
curl -s "http://localhost:8000/search?q=Aurora&type=albums" -H "X-API-Key: your-secret" | jq .

# Download album (returns job_id)
curl -X POST "http://localhost:8000/download/album" -H "Content-Type: application/json" -H "X-API-Key: your-secret" -d '{"id":"MPREb_xxx","concurrent":4}' | jq .

# Download playlist
curl -X POST "http://localhost:8000/download/playlist" -H "Content-Type: application/json" -H "X-API-Key: your-secret" -d '{"id":"VLx_xxx","concurrent":4}' | jq .

# Check job status
curl -s "http://localhost:8000/jobs/<job_id>" -H "X-API-Key: your-secret" | jq .

# List jobs
curl -s "http://localhost:8000/jobs?limit=50" -H "X-API-Key: your-secret" | jq .

# Health check (always open, no auth required)
curl -s "http://localhost:8000/health" | jq .
```

## Authentication (optional)
Two optional auth methods: set `USE_SIMPLE_AUTH=true` to require API key (`X-API-Key` header matching `API_KEY`), or set `USE_SOCIAL_LOGIN=true` to require a Bearer token (`Authorization: Bearer <JWT>` validated against `OIDC_ISSUER_URL`/`OIDC_CLIENT_ID`, or a GitHub token validated against the GitHub API). Either suffices when both are enabled.

## Runtime requirements
- **Python 3.14+**, dependencies split across requirement files:
  - `requirements.txt` — API container (FastAPI, uvicorn + `requirements-base.txt`)
  - `requirements-base.txt` — shared minimal deps (ytmusicapi, celery, sqlalchemy, pymysql, requests)
  - `requirements-worker.txt` — worker container (same as base + yt-dlp, boto3, mutagen)
- **ffmpeg** (system-level, worker container only)
- **Deno binary** at `/usr/bin/deno` (worker container only)
- **Valkey** for Celery broker/backend (docker compose provides this); uses `redis://` URL scheme with the `redis` python client
- **MariaDB** for job + song tracking (docker compose provides this); env vars `DB_HOST`, `DB_USER`, `DB_PASS`, `DB_NAME
- **S3** configured via env vars: `AWS_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_BUCKET`
- **Temp mount**: expects `/data/zimmer/importer` for intermediate files

## Docker
- Default CMD: `uvicorn api.app:app --host 0.0.0.0 --port 8000`

## Architecture
- `zimmporter.core.Zimmporter` — core logic: search (ytmusicapi), download (yt_dlp + billiard.Pool)
- `zimmporter.core.Zimmporter.search()` — returns structured list of dicts (no print/emoji)
- `api/routes/search.py` — `GET /search` calls `Zimmporter.search()` synchronously; supports `limit` (1-50); results cached in Valkey db 2 with 5 min TTL; enriches each result with an `available` flag from the `available_albums` index (after cache read, so it stays fresh)
- `tasks/index.py` — Celery task `tasks.index_albums` scans the S3 bucket (`{artist}/{album}/` prefixes) and reconciles the `available_albums` table (upsert + prune); `upsert_available_album()` is also called by download tasks to record exact `browse_id`s
- `api/scheduler.py` — periodic dispatcher running inside the API pod (no Celery beat container): dispatches `tasks.index_albums` every `INDEX_INTERVAL_MINUTES` (default 30) via `send_task`, guarded by a Valkey lock so multiple API replicas dispatch only once per interval
- `api/routes/download.py` — `POST /download/album|/playlist` creates DB Job row, then triggers Celery task
- `api/routes/cookies.py` — `GET /cookies` (metadata) and `POST /cookies` (multipart upload) manage the yt-dlp cookies file; validated and stored in Valkey via `zimmporter/cookie_store.py` (no shared volume), contents never exposed
- `api/routes/jobs.py` — `GET /jobs/<id>` reads Job + Song rows from DB
- `api/app.py` — `GET /health` checks API, Valkey connectivity, Celery worker liveness, and MariaDB; always returns HTTP 200 with `"status": "ok"` or `"degraded"` to report partial outages without breaking callers; also purges jobs older than `JOB_RETENTION_DAYS` (default 0 = never purge) and fails stalled jobs via `_fail_stalled_jobs()` (controlled by `JOB_STALLED_TIMEOUT`, default 5m); `AuthMiddleware` adds optional auth (`USE_SIMPLE_AUTH`/`USE_SOCIAL_LOGIN` env vars) to all routes except `/health`
- `tasks/download.py` — Celery tasks wrap `download_bulk` with `billiard.Pool`. Updates task state per song for progress tracking. Records successfully downloaded albums/playlists into `available_albums` with their `browse_id`.
- The S3 library index is triggered from the API pod (`api/scheduler.py`, interval `INDEX_INTERVAL_MINUTES`), not from Celery beat — no beat container/deployment exists.
- Logger + `YTDL_OPTS` mutated on module level — reinitialized in each forked worker because state is lost after `billiard.Pool` fork

## Gotchas
- S3 configured via env vars (`AWS_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_BUCKET`) with previous defaults for backward compat
- `self.yt = None` is set before `billiard.Pool` in download logic to avoid pickling YTMusic client across forks
- Uses `billiard.Pool` (Celery's vendored multiprocessing) instead of `multiprocessing.Pool` — the latter causes issues when running inside Celery workers
- Uses the `redis` python client against Valkey (drop-in compatible) with `redis://` URLs
- `/` in artist/album/song names is replaced with `-` for S3 paths (`zimmporter/postprocessors.py:98-100`)
- Concurrent downloads share `YTDL_OPTS` global dict; workers modify `outtmpl` per song
- Heavy imports (`yt_dlp`, `boto3`, `mutagen`, `billiard.Pool`) are lazy — inside the methods that use them, not at module level. This keeps the API container from needing them at import time.
- 71 pytest tests covering core, routes, postprocessors, health, cert — run with `uv run python -m pytest tests/`
