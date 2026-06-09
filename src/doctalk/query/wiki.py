"""Wiki page retrieval — the synthesized substrate for wiki-first chat.

Semantic-search the entity name+definition vectors (the same index ``synth_resolve`` blocks on),
then load each matched entity's *active claims with their provenance*. This is the "read index →
drill into pages" step: cross-document answers are built from the compounding wiki first, with raw
chunk-RAG only filling gaps (see ``query.wikichat``). Embedding is indirected through
``_embed_query`` so tests stay model-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from doctalk.db import repo
from doctalk.db.models import Chunk, Entity, File
from doctalk.db.session import session_scope


@dataclass
class PageClaim:
    text: str
    sources: list[str] = field(default_factory=list)  # "filename p.N" display strings


@dataclass
class PageHit:
    entity_id: int
    name: str
    type: str
    path: str | None
    score: float
    claims: list[PageClaim] = field(default_factory=list)


def _embed_query(text: str) -> list[float] | None:
    try:
        from doctalk.models.embed import embed_query

        return embed_query(text)
    except Exception:  # noqa: BLE001 - no model: wiki retrieval yields nothing, chunk-RAG carries
        return None


def _claim_sources(session, claim_id: int) -> list[str]:
    out: set[str] = set()
    for cs in repo.get_claim_sources(session, claim_id):
        file = session.get(File, cs.file_id)
        name = file.filename if file else f"file:{cs.file_id}"
        if cs.chunk_id is not None:
            chunk = session.get(Chunk, cs.chunk_id)
            out.add(f"{name} p.{chunk.page}" if chunk else name)
        else:
            out.add(name)
    return sorted(out)


def retrieve_pages(question: str, k: int = 6, *, min_score: float | None = None) -> list[PageHit]:
    """Top-k active entity pages for the question, each with its claims + provenance.

    Pages below ``min_score`` (cosine name+definition relevance; default ``wiki_page_min_score``) are
    dropped so an off-topic wiki — e.g. only recipe entities — doesn't get cited for a question about
    something else just because those are the only pages that exist. Falls back to ``settings``.
    """
    qv = _embed_query(question)
    if qv is None:
        return []
    if min_score is None:
        from doctalk.config import get_settings

        min_score = get_settings().wiki_page_min_score
    from doctalk.vector import store

    raw = store.search_entity_names(qv, k * 3)  # over-fetch; we drop inactive / claimless / off-topic
    hits: list[PageHit] = []
    with session_scope() as session:
        for row in raw:
            score = round(1.0 - float(row.get("_distance", 0.0)), 4)
            if score < min_score:  # off-topic page (cosine relevance below the gate)
                continue
            entity = session.get(Entity, row["entity_id"])
            if entity is None or entity.status != "active":
                continue
            claims = [c for c in repo.get_claims_for_entity(session, entity.id) if c.status == "active"]
            if not claims:
                continue
            hits.append(
                PageHit(
                    entity_id=entity.id,
                    name=entity.name,
                    type=entity.type,
                    path=entity.wiki_path,
                    score=score,
                    claims=[PageClaim(c.text, _claim_sources(session, c.id)) for c in claims],
                )
            )
            if len(hits) >= k:
                break
    return hits
