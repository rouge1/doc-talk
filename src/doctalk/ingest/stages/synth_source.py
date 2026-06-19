"""synth_source — sub-stage (5) of the Phase 4 synthesis pass: one profile per source document.

The synthesis ladder runs corpus -> document -> chapter -> entity -> chunk. ``overview.md`` is the
corpus rung (an evolving thesis over everything); topic pages are the chapter rung; this stage
fills the missing *document* rung: one ``sources/<stem>.md`` per ingested file that says what the
source is, maps its outline (each covered chapter wikilinked to its topic page), and lists the
entities it introduced.

Like topics, the lead paragraph is authored ONLY from the source's own table of contents + its
claims, the structural sections are pure DB reads, and every link chains down to ``claim_sources``
— so the page is provenance-safe by construction (not a free-floating "document summary"). One LLM
call per source, best-effort: a flaky/absent model leaves the structural page without its lead
paragraph, never fails the stage. Idempotent: the path is deterministic (``slugify`` of the file
stem), so a re-run overwrites in place. A renamed re-drop (same content_hash, new stem) leaves the
old page as an orphan that ``wiki-lint`` flags — by design, we keep clean Obsidian-browsable names.
"""

from __future__ import annotations

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.db.models import utcnow
from doctalk.ingest.dag import StageContext
from doctalk.models.chat import chat as _chat
from doctalk.synth import pages, wikirepo
from doctalk.synth.outline import cluster_entities, linkify, slugify

_SYSTEM = (
    "You write the opening of a knowledge-wiki page profiling ONE source document. Given the "
    "document's title, its table of contents, and its most substantial entities (with one claim "
    "each), write a 120-180 word factual overview of what the document is and what it covers. Use "
    "ONLY the provided material — never invent facts. When you name a listed entity, use its "
    "[[wikilink]] exactly as given. Return only the prose: no heading, no preamble."
)


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _key_entities(session, entity_ids: set[int], cap: int):
    """The cluster's most claim-rich entities, ranked: (refs, prompt-digest lines). ``refs`` are
    (slug, name) pairs for the Key-entities section + linkifying the lead; the digest gives the
    model one grounding claim each — the same shape ``synth_topics`` feeds its chapter prompt."""
    counts = repo.count_claims_by_entity(session, list(entity_ids))
    ranked = sorted(entity_ids, key=lambda eid: counts.get(eid, 0), reverse=True)
    refs: list[tuple[str, str]] = []
    digest: list[str] = []
    for eid in ranked:
        if len(refs) >= cap:
            break
        entity = session.get(repo.Entity, eid)
        if entity is None or entity.status not in ("active", "unresolved"):
            continue
        first = next(
            (c.text for c in repo.get_claims_for_entity(session, eid) if c.status == "active"), ""
        )
        slug = pages.slug_for(entity)
        refs.append((slug, entity.name))
        digest.append(f"- [[{slug}|{entity.name}]] ({entity.type}): {first}")
    return refs, digest


def _render(file, prose, refs, contents, *, n_top, n_entities, model) -> str:
    # ingested = the file's real first-seen date (File.created_at), NOT now() — a re-synthesis
    # must not relabel when the document arrived. The footer date below is the synthesis stamp.
    ingested = file.created_at.date().isoformat() if file.created_at else "unknown"
    out = [
        f"# {file.filename}",
        "",
        f"> **source** · {file.format} · {_human_size(file.byte_size)} · {n_top} chapters · "
        f"{n_entities} entities · ingested {ingested}",
        "",
    ]
    if prose:
        out += [linkify(prose.strip(), refs), ""]
    # Contents IS the topic index: each covered chapter links to its topic page when one exists
    # (plain text otherwise), so a separate Topics list would just repeat these same chapters.
    if contents:
        out += ["## Contents", "", *contents, ""]
    if refs:
        out += ["## Key entities", "", " · ".join(f"[[{s}|{n}]]" for s, n in refs), ""]
    # plain footer, not _italic_: the wiki renderer only italicizes *asterisks* (underscores are
    # left literal so identifier names like ATT_READ survive), so _..._ would leak as raw text.
    out += ["---", f"Synthesized by {model} on {utcnow().date().isoformat()}."]
    return "\n".join(out).rstrip() + "\n"


