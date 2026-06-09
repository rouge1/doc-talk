"""synth_integrate — sub-stage (3) of the Phase 4 synthesis pass.

Materializes the compounding wiki on disk: for each entity this source touched, (re)write its
``entities/<slug>.md`` page from the *cumulative* set of claims (across all sources, each cited to
its chunk), interlinked via ``[[wikilinks]]`` to co-mentions. Then revise ``overview.md`` (the
evolving thesis — ``synth.overview``), regenerate ``index.md``, append a grep-parseable line to
``log.md``, and **commit to git — one commit per ingested source** (the wiki's version history).
Pages with >=2 claims get a best-effort LLM-authored lead paragraph.

Idempotent: pages are regenerated deterministically from the DB, so a re-run reproduces them (git
sees no diff → no empty commit). The markdown lives in the ``wiki/`` git repo, not MySQL; the
``wiki_pages`` catalog row + ``entity.wiki_path`` index it back to the truth store.
"""

from __future__ import annotations

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.db.models import utcnow
from doctalk.ingest.dag import StageContext
from doctalk.synth import pages, wikirepo


def _summarize(entity_name: str, claim_texts: list[str], model: str) -> str | None:
    """Best-effort 1–2 sentence integrated summary of an entity's claims. Never raises — a missing
    or flaky local LLM just yields no lead paragraph (the cited claims still carry the page)."""
    from doctalk.models.chat import chat

    try:
        joined = "\n".join(f"- {t}" for t in claim_texts)
        text = chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You write one neutral, factual sentence (two at most) summarizing what is "
                        "known about a topic, using ONLY the provided claims. No new facts, no "
                        "preamble."
                    ),
                },
                {"role": "user", "content": f"Topic: {entity_name}\nClaims:\n{joined}"},
            ],
            model=model,
            options={"temperature": 0},
        ).strip()
        return text or None
    except Exception:  # noqa: BLE001 - synthesis prose is optional; never fail the stage on it
        return None


def run(ctx: StageContext) -> None:
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    file = repo.get_file(ctx.session, ctx.content_hash)
    if file_id is None or file is None:  # pragma: no cover - defensive
        raise ValueError(f"synth_integrate: no file row for {ctx.content_hash}")

    entity_ids = repo.get_entity_ids_for_file(ctx.session, file_id)
    if not entity_ids:  # nothing was extracted (e.g. image-only source)
        return

    s = get_settings()
    model = s.synth_model or s.chat_model
    wikirepo.ensure_scaffold()

    written = 0
    for entity_id in entity_ids:
        entity = ctx.session.get(repo.Entity, entity_id)
        if entity is None:  # pragma: no cover - defensive
            continue
        claims = repo.get_claims_for_entity(ctx.session, entity_id)
        active = [c.text for c in claims if c.status == "active"]
        summary = (
            _summarize(entity.name, active, model)
            if s.synth_summaries and len(active) >= 2
            else None
        )
        path = f"entities/{pages.slug_for(entity)}.md"
        md_hash = wikirepo.write_page(path, pages.render_entity_page(ctx.session, entity, summary))
        repo.upsert_wiki_page(
            ctx.session,
            path=path,
            title=entity.name,
            kind="entity",
            entity_id=entity_id,
            source_count=entity.source_count,
            last_synth_at=utcnow(),
            md_hash=md_hash,
        )
        repo.set_entity_wiki_path(ctx.session, entity_id, path)
        written += 1

    if s.synth_overview:  # the evolving thesis: revise overview.md in light of this source
        from doctalk.synth import overview

        overview.rewrite(ctx.session, filename=file.filename, entity_ids=entity_ids, model=model)

    wikirepo.write_page("index.md", pages.render_index(ctx.session))  # catalog, every ingest
    wikirepo.append_log(f"## [{utcnow().date().isoformat()}] ingest | {file.filename} ({written} entities)")
    wikirepo.commit(f"synth: integrate {file.filename} ({written} entities)")
    ctx.scratch["wiki_pages"] = written
