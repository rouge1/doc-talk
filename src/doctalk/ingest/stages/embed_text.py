"""embed_text — embed a file's chunks and write them to the LanceDB text index.

Reads chunk text from MySQL (the truth store), embeds with bge, and (re)writes the file's rows in
LanceDB. Idempotent: clears the file's existing vectors first, so a re-run never duplicates. The
index is derived — ``rebuild-index`` can regenerate it from MySQL at any time.
"""

from __future__ import annotations

from doctalk.db import repo
from doctalk.ingest.dag import StageContext
from doctalk.models.embed import embed_passages
from doctalk.vector import store
from doctalk.vector.store import NO_CHAPTER


def run(ctx: StageContext) -> None:
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    if file_id is None:  # pragma: no cover - defensive
        raise ValueError(f"embed_text: no file row for {ctx.content_hash}")

    chunks = repo.get_chunks(ctx.session, file_id)
    store.delete_file_text(file_id)  # idempotent re-run

    if chunks:
        vectors = embed_passages([c.text for c in chunks])
        store.add_text_chunks(
            [
                {
                    "chunk_id": c.id,
                    "file_id": file_id,
                    "chapter_id": c.chapter_id if c.chapter_id is not None else NO_CHAPTER,
                    "page": c.page,
                    "vector": vec,
                }
                for c, vec in zip(chunks, vectors)
            ]
        )

    ctx.scratch["n_embedded"] = len(chunks)
