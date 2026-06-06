"""Phase 0 verification: the DAG is idempotent and resumable.

  * run twice on one source  -> second run is fully skipped (no stage re-executes)
  * a crash mid-run          -> only the failed stage + downstream re-run; the rest stay skipped
"""

from __future__ import annotations

import pytest

from doctalk.db import repo
from doctalk.db.models import JobStatus
from doctalk.db.session import session_scope
from doctalk.hashing import hash_bytes
from doctalk.ingest.dag import Stage, StageContext, run_dag

CH = hash_bytes(b"phase-0 fixture source")


def _counting_pipeline(counter: dict[str, int]) -> list[Stage]:
    def a(ctx: StageContext) -> None:
        counter["a"] += 1

    def b(ctx: StageContext) -> None:
        counter["b"] += 1

    return [Stage("a", a), Stage("b", b, deps=("a",))]


def test_second_run_is_fully_skipped(db):
    counter = {"a": 0, "b": 0}

    first = run_dag(CH, _counting_pipeline(counter))
    assert [r.status for r in first] == ["done", "done"]
    assert counter == {"a": 1, "b": 1}

    second = run_dag(CH, _counting_pipeline(counter))
    assert [r.status for r in second] == ["skipped", "skipped"]
    assert counter == {"a": 1, "b": 1}  # nothing re-executed


def test_crash_then_resume(db):
    counter = {"a": 0, "b": 0}
    boom = {"explode": True}

    def a(ctx: StageContext) -> None:
        counter["a"] += 1

    def b(ctx: StageContext) -> None:
        if boom["explode"]:
            raise RuntimeError("kaboom")
        counter["b"] += 1

    stage_a, stage_b = Stage("a", a), Stage("b", b, deps=("a",))
    pipeline = [stage_a, stage_b]

    # First pass: 'a' completes, 'b' fails -> downstream halts.
    first = run_dag(CH, pipeline)
    assert [r.status for r in first] == ["done", "error"]
    assert counter == {"a": 1, "b": 0}

    with session_scope() as s:
        assert repo.is_stage_done(s, stage_a.input_hash(CH))
        assert repo.get_job(s, stage_b.input_hash(CH)).status == JobStatus.error

    # Fix the cause and resume: 'a' is skipped (already done), 'b' now succeeds.
    boom["explode"] = False
    second = run_dag(CH, pipeline)
    assert [r.status for r in second] == ["skipped", "done"]
    assert counter == {"a": 1, "b": 1}  # 'a' did NOT re-run


def test_param_change_reruns_only_that_stage(db):
    """Changing a stage's params changes its input_hash -> it re-runs; unaffected stages stay
    skipped. This is the model-upgrade path from CLAUDE.md."""
    counter = {"a": 0, "b": 0}

    def a(ctx: StageContext) -> None:
        counter["a"] += 1

    def b(ctx: StageContext) -> None:
        counter["b"] += 1

    run_dag(CH, [Stage("a", a), Stage("b", b, deps=("a",))])
    assert counter == {"a": 1, "b": 1}

    # Bump 'b' params (e.g. a new model_version would do the same) -> only 'b' re-runs.
    run_dag(CH, [Stage("a", a), Stage("b", b, params={"v": 2}, deps=("a",))])
    assert counter == {"a": 1, "b": 2}


@pytest.mark.parametrize("bad", ["cycle", "unknown_dep"])
def test_invalid_dags_raise(db, bad):
    if bad == "cycle":
        stages = [Stage("x", lambda c: None, deps=("y",)), Stage("y", lambda c: None, deps=("x",))]
    else:
        stages = [Stage("x", lambda c: None, deps=("nope",))]
    with pytest.raises(ValueError):
        run_dag(CH, stages)
