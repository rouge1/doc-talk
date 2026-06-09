"""wiki-prune — retroactively apply the pageworthiness gate to already-synthesized entities.

The shape gate (``synth.gate``) now rejects data values at extraction time, but entities synthesized
before it existed (e.g. "0", "0x0009" from Core_v6.0.pdf) are already canonical rows with pages.
Re-sweeping a large spec costs hundreds of LLM calls; pruning from the truth store is free: mark the
entity ``pruned`` (reversible — its claims/mentions stay, auditable), delete its page file + catalog
row + name vector, and let the caller regenerate the index and commit.

Salience (the one-window-one-claim signal) is *not* re-checked here — window counts aren't stored.
A future re-drop of the source re-runs extraction with both gates and re-synthesizes cleanly.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from doctalk.db import repo
from doctalk.db.models import Entity
from doctalk.synth.gate import is_pageworthy
from doctalk.vector import store as vstore


def junk_entities(session) -> list[Entity]:
    """Entities that fail the pageworthiness gate (active/unresolved only — merged rows are
    redirects pointing at a survivor, and re-pruning already-pruned rows is a no-op)."""
    rows = session.scalars(select(Entity).where(Entity.status.in_(("active", "unresolved"))))
    return [e for e in rows if not is_pageworthy(e.name, e.type)]


def prune(session, wiki_dir: Path) -> list[str]:
    """Prune every gate-failing entity: status -> ``pruned``, page file + catalog row + name vector
    removed. Returns the pruned names; the caller regenerates the index, logs, and commits."""
    pruned: list[str] = []
    for entity in junk_entities(session):
        if entity.wiki_path:
            (wiki_dir / entity.wiki_path).unlink(missing_ok=True)
            repo.delete_wiki_page(session, entity.wiki_path)
        repo.prune_entity(session, entity.id)
        vstore.delete_entity_name(entity.id)  # derived index; keeps junk out of page retrieval
        pruned.append(entity.name)
    return pruned
