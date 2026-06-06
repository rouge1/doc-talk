"""doctalk — a fully-local drop-files -> wiki + chat your data knowledge base.

Phase 0 lays the skeleton and the truth store: config, the blake3 content-hash, the MySQL
metadata models (``files`` + the ``jobs`` ledger), the single metadata writer (``db.repo``),
and the resumable, idempotent ingest DAG. See ``PLAN.md`` for the full design.
"""

__version__ = "0.0.0"
