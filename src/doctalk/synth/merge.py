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


def canonical_display_name(names: list[str]) -> str:
    """Choose one clean, consistent title for a merged survivor from the colliding spellings.

    A slug collision means the same concept was written both with underscores and with spaces, so the
    underscore is incidental formatting (not a code symbol — a real identifier wouldn't have a spaced
    twin in the corpus). We render spaces and acronym-preserving Title Case: 'Channel_Map'/'channel
    map' -> 'Channel Map', 'AFH_channel_map' -> 'AFH Channel Map', 'CS_DRBG' -> 'CS DRBG'. Purely
    cosmetic — the raw spellings survive as aliases and the slug (from norm_key) is untouched."""
    # Start from the spelling carrying the most case information so real acronyms (AFH, SDU, DRBG…)
    # survive; ties keep the first (the survivor's own spelling).
    base = max(names, key=lambda n: sum(c.isupper() for c in n))
    # Capitalize lowercase words; leave already-cased tokens (acronyms, MixedCase) alone.
    return " ".join(t if t != t.lower() else t.capitalize() for t in base.replace("_", " ").split())


def apply_merge(session, src: Entity, dst: Entity, reason: str, *, prefer_name: bool = False) -> int:
    """Fold ``src`` into ``dst``: repoint claims/mentions, rewrite the survivor page, stub a redirect
    (skipped when src and dst share a slug — the one file already IS the survivor's), drop src's name
    vector. Returns the ``entity_merges`` id. The caller writes ``index.md`` and commits, so a batch
    collapses into a single wiki commit.

    ``prefer_name`` (set by the slug-collision batch) gives the survivor a clean, consistent title
    drawn from both colliding spellings — see ``canonical_display_name``. Off for a deliberate single
    merge, where the chosen target's name is respected as-is."""
    from doctalk.db.models import utcnow

    src_name, src_slug = src.name, pages.slug_for(src)
    dst_id, dst_slug = dst.id, pages.slug_for(dst)
    display = canonical_display_name([dst.name, src.name]) if prefer_name else None
    merge_id = repo.merge_entities(session, src.id, dst.id, reason=reason, display_name=display).id

    dst = session.get(Entity, dst_id)  # reload with merged aliases/source_count/renamed title
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
    """Propose merges for active entities that share a slug. Per colliding slug the survivor is, by
    preference, the member already named the clean canonical form (so it keeps that title with no
    rename and no (name,type) clash) when that member is a compatible merge partner of the richest;
    otherwise the richest (most claims, tie -> source_count, tie -> lowest id). Each other member is
    merged into the survivor ONLY when their merge keys match (underscore/space-insensitive) AND types
    are compatible; otherwise the pair is skipped with a reason, never auto-merged.

    Returns ``(mergeable, skipped)`` where mergeable items are ``(src, dst)`` and skipped items are
    ``(src, dst, reason)``. Read-only — the caller decides whether to apply."""
    from doctalk.synth.resolve import _types_compatible

    def _mergeable(a: Entity, b: Entity) -> bool:
        return merge_key(a.norm_key) == merge_key(b.norm_key) and (
            _types_compatible(a.type, b.type) or _types_compatible(b.type, a.type)
        )

    groups: dict[str, list[Entity]] = defaultdict(list)
    for e in session.scalars(select(Entity).where(Entity.status == "active")):
        groups[pages.slug_for(e)].append(e)

    mergeable: list[tuple[Entity, Entity]] = []
    skipped: list[tuple[Entity, Entity, str]] = []
    for es in groups.values():
        if len(es) < 2:
            continue
        richest = max(es, key=lambda e: (_claim_count(session, e.id), e.source_count, -e.id))
        # If a member already carries the clean canonical title and can merge with the richest, let it
        # survive — no rename, and the merge can't collide with its own partner's (name, type).
        canon = canonical_display_name([e.name for e in es])
        survivor = next(
            (e for e in es if e.name == canon and e.id != richest.id and _mergeable(e, richest)),
            richest,
        )
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


def merge_slug_collisions(session) -> tuple[list[tuple[str, str, int]], list[tuple[str, str, str]]]:
    """Apply the safe slug-collision merges and rewrite ``index.md``. Returns ``(applied, skipped)``
    where applied items are ``(src_name, dst_name, merge_id)`` (names captured before the merge mutates
    them; the merge_id lets the caller stamp the wiki commit onto these rows so the batch is undoable)
    and skipped items are ``(src_name, dst_name, reason)``. The caller commits the wiki. Shared by the
    ``wiki-merge --slug-collisions`` CLI and the /maintenance API so both heal identically."""
    mergeable, skipped = plan_slug_collision_merges(session)
    applied: list[tuple[str, str, int]] = []
    for src, dst in mergeable:
        sname = src.name
        merge_id = apply_merge(session, src, dst, reason="slug-collision batch", prefer_name=True)
        applied.append((sname, dst.name, merge_id))  # dst.name is now the cleaned-up survivor title
    if applied:
        wikirepo.write_page("index.md", pages.render_index(session))
    return applied, [(s.name, d.name, why) for s, d, why in skipped]


def undo_merge(session, merge) -> tuple[str, str]:
    """Reverse one merge: repoint its claims/mentions back (``repo.unmerge_entities``), restore the
    resurrected entity's name vector + real page, and rewrite the survivor's now-thinner page. Returns
    ``(resurrected_name, survivor_name)``. The caller rewrites ``index.md`` and commits, so a batch
    undo collapses into one wiki commit.

    Note: undoing a *slug-collision* merge faithfully restores the collision — src and dst share a slug
    again, so both want the same file. We write the survivor last (it's the richer page) and let lint
    re-flag the pair, exactly as before the merge. Undo restores the prior state, warts and all."""
    from doctalk.db.models import utcnow
    from doctalk.synth.resolve import _embed, _store_vector

    dst_id = merge.into_id
    src = repo.unmerge_entities(session, merge)  # resurrects src (status active, wiki_path cleared)
    dst = session.get(Entity, dst_id)

    # apply_merge dropped src's name vector; restore it or src comes back invisible to retrieval/resolution.
    _store_vector(session, src.id, src.type, _embed(src.name))

    def _write(entity) -> None:
        path = f"entities/{pages.slug_for(entity)}.md"
        md_hash = wikirepo.write_page(path, pages.render_entity_page(session, entity))
        repo.upsert_wiki_page(
            session, path=path, title=entity.name, kind="entity", entity_id=entity.id,
            source_count=entity.source_count, last_synth_at=utcnow(), md_hash=md_hash,
        )
        repo.set_entity_wiki_path(session, entity.id, path)

    _write(src)
    _write(dst)  # survivor written last: on a shared-slug collision its richer page is what remains
    return src.name, dst.name


def undo_batch(session, sha: str) -> list[tuple[str, str]]:
    """Reverse every merge a wiki commit enacted (the unit the maintenance page's "Undo this batch"
    button reverses), newest first, then rewrite ``index.md``. Returns ``(resurrected, survivor)``
    name pairs. The caller commits the wiki."""
    undone: list[tuple[str, str]] = []
    for merge in repo.get_merges_by_sha(session, sha):
        undone.append(undo_merge(session, merge))
    if undone:
        wikirepo.write_page("index.md", pages.render_index(session))
    return undone
