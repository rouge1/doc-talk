"""``doctalk`` CLI (Phase 0).

Commands:
  * ``initdb``  — create tables directly from the models (dev convenience; production uses
                  ``alembic upgrade head``).
  * ``ingest``  — hash a file, upsert its truth-store row, run the resumable DAG, print results.
  * ``stats``   — counts of files and jobs by status.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

import typer
from sqlalchemy import func, select

from doctalk.db import repo
from doctalk.db.models import (
    Base,
    Chapter,
    Chunk,
    Claim,
    Entity,
    EntityMerge,
    EntityReview,
    Figure,
    File,
    Image,
    Job,
    JobStatus,
    Link,
    Mention,
    Relation,
    WikiPage,
)
from doctalk.db.session import get_engine, session_scope
from doctalk.hashing import hash_file
from doctalk.ingest.dag import run_dag
from doctalk.ingest.pipeline import pipeline_for

app = typer.Typer(help="doctalk — local drop-files -> wiki + chat knowledge base.", no_args_is_help=True)


@app.command()
def initdb() -> None:
    """Create all tables from the ORM models (dev convenience; prefer Alembic in production)."""
    Base.metadata.create_all(get_engine())
    typer.echo("Created tables from models.")


def _ingest_one(path: Path) -> None:
    content_hash = hash_file(path)
    fmt = path.suffix.lstrip(".").lower()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    byte_size = path.stat().st_size

    with session_scope() as session:
        repo.upsert_file(
            session,
            content_hash=content_hash,
            path=str(path.resolve()),
            filename=path.name,
            format=fmt,
            mime=mime,
            byte_size=byte_size,
        )

    typer.echo(f"{path.name}  content_hash={content_hash[:12]}…  ({byte_size} bytes)")
    results = run_dag(content_hash, pipeline_for(fmt), file_path=str(path))
    for r in results:
        typer.echo(f"  {r.stage:<14} {r.status}" + (f"  {r.error}" if r.error else ""))


@app.command()
def ingest(path: Path) -> None:
    """Ingest a file, or every file in a directory: hash -> upsert -> run the DAG."""
    if path.is_dir():
        files = sorted(p for p in path.iterdir() if p.is_file() and not p.name.startswith("."))
        if not files:
            raise typer.BadParameter(f"no files in directory: {path}")
        for f in files:
            _ingest_one(f)
    elif path.is_file():
        _ingest_one(path)
    else:
        raise typer.BadParameter(f"not a file or directory: {path}")


@app.command()
def stats() -> None:
    """Print counts: files, jobs by status, and extracted structure."""
    with session_scope() as session:
        for table, label in (
            (File, "files"),
            (Chapter, "chapters"),
            (Chunk, "chunks"),
            (Link, "links"),
            (Relation, "relations"),
            (Figure, "figures"),
            (Image, "images"),
            (Entity, "entities"),
            (Claim, "claims"),
            (Mention, "mentions"),
            (WikiPage, "wikipages"),
        ):
            n = session.scalar(select(func.count()).select_from(table))
            typer.echo(f"{label:<9} {n}")
        # Image dedup summary: distinct clusters + redundant (non-representative) images.
        open_reviews = session.scalar(
            select(func.count()).select_from(EntityReview).where(EntityReview.state == "open")
        )
        merges = session.scalar(select(func.count()).select_from(EntityMerge))
        if open_reviews or merges:
            typer.echo(f"resolve   {open_reviews} review(s) open · {merges} merge(s)")

        clustered = session.scalar(
            select(func.count()).select_from(Image).where(Image.cluster_id.is_not(None))
        )
        n_clusters = session.scalar(
            select(func.count(func.distinct(Image.cluster_id))).where(Image.cluster_id.is_not(None))
        )
        if clustered:
            typer.echo(f"clusters  {n_clusters} ({clustered - n_clusters} redundant)")

        rows = session.execute(select(Job.status, func.count()).group_by(Job.status)).all()
        if not rows:
            typer.echo("jobs:     (none)")
        for status, count in rows:
            value = status.value if isinstance(status, JobStatus) else status
            typer.echo(f"jobs:     {value:<8} {count}")


def _resolve_hash(session, target: str) -> str:
    """Accept either a file path (hash it) or a content_hash (full or unique prefix)."""
    p = Path(target)
    if p.is_file():
        return hash_file(p)
    match = session.scalar(select(File.content_hash).where(File.content_hash.like(f"{target}%")))
    if match is None:
        raise typer.BadParameter(f"no ingested file matches {target!r} (path or content_hash)")
    return match


@app.command()
def outline(target: str, max_depth: int = typer.Option(3, help="deepest heading level to show")) -> None:
    """Print a file's chapter tree (browse the outline). TARGET is a path or content_hash."""
    with session_scope() as session:
        content_hash = _resolve_hash(session, target)
        file = repo.get_file(session, content_hash)
        if file is None:
            raise typer.BadParameter(f"{target!r} is not ingested yet")
        chapters = repo.get_chapters(session, file.id)
        if not chapters:
            typer.echo("(no outline — not a structured PDF, or not yet processed)")
            return
        for c in chapters:
            if c.level <= max_depth:
                typer.echo("  " * (c.level - 1) + f"{c.title}  · p.{c.page_start}")


