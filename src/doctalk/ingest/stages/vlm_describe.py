"""vlm_describe — describe a photo with the local vision model (Ollama llama3.2-vision).

Stores a short natural-language description on the images row, which feeds search and (later) the
wiki. GPU-heavy; PLAN runs this as an offline batch behind a GPU lease — for Phase 1 it's a direct
per-image call.
"""

from __future__ import annotations

from doctalk.db import repo
from doctalk.ingest.dag import StageContext
from doctalk.models.vlm import describe_image


def run(ctx: StageContext) -> None:
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    if file_id is None:  # pragma: no cover - defensive
        raise ValueError(f"vlm_describe: no file row for {ctx.content_hash}")

    description = describe_image(ctx.file_path)
    repo.upsert_image(ctx.session, file_id, vlm_description=description)
    ctx.scratch["vlm_chars"] = len(description)
