"""Phase 0 pipeline — placeholder stages that exercise the resumable DAG.

These two stages do no real extraction; they exist so the idempotency/resumability behaviour is
demonstrable end to end (run twice -> second run all-skipped; crash -> resumes). Phase 1 replaces
them with the real DAG: identify -> pdf_structure/docx_structure/image_extract -> ocr ->
vlm_describe -> embed_* -> link_* -> wiki_build.
"""

from __future__ import annotations

from doctalk.ingest.dag import Stage, StageContext


def _identify(ctx: StageContext) -> None:
    """Placeholder: in Phase 1 this routes the file to a format-specific extractor. For now it
    just records that the source was seen (the File row is written at ingest time)."""
    ctx.scratch["identified"] = ctx.content_hash


def _probe(ctx: StageContext) -> None:
    """Placeholder downstream stage; depends on identify to exercise topological ordering."""
    ctx.scratch["probed"] = True


def phase0_pipeline() -> list[Stage]:
    return [
        Stage("identify", _identify),
        Stage("probe", _probe, deps=("identify",)),
    ]
