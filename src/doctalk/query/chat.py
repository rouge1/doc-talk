"""RAG chat: retrieve -> assemble context with provenance -> Ollama answers citing (file,
chapter, page). Returns both the answer text and the citation list so the caller (CLI now, web
later) can render citation links."""

from __future__ import annotations

from typing import Any

from doctalk.config import get_settings
from doctalk.query.prompt import build_messages, format_citations
from doctalk.query.retriever import Hit, apply_relevance_floor, retrieve


def answer(question: str, k: int = 8, file_id: int | None = None) -> dict[str, Any]:
    hits: list[Hit] = retrieve(question, k=k, file_id=file_id)
    # Off-topic filler the ANN returns to fill k shouldn't reach the LLM (see apply_relevance_floor).
    _s = get_settings()
    hits = apply_relevance_floor(hits, _s.chat_relevance_floor, _s.chat_relevance_min)
    if not hits:
        return {
            "answer": "I don't find anything about that in the corpus.",
            "citations": [],
            "hits": [],
        }
    from doctalk.models.chat import chat as ollama_chat

    text = ollama_chat(build_messages(question, hits))
    return {"answer": text, "citations": format_citations(hits), "hits": hits}
