"""Retrieval: embed the query, ANN-search the LanceDB index, join hits back to MySQL for text +
provenance (chapter title, file name). The vector store holds only ids/scalars; the truth store
supplies the rest."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast

from sqlalchemy import select

from doctalk.config import get_settings
from doctalk.db.models import Chapter, Chunk, File, Image
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
    # A photo retrieved by its VLM caption rides the same Hit so it fuses into one ranking. For
    # "image" hits ``text`` holds the caption (so rerank/snippet just work), ``file`` the filename,
    # and ``file_id`` points at the image to render; chunk_id/chapter/page carry no meaning.
    kind: str = "passage"  # "passage" | "image"
    file_id: int | None = None
    cluster_id: int | None = None  # image hits: near-duplicate group, so the same photo isn't shown twice


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


def _hit_from_image(session, file_id: int, score: float, source: str | None = None) -> Hit | None:
    """Join a caption-index hit back to its image for a renderable Hit. The caption is the ``text``
    so the reranker and the snippet highlighter treat it like any passage."""
    file = session.get(File, file_id)
    if file is None:  # index/truth drift — skip; rebuild-index fixes it
        return None
    image = session.scalar(select(Image).where(Image.file_id == file_id))
    return Hit(
        chunk_id=0,
        file=file.filename,
        chapter=None,
        page=0,
        text=(image.vlm_description if image else None) or "",
        score=score,
        content_hash=file.content_hash,
        chapter_id=None,
        source=source,
        kind="image",
        file_id=file_id,
        cluster_id=image.cluster_id if image else None,
    )


def _dedupe_image_clusters(hits: list[Hit]) -> list[Hit]:
    """Collapse near-duplicate image hits (same ``cluster_id``) to one, mirroring the gallery so the
    same photo never appears twice. The cluster keeps its best rank (the list is already ordered, so
    the first member seen is the strongest), but is shown via its *canonical* member — the one whose
    ``file_id`` equals the ``cluster_id`` (the component's original, the likeliest to still have its
    file on disk) — when that member is among the hits. Passages and unclustered images pass through
    untouched, so ordering is preserved."""
    canonical: dict[int, Hit] = {}
    for h in hits:
        if h.kind == "image" and h.cluster_id is not None and h.file_id == h.cluster_id:
            canonical.setdefault(h.cluster_id, h)
    out: list[Hit] = []
    seen: set[int] = set()
    for h in hits:
        if h.kind != "image" or h.cluster_id is None:
            out.append(h)
            continue
        if h.cluster_id in seen:
            continue  # a weaker duplicate of an already-emitted cluster
        seen.add(h.cluster_id)
        rep = canonical.get(h.cluster_id, h)  # show the original; rank it by this (best) member
        if rep is not h:
            rep.score, rep.rerank_score, rep.source = h.score, h.rerank_score, h.source
        out.append(rep)
    return out


def retrieve(
    question: str, k: int = 8, file_id: int | None = None, rerank: bool | None = None
) -> list[Hit]:
    """Dense (semantic) retrieval: embed the query, ANN-search chunks *and* image captions, fuse by
    the (comparable) cosine score, then optionally cross-encoder rerank. A photo surfaces here by
    what its VLM caption describes — the same path feeds Search and Ask."""
    from doctalk.models.embed import embed_query
    from doctalk.vector import store

    settings = get_settings()
    use_rerank = settings.rerank_enabled if rerank is None else rerank
    # Over-fetch a candidate pool when reranking; otherwise fetch exactly k.
    fetch_k = max(k, settings.rerank_candidates) if use_rerank else k

    query_vector = embed_query(question)
    raw = store.search_text(query_vector, fetch_k, file_id=file_id)
    # Captions are corpus-wide images; a search scoped to one document shouldn't pull them in.
    raw_caps = store.search_captions(query_vector, fetch_k) if file_id is None else []

    hits: list[Hit] = []
    with session_scope() as session:
        for row in raw:
            chunk = session.get(Chunk, row["chunk_id"])
            if chunk is None:  # index/truth drift — skip; rebuild-index fixes it
                continue
            hits.append(
                _hit_from_chunk(session, chunk, round(1.0 - float(row.get("_distance", 0.0)), 4))
            )
        for row in raw_caps:
            hit = _hit_from_image(session, row["file_id"], round(1.0 - float(row.get("_distance", 0.0)), 4))
            if hit:
                hits.append(hit)

    # Same metric/space across both arms, so the scores sort directly into one ranking.
    hits.sort(key=lambda h: h.score, reverse=True)
    if use_rerank and hits:
        hits = _rerank_and_order(question, hits, len(hits))  # rerank all, then dedup/slice below
    return _dedupe_image_clusters(hits)[:k]


def keyword_search(query: str, k: int = 8, file_id: int | None = None) -> list[Hit]:
    """Simple lexical search: chunks containing the query's phrase(s)/word(s), case-insensitive.

    Quoted spans are matched as exact contiguous substrings; loose words are matched individually
    (stopwords dropped). Ranking favors, in order: phrase matches, then how many distinct words are
    present (coverage), then total frequency, then the more concise passage. Portable across
    SQLite/MySQL (LIKE) — no FTS index. ``score`` is the fraction of needles a chunk matches."""
    from sqlalchemy import or_

    phrases, words = _parse_query(query)
    needles = phrases + words  # each is a substring to look for; phrases first = stronger signal
    if not needles:
        return []
    settings = get_settings()

    def _signals(text: str) -> tuple[int, int, int] | None:
        low = text.lower()
        phrase_hits = sum(1 for p in phrases if p in low)       # exact-phrase matches (strongest)
        word_hits = sum(1 for w in words if w in low)           # distinct words present
        if not phrase_hits and not word_hits:
            return None
        occ = sum(low.count(n) for n in needles)                # total occurrences
        return phrase_hits, word_hits, occ

    with session_scope() as session:
        stmt = select(Chunk).where(
            or_(*[Chunk.text.ilike(f"%{_like_escape(n)}%", escape="\\") for n in needles])
        )
        if file_id is not None:
            stmt = stmt.where(Chunk.file_id == file_id)
        stmt = stmt.limit(settings.keyword_candidates)  # bound the scan; we rank below
        # (phrase, word, occ, -length, kind, payload) — sortable on the first 4 ints regardless of kind.
        ranked: list[tuple[int, int, int, int, str, object]] = []
        for ch in session.scalars(stmt):
            if is_noise_chunk(ch.text):  # TOC / page furniture — matches by coincidence, no content
                continue
            sig = _signals(ch.text)
            if sig is None:
                continue
            ranked.append((*sig, -ch.char_count, "passage", ch))
        # Captions are corpus-wide images; a search scoped to one document shouldn't pull them in.
        if file_id is None:
            istmt = select(Image, File).join(File, Image.file_id == File.id).where(
                or_(*[Image.vlm_description.ilike(f"%{_like_escape(n)}%", escape="\\") for n in needles])
            ).limit(settings.keyword_candidates)
            for image, _file in session.execute(istmt).all():
                sig = _signals(image.vlm_description or "")
                if sig is None:
                    continue
                ranked.append((*sig, -len(image.vlm_description or ""), "image", image.file_id))
        # phrase matches first, then word coverage, then frequency, then prefer the concise passage
        ranked.sort(key=lambda r: r[:4], reverse=True)
        hits: list[Hit] = []
        # Materialize a few past k so cluster-dedup has both near-duplicate members to choose from
        # before we trim back to k.
        for ph, wh, _occ, _neg, kind, payload in ranked[: k + 8]:
            score = round((ph + wh) / len(needles), 4)
            if kind == "image":
                hit = _hit_from_image(session, cast(int, payload), score, source="keyword")
                if hit:
                    hits.append(hit)
            else:
                hits.append(_hit_from_chunk(session, cast(Chunk, payload), score, source="keyword"))
        return _dedupe_image_clusters(hits)[:k]


def _rrf_merge(rankings: list[list[Any]], k_rrf: int = 60) -> dict[Any, float]:
    """Reciprocal Rank Fusion: combine ranked id lists into one score map. A doc's score is the sum
    over lists of 1/(k_rrf + rank); rank-based so the two arms' incomparable scores never need
    calibration. ``k_rrf`` damps the contribution of low ranks (the standard default is 60). Keys
    are opaque (here ``c{chunk_id}``/``i{file_id}``) so passages and images share one ranking."""
    scores: dict[Any, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k_rrf + rank + 1)
    return scores


def hybrid_search(question: str, k: int = 8, file_id: int | None = None) -> list[Hit]:
    """Fuse the lexical and dense arms with RRF, then cross-encoder rerank the merged pool. Catches
    keyword/code/acronym matches (lexical) and paraphrase/concept matches (dense) in one ranking —
    over passages *and* image captions, so a photo ranks alongside the text it relates to."""
    from doctalk.models.embed import embed_query
    from doctalk.vector import store

    settings = get_settings()
    pool = max(settings.rerank_candidates, k)
    qv = embed_query(question)

    # Dense arm: chunks + image captions merged into one ANN ranking by their comparable cosine score
    # (both indexes share the bge space). Keys are prefixed so the two id namespaces never collide.
    dense_scored: list[tuple[str, float]] = [
        (f"c{row['chunk_id']}", 1.0 - float(row.get("_distance", 0.0)))
        for row in store.search_text(qv, pool, file_id=file_id)
    ]
    if file_id is None:  # captions are corpus-wide images; not part of a single-doc search
        dense_scored += [
            (f"i{row['file_id']}", 1.0 - float(row.get("_distance", 0.0)))
            for row in store.search_captions(qv, pool)
        ]
    dense_scored.sort(key=lambda t: t[1], reverse=True)
    dense_ids = [key for key, _ in dense_scored]
    # Lexical arm: keyword hits (chunks + captions), in keyword-rank order, same key scheme.
    kw_ids = [
        (f"i{h.file_id}" if h.kind == "image" else f"c{h.chunk_id}")
        for h in keyword_search(question, k=pool, file_id=file_id)
    ]

    fused = _rrf_merge([dense_ids, kw_ids])
    if not fused:
        return []
    ordered = sorted(fused, key=lambda c: fused[c], reverse=True)[:pool]

    kw_set, dense_set = set(kw_ids), set(dense_ids)
    hits: list[Hit] = []
    with session_scope() as session:
        for key in ordered:
            in_kw, in_dense = key in kw_set, key in dense_set
            source = "both" if in_kw and in_dense else "keyword" if in_kw else "semantic"
            score = round(fused[key], 4)
            if key.startswith("i"):  # image caption hit
                hit = _hit_from_image(session, int(key[1:]), score, source=source)
                if hit:
                    hits.append(hit)
                continue
            chunk = session.get(Chunk, int(key[1:]))
            if chunk is None:  # index/truth drift — skip
                continue
            if is_noise_chunk(chunk.text):  # TOC / page furniture
                continue
            hits.append(_hit_from_chunk(session, chunk, score, source=source))

    if settings.rerank_enabled and hits:
        hits = _rerank_and_order(question, hits, len(hits))  # rerank all, then dedup/slice below
    return _dedupe_image_clusters(hits)[:k]


class _Scored(Protocol):
    score: float


_ScoredT = TypeVar("_ScoredT", bound=_Scored)


def apply_relevance_floor(
    hits: list[_ScoredT], ratio: float, abs_min: float = 0.0, *, keep_top: bool = True
) -> list[_ScoredT]:
    """Drop hits far below the top one's relevance — the chat paths' guard against off-topic filler.

    The ANN always returns k candidates; for a narrow query only the first is truly relevant and the
    rest are weak neighbors returned just to fill k. Fed to the LLM, that filler becomes confident,
    off-topic answer sentences. A hit is kept only if it clears ``max(top × ratio, abs_min)``, scored
    by the rerank value when present (a sharper signal) else the raw similarity:

    - the **relative** term handles the normal case — one strong hit, a weak tail ("cats": 0.53 then
      0.07 → only the cat survives);
    - the **absolute** term handles the case where the reranker is *flat and unsure* — every score
      near zero, so ratios are meaningless ("cat?": 0.003 vs 0.002) — by requiring a minimum confidence.

    ``keep_top`` decides the all-weak case. For **chunks** it's True: a low-confidence query still
    answers from its single closest match rather than nothing (the cat photo for "cat?"). For **wiki
    pages** it's False: if no page clears the bar there simply is no relevant page, so drop them all
    and let chunk-RAG carry — keeping the nearest off-topic page is what dragged "eggs"/"PAwR" into a
    cat answer. Works on anything with a ``score`` (and optionally ``rerank_score``). ``ratio`` <= 0
    disables the relative term. Deliberately *not* applied inside ``retrieve`` — plain search wants
    every ranked hit; only the LLM context needs the floor."""
    if not hits:
        return hits

    def eff(h: _ScoredT) -> float:
        rr = getattr(h, "rerank_score", None)
        return rr if rr is not None else h.score

    floor = max(eff(hits[0]) * ratio, abs_min) if ratio > 0 else abs_min
    if keep_top:
        return [hits[0], *(h for h in hits[1:] if eff(h) >= floor)]
    return [h for h in hits if eff(h) >= floor]


def resolve_file_id(content_hash_or_prefix: str | None) -> int | None:
    """Map a content-hash (full or unique prefix) to a file id, or None to search all files."""
    if not content_hash_or_prefix:
        return None
    from sqlalchemy import select

    with session_scope() as session:
        return session.scalar(
            select(File.id).where(File.content_hash.like(f"{content_hash_or_prefix}%"))
        )
