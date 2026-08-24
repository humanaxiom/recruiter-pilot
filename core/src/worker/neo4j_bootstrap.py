"""Idempotently create the Neo4j constraints + vector indexes the ranking
pipeline needs. Ported near-verbatim from hris
``apps/worker/src/worker/neo4j_bootstrap.py``.

Run from the worker's ``on_startup`` and the API lifespan. Cheap to re-execute:
every statement is ``IF NOT EXISTS`` / ``IF EXISTS``.

Three things here are load-bearing and must not be "tidied":

1. **``ResumeChunk`` carries NO uniqueness constraint on ``id``.** Chunk ids
   (``c_001``, ``c_002``, …) are deterministic per-resume and intentionally
   COLLIDE across resumes. The old ``chunk_id_unique`` meant the second
   resume's projection always failed, which silently capped every shortlist at
   one candidate. It is actively DROPPED here so existing installs heal on the
   next restart; a plain composite index on ``(resume_id, id)`` keeps citation
   lookups fast without forcing global uniqueness.
2. **``resume_job_id_idx``** lets the stage-1 vector query scope to "resumes
   uploaded for THIS job". Without it, other jobs' candidates leak into a
   recruiter's shortlist.
3. **``skill_name_unique`` is DROPPED, never reused.** It was Phase 3's
   constraint name, enforcing uniqueness on the long-abandoned
   ``canonical_name`` property. A same-named ``CREATE CONSTRAINT ... IF NOT
   EXISTS`` with a DIFFERENT ``REQUIRE`` clause is a Neo4j no-op (the name,
   not the property, is what "already exists" checks) — every install that
   ever ran the old statement silently kept NO uniqueness constraint on
   ``canonical_key`` at all. The replacement, ``skill_canonical_key_unique``,
   is a genuinely new name so its ``CREATE CONSTRAINT`` actually runs. Do
   not rename a constraint by changing its ``REQUIRE`` clause in place ever
   again — always DROP the old name and CREATE a new one, exactly like the
   ``chunk_id_unique`` fix above.

The vector dimension is read from ``settings.llm_embedding_dim`` — the single
source of the 768-d contract. Change the embedding model and this moves with
it; the two can never drift apart.
"""

from __future__ import annotations

import logging
from typing import Final

from neo4j import AsyncDriver

from src.settings import get_settings

logger = logging.getLogger(__name__)

# CONTRACT: one number, one place. nomic-embed-text → 768-d, cosine.
_DIM: Final[int] = get_settings().llm_embedding_dim

_VECTOR_OPTIONS: Final[str] = (
    "OPTIONS { indexConfig: { "
    f"`vector.dimensions`: {_DIM}, "
    "`vector.similarity_function`: 'cosine' } }"
)


def _vector_index(name: str, var: str, label: str, prop: str) -> str:
    return (
        f"CREATE VECTOR INDEX {name} IF NOT EXISTS "
        f"FOR ({var}:{label}) ON {var}.{prop} "
        f"{_VECTOR_OPTIONS}"
    )


_STATEMENTS: tuple[str, ...] = (
    # ── Uniqueness constraints ───────────────────────────────────────────────
    "CREATE CONSTRAINT job_id_unique IF NOT EXISTS "
    "FOR (j:Job) REQUIRE j.id IS UNIQUE",
    "CREATE CONSTRAINT resume_id_unique IF NOT EXISTS "
    "FOR (r:Resume) REQUIRE r.id IS UNIQUE",
    # ADR-008: the unique key is `canonical_key` (a vocab term, cleartext, or
    # a salted hash for a non-vocab name) — never `canonical_name`, which
    # would tempt someone to write a résumé-derived name into it.
    #
    # F2 (security re-audit round 2): this constraint used to be named
    # `skill_name_unique` (Phase 3's original name) while its REQUIRE clause
    # was changed to `canonical_key` in place — Neo4j treats
    # `CREATE CONSTRAINT <SAME NAME> IF NOT EXISTS ...` as a no-op keyed on
    # the NAME, not the property, so every install that ever ran Phase 3
    # kept its OLD constraint (still enforcing uniqueness on the
    # long-abandoned `canonical_name` property) and got NO uniqueness
    # constraint at all on `canonical_key` — verified against a real
    # neo4j:5-community container (`SHOW CONSTRAINTS` after both migrations
    # still showed only the stale `canonical_name` constraint). Combined with
    # the cron drainer's concurrent `MERGE`s (R7), that silently let one
    # skill fork into two Skill nodes. Fixed the only way a same-database
    # rename can be: DROP the old name, CREATE a new, differently-named
    # constraint on the correct property — the same DROP-then-CREATE pattern
    # already used for `chunk_id_unique` below.
    "DROP CONSTRAINT skill_name_unique IF EXISTS",
    "CREATE CONSTRAINT skill_canonical_key_unique IF NOT EXISTS "
    "FOR (s:Skill) REQUIRE s.canonical_key IS UNIQUE",
    "CREATE CONSTRAINT company_name_unique IF NOT EXISTS "
    "FOR (c:Company) REQUIRE c.canonical_name IS UNIQUE",
    "CREATE CONSTRAINT institution_name_unique IF NOT EXISTS "
    "FOR (i:Institution) REQUIRE i.canonical_name IS UNIQUE",
    # ── The chunk-collision fix (see module docstring) ───────────────────────
    "DROP CONSTRAINT chunk_id_unique IF EXISTS",
    "CREATE INDEX chunk_resume_id_idx IF NOT EXISTS "
    "FOR (c:ResumeChunk) ON (c.resume_id, c.id)",
    # ── Per-job shortlist scoping ────────────────────────────────────────────
    "CREATE INDEX resume_job_id_idx IF NOT EXISTS FOR (r:Resume) ON r.job_id",
    # ── Vector indexes (llm_embedding_dim, cosine) ───────────────────────────
    _vector_index("resume_summary_idx", "r", "Resume", "summary_embedding"),
    _vector_index("job_summary_idx", "j", "Job", "summary_embedding"),
    _vector_index("skill_emb_idx", "s", "Skill", "embedding"),
    _vector_index("chunk_emb_idx", "c", "ResumeChunk", "embedding"),
)


async def bootstrap_neo4j_schema(driver: AsyncDriver) -> None:
    """Run all constraint + index DDL. Idempotent."""
    async with driver.session() as session:
        for statement in _STATEMENTS:
            await session.run(statement)
    logger.info("neo4j.bootstrap.ok statements=%s dim=%s", len(_STATEMENTS), _DIM)
