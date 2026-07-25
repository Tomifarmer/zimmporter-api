# End-to-End Test

Quick integration test that exercises the full download pipeline: search, queue a download, poll progress, and verify the uploaded files in S3.

> This is the **integration-level** test. For unit/component tests run `uv run python -m pytest tests/` (71 tests, no external services required).

## Prerequisites

The following tools must be installed on the test machine:

| Tool | Purpose |
|------|---------|
| `curl` | HTTP requests to the API |
| `jq` | JSON parsing |
| `mc` | S3 client for file verification |
| `minio` | S3 server (if running locally) |

**Services must be running** — the script assumes `docker compose up -d` has already been executed (or equivalent):

- FastAPI on `http://localhost:8000`
- Celery worker(s)
- Valkey
- MariaDB
- S3 (if using a local instance)

## Usage

```bash
export AWS_SECRET_ACCESS_KEY="your_secret_key_here"
./e2e_test.sh "Aurora"
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `API_URL` | `http://localhost:8000` | API endpoint |
| `AWS_ENDPOINT_URL` | *(required)* | S3 endpoint host:port |
| `AWS_ACCESS_KEY_ID` | *(required)* | S3 access key |
| `AWS_SECRET_ACCESS_KEY` | *(required)* | S3 secret key |
| `AWS_BUCKET` | *(required)* | Bucket name |

## What the Script Does

```
$ ./e2e_test.sh "Aurora"

Checking prerequisites...
  ✓ API is up (health OK)

Searching for 'Aurora' ...
  ✓ Found album: Aurora - Runaway (MPREb_5tBw6KxMkqF)

Queuing download ...
  ✓ Job 7 queued.

Polling job 7 every 1s (timeout 900s) ...
  [████████████████████████░░░░]  8/12 songs, album 1/1 - Processed 8/12 songs
  ✓ Job finished with status: success

────────────────────────────────────────────────────────────
Job 7
  Type:       album
  Status:     success
────────────────────────────────────────────────────────────

  Songs: 12 success, 0 failed (12 total)

  TITLE                            STATUS       TRACK S3 PATH
  -------                          ------       ----- ----------
  1. Don't Know You                success      1     Aurora/Runaway/1 - Don't Know You.m4a
  2. Runaway                        success      2     Aurora/Runaway/2 - Runaway.m4a
  ...

────────────────────────────────────────────────────────────
S3 Verification
────────────────────────────────────────────────────────────
  ✓ mc alias configured

  S3 PATH                         S3 STATUS    SIZE
  ----------                       ---------    ----
  ✓ 1. Don't Know You              exists       5.2M
  ✓ 2. Runaway                     exists       4.8M
  ...

────────────────────────────────────────────────────────────
  E2E TEST PASSED
────────────────────────────────────────────────────────────
```

The flow is:

1. **Prerequisite check** — verifies `curl`, `jq`, `mc` are installed and the API health endpoint responds 200 (waits up to 30s).
2. **Search** — hits `GET /search?q=Aurora&type=albums`, extracts the first album's `browseId`.
3. **Download** — posts `POST /download/album` with the `browseId`, captures the returned `job_id`.
4. **Poll** — hits `GET /jobs/{job_id}` every second, showing a progress bar with song count, album progress, and status message. Times out after 15 minutes.
5. **Result table** — prints a per-song table with title, status, track number, and S3 path. Failed songs show their error message.
6. **S3 verification** — configures an `mc` alias, then runs `mc stat` on every song's S3 path to confirm the file exists in the bucket.

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Fully passed — job succeeded, all songs present in S3 |
| `1` | Job failed or S3 verification completely failed |
| `2` | Partial — job completed but one or more songs failed |

## Troubleshooting

### "API not reachable after 30s"

Ensure `docker compose up -d` has finished starting. Check logs:

```bash
docker compose logs api --tail=20
```

### S3 verification shows MISSING

The songs may still be uploading. Re-run the script, or manually check:

```bash
mc alias set e2etestminio "${AWS_ENDPOINT_URL}" "${AWS_ACCESS_KEY_ID}" "${AWS_SECRET_ACCESS_KEY}" --api s3v4
mc stat e2etestminio/musics/Aurora/Runaway/Runaway.m4a
```

### Internal CA cert errors with `mc`

If the S3 endpoint uses a self-signed or internal CA certificate, `mc` may need explicit CA config:

```bash
# If mc warns about an invalid CA:
mc admin config set e2etestminio tls.cert_file /etc/ssl/certs/my-ca.pem
```

Or set the `MC_INSECURE_SKIP_VERIFY` environment variable:

```bash
export MC_INSECURE_SKIP_VERIFY=true
```
