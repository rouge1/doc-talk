"""synth_entities — sub-stage (1) of the Phase 4 synthesis pass.

Sweeps the whole source in **coherent, bounded windows of consecutive chunks** (one LLM call each),
has the local model extract entities + claims (``synth.extract``), and merges the candidates across
windows by normalized key. Each is then persisted with **deterministic, auditable provenance**: every
entity/claim is tied to the real chunks whose text contains its name or an alias — never to ids the
model asserted. Resolution (MATCH/NEW/DEFER) is the two-threshold fuzzy/embedding ``synth_resolve``;
page rewriting + git commit are ``synth_integrate`` (the slice after).

Why a sweep, not a sample: a 40-chunk evenly-spaced sample of a 6,000-chunk spec is an incoherent
jumble that a small local model answers in prose, yielding zero entities. Consecutive windows stay
on-topic, so the model actually emits JSON. Obvious non-content chunks (table-of-contents dotted
leaders, index/glossary lists) are skipped so calls aren't spent on page-number filler. Two gates
keep data values out of the entity space: the shape gate (``synth.gate``, applied inside
``extract``) rejects numeric/hex literals and doc self-references, and the salience gate
(``_drop_low_salience``) drops one-window one-claim candidates from large sweeps.

Idempotent: clears this file's prior mentions/claims first, then recomputes affected entities'
source counts — a re-drop or model upgrade re-synthesizes cleanly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.ingest.dag import StageContext
from doctalk.synth import extract, resolve
from doctalk.synth.normalize import norm_key
from doctalk.textfilter import is_noise_chunk as _is_noise_chunk  # shared with the search retriever

_MAX_PROV_CHUNKS = 25  # cap provenance breadth per entity (a spec-wide term needn't cite 100 chunks)


def _sample_chunks(chunks: list, limit: int) -> list:
    """Evenly-spaced sample across the document so extraction sees its breadth, not just the head."""
    if len(chunks) <= limit:
        return chunks
    stride = len(chunks) / limit
    return [chunks[int(i * stride)] for i in range(limit)]


def _window(chunks: list, char_cap: int) -> str:
    """Concatenate sampled chunk texts (each capped) into one extraction passage."""
    return "\n\n".join(c.text[:char_cap] for c in chunks)


def _windows(chunks: list, size: int) -> list[list]:
    """Split into windows of ``size`` consecutive content chunks (TOC/index filler dropped)."""
    content = [c for c in chunks if not _is_noise_chunk(c.text)] or chunks
    return [content[i : i + size] for i in range(0, len(content), max(1, size))]


@dataclass
class _Candidate:
    """One entity accumulated across every window it surfaced in."""

    name: str
    type: str
    aliases: set[str] = field(default_factory=set)
    claims: list[str] = field(default_factory=list)  # de-duped, insertion-ordered
    _seen_claims: set[str] = field(default_factory=set)
    src_chunk_ids: list[int] = field(default_factory=list)
    _seen_chunks: set[int] = field(default_factory=set)
    windows_seen: int = 0  # distinct windows that surfaced this entity (the salience signal)

    def absorb(self, ent: extract.ExtractedEntity, window: list, max_claims: int) -> None:
        if self.type == "concept" and ent.type != "concept":
            self.type = ent.type  # prefer a specific type over the catch-all
        self.aliases.update(ent.aliases)
        for c in ent.claims:
            if c not in self._seen_claims and len(self.claims) < max_claims:
                self._seen_claims.add(c)
                self.claims.append(c)
        needles = [self.name.lower(), *(a.lower() for a in self.aliases)]
        for chunk in window:
            if chunk.id not in self._seen_chunks and any(n in chunk.text.lower() for n in needles):
                self._seen_chunks.add(chunk.id)
                if len(self.src_chunk_ids) < _MAX_PROV_CHUNKS:
                    self.src_chunk_ids.append(chunk.id)


def _drop_low_salience(session, cands: dict[str, _Candidate], *, n_windows: int, settings) -> int:
    """Drop one-off candidates from a large sweep, in place; returns how many were dropped.

    On a 600-window spec, a name the model surfaced in a single window with a single claim is
    almost always noise (a value, a heading fragment, a hallucinated subject) — a real concept
    recurs. Kept if it surfaced in >= ``synth_min_windows`` windows, carries >=
    ``synth_min_claims`` claims, or exactly norm-key-matches an entity we already know (then it
    adds a source to an established page — cross-source compounding, the point of the wiki).
    Small documents (< ``synth_salience_min_windows`` windows) skip this entirely: a recipe docx
    yields one window, where "appeared once" carries no signal.
    """
    if not settings.synth_full_sweep or n_windows < settings.synth_salience_min_windows:
        return 0
    known = {e.norm_key for e in repo.find_entities_by_norm_keys(session, set(cands))}
    drop = [
        key
        for key, cand in cands.items()
        if key not in known
        and cand.windows_seen < settings.synth_min_windows
        and len(cand.claims) < settings.synth_min_claims
    ]
    for key in drop:
        del cands[key]
    return len(drop)


def run(ctx: StageContext) -> None:
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    if file_id is None:  # pragma: no cover - defensive
        raise ValueError(f"synth_entities: no file row for {ctx.content_hash}")

    chunks = repo.get_chunks(ctx.session, file_id)
    if not chunks:  # nothing textual to synthesize (e.g. an image-only source)
        return

    s = get_settings()
    model = s.synth_model or s.chat_model
    windows = (
        _windows(chunks, s.synth_window_chunks)
        if s.synth_full_sweep
        else [_sample_chunks(chunks, s.synth_max_chunks)]
    )

    # Sweep every window, merging candidates by normalized key as we go. A single slow/failed local
    # call (timeout, transient Ollama error) must not abort the whole sweep — skip that window and
    # press on, so 595 good windows survive one bad one (the stage degrades, never crashes).
    cands: dict[str, _Candidate] = {}
    by_chunk: dict[int, object] = {c.id: c for c in chunks}
    failed = 0
    for win in windows:
        try:
            extracted = extract.extract_entities(
                _window(win, s.synth_chunk_chars), model=model, timeout=s.synth_call_timeout
            )
        except (RuntimeError, TimeoutError):
            # A timed-out / unreachable Ollama call (chat() wraps transport errors as RuntimeError)
            # skips its window — but real bugs (TypeError, etc.) still propagate and fail loudly.
            failed += 1
            continue
        seen_this_window: set[str] = set()
        for ent in extracted:
            key = norm_key(ent.name)
            if not key:
                continue
            cand = cands.get(key)
            if cand is None:
                cand = cands[key] = _Candidate(name=ent.name, type=ent.type)
            cand.absorb(ent, win, s.synth_max_claims_per_entity)
            seen_this_window.add(key)
        for key in seen_this_window:
            cands[key].windows_seen += 1

    skipped = _drop_low_salience(ctx.session, cands, n_windows=len(windows), settings=s)

    touched = set(repo.clear_synth_for_file(ctx.session, file_id))  # idempotent re-synth
    all_keys = set(cands)  # co-extracted entities co-mention each other (resolver signal)

    for key, cand in cands.items():
        # Deterministic provenance: the real chunks that name this entity (null if none did).
        prov = (
            [{"file_id": file_id, "chunk_id": cid} for cid in cand.src_chunk_ids]
            if cand.src_chunk_ids
            else [{"file_id": file_id, "chunk_id": None}]
        )
        context_text = " ".join(
            by_chunk[cid].text for cid in cand.src_chunk_ids if cid in by_chunk
        )

        res = resolve.resolve_candidate(
            ctx.session,
            name=cand.name,
            type_=cand.type,
            aliases=sorted(cand.aliases),
            definition=cand.claims[0] if cand.claims else "",
            context_text=context_text,
            comention_keys=all_keys - {key},
        )
        entity = res.entity
        touched.add(entity.id)

        repo.insert_mentions(
            ctx.session,
            file_id,
            [
                {
                    "entity_id": entity.id,
                    "chunk_id": p["chunk_id"],
                    "score": res.score,
                    "decision": res.decision,
                    "signals": res.signals,
                }
                for p in prov
            ],
        )
        for claim_text in cand.claims:
            claim = repo.insert_claim(
                ctx.session, entity_id=entity.id, file_id=file_id, text=claim_text
            )
            repo.insert_claim_sources(ctx.session, claim.id, prov)

        if res.decision == "DEFER":  # ambiguous — queue for human review
            repo.add_entity_review(
                ctx.session,
                mention_surface=cand.name,
                mention_type=cand.type,
                file_id=file_id,
                entity_id=entity.id,
                payload={"definition": cand.claims[0] if cand.claims else "",
                         "signals": res.signals},
                llm_verdict=res.signals.get("llm"),
            )

    for entity_id in sorted(touched):
        repo.recompute_entity_source_count(ctx.session, entity_id)

    ctx.scratch["synth_entities"] = len(cands)
    ctx.scratch["synth_entities_failed_windows"] = failed
    ctx.scratch["synth_entities_low_salience"] = skipped
