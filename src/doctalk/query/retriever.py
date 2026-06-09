"""Retrieval: embed the query, ANN-search the LanceDB index, join hits back to MySQL for text +
provenance (chapter title, file name). The vector store holds only ids/scalars; the truth store
supplies the rest."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from doctalk.config import get_settings
from doctalk.db.models import Chapter, Chunk, File
from doctalk.db.session import session_scope
from doctalk.textfilter import is_noise_chunk

# Number/version-aware token: an alphanumeric run that may keep internal dots/hyphens, so "6.0",
# "v6.0", "l2cap", "bluetooth-le" survive as single tokens instead of splitting on the punctuation.
_TOKEN = re.compile(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*", re.UNICODE)
_PHRASE = re.compile(r'"([^"]+)"')
# Function words carry no retrieval signal; dropped from loose-word ranking so content words drive
# results. (Phrases are matched verbatim and never stopword-filtered.)
_STOPWORDS = frozenset(
    "a an and are as at be but by for from has have in into is it its of on or that the their "
    "this to was were will with".split()
)


def _terms(query: str) -> list[str]:
    """Lower-cased word tokens of the query (deduped, order-preserving, version/dot-aware)."""
    seen: dict[str, None] = {}
    for t in _TOKEN.findall(query.lower()):
        seen.setdefault(t, None)
    return list(seen)


def _parse_query(query: str) -> tuple[list[str], list[str]]:
    """Split a Simple query into exact phrases (\"...\") and loose content words.

    Phrases are matched as a contiguous substring; loose words are matched individually with
    stopwords removed. If a query is *only* stopwords, fall back to the literal tokens rather than
    returning nothing. Expansion (synonyms/stemming) is deliberately omitted — Simple stays literal;
    meaning-matching is what Hybrid is for.
    """
    phrases = [p.strip().lower() for p in _PHRASE.findall(query) if p.strip()]
    rest = _PHRASE.sub(" ", query)  # strip quoted spans before word-tokenizing the remainder
    words = [t for t in _terms(rest) if t not in _STOPWORDS]
    if not words and not phrases:
        words = _terms(rest)  # all-stopword query: match literally rather than nothing
    return phrases, words


def _like_escape(s: str) -> str:
    r"""Escape LIKE wildcards so a phrase containing % or _ matches literally (paired with escape='\')."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass
class Hit:
    chunk_id: int
    file: str
    chapter: str | None
    page: int
    text: str
    score: float  # cosine similarity (1 - distance); higher is closer
    content_hash: str | None = None  # for building a citation link to the source doc/chapter
    chapter_id: int | None = None
    rerank_score: float | None = None  # cross-encoder relevance (0-1), set when reranking ran
    source: str | None = None  # which arm surfaced it: "keyword" | "semantic" | "both"


def _sigmoid(x: float) -> float:
    """Map a raw cross-encoder logit to 0-1 for display, overflow-safe for large |x|."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _order_by_rerank(hits: list[Hit], scores: list[float], k: int) -> list[Hit]:
    """Attach normalized rerank scores and return the top-k by them. Pure (no model)."""
    for hit, raw in zip(hits, scores):
        hit.rerank_score = round(_sigmoid(raw), 4)
    return sorted(hits, key=lambda h: h.rerank_score or 0.0, reverse=True)[:k]


def _rerank_and_order(question: str, hits: list[Hit], k: int) -> list[Hit]:
    """Re-score candidates with the cross-encoder; on any failure keep the ANN order (skip)."""
    from doctalk.models import rerank as rr

    try:
        scores = rr.rerank(question, [h.text for h in hits])
    except Exception:  # noqa: BLE001 - missing model / load failure: degrade to ANN order
        return hits[:k]
    return _order_by_rerank(hits, scores, k)


def _hit_from_chunk(session, chunk: Chunk, score: float, source: str | None = None) -> Hit:
    """Join a chunk back to its chapter/file for a citable Hit. Caller supplies score + provenance."""
    chapter = session.get(Chapter, chunk.chapter_id) if chunk.chapter_id else None
    file = session.get(File, chunk.file_id)
    return Hit(
        chunk_id=chunk.id,
        file=file.filename if file else "?",
        chapter=chapter.title if chapter else None,
        page=chunk.page,
        text=chunk.text,
        score=score,
        content_hash=file.content_hash if file else None,
        chapter_id=chunk.chapter_id,
        source=source,
    )


def retrieve(
    question: str, k: int = 8, file_id: int | None = None, rerank: bool | None = None
) -> list[Hit]:
    """Dense (semantic) retrieval: embed the query, ANN-search, optional cross-encoder rerank."""
    from doctalk.models.embed import embed_query
    from doctalk.vector import store

    settings = get_settings()
    use_rerank = settings.rerank_enabled if rerank is None else rerank
    # Over-fetch a candidate pool when reranking; otherwise fetch exactly k.
    fetch_k = max(k, settings.rerank_candidates) if use_rerank else k

    query_vector = embed_query(question)
    raw = store.search_text(query_vector, fetch_k, file_id=file_id)

    hits: list[Hit] = []
    with session_scope() as session:
        for row in raw:
            chunk = session.get(Chunk, row["chunk_id"])
            if chunk is None:  # index/truth drift — skip; rebuild-index fixes it
                continue
            hits.append(
                _hit_from_chunk(session, chunk, round(1.0 - float(row.get("_distance", 0.0)), 4))
            )

    if use_rerank and hits:
        return _rerank_and_order(question, hits, k)
    return hits[:k]


def keyword_search(query: str, k: int = 8, file_id: int | None = None) -> list[Hit]:
    """Simple lexical search: chunks containing the query's phrase(s)/word(s), case-insensitive.

    Quoted spans are matched as exact contiguous substrings; loose words are matched individually
    (stopwords dropped). Ranking favors, in order: phrase matches, then how many distinct words are
    present (coverage), then total frequency, then the more concise passage. Portable across
    SQLite/MySQL (LIKE) — no FTS index. ``score`` is the fraction of needles a chunk matches."""
    from sqlalchemy import or_, select

    phrases, words = _parse_query(query)
    needles = phrases + words  # each is a substring to look for; phrases first = stronger signal
    if not needles:
        return []
    settings = get_settings()
    with session_scope() as session:
        stmt = select(Chunk).where(
            or_(*[Chunk.text.ilike(f"%{_like_escape(n)}%", escape="\\") for n in needles])
        )
        if file_id is not None:
            stmt = stmt.where(Chunk.file_id == file_id)
        stmt = stmt.limit(settings.keyword_candidates)  # bound the scan; we rank below
        ranked: list[tuple[int, int, int, int, Chunk]] = []
        for ch in session.scalars(stmt):
            if is_noise_chunk(ch.text):  # TOC / page furniture — matches by coincidence, no content
                continue
            low = ch.text.lower()
            phrase_hits = sum(1 for p in phrases if p in low)       # exact-phrase matches (strongest)
            word_hits = sum(1 for w in words if w in low)           # distinct words present
            if not phrase_hits and not word_hits:
                continue
            occ = sum(low.count(n) for n in needles)                # total occurrences
            ranked.append((phrase_hits, word_hits, occ, -ch.char_count, ch))
        # phrase matches first, then word coverage, then frequency, then prefer the concise passage
        ranked.sort(key=lambda r: r[:4], reverse=True)
        return [
            _hit_from_chunk(session, ch, round((ph + wh) / len(needles), 4), source="keyword")
            for ph, wh, _occ, _neg, ch in ranked[:k]
        ]


def _rrf_merge(rankings: list[list[int]], k_rrf: int = 60) -> dict[int, float]:
    """Reciprocal Rank Fusion: combine ranked id lists into one score map. A doc's score is the sum
    over lists of 1/(k_rrf + rank); rank-based so the two arms' incomparable scores never need
    calibration. ``k_rrf`` damps the contribution of low ranks (the standard default is 60)."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k_rrf + rank + 1)
    return scores


def hybrid_search(question: str, k: int = 8, file_id: int | None = None) -> list[Hit]:
    """Fuse the lexical and dense arms with RRF, then cross-encoder rerank the merged pool. Catches
    keyword/code/acronym matches (lexical) and paraphrase/concept matches (dense) in one ranking."""
    from doctalk.models.embed import embed_query
    from doctalk.vector import store

    settings = get_settings()
    pool = max(settings.rerank_candidates, k)

    # Dense arm: chunk ids in ANN order.
    dense_ids = [row["chunk_id"] for row in store.search_text(embed_query(question), pool, file_id=file_id)]
    # Lexical arm: chunk ids in keyword-rank order.
    kw_ids = [h.chunk_id for h in keyword_search(question, k=pool, file_id=file_id)]

    fused = _rrf_merge([dense_ids, kw_ids])
    if not fused:
        return []
    ordered = sorted(fused, key=lambda c: fused[c], reverse=True)[:pool]

    kw_set, dense_set = set(kw_ids), set(dense_ids)
    hits: list[Hit] = []
    with session_scope() as session:
        for cid in ordered:
            chunk = session.get(Chunk, cid)
            if chunk is None:  # index/truth drift — skip
                continue
            if is_noise_chunk(chunk.text):  # TOC / page furniture
                continue
            in_kw, in_dense = cid in kw_set, cid in dense_set
            source = "both" if in_kw and in_dense else "keyword" if in_kw else "semantic"
            hits.append(_hit_from_chunk(session, chunk, round(fused[cid], 4), source=source))

    if settings.rerank_enabled and hits:
        return _rerank_and_order(question, hits, k)
    return hits[:k]


def resolve_file_id(content_hash_or_prefix: str | None) -> int | None:
    """Map a content-hash (full or unique prefix) to a file id, or None to search all files."""
    if not content_hash_or_prefix:
        return None
    from sqlalchemy import select

    with session_scope() as session:
        return session.scalar(
            select(File.id).where(File.content_hash.like(f"{content_hash_or_prefix}%"))
        )
