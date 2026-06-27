"""resync — backfill pipeline stages that were added *after* a source was ingested.

The ingest DAG is built to re-run only an affected stage + its downstream when the pipeline grows
(a new stage, a model upgrade): each stage is gated by its ``input_hash``, so a stage with no
``done`` ledger row runs on the next pass while everything already done is skipped. But nothing
*triggers* that resweep. The watcher re-ingests new *files* (keyed on mtime+size+path); it is blind
to the *pipeline definition* changing. So a stage added after a file landed leaves that file with a
permanent ledger gap — it shows ``pending`` on ``/jobs`` — until someone manually re-drops it.

resync closes the loop: for every source in the truth store, diff its *current* pipeline against the
ledger and run any stage that has no ``done`` row. Idempotent (the DAG's own skip check does the
work), cheap (only missing stages run), and safe to call on every watcher startup — which is exactly
where it is wired (see ``doctalk.sh``).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from doctalk.db import repo
from doctalk.db.models import File
from doctalk.db.session import session_scope
from doctalk.ingest.dag import StageResult, run_dag
from doctalk.ingest.pipeline import pipeline_for


@dataclass
class ResyncItem:
    """A source with at least one stage in its current pipeline that has never run."""

    content_hash: str
    filename: str
    format: str
    path: str
    missing: list[str]


def plan_resync() -> list[ResyncItem]:
    """Per source, the stages in its CURRENT pipeline with no ``done`` ledger row.

    Mirrors the ``/jobs`` 'pending' computation, but via the DAG's own ``is_stage_done`` so the
    plan can never disagree with what an actual run would do. Sources whose format has no pipeline
    (defensive — shouldn't happen for ingested files) are skipped. Returns plain strings, not ORM
    rows, so the result is safe to use after the session closes.
    """
    items: list[ResyncItem] = []
    with session_scope() as session:
        for f in session.scalars(select(File).order_by(File.id)):
            try:
                stages = pipeline_for(f.format)
            except Exception:  # noqa: BLE001 - unknown format: nothing we can resync
                continue
            missing = [
                s.name for s in stages if not repo.is_stage_done(session, s.input_hash(f.content_hash))
            ]
            if missing:
                items.append(ResyncItem(f.content_hash, f.filename, f.format, f.path, missing))
    return items


def run_resync(item: ResyncItem) -> list[StageResult]:
    """Run one source through its pipeline; the DAG skips every already-done stage, so only the
    missing ones execute. A stage that needs the source file will error (and halt its downstream)
    if the file is gone — that's reported per stage, never raised."""
    return run_dag(item.content_hash, pipeline_for(item.format), file_path=item.path)
