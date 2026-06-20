"""wiki-merge mechanics — fold one entity into another, and the batch heal for slug collisions.

The single-merge side effects (repoint via ``repo.merge_entities``, rewrite the survivor page, stub a
redirect *unless* the slugs collide, drop the merged-away name vector) live here so the ``wiki-merge``
command and the ``--slug-collisions`` batch share one code path. Every merge records an
``entity_merges`` row, so it stays reversible (``docs/entity-resolution.md``: prefer fragmentation
over conflation — and when we do conflate, make it cheap to undo).
"""

from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy import func, select

from doctalk.db import repo
from doctalk.db.models import Claim, Entity
from doctalk.synth import pages, wikirepo
from doctalk.vector import store as vstore

_SEP = re.compile(r"[\s_]+")


def merge_key(norm_key: str) -> str:
    """Underscore/whitespace-insensitive form of a norm_key — the auto-merge rail. Two entities are
    safe to fold together when these match: it catches the underscore-vs-space dupes
    (``afh_channel_map`` / "afh channel map") that fragmented before ``norm_key`` collapsed
    underscores, while keeping genuinely distinct slug collisions apart (``c[t+1`` vs ``c[t-1`` differ
    in the operators, not the spacing, so their keys stay different and they're left to a human)."""
    return _SEP.sub(" ", norm_key).strip()


def apply_merge(session, src: Entity, dst: Entity, reason: str) -> int:
    """Fold ``src`` into ``dst``: repoint claims/mentions, rewrite the survivor page, stub a redirect
    (skipped when src and dst share a slug — the one file already IS the survivor's), drop src's name
    vector. Returns the ``entity_merges`` id. The caller writes ``index.md`` and commits, so a batch
    collapses into a single wiki commit."""
    from doctalk.db.models import utcnow

    src_name, src_slug = src.name, pages.slug_for(src)
    dst_id, dst_slug = dst.id, pages.slug_for(dst)
    merge_id = repo.merge_entities(session, src.id, dst.id, reason=reason).id

    dst = session.get(Entity, dst_id)  # reload with merged aliases/source_count
    survivor_path = f"entities/{dst_slug}.md"
    md_hash = wikirepo.write_page(survivor_path, pages.render_entity_page(session, dst))
    repo.upsert_wiki_page(
        session, path=survivor_path, title=dst.name, kind="entity", entity_id=dst_id,
        source_count=dst.source_count, last_synth_at=utcnow(), md_hash=md_hash,
    )
    if src_slug != dst_slug:
        # Only stub a redirect when src had its OWN file. With a slug collision (shared path) the
        # survivor page we just wrote IS that file; a stub here would clobber it with "merged into
        # [[itself]]". src.wiki_path already points at the survivor, so following the merge still works.
        wikirepo.write_page(
            f"entities/{src_slug}.md",
            f"# {src_name}\n\n> merged\n\nMerged into [[{dst_slug}|{dst.name}]].\n",
        )
    vstore.delete_entity_name(src.id)
    return merge_id


def _claim_count(session, entity_id: int) -> int:
    return session.scalar(
        select(func.count()).select_from(Claim).where(Claim.entity_id == entity_id)
    ) or 0


def plan_slug_collision_merges(session) -> tuple[list[tuple[Entity, Entity]], list[tuple[Entity, Entity, str]]]:
    """Propose merges for active entities that share a slug. Per colliding slug the survivor is the
    richest member (most claims, tie -> source_count, tie -> lowest id); each other member is merged
    into it ONLY when their merge keys match (underscore/space-insensitive) AND types are compatible.
    Otherwise the pair is reported as skipped with a reason, never auto-merged.

    Returns ``(mergeable, skipped)`` where mergeable items are ``(src, dst)`` and skipped items are
    ``(src, dst, reason)``. Read-only — the caller decides whether to apply."""
    from doctalk.synth.resolve import _types_compatible

    groups: dict[str, list[Entity]] = defaultdict(list)
    for e in session.scalars(select(Entity).where(Entity.status == "active")):
        groups[pages.slug_for(e)].append(e)

    mergeable: list[tuple[Entity, Entity]] = []
    skipped: list[tuple[Entity, Entity, str]] = []
    for es in groups.values():
        if len(es) < 2:
            continue
        survivor = max(es, key=lambda e: (_claim_count(session, e.id), e.source_count, -e.id))
        for e in es:
            if e.id == survivor.id:
                continue
            if merge_key(e.norm_key) != merge_key(survivor.norm_key):
                skipped.append((e, survivor, "distinct norm_key — only the slugifier collides them"))
            elif not (_types_compatible(survivor.type, e.type) or _types_compatible(e.type, survivor.type)):
                skipped.append((e, survivor, f"incompatible types ({e.type} vs {survivor.type})"))
            else:
                mergeable.append((e, survivor))
    return mergeable, skipped


def merge_slug_collisions(session) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """Apply the safe slug-collision merges and rewrite ``index.md``. Returns ``(applied, skipped)``
    as ``(src_name, dst_name[, reason])`` tuples (names captured before the merge mutates them). The
    caller commits the wiki. Shared by the ``wiki-merge --slug-collisions`` CLI and the /maintenance
    API so both heal identically."""
    mergeable, skipped = plan_slug_collision_merges(session)
    applied: list[tuple[str, str]] = []
    for src, dst in mergeable:
        sname, dname = src.name, dst.name
        apply_merge(session, src, dst, reason="slug-collision batch")
        applied.append((sname, dname))
    if applied:
        wikirepo.write_page("index.md", pages.render_index(session))
    return applied, [(s.name, d.name, why) for s, d, why in skipped]
