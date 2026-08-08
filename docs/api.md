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

# Inspect the configured yt-dlp cookies file (metadata only)
curl -s "http://localhost:8000/cookies" | jq .

# Upload a Netscape-format cookies file (enables age-restricted downloads)
curl -X POST "http://localhost:8000/cookies" -F "file=@cookies.txt" | jq .
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

### Thumbnail Proxy

| Variable | Default | Description |
|----------|---------|-------------|
| `API_PROXY_FETCH` | `""` | Set to `"true"` to proxy thumbnail fetches through the API; thumbnails are embedded as base64 data URIs in search results |

### S3 Library Index

| Variable | Default | Description |
|----------|---------|-------------|
| `INDEX_INTERVAL_MINUTES` | `30` | How often (minutes) the API pod dispatches the periodic library index scan (min `1`) |
| `INDEX_SOURCE` | `s3` | Which library sources feed the `available_albums` index: `s3` (S3 prefix scan), `navidrome`, or `both` |
| `NAVIDROME_URL` | — | Base URL of the Navidrome server (worker-side; required when `INDEX_SOURCE` uses navidrome) |
| `NAVIDROME_USER` | — | Subsonic API username for Navidrome (worker-side) |
| `NAVIDROME_PASS` | — | Subsonic API password for Navidrome (worker-side) |

### Cookies (YouTube auth)

The cookies file uploaded via `POST /cookies` is stored in **Valkey** (database 3, alongside the staleness flag) — no shared file volume is required. The API writes it on upload; workers read it on each download job and write a local writable copy for yt-dlp.

| Variable | Default | Description |
|----------|---------|-------------|
| *(none)* | — | No environment configuration; content lives in Valkey under `zimmporter:cookies:content` / `zimmporter:cookies:meta` |

### POT Provider (BgUtils)

| Variable | Default | Description |
|----------|---------|-------------|
| `POT_PROVIDER_URL` | — | HTTP URL of a BgUtils yt-dlp POT provider (e.g. `http://bgutil-provider:4416`); unset disables PO-token extraction |

### Stalled Jobs

| Variable | Default | Description |
|----------|---------|-------------|
| `JOB_STALLED_TIMEOUT` | `5` | Minutes after which a stuck `pending`/`running` job is auto-failed (recovers jobs orphaned by a worker crash, e.g. OOM/SIGKILL) |

### Authentication (optional)

Two optional authentication methods, controlled by environment variables:

- **API key** — Set `USE_SIMPLE_AUTH=true` and configure `API_KEY`. Clients must send an `X-API-Key` header matching the value.
- **OIDC Bearer token** — Set `USE_SOCIAL_LOGIN=true` and configure `OIDC_ISSUER_URL` + `OIDC_CLIENT_ID`. Clients must send an `Authorization: Bearer <JWT>` header validated against the issuer's JWKS endpoint.
- **GitHub Bearer token** — Set `USE_SOCIAL_LOGIN=true` and `GITHUB_CLIENT_ID`. Clients must send an `Authorization: Bearer <token>` header validated against the GitHub API.

The `/health` and `/thumbnail` endpoints are always open. If multiple methods are enabled, **any** suffices.

---

## Endpoints

### Health Check

```
GET /health
```

Validates all backend components: Valkey (broker), Celery workers, and MariaDB.  Automatically purges jobs older than `JOB_RETENTION_DAYS` (default `0` — never purge) on every healthy check. Also fails jobs stuck in `pending`/`running` longer than `JOB_STALLED_TIMEOUT` minutes (default `5`) — this recovers jobs orphaned when a worker crashed (e.g. OOM / SIGKILL), since their exception handler never ran. Always returns HTTP 200 with `"status": "ok"` or `"degraded"`. The endpoint never returns HTTP 503 for component-level failures — it always reports the degraded state so callers can inspect which components are down without treating a partial outage as an error response.

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
GET /search?q=<query>&type=<albums|featured_playlists|community_playlists>&limit=10
```

Searches YouTube Music and returns structured result dicts.  Use the returned `browseId` to trigger a download.  Results are cached in Valkey for 5 minutes; subsequent requests for the same query are served from cache.

**Query Parameters:**

| Param | Type | Default | Constraints | Description |
|-------|------|---------|-------------|-------------|
| `q` | string | *(required)* | — | Free-text search query |
| `type` | string | `albums` | — | Result type: `albums`, `featured_playlists`, or `community_playlists` |
| `limit` | int | `10` | 1–50 | Number of results to return |

**Response (when `API_PROXY_FETCH` is not enabled):**
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
      "thumbnail": "https://lh3.googleusercontent.com/...",
      "available": false
    }
  ]
}
```

**Response (when `API_PROXY_FETCH=true`):**
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
      "thumbnail": "data:image/jpeg;base64,/9j/4AAQ...",
      "available": true
    }
  ]
}
```

Each result includes an `available` boolean flagging albums/playlists already present in the S3 library (matched by exact `browseId` or normalized artist+title, sourced from the `available_albums` table).

When `API_PROXY_FETCH=true`, the `thumbnail` field is a **base64 data URI** instead of a CDN URL. The API fetches thumbnails concurrently through its outbound proxy, caches them in Valkey db 3 for 24 hours, and embeds them directly in the response. The frontend can render these `<img src="data:...">` without any additional network requests.
```

---

### Thumbnail Proxy

```
GET /thumbnail?url=<url-encoded-cdn-url>
```

Fetches a thumbnail image from the upstream CDN through the API's outbound connection. Results are cached in Valkey db 3 for 24 hours. The endpoint is excluded from auth middleware so `<img>` tags can load thumbnails without authentication headers.

