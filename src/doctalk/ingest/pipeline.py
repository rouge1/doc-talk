"""Stage wiring. ``pipeline_for`` returns the ordered stages for a file's format; the DAG runner
gates each by the jobs ledger. Phase 1 ships the PDF document backbone; other formats currently
run ``identify`` only (their extractors land in Phase 1's image half and Phase 2).
"""

from __future__ import annotations

from doctalk.ingest.dag import Stage
from doctalk.ingest.stages import (
    embed_image,
    embed_text,
    exif_geo,
    identify,
    image_extract,
    link_internal,
    pdf_structure,
    vlm_describe,
)

IMAGE_FORMATS = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff", "tif"}


def pipeline_for(file_format: str) -> list[Stage]:
    stages: list[Stage] = [Stage("identify", identify.run)]
    if file_format == "pdf":
        stages += [
            Stage(
                "pdf_structure",
                pdf_structure.run,
                model_version="pymupdf-1",
                deps=("identify",),
            ),
            Stage(
                "link_internal",
                link_internal.run,
                model_version="pymupdf-1",
                deps=("pdf_structure",),
            ),
            # Derived vector index; depends only on chunks, so it runs alongside link_internal.
            Stage(
                "embed_text",
                embed_text.run,
                model_version="bge-small-en-v1.5",
                deps=("pdf_structure",),
            ),
        ]
    elif file_format in IMAGE_FORMATS:
        stages += [
            Stage("image_extract", image_extract.run, deps=("identify",)),
            Stage("exif_geo", exif_geo.run, deps=("image_extract",)),
            # embed_image mirrors exif/geo scalars into Lance, so it runs after exif_geo.
            Stage(
                "embed_image",
                embed_image.run,
                model_version="clip-vit-b-32",
                deps=("exif_geo",),
            ),
            Stage(
                "vlm_describe",
                vlm_describe.run,
                model_version="llama3.2-vision",
                deps=("image_extract",),
            ),
        ]
    return stages
