"""Text embeddings via fastembed (ONNX bge — CPU-fast, no torch).

bge models expect an instruction prefix on *queries* but not on *passages*; fastembed's
``query_embed`` handles that, so retrieval quality stays high without us hand-prefixing. The
embedder is cached process-wide and loaded lazily (first use pulls the model from HuggingFace).
"""

from __future__ import annotations

from functools import lru_cache

from doctalk.config import get_settings


@lru_cache
def _embedder():
    from fastembed import TextEmbedding  # lazy: only needed when embedding

    settings = get_settings()
    return TextEmbedding(model_name=settings.embed_text_model, threads=settings.embed_threads)


def embed_passages(texts: list[str]) -> list[list[float]]:
    """Embed document chunks (no query prefix).

    ``parallel=None`` runs inference inline in this process — NOT fastembed's multiprocessing,
    which on this workload either balloons to ~9 GB across worker model-reloads or deadlocks on
    idle workers. Throughput is governed by ``embed_threads`` instead.
    """
    batch_size = get_settings().embed_batch_size
    return [vec.tolist() for vec in _embedder().embed(list(texts), batch_size=batch_size, parallel=None)]


def embed_query(text: str) -> list[float]:
    """Embed a search query (bge query-instruction prefix applied)."""
    return next(iter(_embedder().query_embed([text], parallel=None))).tolist()
