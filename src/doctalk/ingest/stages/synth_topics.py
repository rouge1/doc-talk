"""synth_topics — sub-stage (4) of the Phase 4 synthesis pass: prose above the entity level.

Entity pages are the wiki's substrate (claims + provenance); topic pages are its synthesis. The
document's own outline gives clustering for free: every mention rolls up chunk → chapter →
top-level chapter, so each entity-rich top-level chapter becomes one ``topics/<slug>.md`` — an
LLM-authored encyclopedic overview written ONLY from its member entities' claims, wikilinked to
them (``## Drawn from``), so provenance chains through the entity pages down to chunks.

Slugs are prefixed with the source file's stem (two books both titled "Introduction" must not
overwrite each other). Idempotent: paths are deterministic, the catalog is reconciled (topic rows
for this file that no longer correspond to a written page are dropped), and a re-run overwrites in
place. LLM calls are capped (``synth_topic_max_pages``, busiest chapters first) and best-effort —
a failed call skips that topic, never the stage.
"""

from __future__ import annotations

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.db.models import utcnow
from doctalk.ingest.dag import StageContext
from doctalk.models.chat import chat as _chat
from doctalk.synth import pages, wikirepo
from doctalk.synth.outline import cluster_entities, linkify as _linkify, slugify as _slug

_SYSTEM = (
    "You write one section of a personal knowledge wiki. Given the entities and claims of one "
    "document chapter, write a coherent 120-220 word encyclopedic overview of the chapter's "
    "subject. Use ONLY the provided claims — never invent facts. When you name a listed entity, "
    "use its [[wikilink]] exactly as given. Return only the prose: no heading, no preamble."
)


def _digest(session, entity_ids: set[int], cap: int) -> tuple[list[str], list[tuple[str, str]]]:
    """(prompt lines, (slug, name) refs) for the cluster's most claim-rich entities."""
    counts = repo.count_claims_by_entity(session, list(entity_ids))
    ranked = sorted(entity_ids, key=lambda eid: counts.get(eid, 0), reverse=True)
    lines: list[str] = []
    refs: list[tuple[str, str]] = []
    for eid in ranked:
        if len(lines) >= cap:
            break
        entity = session.get(repo.Entity, eid)
        if entity is None or entity.status not in ("active", "unresolved"):
            continue
        first = next(
            (c.text for c in repo.get_claims_for_entity(session, eid) if c.status == "active"), ""
        )
        slug = pages.slug_for(entity)
        lines.append(f"- [[{slug}|{entity.name}]] ({entity.type}): {first}")
        refs.append((slug, entity.name))
    return lines, refs


def _render(
    title: str, filename: str, n_entities: int, prose: str, refs: list[tuple[str, str]]
) -> str:
    out = [
        f"# {title}",
        "",
        f"> **topic** · {filename} · {n_entities} entities",
        "",
        _linkify(prose.strip(), refs),
        "",
        "## Drawn from",
        "",
        " · ".join(f"[[{slug}|{name}]]" for slug, name in refs),
    ]
    return "\n".join(out).rstrip() + "\n"


def run(ctx: StageContext) -> None:
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    file = repo.get_file(ctx.session, ctx.content_hash)
    if file_id is None or file is None:  # pragma: no cover - defensive
        raise ValueError(f"synth_topics: no file row for {ctx.content_hash}")

    s = get_settings()
    if not s.synth_topics:
        return
    clusters = cluster_entities(ctx.session, file_id)
    eligible = sorted(
        ((cid, eids) for cid, eids in clusters.items() if len(eids) >= s.synth_topic_min_entities),
        key=lambda item: len(item[1]),
        reverse=True,
    )
    capped = eligible[: s.synth_topic_max_pages]
    model = s.synth_model or s.chat_model
    stem = _slug(file.filename.rsplit(".", 1)[0])

    wikirepo.ensure_scaffold()
    written: set[str] = set()
    failed = 0
    for chapter_id, entity_ids in capped:
        chapter = ctx.session.get(repo.Chapter, chapter_id)
        if chapter is None:  # pragma: no cover - defensive
            continue
        lines, refs = _digest(ctx.session, entity_ids, s.synth_topic_max_entities)
        if not refs:
            continue
        user = (
            f"CHAPTER: {chapter.title}\nSOURCE: {file.filename}\n"
            f"ENTITIES AND CLAIMS:\n" + "\n".join(lines)
        )
        try:
            prose = _chat(
                [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
                model=model,
                options={"temperature": 0},
                timeout=s.synth_call_timeout,
            ).strip()
        except (RuntimeError, TimeoutError):  # a flaky call skips its topic, never the stage
            failed += 1
            continue
        if not prose:
            failed += 1
            continue
        path = f"topics/{stem}--{_slug(chapter.title)}.md"
        md_hash = wikirepo.write_page(
            path, _render(chapter.title, file.filename, len(entity_ids), prose, refs)
        )
        repo.upsert_wiki_page(
            ctx.session, path=path, title=chapter.title, kind="topic", entity_id=None,
            source_count=1, last_synth_at=utcnow(), md_hash=md_hash,
        )
        written.add(path)

    # Reconcile: drop this file's topic rows/pages that no longer exist (chapter tree changed).
    prefix = f"topics/{stem}--"
    for page in repo.get_wiki_pages_by_kind(ctx.session, "topic"):
        if page.path.startswith(prefix) and page.path not in written:
            (wikirepo.repo_dir() / page.path).unlink(missing_ok=True)
            repo.delete_wiki_page(ctx.session, page.path)

    if written or failed:
        wikirepo.write_page("index.md", pages.render_index(ctx.session))
        wikirepo.append_log(
            f"## [{utcnow().date().isoformat()}] topics | {file.filename} ({len(written)} pages)"
        )
        wikirepo.commit(f"synth: topics for {file.filename} ({len(written)} pages)")

    ctx.scratch["synth_topics"] = len(written)
    ctx.scratch["synth_topics_failed"] = failed
    ctx.scratch["synth_topics_skipped"] = len(eligible) - len(capped)  # cap is never silent
