"""Cross-encoder reranking via fastembed (ONNX bge-reranker — CPU, no torch).

The bi-encoder ANN retrieval (bge passages) is fast but coarse: it scores query and passage
independently. A cross-encoder reads the (query, passage) pair jointly and scores relevance far
more accurately — so we over-fetch candidates from LanceDB and rerank them down to top_k. The
model is cached process-wide and loaded lazily (first use pulls the ONNX weights from HuggingFace).

Optional by design: if the engine/model is unavailable, callers fall back to raw ANN order (the
PLAN "skip" path), so retrieval never hard-fails on a missing reranker.
"""

from __future__ import annotations

from functools import lru_cache

from doctalk.config import get_settings


@lru_cache
def _reranker():
    from fastembed.rerank.cross_encoder import TextCrossEncoder  # lazy: only when reranking

    settings = get_settings()
    return TextCrossEncoder(model_name=settings.rerank_model, threads=settings.embed_threads)


def rerank_available() -> bool:
    """True if the fastembed cross-encoder is importable and the configured model is supported.
    Does not download — the actual weights load lazily on the first ``rerank`` call."""
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        supported = {m["model"] for m in TextCrossEncoder.list_supported_models()}
        return get_settings().rerank_model in supported
    except Exception:  # noqa: BLE001 - missing package / API drift
        return False


def rerank(query: str, passages: list[str]) -> list[float]:
    """Return a relevance score per passage (higher = more relevant). Raw cross-encoder logits;
    callers normalize for display. Raises if the model can't load — callers handle the fallback."""
    if not passages:
        return []
    return list(_reranker().rerank(query, passages))
