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


def _page_doc(hit: PageHit, max_claims: int = 8) -> str:
    """The text a reranker scores against the question: the entity name + its claims."""
    return f"{hit.name}. " + " ".join(c.text for c in hit.claims[:max_claims])


def _rerank_pages(question: str, hits: list[PageHit], k: int) -> list[PageHit]:
    """Reorder candidate pages by a cross-encoder against the question, keep the top-k.

    The bi-encoder name+definition vector centers on literal token overlap (a query for "control
    channels" pulls in every entity whose name contains "channel", plus vague pages like "Bluetooth
    system"). A cross-encoder reads the page's actual claims jointly with the question, so it promotes
    the genuinely relevant pages (e.g. "L2CAP channels") and demotes the peripheral ones. Mirrors the
    chunk retriever's rerank pass; degrades to bi-encoder order if the model is unavailable.
    """
    from doctalk.models import rerank as rr

    try:
        scores = rr.rerank(question, [_page_doc(h) for h in hits])
    except Exception:  # noqa: BLE001 - no reranker model: fall back to ANN order
        return hits[:k]
    return [h for h, _ in sorted(zip(hits, scores), key=lambda hs: hs[1], reverse=True)][:k]


def retrieve_pages(question: str, k: int = 6, *, min_score: float | None = None) -> list[PageHit]:
    """Top-k active entity pages for the question, each with its claims + provenance.

    Pages below ``min_score`` (cosine name+definition relevance; default ``wiki_page_min_score``) are
    dropped so an off-topic wiki — e.g. only recipe entities — doesn't get cited for a question about
    something else just because those are the only pages that exist. The survivors are then reordered
    by a cross-encoder (``_rerank_pages``) for relevance centering. Falls back to ``settings``.
    """
    qv = _embed_query(question)
    if qv is None:
        return []
    from doctalk.config import get_settings

    settings = get_settings()
    if min_score is None:
        min_score = settings.wiki_page_min_score
    use_rerank = settings.rerank_enabled
    from doctalk.vector import store

    # Over-fetch a candidate pool (wide when reranking) and gate off-topic pages by cosine first.
    fetch_k = max(k, settings.rerank_candidates) if use_rerank else k * 3
    raw = store.search_entity_names(qv, fetch_k)
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
            if not use_rerank and len(hits) >= k:
                break
    if use_rerank and hits:
        return _rerank_pages(question, hits, k)
    return hits[:k]
