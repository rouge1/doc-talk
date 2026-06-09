"""Presenter agent — a second LLM pass that typesets the raw wiki-chat answer for display.

The answering model is optimized for *correctness* over *presentation*: its markdown drifts (double
brackets, label-stuffed citations, meta-commentary, ragged structure). This stage rewrites that draft
into a consistent reference "dispatch": a one-line standfirst, tight supporting prose/bullets, clean
``[n]`` citations — without adding or altering facts. It is deliberately constrained (preserve every
claim and citation verbatim; no new information) so it polishes rather than re-answers, and it falls
back to the raw draft on any failure so a formatting hiccup never blanks the answer.
"""

from __future__ import annotations

from doctalk.config import get_settings

FORMAT_SYSTEM = (
    "You are a typesetting editor for a reference wiki. Rewrite the DRAFT answer into clean, readable "
    "GitHub-flavored Markdown for display. Follow these rules exactly:\n"
    "1. Open with a STANDFIRST: a single sentence that directly answers the question, on one line "
    "prefixed with '> ' (a Markdown blockquote).\n"
    "2. Then give the supporting detail as short paragraphs and/or a bullet list ('- '). Use a "
    "'## Heading' only when there are two or more genuinely distinct sections; short answers need none.\n"
    "3. Preserve every factual claim and every bracketed citation EXACTLY, as single-bracket numbers "
    "like [1] or [1][2]. Never renumber, merge, invent, drop, double-bracket, or label them.\n"
    "4. Add nothing: no new facts, no opinions, no meta-commentary, no notes about your formatting.\n"
    "5. Be concise. Output only the formatted answer — no preamble."
)


def format_answer(question: str, draft: str, *, model: str | None = None) -> str:
    """Typeset ``draft`` into a clean dispatch. Returns the draft unchanged on empty input/failure."""
    if not draft.strip():
        return draft
    settings = get_settings()
    from doctalk.models.chat import chat  # lazy: picks up test monkeypatching, keeps imports light

    try:
        out = chat(
            [
                {"role": "system", "content": FORMAT_SYSTEM},
                {"role": "user", "content": f"Question: {question}\n\nDRAFT:\n{draft}"},
            ],
            model=model or settings.synth_model or settings.chat_model,
            options={"temperature": 0},
        )
    except Exception:  # noqa: BLE001 - a formatting failure must never blank a good answer
        return draft
    return out.strip() or draft
