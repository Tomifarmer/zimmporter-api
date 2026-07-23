#!/usr/bin/env bash
set -euo pipefail

# ─── Load .env if present ─────────────────────────────────────────────────────
if [[ -f ".env.e2e" ]]; then
    set -a
    source ./.env.e2e
    set +a
fi

# ─── Configuration ────────────────────────────────────────────────────────────
API_URL="${API_URL:-http://localhost:8000}"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-}"
MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-}"
MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-}"
MINIO_BUCKET="${MINIO_BUCKET:-}"
MINIO_USE_SSL="${MINIO_USE_SSL:-true}"
POLL_INTERVAL=1
POLL_TIMEOUT=900       # 15 minutes max
ALIAS_NAME="e2etestminio"

# ─── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

OK="${GREEN}✓${NC}"
FAIL="${RED}✗${NC}"
WAIT="${YELLOW}◌${NC}"

# ─── Helpers ──────────────────────────────────────────────────────────────────
die() { printf "${RED}[ERROR]${NC} %s\n" "$*" >&2; exit 1; }
info() { printf "${CYAN}[INFO]${NC}  %s\n" "$*"; }
pass() { printf "${GREEN}[ OK ]${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}[WARN]${NC} %s\n" "$*"; }
fail_line() { printf "${RED}[FAIL]${NC} %s\n" "$*"; }

# ─── Usage ────────────────────────────────────────────────────────────────────
if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <search-query>"
    echo ""
    echo "E2E test: search an album, download it, poll job status, verify MinIO upload."
    echo ""
    echo "Environment overrides:"
    echo "  API_URL          API endpoint          (default: http://localhost:8000)"
    echo "  MINIO_ENDPOINT   MinIO host:port       (required!)"
    echo "  MINIO_ACCESS_KEY MinIO access key      (required!)"
    echo "  MINIO_SECRET_KEY MinIO secret key      (required!)"
    echo "  MINIO_BUCKET     MinIO bucket name     (required!)"
    exit 1
fi

SEARCH_QUERY="$1"

# ─── 1. Prerequisite checks ──────────────────────────────────────────────────
info "Checking prerequisites..."

for cmd in curl jq mc; do
    command -v "$cmd" &>/dev/null || die "'$cmd' not found — install it first."
done

if [[ -z "$MINIO_SECRET_KEY" ]]; then
    die "MINIO_SECRET_KEY is empty — set it (or put a key in .env and source it)."
fi

info "Waiting for API at ${API_URL}/health ..."
HEALTH_DEADLINE=$((SECONDS + 30))
while (( SECONDS < HEALTH_DEADLINE )); do
    HEALTH_CODE=$(curl -so /dev/null -w "%{http_code}" "${API_URL}/health" 2>/dev/null || true)
    if [[ "$HEALTH_CODE" == "200" ]]; then
        pass "API is up (health OK)"
        break
    fi
    sleep "$POLL_INTERVAL"
done || true

if [[ "$HEALTH_CODE" != "200" ]]; then
    die "API not reachable after 30s (got HTTP $HEALTH_CODE)."
fi

# ─── 2. Search ────────────────────────────────────────────────────────────────
info "Searching for '${BOLD}${SEARCH_QUERY}${NC}' ..."

SEARCH_JSON=$(curl -sf "${API_URL}/search?q=${SEARCH_QUERY}&type=albums")
ALBUM_COUNT=$(echo "$SEARCH_JSON" | jq '.results | length')

if (( ALBUM_COUNT == 0 )); then
    die "No albums found for '${SEARCH_QUERY}'. Try a different query."
fi

BROWSE_ID=$(echo "$SEARCH_JSON" | jq -r '.results[] | select(.resultType=="album") | .browseId' | head -1)
ALBUM_TITLE=$(echo "$SEARCH_JSON" | jq -r '.results[] | select(.resultType=="album") | .title' | head -1)
ALBUM_ARTIST=$(echo "$SEARCH_JSON" | jq -r '.results[] | select(.resultType=="album") | .artist[0]' | head -1)

if [[ -z "$BROWSE_ID" || "$BROWSE_ID" == "null" ]]; then
    die "Search returned results but no album browseId found."
fi

pass "Found album: ${BOLD}${ALBUM_ARTIST} - ${ALBUM_TITLE}${NC} (${BROWSE_ID})"

# ─── 3. Queue download ───────────────────────────────────────────────────────
info "Queuing download ..."

DOWNLOAD_JSON=$(curl -sf -X POST "${API_URL}/download/album" \
    -H "Content-Type: application/json" \
    -d "{\"id\": \"${BROWSE_ID}\", \"concurrent\": 4}")

