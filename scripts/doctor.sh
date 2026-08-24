#!/usr/bin/env bash
# Ask the RUNNING deployment whether its invariants actually hold.
#
# `scripts/verify.sh` proves the CODE is correct. This proves the DATA is —
# and those are different claims. The defect that forced this module (ROADMAP
# A7 (20)) was a fix that shipped correct, passed every gate, and had never
# applied to a single row thirteen days later, because the write only fires on
# new data and every row predated it. No test can see that: a fixture is always
# freshly built and always well-formed.
#
# Runs INSIDE the api container, which already has the code, the dependencies
# and network reach to Postgres and Neo4j — the same "never hand-write the
# command" discipline as verify.sh.
#
# Exit code: 1 if any check FAILED (including a datastore it could not reach —
# that is never a clean bill of health), else 0.
set -euo pipefail

SERVICE="${DOCTOR_SERVICE:-api}"
CONTAINER="$(docker compose ps -q "$SERVICE" 2>/dev/null || true)"

if [[ -z "$CONTAINER" ]]; then
  echo "🔴 doctor: the '$SERVICE' container is not running — start the stack first" >&2
  echo "   docker compose up -d" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_ROOT="$REPO_ROOT"
if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]]; then
  export MSYS_NO_PATHCONV=1
  export MSYS2_ARG_CONV_EXCL='*'
  HOST_ROOT="$(cd "$REPO_ROOT" && pwd -W 2>/dev/null || echo "$REPO_ROOT")"
fi

echo "▶ doctor: checking the live deployment via the '$SERVICE' container"
echo

# The committed model profiles have to be COPIED IN. The container mounts only
# core/ at /app, so the repo-root-relative default resolves to /docs and finds
# nothing — the doctor then reports every model as UNPROFILED while a perfectly
# good profile sits committed on the host. A tool built to detect missing
# measurements, blind to the measurements.
if [[ -d "$REPO_ROOT/docs/model-profiles" ]]; then
  docker exec "$CONTAINER" rm -rf /tmp/model-profiles >/dev/null 2>&1 || true
  docker cp "${HOST_ROOT}/docs/model-profiles" "${CONTAINER}:/tmp/model-profiles" \
    >/dev/null 2>&1 || true
fi

status=0
docker exec "$CONTAINER" python -m src.doctor --profiles /tmp/model-profiles || status=$?
docker exec "$CONTAINER" rm -rf /tmp/model-profiles >/dev/null 2>&1 || true
exit "$status"