def run(ctx: StageContext) -> None:
    s = get_settings()
    if not s.synth_sources:
        return
    file = repo.get_file(ctx.session, ctx.content_hash)
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    if file is None or file_id is None:  # pragma: no cover - defensive
        raise ValueError(f"synth_source: no file row for {ctx.content_hash}")

    clusters = cluster_entities(ctx.session, file_id)
    if not clusters:
        return  # nothing extracted from this source -> no profile to write
    stem = slugify(file.filename.rsplit(".", 1)[0])
    model = s.synth_model or s.chat_model

    # --- structural sections (deterministic DB reads) --------------------------
    all_ids: set[int] = set().union(*clusters.values())
    refs, digest = _key_entities(ctx.session, all_ids, s.synth_source_max_entities)

    all_chapters = repo.get_chapters(ctx.session, file_id)
    chap_by_id = {c.id: c for c in all_chapters}
    n_top = sum(1 for c in all_chapters if c.parent_id is None)
    topic_paths = {
        p.path for p in repo.get_wiki_pages_by_kind(ctx.session, "topic")
        if p.path.startswith(f"topics/{stem}--")
    }

    covered = sorted(
        (chap_by_id[cid] for cid in clusters if cid in chap_by_id), key=lambda c: c.ord
    )
    contents: list[str] = []
    for chapter in covered[: s.synth_source_max_chapters]:
        n = len(clusters[chapter.id])
        tpath = f"topics/{stem}--{slugify(chapter.title)}.md"
        label = (
            f"[[{stem}--{slugify(chapter.title)}|{chapter.title}]]"
            if tpath in topic_paths else chapter.title
        )
        contents.append(f"- {label} — {n} entities")
    dropped = len(covered) - len(contents)
    if dropped > 0:  # the cap is never silent (house rule)
        contents.append(f"- _(+{dropped} more chapters)_")

    # --- lead paragraph (one LLM call, best-effort) ----------------------------
    toc = "\n".join(f"- {c.title}" for c in all_chapters if c.parent_id is None)
    user = (
        f"DOCUMENT: {file.filename}\nTABLE OF CONTENTS:\n{toc or '(no outline)'}\n\n"
        f"KEY ENTITIES:\n" + "\n".join(digest)
    )
    prose = ""
    try:
        prose = _chat(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            model=model,
            options={"temperature": 0},
            timeout=s.synth_call_timeout,
        ).strip()
    except (RuntimeError, TimeoutError):  # structural page is still valid without the lead
        prose = ""

    # --- write + catalog + commit ----------------------------------------------
    # entities = the source's FULL contribution (every entity it mentions), matching the catalog
    # card + the /api source profile — not just the clustered subset (all_ids) used for the digest.
    n_entities = len(repo.get_entity_ids_for_file(ctx.session, file_id))
    wikirepo.ensure_scaffold()
    path = f"sources/{stem}.md"
    md_hash = wikirepo.write_page(
        path, _render(file, prose, refs, contents,
                      n_top=n_top, n_entities=n_entities, model=model)
    )
    repo.upsert_wiki_page(
        ctx.session, path=path, title=file.filename, kind="source", entity_id=None,
        source_count=1, last_synth_at=utcnow(), md_hash=md_hash,
    )
    wikirepo.write_page("index.md", pages.render_index(ctx.session))
    wikirepo.append_log(f"## [{utcnow().date().isoformat()}] source | {file.filename}")
    wikirepo.commit(f"synth: source profile for {file.filename}")

    ctx.scratch["synth_source"] = 1
    ctx.scratch["synth_source_authored"] = 1 if prose else 0
