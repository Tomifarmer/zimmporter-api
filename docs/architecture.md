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
| `cookie_health.py` | Cookie staleness helpers: `mark_stale()`, `is_stale()`, `clear_stale()` backed by a Valkey flag (db 3) |
| `postprocessors.py` | yt-dlp postprocessors: `EnrichMeta` (ID3 + MP4 tags + cover art + lyrics + genre embed), `UploadToS3` (S3 upload + file cleanup) |
| `ytdlp_logger.py` | Custom logger with per-song `[album/song]` context injected into every log line |
| `lyrics.py` | Best-effort lyrics lookup against LRCLIB (`fetch_lyrics`), embedded as `USLT` (ID3) / `©lyr` (MP4) |
| `genre.py` | Best-effort album genre lookup against the iTunes Search API (`lookup_genre`), embedded as `TCON`/`©gen` |


### `api/` - FastAPI Application

| Module | Purpose |
|--------|---------|
| `app.py` | FastAPI instance, startup SSL config + DB init, `/health` endpoint (always returns HTTP 200 with `"ok"` or `"degraded"` status to report partial outages without breaking callers), stalled-job cleanup (`JOB_STALLED_TIMEOUT`), CORS middleware |
| `scheduler.py` | Periodic library index dispatcher (`INDEX_INTERVAL_MINUTES`, default 30; `INDEX_SOURCE` selects `s3`/`navidrome`/`both`) — runs inside the API pod, guarded by a Valkey lock (db 4), no Celery beat container |
| `models.py` | Pydantic request/response models for OpenAPI schema generation |
| `routes/search.py` | `GET /search` — synchronous ytmusicapi query with Valkey caching (5 min TTL, db 2), supports `limit` and `type` (albums/featured_playlists/community_playlists); enriches results with an `available` flag from the `available_albums` index; when `API_PROXY_FETCH=true` fetches and embeds thumbnails as base64 data URIs |
| `routes/thumbnail.py` | `GET /thumbnail?url=` — proxies thumbnail from CDN through the API, cached in db 3 (24 h TTL), excluded from auth middleware |
| `routes/cookies.py` | `GET /cookies` (metadata) and `POST /cookies` (multipart upload) manage the yt-dlp cookies file; validated, written atomically into `COOKIE_DIR`; contents never exposed |
| `routes/download.py` | `POST /download/{album\|playlist}` — DB Job row first, then Celery task dispatch |
| `routes/jobs.py` | `GET /jobs/{id}` and `GET /jobs` — job/song status from MariaDB |

### `db/` - Database Layer

| Module | Purpose |
|--------|---------|
| `engine.py` | SQLAlchemy sync engine, `get_session()` context manager (commit/rollback), env var configuration |
| `models.py` | ORM models: `Job` (download batches), `Song` (per-song status, FK to Job), `AvailableAlbum` (S3 library index, keyed by `browse_id`) |

### `tasks/` - Celery Workers

| Module | Purpose |
|--------|---------|
| `celery_app.py` | Celery instance, Valkey broker/backend, JSON serialization, late ACKs; calls `configure_ssl()` at module load for worker processes; pre-warms yt-dlp plugins serially on worker process init (avoids first-import race) |
| `download.py` | `download_album` / `download_playlist` tasks: billiard.Pool concurrency, per-song DB updates; upserts downloaded items into `available_albums` |
| `index.py` | `index_albums` task: scans the S3 bucket (`{artist}/{album}/` prefixes) and reconciles the `available_albums` table (upsert + prune) |

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

### `available_albums` Table

Mirrors the current S3 library contents and drives the `available` flag on search results.

| Column | Type | Description |
|--------|------|-------------|
| `browse_id` | `VARCHAR(512)` | YT Music browse ID of the album/playlist |
| `title` | `VARCHAR(512)` | Title |
| `artist` | `VARCHAR(512)` | Artist name |
| `album` | `VARCHAR(512)` | Album/playlist title |
| `updated_at` | `DATETIME(3)` | UTC last update |

Populated by download tasks (exact `browse_id`) and reconciled by the periodic `index_albums` S3 scan (upsert + prune of entries no longer in S3).

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

**Valkey database usage:** db 0 = Celery broker, db 1 = Celery backend, db 2 = search result cache (5 min TTL) + available-albums index reads, db 3 = thumbnail image cache (24 h TTL) + cookie staleness flag, db 4 = S3 library index dispatch lock.

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

### S3 Library Index

The `available_albums` table is kept in sync with the backend library. The
index source is selected via `INDEX_SOURCE` (`s3` default, `navidrome`, or
`both`):

