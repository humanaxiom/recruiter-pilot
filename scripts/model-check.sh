#!/usr/bin/env bash
# Measure the configured model against THIS product's real prompts.
#
# Run this FIRST when you change LLM_MODEL_GENERATION — before deploying, before
# uploading anything. Every token budget and timeout in this repo was measured
# against gpt-oss:20b; a different model invalidates all of them at the same
# moment, with no signal. On 2026-08-21 that happened within a single model and
# it took a production incident to notice: résumé skill extraction returned
# nothing, every candidate degraded, and no shortlist could be produced, all
# behind a fully green test suite.
#
# What it does: builds the REAL prompts from ./fixtures (a real résumé PDF, a
# real JD .docx), runs each one at the worker's own concurrency, and finds the
# smallest token budget that yields schema-valid JSON for every concurrent call.
#
# Why concurrency matters: a single uncontended call to the failing prompt
# returned valid JSON in ~35s while production was failing every time. Four jobs
# share one GPU. Measuring one call at a time produces a latency figure that is
# accurate and useless.
#
# Writes docs/model-profiles/<model>.json — COMMIT IT. A model swap then appears
# in a diff next to the config it justifies, and `scripts/doctor.sh` fails when
# the configured model has no profile, or when LLM_TIMEOUT_S is below what was
# measured.
#
# Exit code: 0 if every real prompt produced schema-valid JSON, else 1.
set -euo pipefail

SERVICE="${MODEL_CHECK_SERVICE:-worker}"
CONTAINER="$(docker compose ps -q "$SERVICE" 2>/dev/null || true)"

if [[ -z "$CONTAINER" ]]; then
  echo "🔴 model-check: the '$SERVICE' container is not running — start the stack first" >&2
  echo "   docker compose up -d" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Git Bash rewrites container-side paths like /tmp/fixtures into Windows paths
# before docker ever sees them (the first run of this script measured nothing
# because --fixtures arrived as C:/Users/.../Temp/fixtures). Same guard, and the
# same reason, as scripts/verify.sh.
# HOST_ROOT is the repo in a form docker itself understands (C:/... on Windows);
# REPO_ROOT stays POSIX for shell use. Both are needed and they are not
# interchangeable: with path conversion off, a host path must already be native,
# while container-side paths must be left alone.
HOST_ROOT="$REPO_ROOT"
if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]]; then
  export MSYS_NO_PATHCONV=1
  export MSYS2_ARG_CONV_EXCL='*'
  HOST_ROOT="$(cd "$REPO_ROOT" && pwd -W 2>/dev/null || echo "$REPO_ROOT")"
fi

if [[ ! -d "$REPO_ROOT/fixtures" ]]; then
  echo "🔴 model-check: no ./fixtures directory — the harness measures REAL" >&2
  echo "   documents on purpose; a synthetic prompt certified a broken" >&2
  echo "   configuration as healthy during the 2026-08-21 incident." >&2
  exit 1
fi

echo "▶ model-check: measuring the live model against this product's real prompts"
echo "  (runs at the worker's own concurrency — allow several minutes)"
echo

docker cp "${HOST_ROOT}/fixtures" "${CONTAINER}:/tmp/fixtures" >/dev/null

status=0
# Flags, not env vars: CLAUDE.md forbids `os.environ` outside settings.py and a
# meta-test enforces it. The harness is not exempt from the repo's own rule.
docker exec "$CONTAINER" python -m src.model_probe_live \
  --fixtures /tmp/fixtures \
  --out /tmp/model-profiles \
  --concurrency "${MODEL_CHECK_CONCURRENCY:-4}" || status=$?

mkdir -p "$REPO_ROOT/docs/model-profiles"
docker cp "${CONTAINER}:/tmp/model-profiles/." "${HOST_ROOT}/docs/model-profiles/" \
  2>/dev/null || true
docker exec "$CONTAINER" rm -rf /tmp/fixtures /tmp/model-profiles >/dev/null 2>&1 || true

echo
if [[ "$status" -eq 0 ]]; then
  echo "profile written into docs/model-profiles/ — commit it."
else
  # Do NOT claim a profile was written when the run failed. Announcing success
  # on a failed run is the exact false-confidence this whole tool exists to
  # remove, and the first version of this script did it.
  echo "🔴 model-check FAILED — no usable profile. Do not deploy this model." >&2
fi
exit "$status"
