"""wiki-dedupe — triage the near-duplicate entities lint flags, before any merge runs.

``lint`` flags entity pairs whose names overlap (same type, norm_key token Jaccard >= ``_DUP_JACCARD``)
as possible duplicates — but that is a high-recall, low-precision signal: "Server" and "SDP Server"
share a token yet are different entities. This module scores each candidate pair with the *same*
signals the ingest resolver uses (exact alias/acronym hit, lexical token overlap, name-embedding
cosine, co-mention overlap; see ``synth/resolve.py``) and sorts them into three bands so a human can
see the real shape before anything is merged:

  - ``fold``  : composite >= ``FOLD_CUT``                  -> the same entity; safe to merge
  - ``judge`` : ``resolve_tau_low`` <= composite < FOLD    -> ambiguous; the LLM adjudicator decides
  - ``aside`` : composite < ``resolve_tau_low``            -> name look-alikes the signals call distinct

Read-only and model-free: no merges, no LLM calls — just the score, so the bands can be tuned before
a gated apply/undo is built on top. ``judge``'s floor reuses the resolver's NEW threshold
(``resolve_tau_low``); ``FOLD_CUT`` is the dedup auto-fold floor — set lower than the resolver's MATCH
threshold because entity-vs-entity pairs rarely fire the exact-alias term, so the achievable composite
tops out well under 0.85 (empirically ~0.81 on the corpus).
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func, select

from doctalk.cluster.grouping import cosine
from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.db.models import Chunk, Entity, File, Mention
from doctalk.synth import pages
from doctalk.synth.lint import _DUP_JACCARD
from doctalk.synth.resolve import _candidate_surfaces, _jaccard, _types_compatible
from doctalk.vector import store

FOLD_CUT = 0.70  # composite at/above which a pair is confidently the same entity (tunable)


def _composite(alias: float, lex: float, emb: float, com: float) -> float:
    """The one scoring formula (resolver weights), shared by the batch plan and the single-pair scorer
    so the two never drift."""
    s = get_settings()
    return min(1.0, s.resolve_w_alias * alias + s.resolve_w_lexical * lex
               + s.resolve_w_embed * emb + s.resolve_w_comention * com)

_BANDS = [
    ("fold", "fold automatically", "the same entity, written two ways"),
    ("judge", "let the judge decide", "ambiguous — needs a same-or-different call"),
    ("aside", "probably distinct", "names overlap, but the signals point different ways"),
]


def _candidate_pairs(session) -> list[tuple[Entity, Entity, float]]:
    """The same near-duplicate pairs lint counts: same type, norm_key token Jaccard >= the threshold."""
    by_type: dict[str, list[Entity]] = defaultdict(list)
    for e in session.scalars(select(Entity).where(Entity.status == "active")):
        by_type[e.type].append(e)
    out: list[tuple[Entity, Entity, float]] = []
    for grp in by_type.values():
        toks = [set(e.norm_key.split()) for e in grp]
        for i in range(len(grp)):
            for j in range(i + 1, len(grp)):
                lex = _jaccard(toks[i], toks[j])
                if lex >= _DUP_JACCARD:
                    out.append((grp[i], grp[j], lex))
    return out


def _band(score: float, tau_low: float) -> str:
    if score >= FOLD_CUT:
        return "fold"
    return "judge" if score >= tau_low else "aside"


# Scoring all ~255 pairs reads the full entity-vector table + a co-mention query per entity — seconds of
# work, and the bands only move when the underlying entities/claims change. So memoize the plan keyed by a
# cheap fingerprint of those inputs: repeat visits (and every return from a compare) are then instant.
_PLAN_CACHE: tuple[tuple[int, int, int], dict] | None = None


def _plan_fingerprint(session) -> tuple[int, int, int]:
    """A cheap signature of everything the plan reads. Active-entity count moves on a merge/prune (status
    flips); max entity id moves when a new entity is created; max mention id moves when an ingest files new
    claims (which feed the co-mention signal). Same triple -> identical plan."""
    n = session.scalar(select(func.count()).select_from(Entity).where(Entity.status == "active")) or 0
    emax = session.scalar(select(func.max(Entity.id))) or 0
    mmax = session.scalar(select(func.max(Mention.id))) or 0
    return (int(n), int(emax), int(mmax))


def invalidate_plan_cache() -> None:
    """Drop the memoized plan. Called by the in-process write paths (disambiguate/merge/undo) so the next
    load recomputes immediately rather than waiting for the fingerprint to drift."""
    global _PLAN_CACHE
    _PLAN_CACHE = None


def plan_duplicates(session, *, refresh: bool = False) -> dict:
    """Score every near-duplicate candidate pair and bucket it into fold/judge/aside. Read-only.
    Returns band counts + a small sample per band + every pair's score (for the confidence gauge).
    Memoized on a cheap input fingerprint; pass ``refresh=True`` to force a recompute."""
    global _PLAN_CACHE
    fp = _plan_fingerprint(session)
    if not refresh and _PLAN_CACHE is not None and _PLAN_CACHE[0] == fp:
        return _PLAN_CACHE[1]
    s = get_settings()
    pairs = _candidate_pairs(session)

    ids = list({e.id for a, b, _ in pairs for e in (a, b)})
    vecs = store.get_entity_vectors(ids) if ids else {}
    nbr: dict[int, set[str]] = {}

    def neighbors(eid: int) -> set[str]:
        if eid not in nbr:
            ks: set[str] = set()
            for nid in repo.get_comention_entity_ids(session, eid):
                n = session.get(Entity, nid)
                if n is not None:
                    ks.add(n.norm_key)
            nbr[eid] = ks
        return nbr[eid]

    scored: list[tuple[float, Entity, Entity, dict]] = []
    for a, b, lex in pairs:
        if not _types_compatible(a.type, b.type):  # belt-and-braces; same-type by construction
            continue
        alias = 1.0 if (_candidate_surfaces(a) & _candidate_surfaces(b)) else 0.0
        va, vb = vecs.get(a.id), vecs.get(b.id)
        emb = max(0.0, cosine(va, vb)) if (va and vb) else 0.0
        com = _jaccard(neighbors(a.id), neighbors(b.id))
        comp = _composite(alias, lex, emb, com)
        scored.append((comp, a, b, {"lexical": round(lex, 2), "embed": round(emb, 2),
                                    "comention": round(com, 2), "alias": alias}))
    scored.sort(key=lambda r: r[0], reverse=True)

    tau_low = s.resolve_tau_low
    grouped: dict[str, list[tuple[float, Entity, Entity, dict]]] = defaultdict(list)
    for row in scored:
        grouped[_band(row[0], tau_low)].append(row)

    def _pair(comp: float, a: Entity, b: Entity, sig: dict) -> dict:
        return {
            "a": {"id": a.id, "name": a.name, "stem": pages.slug_for(a)},
            "b": {"id": b.id, "name": b.name, "stem": pages.slug_for(b)},
            "score": round(comp, 2), "signals": sig,
        }

    bands = [
        {"key": key, "verb": verb, "gloss": gloss, "count": len(grouped.get(key, [])),
         "sample": [_pair(*row) for row in grouped.get(key, [])[:6]]}
        for key, verb, gloss in _BANDS
    ]
    result = {
        "total": len(scored),
        "cuts": {"judge": round(tau_low, 2), "fold": FOLD_CUT},
        "bands": bands,
        "scores": [round(row[0], 3) for row in scored],  # every pair's score, for the gauge rug
        # every pair (not just the per-band sample) so the UI can re-bucket live when the cuts are dragged
        "pairs": [_pair(*row) for row in scored],
    }
    _PLAN_CACHE = (fp, result)
    return result


# --- pair comparison: the evidence a human reads to make the same-or-different call ----------------


def _neighbor_keys(session, entity_id: int) -> set[str]:
    keys: set[str] = set()
    for nid in repo.get_comention_entity_ids(session, entity_id):
        n = session.get(Entity, nid)
        if n is not None:
            keys.add(n.norm_key)
    return keys


def score_pair(session, a: Entity, b: Entity) -> tuple[float, dict]:
    """Score one entity pair exactly as ``plan_duplicates`` does (same signals + weights), for the
    compare view's header. Returns ``(composite, signals)``."""
    lex = _jaccard(set(a.norm_key.split()), set(b.norm_key.split()))
    alias = 1.0 if (_candidate_surfaces(a) & _candidate_surfaces(b)) else 0.0
    vecs = store.get_entity_vectors([a.id, b.id])
    va, vb = vecs.get(a.id), vecs.get(b.id)
    emb = max(0.0, cosine(va, vb)) if (va and vb) else 0.0
    com = _jaccard(_neighbor_keys(session, a.id), _neighbor_keys(session, b.id))
    return _composite(alias, lex, emb, com), {
        "lexical": round(lex, 2), "embed": round(emb, 2), "comention": round(com, 2), "alias": alias,
    }