@app.command()
def figures(
    target: str,
    kind: str = typer.Option(None, help="restrict to 'table' or 'figure'"),
) -> None:
    """List the tables and figures extracted from a document. TARGET is a path or content_hash."""
    with session_scope() as session:
        content_hash = _resolve_hash(session, target)
        file = repo.get_file(session, content_hash)
        if file is None:
            raise typer.BadParameter(f"{target!r} is not ingested yet")
        rows = repo.get_figures(session, file.id)
        if kind:
            rows = [r for r in rows if r.kind == kind]
        if not rows:
            typer.echo("(no figures/tables — not extracted yet, or none found)")
            return
        for r in rows:
            tag = r.kind.upper()
            typer.echo(f"[{tag}] p.{r.page}" + (f"  {r.width}x{r.height}" if r.width else ""))
            if r.table_md:
                first = r.table_md.strip().splitlines()[0][:100]
                typer.echo(f"      {first}…")
            if r.image_path:
                typer.echo(f"      {r.image_path}")
            if r.ocr_text:
                typer.echo(f"      ocr: {r.ocr_text[:100].replace(chr(10), ' ').strip()}…")


@app.command()
def entities(limit: int = typer.Option(40, help="max entities to list")) -> None:
    """List synthesized entities (most-referenced first) with claim + source counts."""
    with session_scope() as session:
        rows = repo.get_entities(session, limit=limit)
        if not rows:
            typer.echo("(no entities — ingest a document so synth_entities can run, with Ollama up)")
            return
        for e in rows:
            claims = len(repo.get_claims_for_entity(session, e.id))
            aliases = f"  ({', '.join(e.aliases)})" if e.aliases else ""
            typer.echo(
                f"[{e.type}] {e.name}{aliases}  · {claims} claim(s) · {e.source_count} source(s)"
            )


def _resolve_entity(session, target: str) -> Entity:
    """Find an entity by numeric id, exact norm_key, or a name substring."""
    from doctalk.synth.normalize import norm_key

    if target.isdigit():
        entity = session.get(Entity, int(target))
        if entity is not None:
            return entity
    entity = session.scalar(select(Entity).where(Entity.norm_key == norm_key(target)))
    if entity is None:
        entity = session.scalar(select(Entity).where(Entity.name.like(f"%{target}%")))
    if entity is None:
        raise typer.BadParameter(f"no entity matches {target!r} (id, norm_key, or name substring)")
    return entity


