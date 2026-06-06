"""Resumable, idempotent DAG runner backed by the jobs ledger.

Each stage is gated by its ``input_hash``: if a committed ``done`` row exists it is skipped;
otherwise it is marked ``running`` (its own committed breadcrumb), executed, and marked ``done``
in a single transaction with whatever metadata it wrote — so a stage that half-writes then
raises rolls back its data *and* is not marked done. A hard crash leaves a ``running``/absent
row, which re-runs on the next pass. Stages run in dependency (topological) order; a failed
stage stops its downstream.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.hashing import job_input_hash


@dataclass
class StageContext:
    """Handed to each stage. ``session`` is a live transaction; ``scratch`` passes in-memory
    data between stages within a single run (never a substitute for the truth store)."""

    content_hash: str
    file_path: str | None
    session: Any  # sqlalchemy Session — typed loosely to avoid import churn in stages
    scratch: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Stage:
    name: str
    run: Callable[[StageContext], None]
    model_version: str = "v1"
    params: dict[str, Any] = field(default_factory=dict)
    deps: tuple[str, ...] = ()

    def input_hash(self, content_hash: str) -> str:
        """The ledger key for this stage on a given source. Single source of this computation so
        the runner and callers (tests, observability) can never drift on defaults."""
        return job_input_hash(content_hash, self.name, self.model_version, self.params)


@dataclass
class StageResult:
    stage: str
    input_hash: str
    status: str  # "done" | "skipped" | "error"
    error: str | None = None


def _toposort(stages: Iterable[Stage]) -> list[Stage]:
    """Kahn's algorithm. Raises on unknown deps or cycles."""
    by_name = {s.name: s for s in stages}
    indeg = {s.name: 0 for s in by_name.values()}
    adj: dict[str, list[str]] = {s.name: [] for s in by_name.values()}
    for s in by_name.values():
        for dep in s.deps:
            if dep not in by_name:
                raise ValueError(f"stage {s.name!r} depends on unknown stage {dep!r}")
            adj[dep].append(s.name)
            indeg[s.name] += 1

    queue = sorted(n for n, d in indeg.items() if d == 0)
    order: list[str] = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
                queue.sort()
    if len(order) != len(by_name):
        raise ValueError("cycle detected in DAG")
    return [by_name[n] for n in order]


def run_dag(
    content_hash: str,
    stages: Iterable[Stage],
    *,
    file_path: str | None = None,
) -> list[StageResult]:
    """Run every stage for one source in dependency order, gated by the ledger."""
    results: list[StageResult] = []
    scratch: dict[str, Any] = {}

    for stage in _toposort(stages):
        input_hash = stage.input_hash(content_hash)

        # Skip if already done (the idempotency check).
        with session_scope() as session:
            if repo.is_stage_done(session, input_hash):
                results.append(StageResult(stage.name, input_hash, "skipped"))
                continue
            # Breadcrumb: mark running in its own committed transaction.
            repo.begin_job(
                session,
                content_hash=content_hash,
                stage=stage.name,
                input_hash=input_hash,
                model_version=stage.model_version,
                params=stage.params,
            )

        # Execute the stage and mark done atomically with its writes.
        try:
            with session_scope() as session:
                ctx = StageContext(content_hash, file_path, session, scratch)
                stage.run(ctx)
                repo.complete_job(session, input_hash)
            results.append(StageResult(stage.name, input_hash, "done"))
        except Exception as exc:  # noqa: BLE001 - record and halt downstream
            with session_scope() as session:
                repo.fail_job(session, input_hash, repr(exc))
            results.append(StageResult(stage.name, input_hash, "error", repr(exc)))
            break

    return results
