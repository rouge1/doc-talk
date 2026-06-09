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

    out = [
        Finding("unresolved", "provisional #unresolved page", e.name)
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
        *_duplicate_candidates(session),
        *_contradictions(session),
        *_stale_queries(session, wiki_dir),
    ]


# --- integrity audit (wiki <-> truth drift) --------------------------------


def audit(session, wiki_dir: Path) -> list[Finding]:
    from sqlalchemy import select

    out: list[Finding] = []
    # every cited chunk still exists
    for cs in session.scalars(select(ClaimSource).where(ClaimSource.chunk_id.is_not(None))):
        if session.get(Chunk, cs.chunk_id) is None:
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
    """Create pages for active entities that have none (never overwrites an existing page). Returns
    the entity names materialized; the caller regenerates the index + commits."""
    from datetime import datetime, timezone
    from sqlalchemy import select

    created: list[str] = []
    rows = session.scalars(
        select(Entity).where(Entity.status == "active", Entity.wiki_path.is_(None))
    )
    for entity in rows:
        if not repo.get_claims_for_entity(session, entity.id):
            continue  # nothing to write yet
        path = f"entities/{pages.slug_for(entity)}.md"
        if (wiki_dir / path).exists():
            repo.set_entity_wiki_path(session, entity.id, path)  # catalog drifted; just relink
            continue
        md_hash = wikirepo.write_page(path, pages.render_entity_page(session, entity))
        repo.upsert_wiki_page(
            session, path=path, title=entity.name, kind="entity", entity_id=entity.id,
            source_count=entity.source_count,
            last_synth_at=datetime.now(timezone.utc).replace(tzinfo=None), md_hash=md_hash,
        )
        repo.set_entity_wiki_path(session, entity.id, path)
        created.append(entity.name)
    return created
