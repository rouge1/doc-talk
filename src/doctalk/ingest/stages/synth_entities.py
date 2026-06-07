"""synth_entities — sub-stage (1) of the Phase 4 synthesis pass.

Reads a bounded, evenly-spaced sample of the source's chunks, has the local LLM extract entities +
claims (``synth.extract``), then persists them with **deterministic, auditable provenance**: each
entity/claim is tied to the real chunks whose text contains the entity's name or an alias — never to
ids the model asserted. Resolution here is a *placeholder* (exact normalized-name match via
``repo.get_or_create_entity``); the real two-threshold fuzzy/embedding ``synth_resolve`` supersedes
it next. Page rewriting + git commit are ``synth_integrate`` (the slice after).

Idempotent: clears this file's prior mentions/claims first, then recomputes affected entities'
source counts — a re-drop or model upgrade re-synthesizes cleanly.
"""

from __future__ import annotations

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.ingest.dag import StageContext
from doctalk.synth import extract
from doctalk.synth.normalize import norm_key


def _sample_chunks(chunks: list, limit: int) -> list:
    """Evenly-spaced sample across the document so extraction sees its breadth, not just the head."""
    if len(chunks) <= limit:
        return chunks
    stride = len(chunks) / limit
    return [chunks[int(i * stride)] for i in range(limit)]


def _window(chunks: list, char_cap: int) -> str:
    """Concatenate sampled chunk texts (each capped) into one extraction passage."""
    return "\n\n".join(c.text[:char_cap] for c in chunks)


def run(ctx: StageContext) -> None:
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    if file_id is None:  # pragma: no cover - defensive
        raise ValueError(f"synth_entities: no file row for {ctx.content_hash}")

    chunks = repo.get_chunks(ctx.session, file_id)
    if not chunks:  # nothing textual to synthesize (e.g. an image-only source)
        return

    s = get_settings()
    sample = _sample_chunks(chunks, s.synth_max_chunks)
    entities = extract.extract_entities(
        _window(sample, s.synth_chunk_chars), model=s.synth_model or s.chat_model
    )

    touched = set(repo.clear_synth_for_file(ctx.session, file_id))  # idempotent re-synth

    for ent in entities:
        key = norm_key(ent.name)
        if not key:
            continue
        entity = repo.get_or_create_entity(
            ctx.session, name=ent.name, type_=ent.type, norm_key=key, aliases=ent.aliases
        )
        touched.add(entity.id)

        # Deterministic provenance: which sampled chunks actually name this entity?
        needles = [ent.name.lower(), *(a.lower() for a in ent.aliases)]
        src_chunks = [c for c in sample if any(n in c.text.lower() for n in needles)]
        prov = (
            [{"file_id": file_id, "chunk_id": c.id} for c in src_chunks]
            if src_chunks
            else [{"file_id": file_id, "chunk_id": None}]
        )

        repo.insert_mentions(
            ctx.session,
            file_id,
            [{"entity_id": entity.id, "chunk_id": p["chunk_id"]} for p in prov],
        )
        for claim_text in ent.claims:
            claim = repo.insert_claim(
                ctx.session, entity_id=entity.id, file_id=file_id, text=claim_text
            )
            repo.insert_claim_sources(ctx.session, claim.id, prov)

    for entity_id in sorted(touched):
        repo.recompute_entity_source_count(ctx.session, entity_id)

    ctx.scratch["synth_entities"] = len(entities)
