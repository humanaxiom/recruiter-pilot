# Agent Harness v2 — developer interface
.PHONY: up down gates gates-fast gates-integration gates-all branch-name logs

up:               ## Start the full stack (Ollama must be running on host)
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f api worker

# ── Gates: THE non-negotiable suite ────────────────────────────────────────
# `make gates` is the OFFLINE default — no Docker, green on a fresh clone.
# Integration (real Postgres/Neo4j/Redis via testcontainers) is a separate
# target because it needs a running Docker socket. CI runs `gates-all`.

# `main` is exempt, matching `.github/workflows/ci.yml`'s branch-name job
# verbatim (it does `if [[ "$$BRANCH" == "main" ]]; then exit 0; fi`). Without
# this the local gate REJECTED a branch CI accepts, so `./scripts/verify.sh`
# failed instantly on a freshly cloned repo sitting on its default branch —
# exactly the local-vs-CI drift this Makefile exists to make impossible.
branch-name:      ## Enforce branch naming (agent|feat|fix|chore)/<slug>; main exempt
	@B=$$(git branch --show-current); \
	  if [ "$$B" = "main" ]; then \
	    echo "✅ branch name OK (main)"; \
	  elif echo "$$B" | grep -Eq '^(agent|feat|fix|chore)/[a-zA-Z0-9._-]+$$'; then \
	    echo "✅ branch name OK"; \
	  else \
	    echo "❌ branch '$$B' must match (agent|feat|fix|chore)/<slug>"; exit 1; \
	  fi

gates: branch-name  ## Offline gate suite (ruff·black·mypy·unit·coverage·branch)
	cd core && ruff check src tests frontend
	cd core && black --check src tests frontend
	cd core && mypy src frontend --strict
	cd core && pytest tests/unit \
		--cov=src --cov=frontend --cov-fail-under=$${COVERAGE_THRESHOLD:-80} --timeout=120 -q
	@echo "✅ OFFLINE GATES GREEN"

gates-fast:       ## Pre-commit subset (no coverage, no integration)
	cd core && ruff check src tests frontend && black --check src tests frontend
	cd core && mypy src frontend --strict
	cd core && pytest tests/unit -q --timeout=120

gates-integration:  ## Integration tests — requires a running Docker socket
	cd core && pytest tests/integration --timeout=300 -q

gates-all: gates gates-integration  ## Everything CI runs
	@echo "✅ ALL GATES GREEN"
