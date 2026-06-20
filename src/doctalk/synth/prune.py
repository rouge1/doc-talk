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


def orphan_entities(session) -> list[Entity]:
    """Active entities no source attests anymore — no claims, no mentions. A re-synthesis clears a
    file's claims/mentions and re-resolves what it extracts *now*; entities the new sweep didn't
    re-extract stay behind as zero-attestation rows whose pages render claims the truth store no
    longer holds. Reaping is reversible: a future mention reactivates them (resolve._apply_match)."""
    from sqlalchemy import exists

    from doctalk.db.models import Claim, Mention

    has_claim = exists().where(Claim.entity_id == Entity.id)
    has_mention = exists().where(Mention.entity_id == Entity.id)
    return list(
        session.scalars(
            select(Entity).where(Entity.status == "active", ~has_claim, ~has_mention)
        )
    )


def prune(session, wiki_dir: Path) -> list[str]:
    """Prune gate-failing + unattested entities: status -> ``pruned``, page file + catalog row +
    name vector removed. Returns the pruned names; the caller regenerates index, logs, commits."""
    pruned: list[str] = []
    seen: set[int] = set()  # a gate-failing entity with no attestation qualifies on both counts
    for entity in (*junk_entities(session), *orphan_entities(session)):
        if entity.id in seen:
            continue
        seen.add(entity.id)
        if entity.wiki_path:
            # Slug-collision guard: only delete the page file + catalog row when the catalog row at
            # this path still belongs to THIS entity. Slugs aren't unique — an unattested "HCI" and
            # an active "hci" both resolve to entities/hci.md, and integrate's last-writer-wins means
            # the survivor may own the shared file + catalog row. Deleting by path alone then destroys
            # the live entity's page (the regression that orphaned 204 pages / 1,534 links). When a
            # different entity owns the row (or none does), we drop only this entity's stale pointer.
            page = repo.get_wiki_page_by_path(session, entity.wiki_path)
            if page is not None and page.entity_id == entity.id:
                (wiki_dir / entity.wiki_path).unlink(missing_ok=True)
                repo.delete_wiki_page(session, entity.wiki_path)
        repo.prune_entity(session, entity.id)
        vstore.delete_entity_name(entity.id)  # derived index; keeps junk out of page retrieval
        pruned.append(entity.name)
    return pruned
