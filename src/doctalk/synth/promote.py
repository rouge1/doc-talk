"""synth/promote — file a good chat answer back into the wiki as a durable query page.

"Query answers compound": instead of vanishing into chat history, an answer you asked for is written
to ``wiki/queries/<slug>.md``, catalogued in ``wiki_pages`` (kind=query), linked from ``index.md``,
and committed — so explorations accumulate. The page records the question, the answer, and links to
the entity pages + source excerpts it drew on (keeping the provenance chain intact).
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


def _render(question: str, answer: str, page_hits: list, chunk_hits: list, date: str) -> str:
    out = [f"# {question}", "", answer.strip(), "", "## Sources", ""]
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
    out += ["", "---", f"*Filed {date} by wiki-first chat.*"]
    return "\n".join(out).rstrip() + "\n"


def promote_query(question: str, answer: str, page_hits: list, chunk_hits: list) -> str:
    """Write the query page, catalog + index it, append the log, and commit. Returns the rel path."""
    wikirepo.ensure_scaffold()
    path = f"queries/{slug_for_query(question)}.md"
    date = utcnow().date().isoformat()
    md_hash = wikirepo.write_page(path, _render(question, answer, page_hits, chunk_hits, date))
    with session_scope() as session:
        repo.upsert_wiki_page(
            session, path=path, title=question, kind="query", entity_id=None,
            source_count=len(page_hits), last_synth_at=utcnow(), md_hash=md_hash,
        )
        wikirepo.write_page("index.md", pages.render_index(session))
    wikirepo.append_log(f"## [{date}] query | {question}")
    wikirepo.commit(f"synth: promote query — {question[:60]}")
    return path
