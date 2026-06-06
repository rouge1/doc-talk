"""identify — classify a source and route it to the right extractor.

Phase 1: the File row's ``format``/``mime`` are set at ingest time, so this is a lightweight
breadcrumb that also asserts the file is readable. Phase 2 grows this into real content-type
sniffing (magic bytes, floorplan detection, etc.)."""

from __future__ import annotations

from pathlib import Path

from doctalk.ingest.dag import StageContext


def run(ctx: StageContext) -> None:
    if ctx.file_path is None or not Path(ctx.file_path).is_file():
        raise FileNotFoundError(f"identify: source not readable: {ctx.file_path}")
    ctx.scratch["source_path"] = ctx.file_path
