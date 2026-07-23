# Zimmporter API Reference

Internal REST API for searching YouTube Music, queueing album/playlist downloads, and tracking job progress.  Served by a FastAPI + Uvicorn instance, backed by a Celery worker pool, MariaDB, and Valkey.

## Quick Start

```bash
# Search for an album (default 10 results)
curl -s "http://localhost:8000/search?q=Aurora&type=albums" | jq .

# Request more results
curl -s "http://localhost:8000/search?q=Aurora&type=albums&limit=25" | jq .

# Queue album download (returns job_id)
curl -X POST "http://localhost:8000/download/album" \
  -H "Content-Type: application/json" \
  -d '{"id":"MPREb_m9b9z4z4z4z","concurrent":4}' | jq .

# Queue playlist download
curl -X POST "http://localhost:8000/download/playlist" \
  -H "Content-Type: application/json" \
  -d '{"id":"VLx_xxxxxxx","concurrent":4}' | jq .

# Poll job status
curl -s "http://localhost:8000/jobs/1" | jq .

# List recent jobs (paginated)
curl -s "http://localhost:8000/jobs?limit=20&offset=0" | jq .

# Health check (always returns 200; status is "ok" or "degraded")
curl -s "http://localhost:8000/health" | jq .
```

## Configuration

### Private CA Certificate

To trust a private CA certificate (e.g., for MinIO behind a corporate proxy or self-signed TLS), set `CA_CERT` and `REQUESTS_CA_BUNDLE` to the path of a PEM file mounted into the container. Both env vars should point to the same file. The cert is used by all HTTPS clients: requests, urllib3/MinIO, yt-dlp, and ytmusicapi.

```yaml
# docker-compose.yml (add to api and worker services)
environment:
  - CA_CERT=/etc/ssl/certs/private-ca.crt
  - REQUESTS_CA_BUNDLE=/etc/ssl/certs/private-ca.crt
volumes:
  - ./my-ca.crt:/etc/ssl/certs/private-ca.crt:ro
```

If the configured file does not exist, a warning is logged and the clients fall back to the system CA bundle. Without `CA_CERT` set, all clients use their default system certificates.

### Authentication (optional)

Set `REQUIRE_AUTH=true` to enforce API key authentication on all endpoints except `/health`. Clients must send an `X-API-Key` header matching the value of the `API_KEY` env var.

---

## Endpoints

### Health Check

```
GET /health
```

Validates all backend components: Valkey (broker), Celery workers, and MariaDB.  Automatically purges jobs older than 30 days on every healthy check. Always returns HTTP 200 with `"status": "ok"` or `"degraded"`. The endpoint never returns HTTP 503 for component-level failures — it always reports the degraded state so callers can inspect which components are down without treating a partial outage as an error response.

**All healthy (HTTP 200):**
```json
{
  "status": "ok",
  "components": {
    "api": "ok",
    "redis": "ok",
    "celery_worker": "ok",
    "mariadb": "ok"
  },
  "timestamp": "2025-01-15T12:00:00+00:00"
}
```

**Partial outage (HTTP 200, degraded):**
```json
{
  "status": "degraded",
  "components": {
    "api": "ok",
    "redis": "ok",
    "celery_worker": "no_workers_online",
    "mariadb": "ok"
  },
  "timestamp": "2025-01-15T12:00:00+00:00"
}
```

**Total outage (HTTP 200, degraded):**
```json
{
  "status": "degraded",
  "components": {
    "api": "ok",
    "redis": "error",
    "celery_worker": "error",
    "mariadb": "error"
  },
  "timestamp": "2025-01-15T12:00:00+00:00"
}
```
```

---

### Search

```
GET /search?q=<query>&type=<albums|playlists>&limit=10
```

Searches YouTube Music and returns structured result dicts.  Use the returned `browseId` to trigger a download.  Results are cached in Valkey for 5 minutes; subsequent requests for the same query are served from cache.

**Query Parameters:**

| Param | Type | Default | Constraints | Description |
|-------|------|---------|-------------|-------------|
| `q` | string | *(required)* | — | Free-text search query |
| `type` | string | `albums` | — | Result type: `albums` or `playlists` |
| `limit` | int | `10` | 1–50 | Number of results to return |

**Response:**
```json
{
  "results": [
    {
      "resultType": "album",
      "browseId": "MPREb_xxxxx",
      "title": "Album Title",
      "year": "2023",
      "type": "Album",
      "artist": ["Artist Name"],
      "thumbnail": "https://lh3.googleusercontent.com/..."
    }
  ]
}
```

Each result includes a `thumbnail` field with the URL of the largest available thumbnail image (or `null` if none).
```

