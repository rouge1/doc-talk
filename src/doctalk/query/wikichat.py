"""Wiki-first chat: answer from the synthesized wiki pages first, chunk-RAG fills the gaps.

The compounding wiki — not raw retrieval — is the primary substrate (``PLAN.md`` → Synthesis layer).
Retrieve the most relevant entity pages and the top chunk excerpts, let the local LLM answer
preferring the wiki, and (optionally) ``promote`` the answer back to ``wiki/queries/`` so good
explorations accumulate. Degrades cleanly: with no wiki pages yet it's just chunk-RAG.
"""

from __future__ import annotations

from typing import Any

from doctalk.config import get_settings
from doctalk.query.prompt import format_citations
from doctalk.query.retriever import apply_relevance_floor, retrieve
from doctalk.query.wiki import retrieve_pages
from doctalk.query.wikiprompt import build_wiki_messages, format_wiki_citations


def answer(
    question: str,
    *,
    k_pages: int = 6,
    k_chunks: int = 6,
    file_id: int | None = None,
    save: bool | str = False,
) -> dict[str, Any]:
    """``save``: False = never file; True = force-file; "auto" = the evaluator decides
    (``synth.evaluate``) whether the answer deserves a ``wiki/queries/`` page."""
    pages = retrieve_pages(question, k=k_pages)
    chunks = retrieve(question, k=k_chunks, file_id=file_id)
    # Relevance floor on both substrates: off-topic neighbors the ANN/bi-encoder returned only to fill
    # k would otherwise become confident, unrelated answer sentences (a cat question drifting into
    # "PAwR" and "eggs"). Chunks keep their single best match (keep_top); pages drop entirely when none
    # is relevant (keep_top=False) — there's no on-topic page, so chunk-RAG should carry the answer.
    _s = get_settings()
    chunks = apply_relevance_floor(chunks, _s.chat_relevance_floor, _s.chat_relevance_min)
    pages = apply_relevance_floor(pages, _s.chat_relevance_floor, _s.chat_relevance_min, keep_top=False)

    if not pages and not chunks:
        return {"answer": "I don't find anything about that in the corpus.",
                "wiki_citations": [], "citations": [], "pages": [], "hits": [],
                "saved_path": None, "save_reason": None}

    from doctalk.models.chat import chat as ollama_chat

    text = ollama_chat(build_wiki_messages(question, pages, chunks))

    # Presenter pass: typeset the raw draft into a clean dispatch (best-effort; raw on failure).
    formatted = text
    if get_settings().chat_format:
        from doctalk.query.format import format_answer

        formatted = format_answer(question, text)

    result: dict[str, Any] = {
        "answer": text,
        "formatted": formatted,
        "wiki_citations": format_wiki_citations(pages),
        "citations": format_citations(chunks),
        "pages": pages,
        "hits": chunks,
        "saved_path": None,
        "save_reason": None,
    }
    do_save = bool(save)
    if save == "auto":  # the evaluator decides whether this answer compounds
        from doctalk.synth.evaluate import should_save

        verdict = should_save(question, text, n_pages=len(pages), n_chunks=len(chunks))
        do_save, result["save_reason"] = verdict.save, verdict.reason
    if do_save:
        from doctalk.synth.promote import promote_query

        result["saved_path"] = promote_query(question, text, pages, chunks)
    return result
