"""Wiki-first chat: answer from the synthesized wiki pages first, chunk-RAG fills the gaps.

The compounding wiki — not raw retrieval — is the primary substrate (``PLAN.md`` → Synthesis layer).
Retrieve the most relevant entity pages and the top chunk excerpts, let the local LLM answer
preferring the wiki, and (optionally) ``promote`` the answer back to ``wiki/queries/`` so good
explorations accumulate. Degrades cleanly: with no wiki pages yet it's just chunk-RAG.
"""

from __future__ import annotations

from typing import Any

from doctalk.query.prompt import format_citations
from doctalk.query.retriever import retrieve
from doctalk.query.wiki import retrieve_pages
from doctalk.query.wikiprompt import build_wiki_messages, format_wiki_citations


def answer(
    question: str,
    *,
    k_pages: int = 6,
    k_chunks: int = 6,
    file_id: int | None = None,
    save: bool = False,
) -> dict[str, Any]:
    pages = retrieve_pages(question, k=k_pages)
    chunks = retrieve(question, k=k_chunks, file_id=file_id)

    if not pages and not chunks:
        return {"answer": "I don't find anything about that in the corpus.",
                "wiki_citations": [], "citations": [], "pages": [], "hits": [], "saved_path": None}

    from doctalk.models.chat import chat as ollama_chat

    text = ollama_chat(build_wiki_messages(question, pages, chunks))
    result: dict[str, Any] = {
        "answer": text,
        "wiki_citations": format_wiki_citations(pages),
        "citations": format_citations(chunks),
        "pages": pages,
        "hits": chunks,
        "saved_path": None,
    }
    if save:
        from doctalk.synth.promote import promote_query

        result["saved_path"] = promote_query(question, text, pages, chunks)
    return result
