"""Ingest: the resumable, idempotent DAG runner (``dag``) and the stage definitions
(``pipeline``). Phase 0 ships the runner plus placeholder stages that prove idempotency;
Phase 1 swaps in the real extraction stages."""