- **Recording** — download tasks (`tasks/download.py`) upsert every successfully downloaded album/playlist with its exact YT Music `browse_id`.
- **Periodic scan** — `api/scheduler.py` runs inside the API pod and dispatches `tasks.index_albums` (S3) and/or `tasks.index_navidrome` (Navidrome) every `INDEX_INTERVAL_MINUTES` (default 30, min 1). The S3 task scans the bucket (`{artist}/{album}/` prefixes); the Navidrome task queries the server's Subsonic API (`getAlbumList2`, via `zimmporter/navidrome.py`) for the albums it has indexed. Both upsert found items and prune entries no longer present. A Valkey lock (db 4) deduplicates dispatch across multiple API replicas — no Celery beat container is needed.
- **Search enrichment** — `GET /search` matches each result against the index by `browse_id` (or normalized artist+title) after the cache read, so results stay fresh.

### Cookies (YouTube auth)

Age-restricted downloads can be authenticated with a shared yt-dlp cookies file:

- **Upload** — `POST /cookies` accepts a Netscape-format `cookies.txt` (multipart, field `file`), validates it (parses + ≥1 `youtube.com` cookie, ≤2 MB), and writes it atomically into `COOKIE_DIR`. `GET /cookies` exposes metadata only (`exists`, `size`, `cookie_count`, `domains`, `modified_at`, `is_stale`).
- **Stale detection** — when yt-dlp reports bot checks, invalid cookies, or cookie rotation during a download, the worker flags the cookies via `zimmporter/cookie_health.py` (Valkey db 3). Downloads then run anonymously until a fresh upload clears the flag.
- **Workers** — each job re-applies the cookie config from `YTDLP_COOKIEFILE` without a restart (`tasks/download.py::_refresh_cookie_config`).

### POT Provider (BgUtils)

When `POT_PROVIDER_URL` is set (e.g. `http://bgutil-provider:4416`), yt-dlp requests PO (Proof of Origin) tokens from a [BgUtils yt-dlp POT provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) server to bypass YouTube bot checks (`YTDL_OPTS["extractor_args"]["youtubepot-bgutilhttp"]`). A `bgutil-provider` service is included in docker-compose and the Helm chart.

### Thumbnail Proxy

When `API_PROXY_FETCH=true` is set, the API proxies thumbnail images:

- **Search route**: Thumbnails are fetched concurrently (up to 10 at once), cached in Valkey db 3, and embedded as base64 data URIs in the response
- **Standalone endpoint**: `GET /thumbnail?url=` returns raw image bytes (excluded from auth)
- **Config**: `API_PROXY_FETCH` env var controls the feature; `_MAX_THUMB_SIZE` (10 MB) caps image size

### Metadata Enrichment (Lyrics & Genre)

YouTube Music never exposes lyrics or a real genre, so the worker computes both at download time and embeds them into standard audio tags:

- **Lyrics** — when `ENABLE_LYRICS=true` (default), `zimmporter/lyrics.py` queries the [LRCLIB API](https://lrclib.net) (`GET /api/get` with an `/api/search` fallback) per **album** song and strips LRC timestamps. Only plain lyrics are embedded (`USLT` for ID3/mp3, `©lyr` for MP4/m4a), because the downloaded audio is a YouTube clip whose timing cannot be matched by synced timestamps. Playlist downloads skip lyrics (artist is unknown there).
- **Genre** — when `ENABLE_GENRE=true` (default), `zimmporter/genre.py` looks the album genre up on the iTunes Search API (`zimmporter/genre.py:59`) before embedding `primaryGenreName` as `TCON` (ID3) or `©gen` (MP4). Files with no resolved genre have any stale genre tag cleared (`zimmporter/postprocessors.py:61`).

Both lookups are **best-effort**: misses, timeouts, HTTP errors, or disabled lookups return `None` and never fail or block the download. The worker logs each lookup (`Genre lookup: <artist> - <album> -> <genre>`, `Lyrics fetched for <artist> - <title>`), so a missing tag is diagnosable from worker logs.

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_LYRICS` | `true` | Set to `"false"` to disable best-effort lyrics lookup/embedding (LRCLIB) |
| `LRCLIB_BASE_URL` | `https://lrclib.net/api` | LRCLIB endpoint override |
| `ENABLE_GENRE` | `true` | Set to `"false"` to disable iTunes album genre lookup |
| `ITUNES_LOOKUP_LIMIT` | `3` | Number of candidate albums to inspect per genre lookup |

### Authentication (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `USE_SIMPLE_AUTH` | `false` | Enable API key auth (`X-API-Key` header) on all endpoints except `/health` and `/thumbnail` |
| `API_KEY` | *(none)* | Expected secret value for `X-API-Key` header |
| `USE_SOCIAL_LOGIN` | `false` | Enable social login Bearer token auth (OIDC/GitHub) on all endpoints except `/health` |
| `OIDC_ISSUER_URL` | *(none)* | OIDC issuer URL for JWKS key resolution |
| `OIDC_CLIENT_ID` | *(none)* | Expected `aud` claim in the Bearer token |

Both auth methods can be enabled independently; providing valid credentials for either method suffices.

### Index / Cookies / POT / Stalled Jobs

| Variable | Default | Description |
|----------|---------|-------------|
| `INDEX_INTERVAL_MINUTES` | `30` | How often (minutes) the API pod dispatches the periodic library index scan (min `1`) |
| `INDEX_SOURCE` | `s3` | Which library sources feed the `available_albums` index: `s3`, `navidrome`, or `both` |
| `NAVIDROME_URL` | *(none)* | Base URL of the Navidrome server (worker-side; required when `INDEX_SOURCE` uses navidrome) |
| `NAVIDROME_USER` | *(none)* | Subsonic API username for Navidrome (worker-side) |
| `NAVIDROME_PASS` | *(none)* | Subsonic API password for Navidrome (worker-side) |
| `COOKIE_DIR` | `/var/zimmporter/cookies` | Directory holding the shared yt-dlp cookies file (written by `POST /cookies`) |
| `YTDLP_COOKIEFILE` | *(none)* | Worker-side path to the cookies file used by yt-dlp |
| `POT_PROVIDER_URL` | *(none)* | HTTP URL of a BgUtils yt-dlp POT provider (unset disables PO-token extraction) |
| `JOB_STALLED_TIMEOUT` | `5` | Minutes after which a stuck `pending`/`running` job is auto-failed during `/health` |

## Retention Policy

Jobs and songs are automatically deleted after the number of days set via the `JOB_RETENTION_DAYS` environment variable (default `0` — never purge).  The cleanup runs on every successful `/health` check to prevent the database from accumulating stale data.  The cleanup runs via `DELETE ... WHERE created_at < cutoff` with `synchronize_session=False` for performance.

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
│   ├── cookie_health.py     # Cookie staleness flag helpers
│   ├── postprocessors.py    # yt-dlp postprocessors (metadata + S3)
│   └── ytdlp_logger.py      # Custom logger with album/song context
├── api/                     # FastAPI application
│   ├── __init__.py
│   ├── app.py               # FastAPI app, /health, job retention + stalled-job cleanup
│   ├── scheduler.py         # Periodic S3 library index dispatcher
│   ├── models.py            # Pydantic models
│   └── routes/
│       ├── __init__.py
│       ├── search.py        # GET /search (+ available flag, base64 thumbnails)
│       ├── download.py      # POST /download/{album|playlist}
│       ├── jobs.py          # GET /jobs/{id}, GET /jobs
│       ├── cookies.py       # GET/POST /cookies (yt-dlp cookie management)
│       └── thumbnail.py     # GET /thumbnail (proxy)
├── db/                      # Database layer
│   ├── __init__.py
│   ├── engine.py            # SQLAlchemy engine, session manager
│   └── models.py            # Job/Song/AvailableAlbum ORM models
├── tasks/                   # Celery workers
│   ├── __init__.py
│   ├── celery_app.py        # Celery configuration
│   ├── download.py          # download_album / download_playlist tasks
│   └── index.py             # index_albums S3 library scan task
├── .github/                 # GitHub Actions
│   └── workflows/
│       └── build.yml        # Test + build + push to GHCR
├── docs/                    # Documentation
│   ├── api.md
│   └── architecture.md
├── tests/                   # pytest test suite (150 test functions)
│   ├── conftest.py          # Fixtures: SQLite DB, test client, module-level mocks
│   ├── mock_data.py         # Mock ytmusicapi response data
│   ├── test_cert.py         # CA cert configuration
│   ├── test_core.py         # Core search + download logic
│   ├── test_health.py       # GET /health
│   ├── test_index.py        # S3 library index task
│   ├── test_auth.py         # Auth middleware
│   ├── test_postprocessors.py  # EnrichMeta, UploadToS3
│   ├── test_routes_cookies.py  # GET/POST /cookies
│   ├── test_routes_download.py # POST /download
│   ├── test_routes_jobs.py  # GET /jobs, /retry
│   ├── test_routes_search.py  # GET /search
│   └── test_routes_thumbnail.py # GET /thumbnail
├── docker-compose.yml       # Local dev: api, worker, valkey, mariadb, bgutil-provider
├── Dockerfile               # Multi-stage build
├── requirements.txt         # Python dependencies
├── AGENTS.md                # Session notes for AI assistants

```
