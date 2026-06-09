"""synth/evaluate — the gate that decides which chat answers deserve a permanent query page.

"Query answers compound" (CLAUDE.md), but not every answer is worth filing: single-page lookups
and failed answers would silt the wiki up. A mechanical pre-filter rejects the obvious cases
without an LLM call (short answers drawing on at most one source); survivors get one small
judgment: ``{"save": bool, "reason": str}``. Conservative on failure — an unreachable or
unparseable evaluator means *no save* (``ask --save`` always forces past the gate).

Pattern borrowed from new-voice-journey's WikiEvaluator, which proved the loop works: gate on
synthesis value, not answer length.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from doctalk.models.chat import chat as _chat

_MIN_ANSWER_CHARS = 200  # below this AND single-source -> never worth a page
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

_SYSTEM = (
    "You decide whether a Q&A answer deserves a permanent page in a personal knowledge wiki. "
    "SAVE an answer that synthesizes multiple sources, reveals a non-obvious connection or "
    "pattern, or would save real effort if the question came back. DO NOT SAVE single-page "
    "lookups, failed or empty answers ('I don't find anything…'), or chit-chat. "
    'Respond with JSON only: {"save": true|false, "reason": "<one short sentence>"}.'
)


@dataclass
class Verdict:
    save: bool
    reason: str


def parse_verdict(text: str) -> Verdict | None:
    """Parse the evaluator's JSON (possibly fenced); None on garbage. Exposed for testing."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        raw = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("save"), bool):
        return None
    return Verdict(save=raw["save"], reason=str(raw.get("reason", "")).strip())


def should_save(
    question: str, answer: str, *, n_pages: int, n_chunks: int, model: str | None = None
) -> Verdict:
    """Is this answer worth filing to ``wiki/queries/``? Pre-filter first, then one LLM call."""
    if len(answer.strip()) < _MIN_ANSWER_CHARS and (n_pages + n_chunks) < 2:
        return Verdict(False, "trivial: short answer drawing on at most one source")
    user = (
        f"QUESTION: {question}\n\n"
        f"ANSWER (drew on {n_pages} wiki page(s) and {n_chunks} source excerpt(s)):\n{answer}"
    )
    try:
        raw = _chat(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            model=model,
            format="json",
            options={"temperature": 0},
        )
    except Exception:  # noqa: BLE001 - filing is optional; never fail the answer on the gate
        return Verdict(False, "evaluator unavailable")
    verdict = parse_verdict(raw)
    return verdict if verdict is not None else Verdict(False, "evaluator returned no verdict")