@app.command()
def entity_review(limit: int = typer.Option(40, help="max queue entries")) -> None:
    """List the open entity-resolution review queue (ambiguous DEFERs the LLM couldn't settle)."""
    with session_scope() as session:
        rows = repo.get_open_reviews(session, limit=limit)
        if not rows:
            typer.echo("(review queue empty)")
            return
        for r in rows:
            payload = r.payload if isinstance(r.payload, dict) else {}
            cands = payload.get("signals", {}).get("candidates")
            typer.echo(
                f"#{r.id} [{r.mention_type}] {r.mention_surface}  · entity {r.entity_id}"
                f" · llm={r.llm_verdict or '-'}" + (f" · candidates {cands}" if cands else "")
            )


@app.command()
def wiki_merge(
    from_entity: str = typer.Argument(None, help="entity to merge FROM (id/name/norm_key)"),
    into_entity: str = typer.Argument(None, help="entity to merge INTO (id/name/norm_key)"),
    slug_collisions: bool = typer.Option(
        False, "--slug-collisions", help="batch-heal active entities that share a slug"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="with --slug-collisions: print the plan, change nothing"
    ),
    reason: str = typer.Option("manual merge", help="why (recorded in entity_merges)"),
) -> None:
    """Merge one entity into another (reversible). Repoints mentions/claims, rewrites the survivor
    page, leaves a redirect stub, and commits to the wiki git repo. FROM/INTO are id/name/norm_key.

    ``--slug-collisions`` instead batch-heals every active entity that shares a slug with another,
    auto-merging only the safe pairs (matching underscore/space-insensitive key + compatible type);
    genuine collisions like ``C[t+1]``/``C[t-1]`` are reported and left for a manual merge."""
    from doctalk.synth import merge, pages, wikirepo

    if slug_collisions:
        _wiki_merge_collisions(dry_run)
        return
    if not from_entity or not into_entity:
        raise typer.BadParameter("provide FROM and INTO entities, or use --slug-collisions")

    with session_scope() as session:
        src = _resolve_entity(session, from_entity)
        dst = _resolve_entity(session, into_entity)
        if src.id == dst.id:
            raise typer.BadParameter("cannot merge an entity into itself")
        src_name, dst_name = src.name, dst.name
        merge_id = merge.apply_merge(session, src, dst, reason)
        wikirepo.write_page("index.md", pages.render_index(session))

    committed = wikirepo.commit(f"wiki-merge: {src_name} -> {dst_name}")
    sha = wikirepo.head_sha() if committed else None
    if sha:
        with session_scope() as session:
            row = session.get(EntityMerge, merge_id)
            if row is not None:
                row.committed_sha = sha
    typer.echo(f"merged '{src_name}' -> '{dst_name}'" + (f"  ({sha[:8]})" if sha else ""))


def _wiki_merge_collisions(dry_run: bool) -> None:
    """Plan (and unless ``dry_run``, apply) the slug-collision batch heal as one wiki commit."""
    from doctalk.synth import merge, wikirepo

    with session_scope() as session:
        if dry_run:
            mergeable, skipped = merge.plan_slug_collision_merges(session)
            for src, dst, why in skipped:
                typer.echo(f"  skip   {src.name!r} ~ {dst.name!r}  — {why}")
            for src, dst in mergeable:
                typer.echo(f"  would merge {src.name!r} -> {dst.name!r}")
            typer.echo(f"\n{len(mergeable)} merge(s) planned, {len(skipped)} left manual "
                       f"(dry-run — nothing changed)")
            return
        applied, skipped = merge.merge_slug_collisions(session)
        for sname, dname, why in skipped:
            typer.echo(f"  skip   {sname!r} ~ {dname!r}  — {why}")
        for sname, dname in applied:
            typer.echo(f"  merged {sname!r} -> {dname!r}")

    if applied:
        wikirepo.commit(f"wiki-merge: {len(applied)} slug collision(s)")
    typer.echo(f"\n{len(applied)} merged, {len(skipped)} left manual")


