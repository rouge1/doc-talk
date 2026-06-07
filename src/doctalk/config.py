"""Single source of truth for runtime configuration.

Per ``CLAUDE.md``: paths, model names, the VRAM budget, and the DB URL all live here and
nowhere else. Values are env-overridable with the ``DOCTALK_`` prefix (e.g.
``DOCTALK_DB_URL=sqlite:///dev.db``), which is also how the test suite points the truth store
at a throwaway SQLite file while production stays on MySQL.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DOCTALK_", env_file=".env", extra="ignore")

    # --- Truth store -------------------------------------------------------
    # MySQL is the production metadata store (chosen for the multi-user roadmap). The default
    # below assumes a local `doctalk` db/user; override with DOCTALK_DB_URL.
    db_url: str = "mysql+pymysql://doctalk:doctalk@127.0.0.1:3306/doctalk"

    # --- Paths -------------------------------------------------------------
    data_dir: Path = Path("data")          # derived artifacts (never the source of truth)
    watched_dir: Path = Path("inbox")      # drop files here
    wiki_dir: Path = Path("wiki")          # Phase 4 synthesis git repo
    lance_dir: Path = Path("data/lance")   # derived vector index
    figures_dir: Path = Path("data/figures")  # extracted PDF figure rasters (derived)

    # --- Hardware budget ---------------------------------------------------
    vram_budget_gb: int = 8                # RTX 3070 Ti Laptop wall; one model resident at a time

    # --- Model names (see PLAN.md stack table) -----------------------------
    # bge-small via fastembed (ONNX, CPU-fast, no torch) for Phase 1; swap to bge-large later.
    vlm_model: str = "llama3.2-vision"
    chat_model: str = "gemma4:e2b"
    embed_text_model: str = "BAAI/bge-small-en-v1.5"
    # CLIP ViT-B-32 via fastembed (ONNX, no torch); matched image + text towers share a 512-d space.
    clip_image_model: str = "Qdrant/clip-ViT-B-32-vision"
    clip_text_model: str = "Qdrant/clip-ViT-B-32-text"

    # --- Serving / retrieval ----------------------------------------------
    ollama_host: str = "http://127.0.0.1:11434"
    retrieval_top_k: int = 8

    # --- Reranking (cross-encoder over the ANN candidate pool) -------------
    # bge-reranker via fastembed's TextCrossEncoder (ONNX, no torch). PLAN's v2-m3 isn't
    # ONNX-packaged in fastembed, so we use the bge-reranker-base variant. Disable to fall back
    # to raw ANN order (the PLAN "skip" path).
    rerank_enabled: bool = True
    rerank_model: str = "BAAI/bge-reranker-base"
    rerank_candidates: int = 30  # ANN hits fetched, then re-scored down to top_k

    # --- Asset extraction (figures / tables / OCR) -------------------------
    # Embedded rasters smaller than this on either side are icons/rules, not figures — skipped.
    figure_min_px: int = 64
    ocr_lang: str = "eng"                  # Tesseract language pack(s), e.g. "eng+fra"

    # --- Embedding throughput ---------------------------------------------
    # fastembed's multiprocessing path either fans out to all cores (~9 GB RSS) or deadlocks on
    # idle workers; we run inline (parallel=None) with a capped onnxruntime thread count instead.
    embed_threads: int = 8
    embed_batch_size: int = 256


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton. Call ``get_settings.cache_clear()`` in tests after
    mutating the environment."""
    return Settings()
