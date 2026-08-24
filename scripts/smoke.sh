#!/usr/bin/env bash
# Drive the REAL product over HTTP, through the Flask BFF, as a browser does.
#
# Why this exists: ~6,000 tests in this repo and NOT ONE crosses the
# browser→Flask→API seam — every frontend test mocks `api_client`, and the
# integration suite never drives Flask. Four of the last nine defects to reach
# the running product lived exactly there, including a Regenerate button that
# did nothing and a withdraw form that could not record a reason.
#
# `verify.sh` proves the CODE. `doctor.sh` proves the deployment's DATA. This
# proves the SCREEN — the only one of the three that sees what a user sees.
#
# Runs in a throwaway container built from the api image (which already has
# pytest and httpx), joined to the compose network so `frontend:5000` resolves,
# with the REPO ROOT mounted so ./fixtures is reachable. Same "never hand-write
# a narrower command" discipline as verify.sh.
#
# Requires CAS to be DISABLED: these tests cannot complete a CAS handshake, and
# they FAIL rather than skip when it is on — a green smoke run that exercised
# nothing is worse than no smoke run. Pair-test the authenticated paths in a
# browser afterwards.
#
# Creates a job and uploads résumés from ./fixtures, so the LLM must be
# reachable; the local model parses every résumé, so allow several minutes.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="$(basename "$REPO_ROOT")"
NETWORK="${SMOKE_NETWORK:-${PROJECT}_default}"
IMAGE="${SMOKE_IMAGE:-${PROJECT}-api}"

if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  echo "🔴 smoke: docker network '$NETWORK' not found — is the stack up?" >&2
  echo "   docker compose up -d" >&2
  exit 1
fi

if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]]; then
  export MSYS_NO_PATHCONV=1
  export MSYS2_ARG_CONV_EXCL='*'
  MOUNT_SRC="$(cd "$REPO_ROOT" && pwd -W 2>/dev/null || echo "$REPO_ROOT")"
else
  MOUNT_SRC="$REPO_ROOT"
fi

# Prefer the REAL corpus when it has been provisioned, fall back to the
# committed synthetic one otherwise. This is why a bare clone can run smoke at
# all — `fixtures/` is gitignored (real candidate documents live there and are
# never committed), so before `fixtures-synthetic/` existed a fresh checkout had
# no résumé PDFs and this suite could only fail.
#
# Deliberately NOT mirrored in model-check.sh: that harness measures the MODEL,
# and its own docstring records why synthetic inputs are disqualifying there — a
# hand-written prompt of comparable length returned valid JSON while the real
# résumé failed three times out of three. Smoke tests the browser→Flask→API
# seam, where a well-formed synthetic résumé exercises the same path.
if [[ -d "$REPO_ROOT/fixtures/resumes" ]]; then
  SMOKE_FIXTURES_DEFAULT="/repo/fixtures"
  echo "  fixtures: ./fixtures (real corpus)"
elif [[ -d "$REPO_ROOT/fixtures-synthetic/resumes" ]]; then
  SMOKE_FIXTURES_DEFAULT="/repo/fixtures-synthetic"
  echo "  fixtures: ./fixtures-synthetic (generated — no real candidate data)"
else
  echo "🔴 smoke: no résumé corpus found." >&2
  echo "   Expected ./fixtures/resumes (real, provisioned out-of-band) or" >&2
  echo "   ./fixtures-synthetic/resumes (committed). Generate the latter with:" >&2
  echo "     ./scripts/gen-fixtures.sh" >&2
  exit 1
fi

echo "▶ smoke: driving the live product through the Flask BFF"
echo "  (this parses real résumés on the local model — allow several minutes)"
echo

docker run --rm \
  --network "$NETWORK" \
  -v "${MOUNT_SRC}:/repo" \
  -w /repo/core \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/repo/core \
  -e SMOKE_FRONTEND="${SMOKE_FRONTEND:-http://frontend:5000}" \
  -e SMOKE_FIXTURES="${SMOKE_FIXTURES:-$SMOKE_FIXTURES_DEFAULT}" \
  "$IMAGE" \
  python -m pytest tests/smoke -q -p no:cacheprovider "$@"
