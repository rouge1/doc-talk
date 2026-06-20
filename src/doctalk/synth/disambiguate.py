"""slug disambiguation — give two genuinely-distinct entities that collide on slug their own pages.

A slug collision where the siblings share a ``merge_key`` is a real duplicate, and the ``wiki-merge
--slug-collisions`` heal folds those. But ``C[t-1]`` and ``C[t+1]`` normalize *differently* — only the
slugifier collides them, because ``slug_for`` runs ``[^a-z0-9]+ -> -`` and so flattens ``[``, ``+`` and
``-`` to the same dash (``c[t+1``/``c[t-1`` -> ``c-t-1``). Merging would conflate the cipher state one
step back with the one a step forward, so the merge heal correctly refuses and leaves them "for a
human". But there is nothing for a human to *decide*: both are real, both deserve a page. The fix is
mechanical — give the colliding siblings distinct filenames.

Per colliding base slug, the lowest-id sibling keeps the bare slug (so existing inbound ``[[base]]``
links still resolve) and each genuinely-distinct other sibling gets an explicit ``Entity.slug`` of
``<base>-<disc>``, where ``disc`` is a short blake3 of its norm_key — deterministic, so a re-ingest
reproduces the same assignment, and unique per distinct sibling. No claims move and nothing is
conflated; this only changes *where the page lives*. Reversible: clearing the override restores the
(buggy) shared slug, exactly as un-merge restores the collision it healed.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from doctalk.db import repo
from doctalk.db.models import Entity, utcnow
from doctalk.hashing import hash_bytes
from doctalk.synth import merge, pages, wikirepo


def _disc(entity: Entity) -> str:
    """A short, stable discriminator for an entity's disambiguated slug — derived from its norm_key, so
    distinct siblings get distinct suffixes and the same entity gets the same one across re-ingests."""
    return hash_bytes((entity.norm_key or entity.name.lower()).encode("utf-8"))[:6]


def _active(session) -> list[Entity]:
    return list(session.scalars(select(Entity).where(Entity.status == "active")))


def _base_groups(session) -> dict[str, list[Entity]]:
    """Active entities grouped by their *override-free* base slug — the collision groups."""
    groups: dict[str, list[Entity]] = defaultdict(list)
    for e in _active(session):
        groups[pages.base_slug_for(e)].append(e)
    return groups


def _winner_for_base(session, base: str) -> Entity | None:
    """The sibling that keeps the bare ``base`` slug: lowest-id active entity with no override whose
    base slug is ``base`` (so legacy ``[[base]]`` links keep resolving to the original)."""
    cands = [
        e for e in _active(session)
        if e.slug is None and pages.base_slug_for(e) == base
    ]
    return min(cands, key=lambda e: e.id) if cands else None


def plan_disambiguations(session) -> list[tuple[Entity, str, str]]:
    """Genuinely-distinct entities that collide on base slug and still need their own filename. Per
    base-slug group the lowest-id sibling keeps the bare slug; every *other* sibling whose ``merge_key``
    differs from it (i.e. not a fold-able duplicate — the merge heal owns those) gets ``<base>-<disc>``.
    Already-disambiguated entities are skipped (idempotent). Returns ``[(entity, base, new_slug)]``,
    read-only — the caller decides whether to apply."""
    out: list[tuple[Entity, str, str]] = []
    for base, es in _base_groups(session).items():
        if len(es) < 2:
            continue
        winner = min(es, key=lambda e: e.id)
        wkey = merge.merge_key(winner.norm_key)
        for e in es:
            if e.id == winner.id:
                continue
            if merge.merge_key(e.norm_key) == wkey:
                continue  # a fold-able duplicate of the winner — left to the slug-collision merge heal
            new_slug = f"{base}-{_disc(e)}"
            if e.slug == new_slug:
                continue  # already split out — nothing to do
            out.append((e, base, new_slug))
    return out


def _write_entity_page(session, entity: Entity) -> None:
    """(Re)write an entity's page at its *current* slug (override-aware) and reconcile the catalog +
    ``wiki_path``. Pure function of the DB, so re-running is idempotent."""
    path = f"entities/{pages.slug_for(entity)}.md"
    md_hash = wikirepo.write_page(path, pages.render_entity_page(session, entity))
    repo.upsert_wiki_page(
        session, path=path, title=entity.name, kind="entity", entity_id=entity.id,
        source_count=entity.source_count, last_synth_at=utcnow(), md_hash=md_hash,
    )
    repo.set_entity_wiki_path(session, entity.id, path)


def disambiguate_collisions(session) -> list[tuple[str, str, str, int]]:
    """Apply slug overrides for genuinely-distinct slug collisions, move each split entity to its own
    page, rewrite the winner's (now sole) base page from the DB, and regenerate ``index.md``. Returns
    ``[(name, base_slug, new_slug, entity_id)]`` — the receipt. The caller commits the wiki, so the
    whole batch is one commit (the handle the page's Undo reverses). Shared by the CLI and the API."""
    plan = plan_disambiguations(session)
    applied: list[tuple[str, str, str, int]] = []
    bases: set[str] = set()
    for e, base, new_slug in plan:
        e.slug = new_slug
        session.flush()  # so slug_for() below sees the override
        _write_entity_page(session, e)
        applied.append((e.name, base, new_slug, e.id))
        bases.add(base)
    # The base file may still hold the loser's clobbered content — rewrite the winner so the shared
    # slug ends up owned by the right (lower-id) sibling.
    for base in bases:
        winner = _winner_for_base(session, base)
        if winner is not None:
            _write_entity_page(session, winner)
    if applied:
        wikirepo.write_page("index.md", pages.render_index(session))
    return applied


def undo_disambiguation(session, entity: Entity) -> tuple[str, str]:
    """Clear one entity's slug override: it returns to the shared base slug (re-creating the collision
    the split fixed — undo restores the prior state, warts and all, mirroring un-merge). Retires its
    standalone page (catalog row + file) and rewrites the base page, winner last. Returns
    ``(name, base_slug)``."""
    old_path = entity.wiki_path
    base = pages.base_slug_for(entity)
    entity.slug = None
    session.flush()
    if old_path and old_path != f"entities/{base}.md":
        repo.delete_wiki_page(session, old_path)
        wikirepo.remove_page(old_path)
    _write_entity_page(session, entity)  # rejoins the base file (the collision is back)
    winner = _winner_for_base(session, base)
    if winner is not None and winner.id != entity.id:
        _write_entity_page(session, winner)  # written last: the original keeps the shared file
    return entity.name, base


def undo_disambiguations(session, entity_ids: list[int]) -> list[tuple[str, str]]:
    """Reverse a disambiguation batch by entity id (the unit the maintenance page's Undo reverses),
    then regenerate ``index.md``. Skips ids that aren't currently disambiguated. The caller commits."""
    undone: list[tuple[str, str]] = []
    for eid in entity_ids:
        e = session.get(Entity, eid)
        if e is not None and e.slug is not None:
            undone.append(undo_disambiguation(session, e))
    if undone:
        wikirepo.write_page("index.md", pages.render_index(session))
    return undone


def disambiguated_entities(session) -> list[Entity]:
    """Active entities that currently carry a slug override (the durable record of what's been split),
    so the page can show the receipt + Undo after a reload. Newest first."""
    return list(
        session.scalars(
            select(Entity)
            .where(Entity.status == "active", Entity.slug.is_not(None))
            .order_by(Entity.id.desc())
        )
    )
