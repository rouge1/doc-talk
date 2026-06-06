"""Ingest stages. Phase 1 implements the document backbone: ``identify`` -> ``pdf_structure``
(outline tree + per-page chunks) -> ``link_internal`` (resolved PDF cross-references). The
GPU/model stages (image_extract, ocr, vlm_describe, embed_*, link_semantic, cluster, wiki_build)
arrive next."""
