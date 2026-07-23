#!/bin/bash
set -e

IMAGE="${ZIMMPORTER_IMAGE}"

case "${1:-}" in
  serve)
    shift
    docker run --rm -it -p 8000:8000 "$IMAGE" "$@"
    ;;
  runworker)
    shift
    docker run --rm -it -v /data/zimmer/importer:/data/zimmer/importer "$IMAGE" \
      celery -A tasks.celery_app worker --loglevel=info "$@"
    ;;
  run)
    shift
    docker run --rm -it -v /data/zimmer/importer:/data/zimmer/importer "$IMAGE" \
      python -m zimmporter "$@"
    ;;
  *)
    echo "Usage: $0 {serve|runworker|run <args>}..."
    echo ""
    echo "Commands:"
    echo "  serve              Start FastAPI server on port 8000"
    echo "  runworker          Start a Celery worker"
    echo "  run <args>         Run the CLI (search/download)"
    echo ""
    echo "CLI examples:"
    echo "  $0 run search 'Aurora'"
    echo "  $0 run download MPREb_xxx"
    ;;
esac
