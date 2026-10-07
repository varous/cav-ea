#!/usr/bin/env sh
# Thin wrapper around bootstrap.py. See README "Setup".
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$DIR"

case "${1:-}" in
  up)
    shift
    exec python3 bootstrap.py "$@"
    ;;
  --dry-run)
    shift
    exec python3 bootstrap.py --dry-run "$@"
    ;;
  ""|-h|--help|help)
    cat <<'EOF'
usage:
  ./deploy.sh up [--dry-run] [--config FILE] [--secret-file NAME=PATH]
  ./deploy.sh --dry-run [options]

Provisions the operating assistant (Cloud Run, Pub/Sub, secrets, schedulers).
Requires an authenticated gcloud. See README "Setup" for the walkthrough.
EOF
    ;;
  *)
    echo "unknown command: $1" >&2
    echo "try: ./deploy.sh help" >&2
    exit 2
    ;;
esac
