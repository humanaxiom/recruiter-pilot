"""Resume/JD parsing pipeline: extraction, chunking, and skill matching.

Ported from hris ``packages/pipeline`` (see
``phase3-source-dossier.md``). LLM client/cache live under
``src.pipeline.llm`` (owned by a parallel Phase 3 coder — not this
package's concern).
"""

from __future__ import annotations
