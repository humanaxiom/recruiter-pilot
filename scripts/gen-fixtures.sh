#!/usr/bin/env bash
# Generate a synthetic résumé corpus — one résumé PDF per real job description.
#
# Why this exists: `smoke.sh` and `model-check.sh` both need
# `fixtures/resumes/*_resume.pdf`, and the only PDFs that ever lived there were
# REAL applicants' — names, personal email addresses, phone numbers. That made
# the corpus unpublishable, unshippable to a new machine, and impossible to put
# in CI, so a fresh deployment either skipped its two most valuable harnesses or
# moved third-party PII around to get them.
#
# The generated corpus has the same shape and none of the exposure: emails at
# `example.invalid` (RFC 2606 — permanently unresolvable), phones in the
# reserved `555-01xx` block, and a `Synthetic-Fixture` marker in every PDF's
# metadata so "is this file synthetic?" is answerable mechanically.
#
# Résumés are built FROM the real JDs, so they reference skills those postings
# actually ask for. A corpus of unrelated lorem would rank identically against
# every posting and would certify a broken ranker as healthy — the same trap
# `model_probe_live` documents for synthetic prompts.
#
# Output is deterministic: seeded from the JD filename, so a re-run reproduces
# the same corpus. A fixture set that changes under you is not a fixture set.
#
# Runs in a throwaway container built from the api image (which already has
# PyMuPDF and python-docx), same "never hand-write a narrower command"
# discipline as verify.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="$(basename "$REPO_ROOT")"
IMAGE="${GEN_FIXTURES_IMAGE:-${PROJECT}-api}"

JDS="${1:-fixtures/JDs}"
OUT="${2:-fixtures/resumes}"

if [[ ! -d "$REPO_ROOT/$JDS" ]]; then
  echo "🔴 gen-fixtures: no job descriptions at '$JDS'" >&2
  echo "   Job descriptions are the INPUT — they are not generated. Provision" >&2
  echo "   them first, then re-run. Usage: $0 [jd_dir] [out_dir]" >&2
  exit 1
fi

# Git Bash rewrites container-side paths before docker sees them; same guard and
# the same reason as verify.sh and model-check.sh.
if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]]; then
  export MSYS_NO_PATHCONV=1
  export MSYS2_ARG_CONV_EXCL='*'
  MOUNT_SRC="$(cd "$REPO_ROOT" && pwd -W 2>/dev/null || echo "$REPO_ROOT")"
else
  MOUNT_SRC="$REPO_ROOT"
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "🔴 gen-fixtures: image '$IMAGE' not found — build the stack first" >&2
  echo "   docker compose build api" >&2
  exit 1
fi

echo "▶ gen-fixtures: one synthetic résumé per job description"
echo "  in:  $JDS"
echo "  out: $OUT"
echo

docker run --rm \
  -v "${MOUNT_SRC}:/repo" \
  -w /repo/core \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/repo/core \
  "$IMAGE" \
  python -m src.synth_fixtures \
    --jds "/repo/$JDS" \
    --out "/repo/$OUT" \
    --skill-data /repo/core/src/pipeline/skill_data

echo
echo "✅ done. These files contain NO real personal information and may be"
echo "   committed, released, or copied to any machine."
