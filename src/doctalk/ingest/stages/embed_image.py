"""embed_image — CLIP-embed a photo and write it to the LanceDB image index.

Mirrors the scalars hybrid search prefilters on (format, byte_size from the File; geo_country,
exif_ts from the images row), plus the CLIP vision vector. Idempotent: clears the file's existing
image rows first. Runs after exif_geo so the geo/time scalars are populated.
"""

from __future__ import annotations

from doctalk.db import repo
from doctalk.ingest.dag import StageContext
from doctalk.models.embed import embed_images
from doctalk.vector import store
from doctalk.vector.store import NO_GEO, NO_TS


def run(ctx: StageContext) -> None:
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    file = repo.get_file(ctx.session, ctx.content_hash)
    if file_id is None or file is None:  # pragma: no cover - defensive
        raise ValueError(f"embed_image: no file row for {ctx.content_hash}")

    image = repo.get_image(ctx.session, file_id)
    vector = embed_images([ctx.file_path])[0]

    store.delete_file_images(file_id)  # idempotent re-run
    store.add_images(
        [
            {
                "file_id": file_id,
                "format": file.format,
                "byte_size": file.byte_size,
                "geo_country": (image.geo_country if image and image.geo_country else NO_GEO),
                "exif_ts": (
                    int(image.exif_datetime.timestamp())
                    if image and image.exif_datetime
                    else NO_TS
                ),
                "vector": vector,
            }
        ]
    )
    ctx.scratch["embedded_image"] = True
    ctx.scratch["image_vector"] = vector  # handed to cluster_image (avoids a re-read)
