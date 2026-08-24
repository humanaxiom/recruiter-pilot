"""Root pytest conftest — cross-cutting test-session environment setup.

ADR-008: ``settings.skill_hash_salt`` must be non-empty for ANY code path
that hashes a non-vocab skill name (``src.pipeline.skills_graph._hash_key``)
to succeed — production fails loud on an empty salt
(``src.worker.main.startup``), and the hashing function itself fails loud
too, as a second, independent line of defence (see its docstring).

Both the unit suite (fake Neo4j sessions) and the integration suite (a real
Neo4j via testcontainers) exercise real skill-name resolution/hashing
constantly, without caring what the salt's actual VALUE is — so a single,
stable, test-only salt is set here for the WHOLE test session, via a plain
environment-variable default, before any module gets a chance to construct
the process-wide cached ``Settings`` singleton (some modules, e.g.
``src.worker.neo4j_bootstrap``, read ``get_settings()`` at IMPORT time).
``os.environ.setdefault`` means a test (or the real environment) that already
set ``SKILL_HASH_SALT`` explicitly is never overridden.

A test that specifically exercises the empty-salt-fails-loud behaviour
constructs its own ``Settings(skill_hash_salt="")`` directly, which is
unaffected by this environment default (an explicit constructor kwarg always
wins over the environment in pydantic-settings).
"""

from __future__ import annotations

import os

os.environ.setdefault("SKILL_HASH_SALT", "test-only-salt-do-not-use-in-prod")
