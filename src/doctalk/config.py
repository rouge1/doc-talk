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
    rendered_dir: Path = Path("data/rendered")  # office docs rendered to PDF for the page viewer

    # --- Hardware budget ---------------------------------------------------
    vram_budget_gb: int = 8                # RTX 3070 Ti Laptop wall; one model resident at a time

    # --- Model names (see PLAN.md stack table) -----------------------------
    # bge-small via fastembed (ONNX, CPU-fast, no torch) for Phase 1; swap to bge-large later.
    vlm_model: str = "llama3.2-vision"
    chat_model: str = "qwen3.5:9b"   # 6.6 GB, fits the 8 GB wall; far better synthesis than a 2B
    # Thinking-capable models (gemma4, qwen3.5) otherwise spend their whole generation budget on a
    # hidden reasoning pass and return empty ``content`` (done_reason=length). We disable thinking and
    # give the prompt real context room — see ``models.chat``. num_ctx must hold the wiki+excerpt
    # prompt AND the answer; 4 K (Ollama's default) truncates both.
    chat_think: bool = False
    chat_num_ctx: int = 16384
    chat_num_predict: int = 1500
    embed_text_model: str = "BAAI/bge-small-en-v1.5"
    # CLIP ViT-B-32 via fastembed (ONNX, no torch); matched image + text towers share a 512-d space.
    clip_image_model: str = "Qdrant/clip-ViT-B-32-vision"
    clip_text_model: str = "Qdrant/clip-ViT-B-32-text"

    # --- Serving / retrieval ----------------------------------------------
    ollama_host: str = "http://127.0.0.1:11434"
    retrieval_top_k: int = 8

    # --- Semantic cross-linking (relations across the corpus) --------------
    # A chapter/image links to the most similar OTHER document sections (bge cosine). Thresholded
    # so unrelated content stays unlinked ("don't force it"); capped per source to limit noise.
    # 0.70 keeps strongly-related sections (same-topic ~0.72-0.85) while letting unrelated content
    # stay unlinked — on a disparate corpus this correctly yields no spurious cross-modal links.
    link_sim_threshold: float = 0.70
    link_top_n: int = 5            # max relations emitted per source chapter/image
    link_fetch_k: int = 40         # ANN candidate chunks fetched, then aggregated to chapters

    # --- Reranking (cross-encoder over the ANN candidate pool) -------------
    # bge-reranker via fastembed's TextCrossEncoder (ONNX, no torch). PLAN's v2-m3 isn't
    # ONNX-packaged in fastembed, so we use the bge-reranker-base variant. Disable to fall back
    # to raw ANN order (the PLAN "skip" path).
    rerank_enabled: bool = True
    rerank_model: str = "BAAI/bge-reranker-base"
    rerank_candidates: int = 30  # ANN hits fetched, then re-scored down to top_k
    # Simple (keyword) search: max chunks scanned for the lexical LIKE match before ranking. Also the
    # per-arm candidate pool size for hybrid fusion (RRF of the lexical + dense arms).
    keyword_candidates: int = 200

    # --- Synthesis layer (Phase 4 — the compounding wiki) ------------------
    # The synthesis LLM shares the chat model's GPU lease (no new resident model, so the 8 GB wall
    # is unchanged). None reuses ``chat_model``; set DOCTALK_SYNTH_MODEL to a stronger extractor
    # (e.g. "llama3.1:8b"). Extraction sweeps the WHOLE source in coherent, bounded windows of
    # consecutive chunks (one LLM call each) so a large spec yields entities instead of one jumbled
    # mega-prompt the model answers in prose. ``synth_full_sweep=False`` falls back to the old
    # single-window evenly-spaced sample (cheap; fine for tiny sources). The wiki repo lives at
    # ``wiki_dir``.
    synth_model: str | None = None
    synth_full_sweep: bool = True    # window the entire document, not a 40-chunk sample
    synth_window_chunks: int = 10    # consecutive chunks per extraction window (full-sweep mode)
    synth_call_timeout: float = 120.0  # per-window LLM timeout; a stuck call skips its window, not the sweep
    synth_max_claims_per_entity: int = 12  # cap claims kept per entity across the sweep
    synth_max_chunks: int = 40       # sample size when synth_full_sweep is off
    synth_chunk_chars: int = 1200    # per-chunk char cap inside the extraction window
    # Salience gate (full-sweep mode only): on a big document, a candidate surfacing in a single
    # window with a single claim is almost always noise — keep it only if it recurs, says more, or
    # matches an entity we already know. Small docs (< synth_salience_min_windows windows) are
    # exempt: with one window, "appeared once" carries no signal. The shape gate (synth.gate —
    # numeric/hex literals, measurements, doc self-references) applies always, at extraction.
    synth_min_windows: int = 2       # windows a one-claim candidate must recur in to be kept
    synth_min_claims: int = 2        # claims that excuse a single-window candidate
    synth_salience_min_windows: int = 5  # sweeps smaller than this skip the salience gate
    # synth_integrate writes an LLM-authored lead paragraph for multi-claim entity pages (the
    # signature "authored prose"). Best-effort + bounded to entities with >=2 claims to cap LLM
    # calls; the page is valid (claims + provenance + links) even when this is off or the LLM fails.
    synth_summaries: bool = True
    # After each ingest, the LLM revises overview.md (previous text is an input — the "evolving
    # thesis"; see synth.overview). Best-effort: a missing model leaves the page untouched.
    synth_overview: bool = True
    # Topic pages (synth_topics stage): one LLM-authored overview per entity-rich top-level
    # chapter, written only from member entities' claims and wikilinked to them. Calls are capped
    # per source (busiest chapters first; the skip count is reported, never silent).
    synth_topics: bool = True
    synth_topic_min_entities: int = 5   # chapters with fewer member entities get no topic page
    synth_topic_max_entities: int = 15  # entities (one claim each) fed to a topic prompt
    synth_topic_max_pages: int = 30     # LLM-call cap per source
    # Wiki-first chat gates authored pages on cosine relevance so off-topic pages (e.g. the recipe
    # entities for a Bluetooth question) aren't cited just because they're the only pages that exist.
    wiki_page_min_score: float = 0.30  # min name+definition cosine to surface a wiki page
    # A second "presenter" LLM pass typesets the raw answer into a clean dispatch (query.format).
    # Off by default in tests/headless; the API turns it on. Doubles answer latency, so the SPA
    # caches results client-side.
    chat_format: bool = True
    # "Query answers compound": good answers are filed to wiki/queries/ automatically, gated by an
    # LLM evaluator (synth.evaluate) so lookups/failures don't silt the wiki up. `ask --save`
    # forces past the gate; set false for read-only chat.
    chat_auto_promote: bool = True
    # Question-title cosine above which two queries get the one-shot "same subject?" LLM judge
    # (synth.promote). Same-subject re-phrasings append to the existing page instead of forking.
    query_dup_threshold: float = 0.85

    # --- Entity resolution (synth_resolve; see docs/entity-resolution.md) ---
    # Two-threshold band over a [0,1] score: confident MATCH only when high AND well-separated from
    # the runner-up; confident NEW only when clearly novel; the murky middle DEFERs (LLM-adjudicated,
    # then human) — never guessed. Bad merges are worse than fragments, so τ_high is set for
    # precision. embed_version is part of the resolver's identity so an embed-model upgrade re-blocks.
    resolve_tau_high: float = 0.85   # MATCH floor
    resolve_tau_low: float = 0.45    # below this => NEW
    resolve_margin: float = 0.15     # required gap over the 2nd-best candidate to auto-MATCH
    resolve_block_k: int = 10        # kNN candidates pulled from the entity-name index
    resolve_embed_version: str = "bge-small-en-v1.5"
    # Hand-set signal weights (graduate to a learned scorer once the review queue yields labels).
    # The two embedding signals from the spec (name + context) are folded into one name+definition
    # vector here; co-mention overlap remains the polysemy disambiguator.
    resolve_w_alias: float = 0.35    # exact alias/acronym hit
    resolve_w_lexical: float = 0.15  # name token-set Jaccard
    resolve_w_embed: float = 0.40    # name+definition embedding cosine
    resolve_w_comention: float = 0.10  # neighbor-overlap Jaccard
    resolve_llm_adjudicate: bool = True  # let the LLM settle DEFERs before the human queue

    # --- Image clustering / dedup (near-duplicate grouping over CLIP) -------
    # Images whose pairwise CLIP-vision cosine >= this threshold land in one cluster; the
    # cluster_id is the smallest file_id in the connected component (stable + order-independent,
    # so a re-run yields identical labels). 0.92 groups near-identical photos (re-saves, resizes,
    # recompressions) without merging merely-similar-but-distinct images. O(n^2) at recluster time
    # — fine for gallery-scale photo sets.
    cluster_sim_threshold: float = 0.92
    cluster_fetch_k: int = 20      # ANN neighbours pulled per image before the threshold cut

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
