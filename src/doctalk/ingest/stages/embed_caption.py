"""embed_caption — embed a photo's VLM caption into the LanceDB caption index.

Reads the caption (``Image.vlm_description``) from MySQL (the truth store), embeds it with bge —
the SAME text embedder used for document chunks — and writes one row to the caption table. Because
it shares the text-chunk embedding space, a plain text search / Ask query finds the photo by what
it depicts, fused into the chunk ranking (not stranded in the parallel CLIP index). Idempotent:
clears the file's existing caption row first. Runs after vlm_describe (the caption must exist); a
photo with no caption is a no-op. The index is derived — ``rebuild-index`` regenerates it.
"""

from __future__ import annotations

from doctalk.db import repo
from doctalk.ingest.dag import StageContext
from doctalk.models.embed import embed_passages
from doctalk.vector import store


def run(ctx: StageContext) -> None:
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    if file_id is None:  # pragma: no cover - defensive
        raise ValueError(f"embed_caption: no file row for {ctx.content_hash}")

    image = repo.get_image(ctx.session, file_id)
    caption = (image.vlm_description or "").strip() if image else ""

    store.delete_file_caption(file_id)  # idempotent re-run
    if caption:
        vector = embed_passages([caption])[0]
        store.add_captions([{"file_id": file_id, "vector": vector}])

    ctx.scratch["embedded_caption"] = bool(caption)