def _terms(e: Entity) -> list[str]:
    """The surface forms to highlight for an entity — its name plus distinct aliases."""
    out = [e.name]
    for a in e.aliases or []:
        if a.strip() and a.strip().lower() != e.name.strip().lower() and a not in out:
            out.append(a)
    return out


def _window(text: str, terms: list[str], radius: int = 260) -> str:
    """A readable snippet of a chunk centered on the first occurrence of any term (the chunk is a whole
    page of text; the window keeps the comparison scannable, with '…' marking the trim)."""
    low = text.lower()
    hits = [i for i in (low.find(t.lower()) for t in terms) if i != -1]
    if not hits:
        return text[: radius * 2].rstrip() + ("…" if len(text) > radius * 2 else "")
    pos = min(hits)
    start, end = max(0, pos - radius), min(len(text), pos + radius)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def _evidence(session, e: Entity, k: int) -> dict:
    """An entity's side of the comparison: identity + up to ``k`` distinct source passages where it's
    mentioned (raw chunk text, windowed), each carrying enough to deep-link into the document viewer."""
    terms = _terms(e)
    passages: list[dict] = []
    seen: set[int] = set()
    for m in session.scalars(
        select(Mention).where(Mention.entity_id == e.id).order_by(Mention.id)
    ):
        if m.chunk_id is None or m.chunk_id in seen:
            continue
        chunk = session.get(Chunk, m.chunk_id)
        if chunk is None:
            continue
        seen.add(m.chunk_id)
        file = session.get(File, chunk.file_id)
        passages.append({
            "file": file.filename if file else None,
            "content_hash": file.content_hash if file else None,
            "page": chunk.page, "chunk_id": chunk.id, "chapter_id": chunk.chapter_id,
            "text": _window(chunk.text, terms),
        })
        if len(passages) >= k:
            break
    return {
        "id": e.id, "name": e.name, "type": e.type, "stem": pages.slug_for(e),
        "aliases": [t for t in terms if t != e.name], "sources": e.source_count,
        "claims": repo.count_claims_by_entity(session, [e.id]).get(e.id, 0),
        "terms": terms, "passages": passages,
    }


def compare_pair(session, a_id: int, b_id: int, *, k: int = 4) -> dict | None:
    """The full evidence packet for a candidate pair: the resolver's score + signals, plus each side's
    source passages. ``None`` if either entity is gone (the API turns that into a 404)."""
    a, b = session.get(Entity, a_id), session.get(Entity, b_id)
    if a is None or b is None:
        return None
    comp, sig = score_pair(session, a, b)
    return {
        "score": round(comp, 2), "band": _band(comp, get_settings().resolve_tau_low), "signals": sig,
        "a": _evidence(session, a, k), "b": _evidence(session, b, k),
    }
