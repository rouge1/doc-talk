"""Pure prompt assembly for wiki-first chat (no heavy imports — unit-testable).

Presents the synthesized wiki claims as the primary substrate and raw chunk excerpts as supporting
gap-fill, instructing the model to prefer the wiki and cite both. ``PageHit``/``Hit`` are duck-typed.
"""

from __future__ import annotations

from typing import Any

WIKI_SYSTEM = (
    "You answer questions from the user's own knowledge base. You are given SYNTHESIZED KNOWLEDGE "
    "(curated wiki claims, each with its source) and SUPPORTING EXCERPTS (raw document snippets, "
    "numbered [n]). Prefer the synthesized knowledge; use the excerpts to fill gaps. Cite entities "
    "by name and excerpts inline as [n]. If the material does not contain the answer, say you don't "
    "find it in the corpus — do not use outside knowledge or invent details. "
    "Do not expand an acronym unless its expansion appears verbatim in the material, and do not state "
    "any number, channel index, or count that is not written in the material — if a specific is not "
    "given, say so rather than guessing. Apply these rules silently: never narrate them or explain "
    "what you are choosing not to expand; just write the answer."
)


def build_wiki_messages(question: str, pages: list[Any], chunks: list[Any]) -> list[dict[str, str]]:
    sections: list[str] = []
    if pages:
        blocks = []
        for p in pages:
            lines = [f"## {p.name} ({p.type})"]
            for c in p.claims:
                src = "; ".join(c.sources) if c.sources else "corpus"
                lines.append(f"- {c.text} (source: {src})")
            blocks.append("\n".join(lines))
        sections.append("SYNTHESIZED KNOWLEDGE (prefer this):\n" + "\n\n".join(blocks))
    if chunks:
        blocks = [
            f"[{i}] (file: {h.file} · {h.chapter or 'n/a'} · p.{h.page})\n{h.text}"
            for i, h in enumerate(chunks, start=1)
        ]
        sections.append("SUPPORTING EXCERPTS:\n" + "\n\n".join(blocks))

    user = (
        f"Question: {question}\n\n"
        + "\n\n".join(sections)
        + "\n\nAnswer using only the material above, preferring the synthesized knowledge. "
        "Cite a supporting excerpt as [n] — single square brackets, number only (e.g. [1]); name "
        "wiki entities in plain prose. If it isn't covered, say so."
    )
    return [
        {"role": "system", "content": WIKI_SYSTEM},
        {"role": "user", "content": user},
    ]


def format_wiki_citations(pages: list[Any]) -> list[dict[str, Any]]:
    """Citation records for the wiki pages used (name + on-disk path for a future page link)."""
    return [{"name": p.name, "type": p.type, "path": p.path} for p in pages]
