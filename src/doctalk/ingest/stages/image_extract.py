"""image_extract — register a standalone image and capture basic properties (dimensions).

Creates the ``images`` row for a photo file. (Extracting embedded images/figures from PDFs is a
separate path added later.) Format/byte_size already live on the File row from ingest.
"""

from __future__ import annotations

from PIL import Image

from doctalk.db import repo
from doctalk.ingest.dag import StageContext


def run(ctx: StageContext) -> None:
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    if file_id is None:  # pragma: no cover - defensive
        raise ValueError(f"image_extract: no file row for {ctx.content_hash}")

    with Image.open(ctx.file_path) as im:
        width, height = im.size

    repo.upsert_image(ctx.session, file_id, width=width, height=height)
    ctx.scratch["dims"] = (width, height)
