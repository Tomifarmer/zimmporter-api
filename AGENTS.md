# Zimmporter — Music Importer

## What it is
Python app that searches YouTube Music, downloads albums/playlists, converts to AAC, embeds metadata + cover art, and uploads to a MinIO bucket (defined in env vars). Exposed as a FastAPI + Celery API with optional CLI. Jobs and songs tracked in MariaDB.

## Structure
```
zimmporter/          — core library (cert config, search, download, yt-dlp postprocess)
api/                 — FastAPI routes (search, download, jobs)
db/                  — SQLAlchemy models + engine (MariaDB)
tasks/               — Celery tasks (download_album, download_playlist)
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

### CLI (legacy)
```bash
zimmporter.sh run search "<query>"
zimmporter.sh run download <album_id>
zimmporter.sh run download <id1,id2> --playlist -c 8
```

### Docker wrapper
```bash
zimmporter.sh serve              # Start API on port 8000
zimmporter.sh runworker          # Start Celery worker
zimmporter.sh run <cli args>     # Run CLI
```

## Authentication (optional)
Set `REQUIRE_AUTH=true` to enforce API key authentication on all endpoints except `/health`. Clients must send an `X-API-Key` header matching the value of the `API_KEY` env var.

## Runtime requirements
- **Python 3.14+**, dependencies in `requirements.txt`
- **ffmpeg** (system-level, for audio extraction)
- **Deno binary** at `/usr/local/bin/deno` (yt-dlp EJS JS runtime)
- **Valkey** for Celery broker/backend (docker compose provides this); uses `redis://` URL scheme with the `redis` python client
- **MariaDB** for job + song tracking (docker compose provides this); env vars `DB_HOST`, `DB_USER`, `DB_PASS`, `DB_NAME
- **MinIO** configured via env vars: `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_BUCKET`
- **Temp mount**: expects `/data/zimmer/importer` for intermediate files

## Docker
- Default CMD: `uvicorn api.app:app --host 0.0.0.0 --port 8000`

## Architecture
- `zimmporter.core.Zimmporter` — core logic: search (ytmusicapi), download (yt_dlp + billiard.Pool)
- `zimmporter.core.Zimmporter.search()` — returns structured list of dicts (no print/emoji)
- `api/routes/search.py` — `GET /search` calls `Zimmporter.search()` synchronously; supports `limit` (1-50); results cached in Valkey db 2 with 5 min TTL
- `api/routes/download.py` — `POST /download/album|/playlist` creates DB Job row, then triggers Celery task
- `api/routes/jobs.py` — `GET /jobs/<id>` reads Job + Song rows from DB
- `api/app.py` — `GET /health` checks API, Valkey connectivity, Celery worker liveness, and MariaDB; always returns HTTP 200 with `"status": "ok"` or `"degraded"` to report partial outages without breaking callers; also purges jobs older than 30 days; `AuthMiddleware` adds optional API key auth (`REQUIRE_AUTH` env var) to all routes except `/health`
- `tasks/download.py` — Celery tasks wrap `download_bulk` with `billiard.Pool`. Updates task state per song for progress tracking.
- Logger + `YTDL_OPTS` mutated on module level — reinitialized in each forked worker because state is lost after `billiard.Pool` fork

## Gotchas
- MinIO configured via env vars (`MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_BUCKET`) with previous defaults for backward compat
- `self.yt = None` is set before `billiard.Pool` in download logic to avoid pickling YTMusic client across forks
- Uses `billiard.Pool` (Celery's vendored multiprocessing) instead of `multiprocessing.Pool` — the latter causes issues when running inside Celery workers
- Uses the `redis` python client against Valkey (drop-in compatible) with `redis://` URLs
- `/` in artist/album/song names is replaced with `-` for MinIO paths (`zimmporter/postprocessors.py:35-37`)
- Concurrent downloads share `YTDL_OPTS` global dict; workers modify `outtmpl` per song
- No local tests exist; verification is manual
