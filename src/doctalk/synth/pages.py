"""Markdown rendering for synthesis pages — the prose layer of the compounding wiki.

Deterministic and provenance-safe by construction: every claim on a page is rendered *with* its
source citation (filename + page, resolved from ``claim_sources``), so the "no unsupported claims"
invariant holds structurally rather than by trust. Contradicted claims render in their own flagged
section (citing the conflicting sources) instead of being overwritten. ``[[wikilinks]]`` to
co-mentioned entities keep pages interlinked and Obsidian-browsable.

The body is composed from the DB (all claims for the entity, across every source — the page is
cumulative), with an optional LLM-authored lead paragraph passed in by the stage; rendering itself
stays pure so it's fully testable without a model.
"""

from __future__ import annotations

import re

from doctalk.db import repo
from doctalk.db.models import Chunk, Entity, File

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def base_slug_for(entity: Entity) -> str:
    """The slug derived purely from the normalized key — what the lossy slugifier produces, *ignoring*
    any disambiguation override. This is the grouping key for collision detection: two entities collide
    exactly when their base slugs match."""
    base = entity.norm_key or entity.name.lower()
    return _SLUG_STRIP.sub("-", base).strip("-") or f"entity-{entity.id}"


def slug_for(entity: Entity) -> str:
    """Stable filename stem for an entity page. An explicit ``slug`` override (set by
    ``synth.disambiguate`` when a genuinely-distinct sibling would otherwise share this base slug) wins;
    otherwise the slug is derived from the normalized key. Every page write and ``[[wikilink]]`` routes
    through here, so setting the override moves the page and all inbound links in one stroke."""
    return entity.slug or base_slug_for(entity)


def _provenance(session, claim) -> str:
    """Human-readable source citation for a claim: 'filename p.N; …' from its claim_sources.
    Always non-empty (synth_entities records at least a file-level source) — the invariant guard."""
    parts: set[str] = set()
    for cs in repo.get_claim_sources(session, claim.id):
        file = session.get(File, cs.file_id)
        name = file.filename if file else f"file:{cs.file_id}"
        if cs.chunk_id is not None:
            chunk = session.get(Chunk, cs.chunk_id)
            parts.add(f"{name} p.{chunk.page}" if chunk else name)
        else:
            parts.add(name)
    return "source: " + "; ".join(sorted(parts)) if parts else "source: (unknown)"


def render_entity_page(session, entity: Entity, summary: str | None = None) -> str:
    """Render an entity's full wiki page from its claims, provenance, and co-mentions."""
    claims = repo.get_claims_for_entity(session, entity.id)
    active = [c for c in claims if c.status == "active"]
    contradicted = [c for c in claims if c.status == "contradicted"]

    out: list[str] = [
        f"# {entity.name}",
        "",
        f"> **{entity.type}** · {entity.source_count} source(s)",
        "",
    ]
    if summary:
        out += [summary.strip(), ""]

    out += ["## Claims", ""]
    if active:
        for c in active:
            out.append(f"- {c.text}  _({_provenance(session, c)})_")
    else:
        out.append("_No active claims._")
    out.append("")

    if contradicted:  # flagged, not overwritten — cite the conflicting sources
        out += ["## Contradictions", ""]
        for c in contradicted:
            out.append(f"- ⚠ {c.text}  _({_provenance(session, c)})_")
        out.append("")

    related = repo.get_comention_entity_ids(session, entity.id)
    links = []
    for rid in related:
        r = session.get(Entity, rid)
        if r is not None:
            links.append(f"[[{slug_for(r)}|{r.name}]]")
    if links:
        out += ["## Related", "", " · ".join(links), ""]

    return "\n".join(out).rstrip() + "\n"


def render_index(session) -> str:
    """Regenerate ``index.md`` — the content catalog — from the wiki_pages table (every ingest)."""
    pages = repo.get_wiki_pages_by_kind(session, "entity")
    out = [
        "# Wiki index",
        "",
        "The content catalog for this knowledge base. Updated every ingest.",
        "",
        "## Sources",
        "",
    ]
    sources = repo.get_wiki_pages_by_kind(session, "source")
    if sources:
        for p in sources:
            stem = p.path.rsplit("/", 1)[-1].removesuffix(".md")
            out.append(f"- [[{stem}|{p.title}]]")
    else:
        out.append("_None yet._")
    out += ["", "## Entities", ""]
    listed = 0
    for p in pages:
        if p.entity_id is not None:  # skip entities merged away (their page is a redirect stub)
            e = session.get(Entity, p.entity_id)
            if e is not None and e.status == "merged_into":
                continue
        stem = p.path.rsplit("/", 1)[-1].removesuffix(".md")
        out.append(f"- [[{stem}|{p.title}]] — {p.source_count} source(s)")
        listed += 1
    if not listed:
        out.append("_None yet._")
    out += ["", "## Concepts", "", "_None yet._", "", "## Topics", ""]
    topics = repo.get_wiki_pages_by_kind(session, "topic")
    if topics:
        for p in topics:
            stem = p.path.rsplit("/", 1)[-1].removesuffix(".md")
            out.append(f"- [[{stem}|{p.title}]]")
    else:
        out.append("_None yet._")

    queries = repo.get_wiki_pages_by_kind(session, "query")
    out += ["", "## Queries", ""]
    if queries:
        for p in queries:
            stem = p.path.rsplit("/", 1)[-1].removesuffix(".md")
            out.append(f"- [[{stem}|{p.title}]]")
    else:
        out.append("_None yet._")
    return "\n".join(out).rstrip() + "\n"
