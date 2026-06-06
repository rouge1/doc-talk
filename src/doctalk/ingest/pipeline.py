"""Stage wiring. ``pipeline_for`` returns the ordered stages for a file's format; the DAG runner
gates each by the jobs ledger. Phase 1 ships the PDF document backbone; other formats currently
run ``identify`` only (their extractors land in Phase 1's image half and Phase 2).
"""

from __future__ import annotations

from doctalk.ingest.dag import Stage
from doctalk.ingest.stages import identify, link_internal, pdf_structure


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
        ]
    return stages
