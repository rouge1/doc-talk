"""wiki-lint / wiki-audit — keep the synthesis layer honest as it grows.

``lint`` is a health check (periodic, not per-ingest): orphan pages (no inbound ``[[links]]``),
unsupported claims (no ``claim_sources`` — the provenance invariant), entities still ``#unresolved``
plus the open review queue, entities mentioned but never materialized into a page, near-duplicate
entities worth a ``wiki-merge``, and unresolved contradictions. ``audit`` checks wiki↔truth integrity:
every cited chunk still exists, and the ``wiki_pages`` catalog matches what's on disk. ``lint`` reports;
``materialize_missing`` is the one safe mechanical auto-fix (create absent pages — never overwrite).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from doctalk.db import repo
from doctalk.db.models import Chunk, Claim, ClaimSource, Entity, WikiPage
from doctalk.synth import pages, wikirepo

_LINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
_DUP_JACCARD = 0.5  # norm_key token overlap above which two same-type entities look like duplicates


@dataclass
class Finding:
    kind: str           # orphan | unsupported_claim | unresolved | missing_page | duplicate | …
    detail: str
    ref: str | None = None
    link: str | None = None   # a wiki stem this finding points at, so the dashboard can link to the page
    entity_id: int | None = None  # the entity this finding is about, for an in-place action (Keep)
    candidate: dict | None = None  # for unresolved: the active entity it most likely duplicates


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if (a and b) else 0.0


# --- health check ----------------------------------------------------------


def _link_targets(wiki_dir: Path) -> set[str]:
    """Every ``[[target]]`` referenced across the wiki markdown (the inbound-link graph)."""
    targets: set[str] = set()
    for md in wiki_dir.rglob("*.md"):
        for m in _LINK.finditer(md.read_text(encoding="utf-8")):
            targets.add(m.group(1).strip())
    return targets


def _orphan_pages(session, wiki_dir: Path) -> list[Finding]:
    if not wiki_dir.exists():
        return []
    targets = _link_targets(wiki_dir)
    out = []
    for p in repo.get_wiki_pages_by_kind(session, "entity"):
        entity = session.get(Entity, p.entity_id) if p.entity_id else None
        if entity is not None and entity.status == "merged_into":
            continue  # redirect stub, not a real orphan
        if Path(p.path).stem not in targets:
            out.append(Finding("orphan", "no inbound [[links]]", p.title))
    return out


def _unsupported_claims(session) -> list[Finding]:
    from sqlalchemy import select

    sourced = select(ClaimSource.claim_id).distinct()
    out = []
    for claim in session.scalars(select(Claim).where(Claim.id.not_in(sourced))):
        entity = session.get(Entity, claim.entity_id)
        out.append(Finding("unsupported_claim", claim.text[:80], entity.name if entity else None))
    return out


def _unresolved(session) -> list[Finding]:
    from sqlalchemy import select

    from doctalk.synth.dedupe import best_candidate  # lazy: dedupe imports lint at module load

    out = [
        Finding("unresolved", "provisional #unresolved page", e.name,
                link=pages.slug_for(e), entity_id=e.id, candidate=best_candidate(session, e))
        for e in session.scalars(select(Entity).where(Entity.status == "unresolved"))
    ]
    n = len(repo.get_open_reviews(session))
    if n:
        out.append(Finding("unresolved", f"{n} item(s) in the review queue", "entity_review"))
    return out


def _missing_pages(session) -> list[Finding]:
    from sqlalchemy import select

    rows = session.scalars(
        select(Entity).where(
            Entity.status == "active", Entity.wiki_path.is_(None), Entity.source_count > 0
        )
    )
    return [Finding("missing_page", "mentioned but no wiki page", e.name) for e in rows]


def _deleted_pages(session, wiki_dir: Path) -> list[Finding]:
    """Active entities whose page file vanished from disk — ``wiki_path`` still points at a path
    that no longer exists. Distinct from ``_missing_pages`` (an entity that never got a page): here
    the file was destroyed out from under a live entity (the prune slug-collision bug left 204 such
    victims, invisible to the old ``wiki_path IS NULL`` check). ``materialize_missing`` regenerates
    them; this is the lint blind spot that let the loss go silent."""
    if not wiki_dir.exists():
        return []
    from sqlalchemy import select

    out = []
    for e in session.scalars(
        select(Entity).where(Entity.status == "active", Entity.wiki_path.is_not(None))
    ):
        if not (wiki_dir / e.wiki_path).exists():
            out.append(Finding("deleted_page", "page file gone from disk — run wiki-lint --fix", e.name))
    return out


def _slug_collisions(session) -> list[Finding]:
    """Active entities whose ``pages.slug_for`` collides — they map to the same ``entities/<slug>.md``,
    so integrate's last-writer-wins silently drops one's page and the wiki shows duplicate cards. Most
    are underscore-vs-space dupes that ``wiki-merge --slug-collisions`` folds together; a few (``C[t+1]``
    vs ``C[t-1]``) are genuinely distinct and only the slugifier conflates them — those stay manual."""
    from collections import defaultdict

    from sqlalchemy import select

    groups: dict[str, list[Entity]] = defaultdict(list)
    for e in session.scalars(select(Entity).where(Entity.status == "active")):
        groups[pages.slug_for(e)].append(e)
    out = []
    for slug, es in groups.items():
        if len(es) > 1:
            names = ", ".join(sorted(e.name for e in es))
            out.append(Finding("slug_collision", f"{len(es)} share this slug: {names}", slug))
    return out


def _duplicate_candidates(session) -> list[Finding]:
    from sqlalchemy import select

    actives = list(session.scalars(select(Entity).where(Entity.status == "active")))
    by_type: dict[str, list[Entity]] = {}
    for e in actives:
        by_type.setdefault(e.type, []).append(e)
    out = []
    for group in by_type.values():
        for i in range(len(group)):
            a = group[i]
            for j in range(i + 1, len(group)):
                b = group[j]
                if _jaccard(set(a.norm_key.split()), set(b.norm_key.split())) >= _DUP_JACCARD:
                    out.append(
                        Finding("duplicate", f"similar to '{b.name}' — consider wiki-merge", a.name)
                    )
    return out


def _contradictions(session) -> list[Finding]:
    from sqlalchemy import select

    return [
        Finding("contradiction", c.text[:80], session.get(Entity, c.entity_id).name)
        for c in session.scalars(select(Claim).where(Claim.status == "contradicted"))
        if session.get(Entity, c.entity_id) is not None
    ]


def _unattested(session) -> list[Finding]:
    """Active entities with no claims and no mentions (left behind by a re-synthesis) — their
    pages render claims the truth store no longer holds. One summary finding; wiki-prune reaps."""
    from doctalk.synth.prune import orphan_entities

    n = len(orphan_entities(session))
    if not n:
        return []
    return [Finding("unattested", f"{n} active entit(ies) with no claims/mentions — run wiki-prune")]


def _stale_queries(session, wiki_dir: Path) -> list[Finding]:
    """Filed query answers whose cited entity pages gained claims after filing — the answer may no
    longer reflect the corpus; re-ask to refresh (the re-ask appends a dated Update snapshot)."""
    stem_to_entity = {
        Path(p.path).stem: p.entity_id
        for p in repo.get_wiki_pages_by_kind(session, "entity")
        if p.entity_id is not None
    }
    out = []
    for page in repo.get_wiki_pages_by_kind(session, "query"):
        md_path = wiki_dir / page.path
        if page.last_synth_at is None or not md_path.exists():
            continue
        cited = [
            stem_to_entity[m.group(1).strip()]
            for m in _LINK.finditer(md_path.read_text(encoding="utf-8"))
            if m.group(1).strip() in stem_to_entity
        ]
        newest = repo.latest_claim_at_for_entities(session, cited)
        if newest is not None and newest > page.last_synth_at:
            out.append(Finding("stale_query", "cited entities gained claims — re-ask", page.title))
    return out


def lint(session, wiki_dir: Path) -> list[Finding]:
    return [
        *_orphan_pages(session, wiki_dir),
        *_unsupported_claims(session),
        *_unresolved(session),
        *_missing_pages(session),
        *_deleted_pages(session, wiki_dir),
        *_slug_collisions(session),
        *_duplicate_candidates(session),
        *_contradictions(session),
        *_unattested(session),
        *_stale_queries(session, wiki_dir),
    ]


# --- integrity audit (wiki <-> truth drift) --------------------------------


def audit(session, wiki_dir: Path) -> list[Finding]:
    from sqlalchemy import exists, select

    out: list[Finding] = []
    # every cited chunk still exists — one NOT EXISTS pass, not a per-source lookup. The old per-row
    # session.get(Chunk, …) was an N+1 that took ~9s on a full corpus, and the whole findings ledger
    # (every maintenance number) waits on audit, so the page sat on "·" until it finished.
    chunk_gone = ~exists().where(Chunk.id == ClaimSource.chunk_id)
    for cs in session.scalars(
        select(ClaimSource).where(ClaimSource.chunk_id.is_not(None), chunk_gone)
    ):
        out.append(Finding("dangling_source", f"claim {cs.claim_id} cites a missing chunk"))
    # claims with no provenance at all (the invariant guard, from the audit side too)
    sourced = select(ClaimSource.claim_id).distinct()
    for claim in session.scalars(select(Claim).where(Claim.id.not_in(sourced))):
        out.append(Finding("unsupported_claim", f"claim {claim.id} has no claim_sources"))
    # catalog row vs on-disk file
    for p in session.scalars(select(WikiPage)):
        if not (wiki_dir / p.path).exists():
            out.append(Finding("catalog_drift", "page in catalog but missing on disk", p.path))
    return out


# --- the one safe mechanical fix -------------------------------------------


def materialize_missing(session, wiki_dir: Path) -> list[str]:
    """Create pages for active entities that have none ON DISK — both entities that never got one
    (``wiki_path`` NULL) and victims whose file was deleted out from under them (``wiki_path`` set
    but the file is gone, e.g. the prune slug-collision bug). Never overwrites a page that exists.
    Returns the entity names (re)materialized; the caller regenerates the index + commits."""
    from datetime import datetime, timezone
    from sqlalchemy import select

    created: list[str] = []
    # Scan every active entity (not just wiki_path-IS-NULL): a victim's pointer is non-null but
    # stale, so the old query skipped exactly the rows that needed healing. The has-a-file fast
    # path below makes this cheap for the (vast) majority whose page is intact.
    for entity in session.scalars(select(Entity).where(Entity.status == "active")):
        path = entity.wiki_path or f"entities/{pages.slug_for(entity)}.md"
        if (wiki_dir / path).exists():
            if entity.wiki_path != path:  # file present, pointer drifted — just relink
                repo.set_entity_wiki_path(session, entity.id, path)
            continue  # never overwrite an existing page
        if not repo.get_claims_for_entity(session, entity.id):
            continue  # nothing to write yet
        md_hash = wikirepo.write_page(path, pages.render_entity_page(session, entity))
        repo.upsert_wiki_page(
            session, path=path, title=entity.name, kind="entity", entity_id=entity.id,
            source_count=entity.source_count,
            last_synth_at=datetime.now(timezone.utc).replace(tzinfo=None), md_hash=md_hash,
        )
        repo.set_entity_wiki_path(session, entity.id, path)
        created.append(entity.name)
    return created