JOB_ID=$(echo "$DOWNLOAD_JSON" | jq -r '.job_id')

if [[ -z "$JOB_ID" || "$JOB_ID" == "null" ]]; then
    die "Failed to queue download. Response: ${DOWNLOAD_JSON}"
fi

pass "Job ${BOLD}${JOB_ID}${NC} queued."

# ─── 4. Poll job status ──────────────────────────────────────────────────────
info "Polling job ${JOB_ID} every ${POLL_INTERVAL}s (timeout ${POLL_TIMEOUT}s) ..."

POLL_DEADLINE=$((SECONDS + POLL_TIMEOUT))
FINAL_STATUS=""
TOTAL_SONGS=0
CURRENT_SONG=0

while (( SECONDS < POLL_DEADLINE )); do
    JOB_JSON=$(curl -sf "${API_URL}/jobs/${JOB_ID}") || die "Failed to fetch job status."

    JOB_STATUS=$(echo "$JOB_JSON" | jq -r '.status')
    TOTAL_SONGS=$(echo "$JOB_JSON" | jq -r '.total_songs')
    CURRENT_SONG=$(echo "$JOB_JSON" | jq -r '.current_song')
    ALBUM_PROGRESS=$(echo "$JOB_JSON" | jq -r '.album_progress')
    TOTAL_ALBUMS=$(echo "$JOB_JSON" | jq -r '.total_albums')
    MSG=$(echo "$JOB_JSON" | jq -r '.message // ""')

    FINAL_STATUS="$JOB_STATUS"

    if [[ "$JOB_STATUS" == "success" || "$JOB_STATUS" == "failed" ]]; then
        # Terminal final state
        break
    fi

    # Build progress bar
    BAR_WIDTH=30
    if (( TOTAL_SONGS > 0 )); then
        FILLED=$(( (CURRENT_SONG * BAR_WIDTH) / TOTAL_SONGS ))
    else
        FILLED=0
    fi

    BAR=""
    for (( i = 0; i < BAR_WIDTH; i++ )); do
        if (( i < FILLED )); then
            BAR+="█"
        else
            BAR+="░"
        fi
    done

    printf "\r${YELLOW}  [%s] %3d/%d songs, album %d/%d - %s${NC}   " \
        "$BAR" "$CURRENT_SONG" "$TOTAL_SONGS" "$ALBUM_PROGRESS" "$TOTAL_ALBUMS" "$MSG"

    sleep "$POLL_INTERVAL"
done

printf "\n"

if [[ -n "$FINAL_STATUS" ]]; then
    pass "Job finished with status: ${BOLD}${FINAL_STATUS}${NC}"
else
    die "Polling timed out after ${POLL_TIMEOUT}s without a final status."
fi

# Fetch fresh data for final report
JOB_JSON=$(curl -sf "${API_URL}/jobs/${JOB_ID}")

# ─── 5. Display results ──────────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────────────────────────"
printf "${BOLD}Job %s${NC}\n" "$JOB_ID"
printf "  Type:       %s\n" "$(echo "$JOB_JSON" | jq -r '.job_type')"
printf "  Status:     %s\n" "$(echo "$JOB_JSON" | jq -r '.status')"
if [[ "$FINAL_STATUS" == "failed" ]]; then
    printf "  Error:      %s\n" "$(echo "$JOB_JSON" | jq -r '.error')"
fi
echo "────────────────────────────────────────────────────────────"
echo ""

SONG_COUNT=$(echo "$JOB_JSON" | jq '.songs | length')
SUCCESS_COUNT=$(echo "$JOB_JSON" | jq '[.songs[] | select(.status=="success")] | length')
FAILED_COUNT=$(echo "$JOB_JSON" | jq '[.songs[] | select(.status=="failed")] | length')

printf "  Songs: ${GREEN}%s success${NC}, ${RED}%s failed${NC} (${BOLD}%s total${NC})\n" \
    "$SUCCESS_COUNT" "$FAILED_COUNT" "$SONG_COUNT"
echo ""

