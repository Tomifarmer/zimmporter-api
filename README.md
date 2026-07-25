# Zimmporter

Music importer API that searches YouTube Music, downloads albums/playlists, converts to AAC, embeds metadata + cover art, and uploads to an S3-compatible bucket. Backed by FastAPI + Celery with MariaDB for job tracking.

## Getting started

```bash
docker compose up -d
```

Then open the Swagger UI at http://localhost:8000/docs or try the API directly.

### CLI (legacy)

```bash
zimmporter.sh search|download
```

## API

### Endpoints

- **`GET /search?q=<query>&type=<albums|playlists>&limit=10`** — Search YouTube Music and return structured result dicts. Results are cached in Valkey for 5 minutes.
- **`POST /download/album`** — Queue one or more albums (`id: "MPREb_xxx,MPREb_yyy"`) with concurrency (`concurrent`: 1–32). Returns `job_id`.
- **`POST /download/playlist`** — Queue one or more playlists. Same flow as album download.
- **`GET /jobs/<id>`** — Poll a specific job for per-song progress.
- **`GET /jobs?limit=50&offset=0`** — List recent jobs newest-first, paginated.
- **`GET /health`** — Health check (always returns 200; status is `"ok"` or `"degraded"`). No auth required.

### Search Example

```bash
curl -s "http://localhost:8000/search?q=Aurora&type=albums" | jq .
# Returns { "results": [ { "resultType": "album", "browseId": "MPREb_xxx", ... } ] }
```

### Download Example

```bash
curl -X POST "http://localhost:8000/download/album" \
  -H "Content-Type: application/json" \
  -d '{"id":"MPREb_m9b9z4z4z4z","concurrent":4}' | jq .
# Returns { "job_id": 1, "status": "pending" } — poll GET /jobs/1 for progress.
```

### Health Check Example

```bash
curl -s "http://localhost:8000/health" | jq .
# { "status": "ok", "components": { "api":"ok","redis":"ok","celery_worker":"ok","mariadb":"ok" }, "timestamp": "..." }
```

## Authentication

Optional API key authentication. Set the following environment variables:

| Variable | Description |
|---|---|
| `REQUIRE_AUTH` | Set to `"true"` (case-insensitive) to enable. Defaults to disabled. |
| `API_KEY` | Expected secret value. Clients must send it in the `X-API-Key` header. |

The `/health` endpoint is always open and not subject to authentication.

## OpenAPI Schema

FastAPI auto-generates an OpenAPI 3.0 schema at:

- **`GET /openapi.json`** — Machine-readable schema
- **`GET /docs`** — Interactive Swagger UI
- **`GET /redoc`** — ReDoc UI

## Architecture Overview

```
Client (curl / UI)
  |
  v
+------------------+     +------------------+
|  FastAPI         |---->|  Valkey          |
|  :8000           |     |  (broker/backend)|
+------------------+     +------------------+
  |                    |
  |  /search            |  Celery task
  |  (sync, cached)     v
  v                     +------------------+
+------------------+    |  Celery Worker    |---->| MariaDB      |
|  ytmusicapi      |    |  tasks/download   |     | jobs/songs   |
+------------------+    +------------------+     +------------------+
        |
        v
+------------------+
|  yt-dlp + ffmpeg |  Download + AAC convert
+------------------+
        |
        v
+------------------+     +------------------+
|  Postprocessors  |---->|  S3              |
|  EnrichMeta      |     |  AWS S3                    |
|  UploadToS3      |     +------------------+
+------------------+
```

### Components

- **`zimmporter/`** — Core library: cert config, search (ytmusicapi), download (yt-dlp + `billiard.Pool`), AAC conversion, per-song download methods returning status dicts
- **`postprocessors.py`** — yt-dlp postprocessors: `EnrichMeta` (ID3 + MP4 tags + cover embed), `UploadToS3` (S3 upload + file cleanup)
- **`api/`** — FastAPI app with routes for search, download, and jobs; `/health` endpoint validates all backend components
- **`tasks/`** — Celery tasks wrap parallel song downloads in a pool of child processes
- **`db/`** — SQLAlchemy engine + ORM models (`Job`, `Song`)

### Concurrency Model

Two levels of parallelism:

1. **Celery level**: One task per job, multiple worker processes can run in parallel.
2. **Pool level**: Within a single album/playlist, each pool child downloads and converts one song at a time via yt-dlp + ffmpegAAC conversion + S3 upload.

### Environment Variables

Key variables:

| Category | Variable | Default | Description |
|---|---|---|---|
| DB | `DB_HOST` | `localhost` | MariaDB hostname |
| DB | `DB_USER` / `DB_PASS` | `root` | Database credentials |
| DB | `DB_NAME` | `zimmporter` | Database name |
| Celery | `CELERY_BROKER` | `redis://localhost:6379/0` | Broker URL (works with Valkey) |
| S3 | `AWS_ENDPOINT_URL` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_BUCKET` / `AWS_USE_SSL` / `AWS_DEFAULT_REGION` | — | S3-compatible storage credentials |
| Auth | `REQUIRE_AUTH` | `false` | Enable API key auth on all routes except `/health` |
| SSL | `CA_CERT`, `REQUESTS_CA_BUNDLE` | unset | Path to private CA PEM file for HTTPS clients |

## Testing

71 pytest tests across all modules (core, API routes, postprocessors, health, cert). Module-level mocks isolate all external services (YTMusic, yt-dlp, Celery, Redis, boto3/S3, MariaDB).

```bash
# Run full suite
uv run python -m pytest tests/

# Run with coverage
uv run python -m pytest tests/ --cov=zimmporter --cov=api --cov=db --cov=tasks

# E2E test (requires full docker compose stack)
./e2e_test.sh "Aurora"
```

### Test layout

| File | Tests | What it covers |
|---|---|---|
| `test_cert.py` | 6 | CA cert resolution, SSL config |
| `test_core.py` | 20 | S3 path building, search, song download |
| `test_health.py` | 6 | GET /health, degraded state |
| `test_postprocessors.py` | 7 | EnrichMeta, UploadToS3 |
| `test_routes_search.py` | 8 | GET /search, caching, validation |
| `test_routes_download.py` | 11 | POST /download, Celery task dispatch |
| `test_routes_jobs.py` | 13 | GET /jobs, retry logic |

The DB layer is replaced with SQLite `:memory:` per test via the `sqlite_db` fixture in `tests/conftest.py`.

## Retention Policy

Jobs and songs older than 30 days are automatically deleted on every successful `/health` check.