@app.command()
def wiki_lint(
    fix: bool = typer.Option(False, "--fix", help="materialize missing entity pages (safe)"),
) -> None:
    """Health-check the wiki: orphans, unsupported claims, unresolved entities, missing pages,
    near-duplicates, contradictions. ``--fix`` creates absent pages (never overwrites)."""
    from doctalk.config import get_settings
    from doctalk.synth import lint, pages, wikirepo

    wiki_dir = get_settings().wiki_dir
    with session_scope() as session:
        findings = lint.lint(session, wiki_dir)

    if not findings:
        typer.echo("wiki-lint: clean ✓")
    else:
        grouped: dict[str, list] = {}
        for f in findings:
            grouped.setdefault(f.kind, []).append(f)
        for kind, items in grouped.items():
            typer.echo(f"\n{kind}  ({len(items)})")
            for f in items:
                ref = f"{f.ref}: " if f.ref else ""
                typer.echo(f"  - {ref}{f.detail}")

    if fix:
        with session_scope() as session:
            created = lint.materialize_missing(session, wiki_dir)
            if created:
                wikirepo.write_page("index.md", pages.render_index(session))
        if created:
            wikirepo.commit(f"wiki-lint: materialize {len(created)} missing page(s)")
            typer.echo(f"\nfixed: created {len(created)} page(s) — {', '.join(created)}")
        else:
            typer.echo("\nfixed: nothing to materialize")


@app.command()
def wiki_audit() -> None:
    """Audit wiki↔truth integrity: every cited chunk still exists; catalog matches disk."""
    from doctalk.config import get_settings
    from doctalk.synth import lint

    with session_scope() as session:
        findings = lint.audit(session, get_settings().wiki_dir)
    if not findings:
        typer.echo("wiki-audit: no drift ✓")
        return
    for f in findings:
        ref = f"{f.ref}: " if f.ref else ""
        typer.echo(f"[{f.kind}] {ref}{f.detail}")


@app.command()
def wiki_prune(
    dry_run: bool = typer.Option(False, "--dry-run", help="list what would be pruned; change nothing"),
) -> None:
    """Drop noise entities (gate-failing names: numeric/hex literals, measurements, document
    self-references) and unattested ones (no claims/mentions after a re-synthesis): status ->
    'pruned' (reversible — a future mention reactivates), page + catalog row + name vector
    removed, index regenerated, one git commit."""
    from doctalk.config import get_settings
    from doctalk.db.models import utcnow
    from doctalk.synth import pages, prune, wikirepo

    wiki_dir = get_settings().wiki_dir
    if dry_run:
        with session_scope() as session:
            junk = [e.name for e in prune.junk_entities(session)]
            orphans = [e.name for e in prune.orphan_entities(session)]
        if not junk and not orphans:
            typer.echo("wiki-prune: nothing to prune ✓")
            return
        for label, names in (("gate-failing", junk), ("unattested", orphans)):
            if names:
                typer.echo(f"{label} ({len(names)}):")
                for name in names[:20]:
                    typer.echo(f"  - {name!r}")
                if len(names) > 20:
                    typer.echo(f"  … and {len(names) - 20} more")
        typer.echo(f"would prune {len(junk) + len(orphans)} entit(ies); run without --dry-run to apply")
        return

    with session_scope() as session:
        pruned = prune.prune(session, wiki_dir)
        if pruned:
            wikirepo.write_page("index.md", pages.render_index(session))
    if not pruned:
        typer.echo("wiki-prune: nothing to prune ✓")
        return
    wikirepo.append_log(f"## [{utcnow().date().isoformat()}] prune | {len(pruned)} noise entities")
    wikirepo.commit(f"wiki-prune: drop {len(pruned)} noise entities")
    sample = ", ".join(repr(n) for n in pruned[:8])
    typer.echo(f"pruned {len(pruned)} entit(ies): {sample}" + (" …" if len(pruned) > 8 else ""))


