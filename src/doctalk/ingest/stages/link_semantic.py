"""link_semantic — connect a file's content to related sections across the whole corpus.

Each source (a document chapter, or an image via its VLM description) is embedded with bge and
matched against the existing chunk index; chunk hits are aggregated up to their chapters, and the
top, above-threshold targets become ``relations`` rows. This is the Phase-2 cross-linking layer:
it joins *separate* documents and attaches images to relevant document sections — driven by
similarity, thresholded so unrelated content stays unlinked ("don't force it").

Images live in CLIP space and can't be compared to bge text directly, so we embed their VLM
description instead — the description is the bridge into the document text space.

Idempotent: clears this file's authored relations first. Runs after the text index + descriptions
exist, and searches the whole corpus, so later ingests link back to earlier ones on their own runs.
"""

from __future__ import annotations

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.ingest.dag import StageContext
from doctalk.models.embed import embed_passages
from doctalk.vector import store
from doctalk.vector.store import NO_CHAPTER

REP_CHARS = 1500  # how much of a chapter's text represents it for matching


def _chapter_reps(session, file_id: int) -> list[tuple[int, str]]:
    """(chapter_id, representative_text) for chapters that have text — title + leading chunks."""
    chunks = repo.get_chunks(session, file_id)
    by_chapter: dict[int, list[str]] = {}
    for c in chunks:
        if c.chapter_id is not None:
            by_chapter.setdefault(c.chapter_id, []).append(c.text)
    reps = []
    for ch in repo.get_chapters(session, file_id):
        parts = by_chapter.get(ch.id)
        if not parts:
            continue  # nothing to compare — skip empty TOC nodes
        reps.append((ch.id, (ch.title + "\n" + "\n".join(parts))[:REP_CHARS]))
    return reps


def run(ctx: StageContext) -> None:
    settings = get_settings()
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    if file_id is None:  # pragma: no cover - defensive
        raise ValueError(f"link_semantic: no file row for {ctx.content_hash}")

    repo.clear_relations_for_file(ctx.session, file_id)  # idempotent re-run

    # Sources: each (src_chapter_id, src_image_id, text). One of the ids is set.
    sources: list[tuple[int | None, int | None, str]] = [
        (ch_id, None, text) for ch_id, text in _chapter_reps(ctx.session, file_id)
    ]
    if not sources:  # an image (or a flat doc): use the VLM description as the bridge
        image = repo.get_image(ctx.session, file_id)
        if image and image.vlm_description:
            sources.append((None, file_id, image.vlm_description))

    if not sources:
        ctx.scratch["n_relations"] = 0
        return

    vectors = embed_passages([text for _, _, text in sources])

    rows: list[dict] = []
    for (src_chapter_id, src_image_id, _text), qv in zip(sources, vectors):
        hits = store.search_text(qv, settings.link_fetch_k)
        # Aggregate chunk hits to their best score per target chapter (skip self + chapterless).
        best: dict[int, tuple[float, int]] = {}
        for h in hits:
            dst_chapter = h["chapter_id"]
            if dst_chapter == NO_CHAPTER or dst_chapter == src_chapter_id:
                continue
            sim = 1.0 - float(h.get("_distance", 0.0))
            if dst_chapter not in best or sim > best[dst_chapter][0]:
                best[dst_chapter] = (sim, h["file_id"])

        ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)
        for dst_chapter, (sim, dst_file_id) in ranked[: settings.link_top_n]:
            if sim < settings.link_sim_threshold:
                continue
            rows.append(
                {
                    "kind": "semantic",
                    "src_chapter_id": src_chapter_id,
                    "src_image_id": src_image_id,
                    "dst_chapter_id": dst_chapter,
                    "src_file_id": file_id,
                    "dst_file_id": dst_file_id,
                    "score": round(sim, 4),
                }
            )

    repo.insert_relations(ctx.session, rows)
    ctx.scratch["n_sources"] = len(sources)
    ctx.scratch["n_relations"] = len(rows)