**Query Parameters:**

| Param | Type | Default | Constraints | Description |
|-------|------|---------|-------------|-------------|
| `url` | string | *(required)* | — | Full URL of the CDN thumbnail image (must be URL-encoded) |

**Response:** The raw image bytes with the original `Content-Type` header.

| Status | Condition |
|--------|-----------|
| `200` | Image returned (body is the raw bytes) |
| `400` | `url` parameter missing |
| `502` | Upstream CDN request failed |

**Response headers:**

| Header | Value | Description |
|--------|-------|-------------|
| `Content-Type` | `image/jpeg`, `image/webp`, etc. | Original content type from the CDN |
| `Cache-Control` | `public, max-age=86400` | Instructs browsers/CDNs to cache for 24 hours |
| `X-Cache` | `HIT` or `MISS` | Whether the response was served from Valkey cache |

**Configuration:**

| Env Var | Description |
|---------|-------------|
| `API_PROXY_FETCH` | Set to `true` to enable thumbnail proxy features (the `/thumbnail` endpoint works regardless) |

---

### Cookies

#### Get Cookie Status

```
GET /cookies
```

Returns metadata about the configured yt-dlp cookies file — never its contents.

**Response:**
```json
{
  "exists": true,
  "size": 4096,
  "cookie_count": 12,
  "domains": [".youtube.com", ".google.com"],
  "modified_at": "2026-08-01T10:00:00+00:00",
  "is_stale": false
}
```

| Field | Type | Description |
|-------|------|-------------|
| `exists` | bool | Whether a cookies file is present |
| `size` | int | File size in bytes |
| `cookie_count` | int | Number of parsed cookies |
| `domains` | array | Cookie domains (e.g. `.youtube.com`) |
| `modified_at` | string \| null | Last file modification timestamp |
| `is_stale` | bool | `true` when the backend detected the cookies are no longer valid |

#### Upload Cookies

```
POST /cookies
```

Multipart upload (field `file`) of a Netscape-format `cookies.txt`. Validates that the file parses and contains at least one `youtube.com` cookie, and is at most 2 MB. The content is stored in **Valkey** (via the cookie store), so running workers pick it up on their next download job without restart.

While stale, downloads run anonymously (bad cookies are skipped) and `GET /cookies` reports `is_stale: true`. Uploading a fresh file clears the flag.

| Status | Condition |
|--------|-----------|
| `200` | Cookies stored and applied |
| `400` | Missing/invalid file, no YouTube cookie, or larger than 2 MB |

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
  "requested_by": "user@example.com",
  "artist": "Artist Name",
  "album_name": "Album Name",
  "songs_downloaded": 2,
  "created_at": "2025-01-15T12:00:00+00:00",
  "updated_at": "2025-01-15T12:05:00+00:00",
  "songs": [
    {
      "id": 100,
      "title": "Track One",
      "artist": "Artist Name",
      "album": "Album Name",
      "track_number": 1,
      "status": "success",
      "s3_path": "Artist-Name/Album-Name/Track-One.m4a",
      "error": null
    }
  ]
}
```

Additional fields on `JobStatusResponse`:

| Field | Type | Description |
|-------|------|-------------|
| `requested_by` | string \| null | Name or sub of the OIDC user who requested the job (`null` for API-key or unauthenticated requests) |
| `artist` | string \| null | Artist name (`null` for playlists) |
| `album_name` | string \| null | Original album/playlist title (raw, unformatted) |
| `songs_downloaded` | int | Count of songs with status `"success"` |
| `created_at` | string \| null | ISO 8601 UTC timestamp of job creation |
| `updated_at` | string \| null | ISO 8601 UTC timestamp of last update |

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

When authenticated via Bearer token (OIDC user), only the requesting user's jobs are returned. Unauthenticated or API-key-authenticated requests return all jobs.

---

### Retry Job

```
POST /jobs/{job_id}/retry
```

Resets all failed songs in a job to `pending` (clearing their error messages) and re-dispatches the original Celery task. Also clears the job's own `error` field, so a stale failure message never resurfaces on a retried job. Ownership check applies: returns `403` if the job belongs to a different OIDC user.

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `job_id` | int | ID of the job to retry |

**Response:**
```json
{
  "job_id": 42,
  "status": "running"
}
```

**Errors:**

| Status | Condition |
|--------|-----------|
| `404` | Job not found |
| `400` | No failed songs to retry |
| `403` | Job belongs to a different OIDC user |

---

## Job Lifecycle

```
POST /download/{album|playlist}
    -> DB: Job row created (status=pending)
    -> Celery: task dispatched (task_id = job.id)

Celery worker picks up task
    -> DB: status = "running", message = "Started", error = NULL  (clears stale failure)
    -> For each album/playlist:
        -> Fetch tracks from ytmusicapi
        -> Insert Song rows (status=pending)
        -> Pool downloads songs in parallel
        -> Per-song: DB updated (success/failed, s3_path)
        -> Job progress updated (current_song / total_songs)
    -> On completion: status = "success", error = NULL
    -> On error: status = "failed", error = message
```

A job's `error` is cleared whenever it starts running again (fresh task, retry, or failed-song retry), so a previously failed job that later succeeds never reports a stale error.

### Job Statuses

| Status | Meaning |
|--------|---------|
| `pending` | Job created, waiting for Celery worker |
| `running` | Worker is processing songs (or retrying after `POST /jobs/{job_id}/retry`) |
| `success` | All albums/playlists processed |
| `failed` | Unrecoverable error occurred (retryable via `POST /jobs/{job_id}/retry`) |

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