@app.command()
def wiki_init() -> None:
    """Create the wiki/ git repo scaffold (dirs + index.md/log.md/overview.md). Idempotent."""
    from doctalk.synth import wikirepo

    root = wikirepo.ensure_scaffold()
    versioned = (root / ".git").exists()
    typer.echo(f"wiki scaffold ready at {root}" + ("" if versioned else "  (git unavailable — unversioned)"))


def _target_file_id(target: str | None) -> int | None:
    """Resolve an optional --file (path or content_hash prefix) to a file id, scoping a query to
    one document. None means search the whole corpus."""
    if not target:
        return None
    path = Path(target)
    with session_scope() as session:
        if path.is_file():
            return repo.get_file_id(session, hash_file(path))
        return session.scalar(select(File.id).where(File.content_hash.like(f"{target}%")))


@app.command()
def search(
    query: str,
    file: str = typer.Option(None, help="restrict to one document (path or content_hash)"),
    k: int = typer.Option(8, help="number of chunks to return"),
) -> None:
    """Show the top-k retrieved chunks for a query (no LLM) — useful to inspect retrieval."""
    from doctalk.query.retriever import retrieve

    hits = retrieve(query, k=k, file_id=_target_file_id(file))
    if not hits:
        typer.echo("(no hits — is anything ingested + embedded? try `doctalk rebuild-index`)")
        return
    for i, h in enumerate(hits, start=1):
        chapter = h.chapter or "n/a"
        score = h.rerank_score if h.rerank_score is not None else h.score
        tag = "rr" if h.rerank_score is not None else "cos"
        typer.echo(f"[{i}] {tag} {score:.3f}  p.{h.page} · {chapter}")
        typer.echo(f"      {h.text[:160].strip().replace(chr(10), ' ')}…")


@app.command()
def ask(
    question: str,
    file: str = typer.Option(None, help="restrict to one document (path or content_hash)"),
    k: int = typer.Option(8, help="number of chunks to retrieve as context"),
    raw: bool = typer.Option(False, "--raw", help="chunk-RAG only; skip the synthesized wiki"),
    save: bool = typer.Option(False, "--save", help="file the answer to wiki/queries/ (compounds)"),
) -> None:
    """Ask a question. Wiki-first: answered from the synthesized pages, chunk-RAG fills gaps.
    ``--raw`` uses chunk-RAG only; ``--save`` files the answer back to the wiki."""
    file_id = _target_file_id(file)
    if raw:
        from doctalk.query.chat import answer

        result = answer(question, k=k, file_id=file_id)
    else:
        from doctalk.config import get_settings
        from doctalk.query.wikichat import answer as wiki_answer

        # --save forces past the evaluator; otherwise good answers auto-file (chat_auto_promote).
        save_mode: bool | str = True if save else (
            "auto" if get_settings().chat_auto_promote else False
        )
        result = wiki_answer(question, k_chunks=k, file_id=file_id, save=save_mode)

    typer.echo(result["answer"])
    for cite in result.get("wiki_citations", []):
        typer.echo(f"  • wiki: {cite['name']} ({cite['type']})")
    if result["citations"]:
        typer.echo("\nSources:")
        for c in result["citations"]:
            typer.echo(f"  [{c['n']}] {c['file']} · {c['chapter'] or 'n/a'} · p.{c['page']}")
    if result.get("saved_path"):
        typer.echo(f"\nfiled to wiki/{result['saved_path']}")
    elif result.get("save_reason"):
        typer.echo(f"\n(not filed — {result['save_reason']})")