---

### Download Album

```
POST /download/album
```

Queues one or more albums for download.  Creates a DB `Job` row, then dispatches a Celery task with matching `task_id`.  Returns immediately.

**Request Body:**
```json
{
  "id": "MPREb_xxx,MPREb_yyy",
  "concurrent": 4
}
```

| Field | Type | Default | Constraints | Description |
|-------|------|---------|-------------|-------------|
| `id` | string | *(required)* | — | Comma-separated album browse IDs |
| `concurrent` | int | `4` | 1–32 | Parallel downloads per album |

**Response:**
```json
{
  "job_id": 42,
  "status": "pending"
}
```

---

### Download Playlist

```
POST /download/playlist
```

Queues one or more playlists for download.  Same flow as album download.

**Request Body:**
```json
{
  "id": "VLx_xxxxx",
  "concurrent": 4
}
```

| Field | Type | Default | Constraints | Description |
|-------|------|---------|-------------|-------------|
| `id` | string | *(required)* | — | Comma-separated playlist browse IDs |
| `concurrent` | int | `4` | 1–32 | Parallel downloads per playlist |

**Response:**
```json
{
  "job_id": 43,
  "status": "pending"
}
```

---

### Get Job Status

```
GET /jobs/{job_id}
```

Returns the current status of a specific job with embedded per-song statuses.  Poll this endpoint to track download progress.

**Response:**
```json
{
  "job_id": 42,
  "job_type": "album",
  "browse_id": "MPREb_xxx",
  "status": "running",
  "message": "Processed 3/12 songs",
  "error": null,
  "current_album": "Artist - Album Name",
  "album_progress": 1,
  "total_albums": 1,
  "current_song": 3,
  "total_songs": 12,
  "songs": [
    {
      "id": 100,
      "title": "Track One",
      "artist": "Artist Name",
      "album": "Album Name",
      "track_number": 1,
      "status": "success",
      "minio_path": "Artist-Name/Album-Name/Track-One.m4a",
      "error": null
    }
  ]
}
```

---

### List Jobs

```
GET /jobs?limit=50&offset=0
```

Returns recent jobs newest-first, each with embedded song statuses.

**Query Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | `50` | Maximum jobs to return |
| `offset` | int | `0` | Number of jobs to skip |

**Response:** List of `JobStatusResponse` objects (same schema as `GET /jobs/{job_id}`).

---

## Job Lifecycle

```
POST /download/{album|playlist}
    -> DB: Job row created (status=pending)
    -> Celery: task dispatched (task_id = job.id)

Celery worker picks up task
    -> DB: status = "running", message = "Started"
    -> For each album/playlist:
        -> Fetch tracks from ytmusicapi
        -> Insert Song rows (status=pending)
        -> Pool downloads songs in parallel
        -> Per-song: DB updated (success/failed, minio_path)
        -> Job progress updated (current_song / total_songs)
    -> On completion: status = "success"
    -> On error: status = "failed", error = message
```

### Job Statuses

| Status | Meaning |
|--------|---------|
| `pending` | Job created, waiting for Celery worker |
| `running` | Worker is processing songs |
| `success` | All albums/playlists processed |
| `failed` | Unrecoverable error occurred |

### Song Statuses

| Status | Meaning |
|--------|---------|
| `pending` | Row inserted, download not yet started |
| `downloading` | Currently being downloaded |
| `success` | Downloaded, converted, metadata embedded, uploaded to MinIO |
| `failed` | Download or upload failed (see `error` field) |

---

## OpenAPI Schema

FastAPI auto-generates an OpenAPI 3.0 schema at:

```
GET /openapi.json
```

An interactive Swagger UI is available at:

```
GET /docs
```

A ReDoc UI is available at:

```
GET /redoc
```
