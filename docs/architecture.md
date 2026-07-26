# Zimmporter Architecture

Music importer that searches YouTube Music, downloads albums and playlists, converts to AAC with embedded metadata and cover art, and uploads to an S3-compatible bucket.  Exposed as a FastAPI + Celery API.

## High-Level Data Flow

```
Client (curl / UI)
  |
  v
+------------------+
|  FastAPI (API)   |  :8000
|  app.py          |
+------------------+
  |                    |
|  /search           |  /download/{album|playlist}
  |  (sync, cached)   |  (async via Celery)
  v                    v
 +------------------+  +------------------+
 |  ytmusicapi      |  |  MariaDB         |
 |  (YouTube Music) |  |  jobs / songs    |
 +------------------+  +------------------+
                         |
                         |  Celery task (task_id = job.id)
                         v
+------------------+     +------------------+
|  Celery Worker   |---->|  Valkey          |
|  tasks/download  |     |  (broker)        |
+------------------+     +------------------+
       |
       |  billiard.Pool (N workers per album)
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

## Components

### `zimmporter/` - Core Library

| Module | Purpose |
|--------|---------|
| `cert.py` | Private CA certificate configuration: `get_ca_cert()` returns cert path from env vars, `configure_ssl()` sets up requests library at startup |
| `core.py` | `Zimmporter` class: search via ytmusicapi, download via yt-dlp + `billiard.Pool`, AAC conversion, per-song download methods returning status dicts |
| `postprocessors.py` | yt-dlp postprocessors: `EnrichMeta` (ID3 + MP4 tags + cover embed), `UploadToS3` (S3 upload + file cleanup) |
| `ytdlp_logger.py` | Custom logger with per-song `[album/song]` context injected into every log line |


### `api/` - FastAPI Application

| Module | Purpose |
|--------|---------|
| `app.py` | FastAPI instance, startup SSL config + DB init, `/health` endpoint (always returns HTTP 200 with `"ok"` or `"degraded"` status to report partial outages without breaking callers), CORS middleware |
| `models.py` | Pydantic request/response models for OpenAPI schema generation |
| `routes/search.py` | `GET /search` — synchronous ytmusicapi query with Valkey caching (5 min TTL, db 2), supports `limit` parameter |
| `routes/download.py` | `POST /download/{album\|playlist}` — DB Job row first, then Celery task dispatch |
| `routes/jobs.py` | `GET /jobs/{id}` and `GET /jobs` — job/song status from MariaDB |

### `db/` - Database Layer

| Module | Purpose |
|--------|---------|
| `engine.py` | SQLAlchemy sync engine, `get_session()` context manager (commit/rollback), env var configuration |
| `models.py` | ORM models: `Job` (download batches), `Song` (per-song status, FK to Job) |

### `tasks/` - Celery Workers

| Module | Purpose |
|--------|---------|
| `celery_app.py` | Celery instance, Valkey broker/backend, JSON serialization, late ACKs; calls `configure_ssl()` at module load for worker processes |
| `download.py` | `download_album` / `download_playlist` tasks: billiard.Pool concurrency, per-song DB updates |

## Concurrency Model

Two levels of parallelism:

1. **Celery level**: Each Celery worker process handles one download job (`worker_prefetch_multiplier=1`, `task_acks_late=True`).  Multiple worker processes can run in parallel.

2. **Pool level**: Within a single album/playlist, `billiard.Pool(concurrent)` spawns child processes for per-song downloads.  Each child runs `yt-dlp` with ffmpegAAC conversion and S3 upload.

```
Celery Worker Process
  |-- download_album task
       |-- Pool(4)
             |-- child 1: song 1 (yt-dlp -> ffmpeg -> S3)
             |-- child 2: song 2
             |-- child 3: song 3
             |-- child 4: song 4
             |-- child 1: song 5  (after child 1 finishes song 1)
            |-- ...
```

**Why `billiard.Pool` over `multiprocessing.Pool`?**  Celery vendors billiard as its multiprocessing fork.  Using stdlib `multiprocessing` inside a Celery worker causes state corruption and process hangs.

**Logger re-initialization**: Logger state is lost after `os.fork()`.  Each pool child re-creates a `YTDLPLogger` via the `_init_worker` initializer.

## Database Schema

### `jobs` Table

| Column | Type | Description |
|--------|------|-------------|
| `id` | `INT AUTO_INCREMENT` | Primary key, doubles as Celery `task_id` |
| `job_type` | `ENUM(album, playlist)` | Job type |
| `browse_id` | `VARCHAR(512)` | Comma-separated YT Music browse IDs |
| `status` | `ENUM(pending, running, success, failed)` | Job state |
| `message` | `TEXT` | Human-readable progress |
| `error` | `TEXT` | Failure reason |
| `current_album` | `VARCHAR(512)` | Album currently being processed |
| `album_progress` | `INT` | 1-based index in batch |
| `total_albums` | `INT` | Total in batch |
| `current_song` | `INT` | Songs done for current album |
| `total_songs` | `INT` | Total songs in current album |
| `created_at` | `DATETIME(3)` | UTC creation time |
| `updated_at` | `DATETIME(3)` | UTC last update |

### `songs` Table

| Column | Type | Description |
|--------|------|-------------|
| `id` | `INT AUTO_INCREMENT` | Primary key |
| `job_id` | `INT (FK -> jobs.id)` | Parent job, CASCADE DELETE |
| `title` | `VARCHAR(512)` | Song title |
| `artist` | `VARCHAR(512)` | Artist name or `"playlists"` |
| `album` | `VARCHAR(512)` | Album/playlist title |
| `track_number` | `INT NULL` | Track index (NULL for playlists) |
| `status` | `ENUM(pending, downloading, success, failed)` | Song state |
| `s3_path` | `VARCHAR(1024)` | S3 object key after upload |
| `error` | `TEXT` | Failure reason |
| `created_at` | `DATETIME(3)` | UTC creation time |

**Cascade behavior**: Deleting a `Job` automatically deletes all related `Song` rows.

## Environment Variables

### Database (MariaDB)

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_HOST` | `localhost` | MariaDB hostname |
| `DB_PORT` | `3306` | MariaDB port |
| `DB_USER` | `root` | Database username |
| `DB_PASS` | `root` | Database password |
| `DB_NAME` | `zimmporter` | Database name |

