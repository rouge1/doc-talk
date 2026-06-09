"""synth/overview — the wiki's evolving thesis, revised (never regenerated) each ingest.

``overview.md`` is the one page whose previous text is an *input* to its next version: after a
source integrates, the LLM is handed the current overview plus a digest of what just arrived and
asked to revise it — keep what still holds, weave in what's new, surface genuine tensions. The
per-ingest git commit then shows the thesis evolving as a readable diff (the llm-wiki pattern's
"the synthesis already reflects everything you've read", made visible).

Best-effort like the entity lead paragraphs: a missing/flaky local model leaves the previous
overview in place — never fails the stage, never blanks the page. One caveat to the stage's
byte-identical idempotency: a forced re-run re-revises the overview (authored prose, temperature 0
but still model output), which is accepted — the job ledger means re-runs only happen on
deliberate param/model changes.
"""

from __future__ import annotations

from doctalk.db import repo
from doctalk.models.chat import chat as _chat
from doctalk.synth import pages, wikirepo

_TOP_ENTITIES = 12      # digest size: the most substantial entities this source touched
_PREV_CAP = 6000        # defensive prompt cap; the instruction keeps the page far below this

_SYSTEM = (
    "You maintain the overview page of a personal knowledge wiki. Revise the CURRENT OVERVIEW to "
    "incorporate the NEW SOURCE, as an editor would: keep what is still true, weave in what the "
    "new source adds, and note genuine tensions between sources rather than papering over them. "
    "Neutral, factual prose, at most 250 words. When naming an entity listed below, use its "
    "[[wikilink]] exactly as given. Return ONLY the revised overview body — no heading, no "
    "preamble, no commentary."
)


def _digest(session, entity_ids: list[int]) -> list[str]:
    """The most claim-rich entities this source touched, as '[[slug|Name]] (type): first claim'
    lines — ready-made wikilinks the model can copy verbatim."""
    counts = repo.count_claims_by_entity(session, entity_ids)
    top = sorted(entity_ids, key=lambda eid: counts.get(eid, 0), reverse=True)[:_TOP_ENTITIES]
    lines = []
    for eid in top:
        entity = session.get(repo.Entity, eid)
        if entity is None or entity.status not in ("active", "unresolved"):
            continue
        claims = repo.get_claims_for_entity(session, eid)
        first = next((c.text for c in claims if c.status == "active"), "")
        lines.append(f"- [[{pages.slug_for(entity)}|{entity.name}]] ({entity.type}): {first}")
    return lines


def rewrite(session, *, filename: str, entity_ids: list[int], model: str) -> bool:
    """Revise ``overview.md`` in light of one newly integrated source. Returns True if the page
    was rewritten; False (page untouched) when the model is unavailable or returns nothing."""
    path = wikirepo.repo_dir() / "overview.md"
    prev = path.read_text(encoding="utf-8") if path.exists() else ""
    prev_body = prev.removeprefix("# Overview").strip()[:_PREV_CAP]

    lines = _digest(session, entity_ids)
    user = (
        f"CURRENT OVERVIEW:\n{prev_body or '(empty — this is the first source)'}\n\n"
        f"NEW SOURCE: {filename}\n"
        f"It contributed {len(entity_ids)} entities; the most substantial:\n" + "\n".join(lines)
    )
    try:
        text = _chat(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            model=model,
            options={"temperature": 0},
        ).strip()
    except Exception:  # noqa: BLE001 - authored prose is optional; never fail the stage on it
        return False
    if not text:
        return False
    wikirepo.write_page("overview.md", f"# Overview\n\n{text}\n")
    return True
