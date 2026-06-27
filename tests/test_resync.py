"""resync backfills stages that were added to the pipeline after a source was ingested.

The scenario CLAUDE.md calls the model-upgrade path, seen from the corpus side: a file is ingested
under an N-stage pipeline; later an (N+1)th stage is added; resync runs only that new stage for the
already-ingested file, leaving the rest skipped. Mirrors test_dag_idempotency's counting-stage
style, but exercises plan_resync/run_resync over real File rows in the truth store.
"""

from __future__ import annotations

import doctalk.ingest.resync as resync
from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.hashing import hash_bytes
from doctalk.ingest.dag import Stage, StageContext, run_dag

CH = hash_bytes(b"resync fixture source")


def _seed_file(fmt: str = "demo") -> None:
    """A source row in the truth store for resync to sweep (resync reads File rows, not the disk)."""
    with session_scope() as s:
        repo.upsert_file(
            s, content_hash=CH, path="/gone/source.demo", filename="source.demo",
            format=fmt, mime="application/x-demo", byte_size=1,
        )


def _pipeline(counter: dict[str, int], *names: str) -> list[Stage]:
    """Build a counting pipeline over the given stage names, chained a->b->c. Stage identity for the
    ledger is (name, model_version, params), so rebuilding the same names yields the same input_hash
    — exactly how a real pipeline that grew a stage still skips the old ones."""
    def make(name: str):
        def run(ctx: StageContext) -> None:
            counter[name] += 1
        return run

    stages, prev = [], None
    for n in names:
        stages.append(Stage(n, make(n), deps=(prev,) if prev else ()))
        prev = n
    return stages


def test_resync_backfills_a_newly_added_stage(db, monkeypatch):
    counter = {"a": 0, "b": 0}
    _seed_file()

    # Original ingest ran only stage 'a'.
    run_dag(CH, _pipeline(counter, "a"))
    assert counter == {"a": 1, "b": 0}

    # A new stage 'b' is added to the pipeline after the fact.
    monkeypatch.setattr(resync, "pipeline_for", lambda fmt: _pipeline(counter, "a", "b"))

    # The plan reports exactly the gap: 'b' missing for this source, 'a' already done.
    plan = resync.plan_resync()
    assert len(plan) == 1
    assert plan[0].content_hash == CH
    assert plan[0].missing == ["b"]

    # Running it backfills only 'b' — 'a' is skipped, not re-executed.
    results = resync.run_resync(plan[0])
    assert {r.stage: r.status for r in results} == {"a": "skipped", "b": "done"}
    assert counter == {"a": 1, "b": 1}

    # Now fully in sync: nothing left to do.
    assert resync.plan_resync() == []


def test_resync_noop_when_in_sync(db, monkeypatch):
    counter = {"a": 0, "b": 0}
    _seed_file()
    monkeypatch.setattr(resync, "pipeline_for", lambda fmt: _pipeline(counter, "a", "b"))

    run_dag(CH, _pipeline(counter, "a", "b"))  # full ingest, both stages done
    assert resync.plan_resync() == []           # no gap -> empty plan
    assert counter == {"a": 1, "b": 1}          # resync planning runs nothing


def test_resync_skips_unknown_format(db, monkeypatch):
    """A source whose format has no pipeline can't be resynced — it's skipped, not crashed."""
    _seed_file(fmt="mystery")

    def boom(fmt: str):
        raise ValueError(f"no pipeline for {fmt}")

    monkeypatch.setattr(resync, "pipeline_for", boom)
    assert resync.plan_resync() == []