### Celery (Valkey Broker)

| Variable | Default | Description |
|----------|---------|-------------|
| `CELERY_BROKER` | `redis://localhost:6379/0` | Broker URL (redis scheme works with Valkey) |
| `CELERY_BACKEND` | `redis://localhost:6379/1` | Result backend URL |

**Valkey database usage:** db 0 = Celery broker, db 1 = Celery backend, db 2 = search result cache.

### S3 (AWS-Compatible Storage)

| Variable | Default | Description |
|----------|---|---|
| `AWS_ENDPOINT_URL` | *(required)* | S3 endpoint (host:port) |
| `AWS_ACCESS_KEY_ID` | *(required)* | S3 access key |
| `AWS_SECRET_ACCESS_KEY` | *(required)* | S3 secret key |
| `AWS_BUCKET` | *(required)* | Target bucket name |
| `AWS_USE_SSL` | `true` | Enable HTTPS for S3 connections |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region for S3 |

### SSL / TLS

| Variable | Default | Description |
|----------|---------|-------------|
| `CA_CERT` | *(unset)* | Path to a PEM file with private CA certificate(s) |
| `REQUESTS_CA_BUNDLE` | *(unset)* | Same as `CA_CERT`; used by requests, yt-dlp, ytmusicapi internally |

When set, all HTTPS clients (requests for thumbnails, boto3/S3, yt-dlp, ytmusicapi) trust the private CA. If the file doesn't exist, a warning is logged and system certs are used as fallback. Set both `CA_CERT` and `REQUESTS_CA_BUNDLE` to the same mounted path.

### Authentication (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `USE_SIMPLE_AUTH` | `false` | Enable API key auth (`X-API-Key` header) on all endpoints except `/health` |
| `API_KEY` | *(none)* | Expected secret value for `X-API-Key` header |
| `USE_OIDC` | `false` | Enable OIDC Bearer token auth (`Authorization: Bearer`) on all endpoints except `/health` |
| `OIDC_ISSUER_URL` | *(none)* | OIDC issuer URL for JWKS key resolution |
| `OIDC_CLIENT_ID` | *(none)* | Expected `aud` claim in the Bearer token |

Both auth methods can be enabled independently; providing valid credentials for either method suffices.

## Retention Policy

Jobs and songs older than 30 days are automatically deleted on every successful `/health` check.  This prevents the database from accumulating stale data.  The cleanup runs via `DELETE ... WHERE created_at < cutoff` with `synchronize_session=False` for performance.

## S3 Object Path Convention

Uploaded files follow the pattern:

```
{artist}/{album}/{title}.m4a
```

The `/` character in any component is replaced with `-` to produce valid S3 keys.  Example:

```
Aurora/Running With The Wolves/The Midningsonne.m4a
```

## File Layout

```
zimmporter-master/
├── zimmporter/              # Core library
│   ├── __init__.py          # Re-exports Zimmporter

│   ├── cert.py              # Private CA certificate configuration
│   ├── core.py              # Zimmporter class, download logic
│   ├── postprocessors.py    # yt-dlp postprocessors (metadata + S3)
│   └── ytdlp_logger.py      # Custom logger with album/song context
├── api/                     # FastAPI application
│   ├── __init__.py
│   ├── app.py               # FastAPI app, /health, 30-day cleanup
│   ├── models.py            # Pydantic models
│   └── routes/
│       ├── __init__.py
│       ├── search.py        # GET /search
│       ├── download.py      # POST /download/{album|playlist}
│       └── jobs.py          # GET /jobs/{id}, GET /jobs
├── db/                      # Database layer
│   ├── __init__.py
│   ├── engine.py            # SQLAlchemy engine, session manager
│   └── models.py            # Job/Song ORM models
├── tasks/                   # Celery workers
│   ├── __init__.py
│   ├── celery_app.py        # Celery configuration
│   └── download.py          # download_album / download_playlist tasks
├── .github/                 # GitHub Actions
│   └── workflows/
│       └── build.yml        # Test + build + push to GHCR
├── docs/                    # Documentation
│   ├── api.md
│   └── architecture.md
├── tests/                   # pytest test suite (88 tests)
│   ├── conftest.py          # Fixtures: SQLite DB, test client, module-level mocks
│   ├── mock_data.py         # Mock ytmusicapi response data
│   ├── test_cert.py         # CA cert configuration
│   ├── test_core.py         # Core search + download logic
│   ├── test_health.py       # GET /health
│   ├── test_postprocessors.py  # EnrichMeta, UploadToS3
│   ├── test_routes_download.py # POST /download
│   ├── test_routes_jobs.py  # GET /jobs, /retry
│   └── test_routes_search.py # GET /search
├── docker-compose.yml       # Local dev: api, worker, valkey, mariadb
├── Dockerfile               # Multi-stage build
├── requirements.txt         # Python dependencies
├── AGENTS.md                # Session notes for AI assistants

```
