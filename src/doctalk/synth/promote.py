"""synth/promote — file a good chat answer back into the wiki as a durable query page.

"Query answers compound": instead of vanishing into chat history, an answer you asked for is written
to ``wiki/queries/<slug>.md``, catalogued in ``wiki_pages`` (kind=query), linked from ``index.md``,
and committed — so explorations accumulate. The page records the question, the answer, and links to
the entity pages + source excerpts it drew on (keeping the provenance chain intact).

Re-asks accumulate rather than overwrite: an answer filed to an existing page is appended as a dated
``## Update`` snapshot (the answer's truth can change as sources arrive — the page keeps the
history), and a byte-identical answer is a silent no-op. Near-duplicate questions merge: before a
new page is created, the question is embedded against existing query titles and a high-cosine match
gets one LLM "same subject?" judgment — same-subject re-phrasings file into the existing page
instead of spawning ``cake-baking-time`` next to ``how-long-to-bake-the-cake``. (The judge asks
about the *subject*, not the question's shape — structural rhymes like "How many X…?" must not
magnet-match across subjects; lesson inherited from new-voice-journey's matcher.)
"""

from __future__ import annotations

import re

from doctalk.db import repo
from doctalk.db.models import utcnow
from doctalk.db.session import session_scope
from doctalk.synth import pages, wikirepo

_SLUG = re.compile(r"[^a-z0-9]+")


def slug_for_query(question: str) -> str:
    base = _SLUG.sub("-", question.lower()).strip("-")
    return (base[:60].rstrip("-")) or "query"


def _sources_block(page_hits: list, chunk_hits: list) -> list[str]:
    out: list[str] = []
    for p in page_hits:
        if p.path:
            stem = p.path.rsplit("/", 1)[-1].removesuffix(".md")
            out.append(f"- [[{stem}|{p.name}]] (wiki)")
        else:
            out.append(f"- {p.name} (wiki)")
    seen: set[str] = set()
    for h in chunk_hits:
        ref = f"{h.file} · {h.chapter or 'n/a'} · p.{h.page}"
        if ref not in seen:
            seen.add(ref)
            out.append(f"- {ref} (excerpt)")
    return out


def _render(question: str, answer: str, page_hits: list, chunk_hits: list, date: str) -> str:
    out = [f"# {question}", "", answer.strip(), "", "## Sources", ""]
    out += _sources_block(page_hits, chunk_hits)
    out += ["", "---", f"*Filed {date} by wiki-first chat.*"]
    return "\n".join(out).rstrip() + "\n"


def _render_update(answer: str, page_hits: list, chunk_hits: list, date: str) -> str:
    out = ["", "---", "", f"## Update {date}", "", answer.strip(), "", "### Sources", ""]
    out += _sources_block(page_hits, chunk_hits)
    return "\n".join(out).rstrip() + "\n"


def _embed_titles(question: str, titles: list[str]) -> list[float] | None:
    """Cosine of the question against each existing query title; None when no model. Indirected
    for tests, like ``wiki._embed_query``."""
    try:
        from doctalk.models.embed import embed_passages, embed_query

        qv = embed_query(question)
        tvs = embed_passages(titles)
    except Exception:  # noqa: BLE001 - no model: skip dedup, a near-dup page is recoverable
        return None
    import math

    def cos(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(x * x for x in b)) or 1.0
        return dot / (na * nb)

    return [cos(qv, tv) for tv in tvs]


def _same_subject(a: str, b: str, model: str | None) -> bool:
    """One-shot LLM judgment: same subject, or merely the same shape? Defaults to different —
    a false negative costs a near-duplicate page (recoverable); a false positive misfiles."""
    from doctalk.models.chat import chat

    try:
        verdict = chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Two questions are SAME only if they ask about the same subject — not "
                        "merely the same kind of question. 'How long does the cake bake?' and "
                        "'What is the cake's baking time?' are SAME; 'How many chapters?' and "
                        "'How many figures?' are DIFFERENT. Answer with exactly one word: "
                        "SAME or DIFFERENT."
                    ),
                },
                {"role": "user", "content": f"Question 1: {a}\nQuestion 2: {b}"},
            ],
            model=model,
            options={"temperature": 0},
        )
    except Exception:  # noqa: BLE001
        return False
    return verdict.strip().upper().startswith("SAME")


def _find_similar_query(session, question: str) -> "repo.WikiPage | None":
    """An existing query page asking the same thing differently (embedding gate + LLM judge)."""
    from doctalk.config import get_settings

    s = get_settings()
    existing = repo.get_wiki_pages_by_kind(session, "query")
    if not existing:
        return None
    scores = _embed_titles(question, [p.title for p in existing])
    if scores is None:
        return None
    best = max(range(len(existing)), key=lambda i: scores[i])
    if scores[best] < s.query_dup_threshold:
        return None
    candidate = existing[best]
    model = s.synth_model or s.chat_model
    return candidate if _same_subject(question, candidate.title, model) else None


def promote_query(question: str, answer: str, page_hits: list, chunk_hits: list) -> str:
    """File the answer: new page, or a dated ``## Update`` on the same/equivalent question's page.
    Catalogs + indexes + logs + commits either way. Returns the wiki-relative path."""
    wikirepo.ensure_scaffold()
    date = utcnow().date().isoformat()
    path = f"queries/{slug_for_query(question)}.md"

    with session_scope() as session:
        on_disk = (wikirepo.repo_dir() / path).exists()
        if not on_disk:  # a re-phrasing of an already-filed question files there instead
            similar = _find_similar_query(session, question)
            if similar is not None:
                path = similar.path
                on_disk = (wikirepo.repo_dir() / path).exists()

        if on_disk:
            existing = (wikirepo.repo_dir() / path).read_text(encoding="utf-8")
            if answer.strip() and answer.strip() in existing:
                return path  # identical re-ask — nothing new to file
            md_hash = wikirepo.write_page(
                path, existing.rstrip() + "\n" + _render_update(answer, page_hits, chunk_hits, date)
            )
            op, message = "query-update", f"synth: update query — {question[:60]}"
        else:
            md_hash = wikirepo.write_page(path, _render(question, answer, page_hits, chunk_hits, date))
            op, message = "query", f"synth: promote query — {question[:60]}"

        page = repo.get_wiki_page_by_path(session, path)
        repo.upsert_wiki_page(
            session, path=path, title=page.title if page else question, kind="query",
            entity_id=None, source_count=len(page_hits), last_synth_at=utcnow(), md_hash=md_hash,
        )
        wikirepo.write_page("index.md", pages.render_index(session))
    wikirepo.append_log(f"## [{date}] {op} | {question}")
    wikirepo.commit(message)
    return path
