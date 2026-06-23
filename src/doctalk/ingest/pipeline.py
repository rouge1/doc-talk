"""Stage wiring. ``pipeline_for`` returns the ordered stages for a file's format; the DAG runner
gates each by the jobs ledger. Phase 1 ships the PDF document backbone; other formats currently
run ``identify`` only (their extractors land in Phase 1's image half and Phase 2).
"""

from __future__ import annotations

from doctalk.config import get_settings
from doctalk.ingest.dag import Stage
from doctalk.synth.extract import prompt_fingerprint as extract_prompt_fingerprint
from doctalk.ingest.stages import (
    cluster_image,
    docx_structure,
    embed_caption,
    embed_image,
    embed_text,
    exif_geo,
    identify,
    image_extract,
    link_internal,
    link_semantic,
    ocr,
    pdf_assets,
    pdf_structure,
    synth_entities,
    synth_integrate,
    synth_source,
    synth_topics,
    vlm_describe,
)


def _link_semantic_stage(dep: str) -> Stage:
    """Cross-corpus semantic links. Depends on the file's text index / description being ready;
    searches the whole corpus, so each file links against whatever is already ingested. The
    threshold/top_n go in params, so retuning them re-runs the stage (the ledger key changes)."""
    s = get_settings()
    return Stage(
        "link_semantic",
        link_semantic.run,
        model_version="bge-small-en-v1.5",
        params={"threshold": s.link_sim_threshold, "top_n": s.link_top_n},
        deps=(dep,),
    )


def _synth_entities_stage(dep: str) -> Stage:
    """Phase-4 entity/claim extraction. Keyed by the synth model (a model upgrade re-synthesizes)
    with the sampling knobs in params so retuning re-runs it. LLM-bound; runs last, after the
    document's chunks exist."""
    s = get_settings()
    return Stage(
        "synth_entities",
        synth_entities.run,
        model_version=s.synth_model or s.chat_model,
        params={
            "max_chunks": s.synth_max_chunks,
            "chunk_chars": s.synth_chunk_chars,
            # resolution is part of this stage; an embed-model upgrade must re-block (re-resolve).
            "embed_version": s.resolve_embed_version,
            # a prompt edit re-extracts, like a model upgrade (found live: prompt regressions
            # otherwise hide behind a 'done' ledger row).
            "prompt": extract_prompt_fingerprint(),
        },
        deps=(dep,),
    )


def _synth_integrate_stage() -> Stage:
    """Phase-4 page materialization: write the affected entity pages + index/log, commit to git.
    Depends on the extraction stage; keyed by the synth model + the summaries toggle so a model
    upgrade or toggling LLM prose re-integrates."""
    s = get_settings()
    return Stage(
        "synth_integrate",
        synth_integrate.run,
        model_version=s.synth_model or s.chat_model,
        # pages render from the claims extraction wrote — chain its prompt key so a re-extraction
        # re-integrates (the manual cross-stage staleness idiom, like embed_version above).
        params={"summaries": s.synth_summaries, "extract_prompt": extract_prompt_fingerprint()},
        deps=("synth_entities",),
    )


def _synth_topics_stage() -> Stage:
    """Phase-4 topic pages: one prose overview per entity-rich top-level chapter. Its own ledger
    entry so retuning topic knobs (or a model upgrade) re-runs topics without re-writing thousands
    of entity pages. Runs after integrate so the entity pages it links to exist."""
    s = get_settings()
    return Stage(
        "synth_topics",
        synth_topics.run,
        model_version=s.synth_model or s.chat_model,
        params={
            "min_entities": s.synth_topic_min_entities,
            "max_entities": s.synth_topic_max_entities,
            "max_pages": s.synth_topic_max_pages,
            "extract_prompt": extract_prompt_fingerprint(),  # topic prose derives from claims too
        },
        deps=("synth_integrate",),
    )


def _synth_source_stage() -> Stage:
    """Phase-4 source profile: one page per document, linking its chapter topics + key entities.
    Runs after topics so the topic/entity pages its Contents/Key-entities sections link already
    exist. Keyed by the synth model + its knobs (own ledger entry, like topics)."""
    s = get_settings()
    return Stage(
        "synth_source",
        synth_source.run,
        model_version=s.synth_model or s.chat_model,
        params={
            "max_entities": s.synth_source_max_entities,
            "max_chapters": s.synth_source_max_chapters,
            "extract_prompt": extract_prompt_fingerprint(),  # lead paragraph derives from claims
        },
        deps=("synth_topics",),
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
            # Tables -> markdown + embedded figure rasters -> disk (PyMuPDF; needs page count only).
            Stage(
                "pdf_assets",
                pdf_assets.run,
                model_version="pymupdf-1",
                deps=("pdf_structure",),
            ),
            # OCR the extracted figure rasters (diagram labels). Graceful if tesseract is absent.
            Stage(
                "figure_ocr",
                ocr.run_figures,
                model_version="tesseract-1",
                deps=("pdf_assets",),
            ),
            _link_semantic_stage("embed_text"),
            _synth_entities_stage("embed_text"),
            _synth_integrate_stage(),
            _synth_topics_stage(),
            _synth_source_stage(),
        ]
    elif file_format == "docx":
        stages += [
            Stage(
                "docx_structure",
                docx_structure.run,
                model_version="python-docx-1",
                deps=("identify",),
            ),
            # Reuses the format-agnostic text embedder (reads chunks from the truth store).
            Stage(
                "embed_text",
                embed_text.run,
                model_version="bge-small-en-v1.5",
                deps=("docx_structure",),
            ),
            _link_semantic_stage("embed_text"),
            _synth_entities_stage("embed_text"),
            _synth_integrate_stage(),
            _synth_topics_stage(),
            _synth_source_stage(),
        ]
    elif file_format in IMAGE_FORMATS:
        stages += [
            Stage("image_extract", image_extract.run, deps=("identify",)),
            Stage("exif_geo", exif_geo.run, deps=("image_extract",)),
            # Read any embedded text (screenshots, scans). Graceful if tesseract is absent.
            Stage("ocr", ocr.run_image, model_version="tesseract-1", deps=("image_extract",)),
            # embed_image mirrors exif/geo scalars into Lance, so it runs after exif_geo.
            Stage(
                "embed_image",
                embed_image.run,
                model_version="clip-vit-b-32",
                deps=("exif_geo",),
            ),
            # Near-duplicate grouping against the whole image index. Threshold in params so
            # retuning re-runs the stage (the ledger key changes), like link_semantic.
            Stage(
                "cluster_image",
                cluster_image.run,
                model_version="clip-vit-b-32",
                params={"threshold": get_settings().cluster_sim_threshold},
                deps=("embed_image",),
            ),
            Stage(
                "vlm_describe",
                vlm_describe.run,
                model_version="llama3.2-vision",
                deps=("image_extract",),
            ),
            # Embed the caption into the text-chunk space so a plain search / Ask finds the photo
            # by what it depicts (fused into the chunk ranking, not the parallel CLIP index).
            Stage(
                "embed_caption",
                embed_caption.run,
                model_version="bge-small-en-v1.5",
                deps=("vlm_describe",),
            ),
            # Attach the image to related document sections via its description (bge bridge).
            _link_semantic_stage("vlm_describe"),
        ]
    return stages