if [[ "$SONG_COUNT" -eq 0 || "$SONG_COUNT" -gt 0 ]]; then
    printf "  ${BOLD}%-40s %-12s %-5s %s${NC}\n" "TITLE" "STATUS" "TRACK" "MINIO PATH"
    printf "  %-40s %-12s %-5s %s\n" "-------" "------" "-----" "----------"

    echo "$JOB_JSON" | jq -r '.songs[] | @json' | while IFS= read -r song; do
        S_TITLE=$(echo "$song" | jq -r '.title')
        S_STATUS=$(echo "$song" | jq -r '.status')
        S_TRACK=$(echo "$song" | jq -r '.track_number // "-"')
        S_PATH=$(echo "$song" | jq -r '.minio_path // "N/A"')
        S_ERROR=$(echo "$song" | jq -r '.error // ""')

        # Truncate long fields
        [[ ${#S_TITLE} -gt 40 ]] && S_TITLE="${S_TITLE:0:37}..."
        [[ ${#S_PATH} -gt 40 ]] && S_PATH="${S_PATH:0:37}..."

        if [[ "$S_STATUS" == "success" ]]; then
            STATUS_COLOR="${GREEN}${S_STATUS}${NC}"
        else
            STATUS_COLOR="${RED}${S_STATUS}${NC}"
        fi

        if [[ -n "$S_ERROR" && "$S_ERROR" != "null" ]]; then
            printf "  %-40s %-12s %-5s %s\n" "$S_TITLE" "$STATUS_COLOR" "$S_TRACK" "$S_PATH"
            printf "    ${RED}error: %s${NC}\n" "${S_ERROR:0:80}"
        else
            printf "  %-40s %-12s %-5s %s\n" "$S_TITLE" "$STATUS_COLOR" "$S_TRACK" "$S_PATH"
        fi
    done
    echo ""
fi

# Bail if job failed
if [[ "$FINAL_STATUS" == "failed" ]]; then
    fail_line "Job failed — skipping MinIO verification."
    exit 1
fi

# ─── 6. MinIO verification ──────────────────────────────────────────────────
echo "────────────────────────────────────────────────────────────"
printf "${BOLD}MinIO Verification${NC}\n"
echo "────────────────────────────────────────────────────────────"

if [[ "MINIO_USE_SSL" == true ]]; then
  MC_PROTOCOL="https"
else
  MC_PROTOCOL="http"
fi
mc alias remove "$ALIAS_NAME" 2>/dev/null || true
mc alias set "$ALIAS_NAME" \
    "$MC_PROTOCOL://${MINIO_ENDPOINT}" \
    "${MINIO_ACCESS_KEY}" \
    "${MINIO_SECRET_KEY}" 2>/dev/null
pass "mc alias configured"

echo ""
VERIFY_PASS=0
VERIFY_FAIL=0
VERIFY_SKIP=0

printf "  ${BOLD}%-40s %-12s %s${NC}\n" "MINIO PATH" "S3 STATUS" "SIZE"
printf "  %-40s %-12s %s\n" "----------" "---------" "----"

echo "$JOB_JSON" | jq -r '.songs[] | select(.minio_path != null) | @json' | while IFS= read -r song; do
    S_PATH=$(echo "$song" | jq -r '.minio_path')
    S_TITLE=$(echo "$song" | jq -r '.title')
    [[ ${#S_TITLE} -gt 40 ]] && S_TITLE="${S_TITLE:0:37}..."

    # Check the object exists in S3
    MC_OUTPUT=$(mc stat "$ALIAS_NAME/${MINIO_BUCKET}/${S_PATH}" 2>&1 || true)

    if echo "$MC_OUTPUT" | grep -q "Size:"; then
        S3_SIZE=$(echo "$MC_OUTPUT" | grep "Size:" | awk '{print $2}')
        printf "  ${GREEN}✓${NC} %-39s %-12s %s\n" "$(echo "$S_TITLE" | cut -c1-40)" "exists" "$S3_SIZE"
    else
        printf "  ${RED}✗${NC} %-39s %-12s %s\n" "$(echo "$S_TITLE" | cut -c1-40)" "MISSING" ""
    fi
done

echo ""

# Final verdict
if [[ "$FINAL_STATUS" == "success" && "$FAILED_COUNT" -eq 0 ]]; then
    echo "────────────────────────────────────────────────────────────"
    echo -e "  ${GREEN}${BOLD}E2E TEST PASSED${NC}"
    echo "────────────────────────────────────────────────────────────"
    exit 0
elif [[ "$FINAL_STATUS" == "success" ]]; then
    echo "────────────────────────────────────────────────────────────"
    echo -e "  ${YELLOW}${BOLD}E2E TEST PARTIAL — job succeeded but ${FAILED_COUNT} song(s) failed${NC}"
    echo "────────────────────────────────────────────────────────────"
    exit 2
else
    echo "────────────────────────────────────────────────────────────"
    echo -e "  ${RED}${BOLD}E2E TEST FAILED${NC}"
    echo "────────────────────────────────────────────────────────────"
    exit 1
fi
