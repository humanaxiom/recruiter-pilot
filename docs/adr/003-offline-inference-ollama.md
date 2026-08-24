# ADR-003: Offline Inference — Ollama on Metal, OpenAI-Compatible Client

**Status:** Accepted
**Date:** 2026-07-09

## Context

We build offline-first AI applications. Inference must not depend on cloud APIs, and GPU access is best from the host, not inside containers.

## Decision

- **Ollama runs on bare metal** (direct GPU/Metal access); everything else in Docker
- Containers reach it via `host.docker.internal:11434` (compose `extra_hosts: host-gateway` for Linux parity)
- All agent code uses the **OpenAI-compatible `/v1` endpoint** through `AsyncOpenAI(base_url=...)` — swapping to vLLM later is a config change, not a code change
- **CI never calls a model endpoint**: unit tests mock the client; integration tests mock only the embedding call and use real Postgres/Neo4j/Redis
- Models: `qwen2.5-coder:14b` (coding), `nomic-embed-text` (768-dim embeddings matching the Neo4j vector index)

## Consequences

- Fully offline runtime; zero cloud spend or data egress
- CI is deterministic and fast (no model variance in gates)
- Model quality gates (evals) must run on a self-hosted runner with GPU — out of scope of the default pipeline, tracked separately

## Alternatives Considered

- **Ollama in Docker**: GPU passthrough friction, worse Metal support on macOS; rejected
- **Native Ollama Python client**: locks us in; the OpenAI-compatible surface keeps vLLM migration trivial (already under evaluation); rejected
