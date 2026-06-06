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
from doctalk.db.models import Base, Chapter, Chunk, Figure, File, Image, Job, JobStatus, Link
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
            (Figure, "figures"),
            (Image, "images"),
        ):
            n = session.scalar(select(func.count()).select_from(table))
            typer.echo(f"{label:<9} {n}")
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
        typer.echo(f"[{i}] {h.score:.3f}  p.{h.page} · {chapter}")
        typer.echo(f"      {h.text[:160].strip().replace(chr(10), ' ')}…")


@app.command()
def ask(
    question: str,
    file: str = typer.Option(None, help="restrict to one document (path or content_hash)"),
    k: int = typer.Option(8, help="number of chunks to retrieve as context"),
) -> None:
    """Ask a question; answer is grounded in retrieved chunks and cites (file, chapter, page)."""
    from doctalk.query.chat import answer

    result = answer(question, k=k, file_id=_target_file_id(file))
    typer.echo(result["answer"])
    if result["citations"]:
        typer.echo("\nSources:")
        for c in result["citations"]:
            typer.echo(f"  [{c['n']}] {c['file']} · {c['chapter'] or 'n/a'} · p.{c['page']}")


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