@app.command()
def rebuild_index() -> None:
    """Regenerate the LanceDB text index from MySQL (the truth store). LanceDB is derived."""
    from doctalk.models.embed import embed_passages
    from doctalk.vector import store
    from doctalk.vector.store import NO_CHAPTER

    store.drop_text_table()
    total = 0
    with session_scope() as session:
        for file_id in repo.get_all_file_ids(session):
            chunks = repo.get_chunks(session, file_id)
            if not chunks:
                continue
            vectors = embed_passages([c.text for c in chunks])
            store.add_text_chunks(
                [
                    {
                        "chunk_id": c.id,
                        "file_id": file_id,
                        "chapter_id": c.chapter_id if c.chapter_id is not None else NO_CHAPTER,
                        "page": c.page,
                        "vector": vec,
                    }
                    for c, vec in zip(chunks, vectors)
                ]
            )
            total += len(chunks)
    typer.echo(f"rebuilt text index: {total} chunks")


@app.command()
def recluster() -> None:
    """Recompute image near-duplicate clusters globally from the CLIP vectors (authoritative;
    order-independent — like ``rebuild-index`` for the dedup labels). cluster_id = the smallest
    file_id in each connected component."""
    from doctalk.cluster.grouping import cluster_components
    from doctalk.config import get_settings
    from doctalk.vector import store

    vectors = store.all_image_vectors()
    if not vectors:
        typer.echo("(no embedded images — ingest some, or run `doctalk rebuild-index`)")
        return
    labels = cluster_components(vectors, get_settings().cluster_sim_threshold)
    with session_scope() as session:
        for file_id, cluster_id in labels.items():
            repo.set_image_cluster(session, file_id, cluster_id)

    sizes: dict[int, int] = {}
    for cluster_id in labels.values():
        sizes[cluster_id] = sizes.get(cluster_id, 0) + 1
    dup_groups = sum(1 for n in sizes.values() if n > 1)
    extras = sum(n - 1 for n in sizes.values() if n > 1)
    typer.echo(
        f"reclustered {len(labels)} images into {len(sizes)} clusters "
        f"({dup_groups} with near-duplicates, {extras} redundant)"
    )


@app.command()
def find(
    query: str = typer.Argument("", help="semantic text for CLIP (empty = metadata-only listing)"),
    format: str = typer.Option(None, help="image format, e.g. png / jpg"),
    min_kb: float = typer.Option(None, help="minimum size in KB"),
    max_kb: float = typer.Option(None, help="maximum size in KB"),
    country: str = typer.Option(None, help="geo country code, e.g. CA"),
    year: int = typer.Option(None, help="filter by capture year (EXIF)"),
    month: int = typer.Option(None, help="filter by capture month (1-12); requires --year"),
    k: int = typer.Option(10, help="max results"),
) -> None:
    """Hybrid image search: metadata filter (format/size/geo/time) + optional CLIP semantic rank."""
    from doctalk.query.hybrid import ImageFilter, find_images, list_images, month_range

    ts_from = ts_to = None
    if year is not None:
        ts_from, ts_to = month_range(year, month)
    filt = ImageFilter(
        format=format,
        min_bytes=int(min_kb * 1024) if min_kb is not None else None,
        max_bytes=int(max_kb * 1024) if max_kb is not None else None,
        geo_country=country,
        ts_from=ts_from,
        ts_to=ts_to,
    )

    hits = find_images(query, filt, k=k) if query.strip() else list_images(filt, limit=k)
    if not hits:
        typer.echo("(no matching images)")
        return
    for h in hits:
        score = f"{h.score:.3f}  " if h.score is not None else ""
        when = h.exif_datetime.date().isoformat() if h.exif_datetime else "—"
        geo = h.geo_country or "—"
        typer.echo(
            f"{score}{h.filename}  [{h.format} · {h.byte_size // 1024}KB · {geo} · {when}]"
        )
        if h.description:
            typer.echo(f"      {h.description[:140].strip()}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="bind address"),
    port: int = typer.Option(8000, help="port"),
) -> None:
    """Run the Phase 1 web UI (FastAPI + Jinja) at http://host:port."""
    import uvicorn

    typer.echo(f"doctalk web UI → http://{host}:{port}")
    uvicorn.run("doctalk.api.app:app", host=host, port=port)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
