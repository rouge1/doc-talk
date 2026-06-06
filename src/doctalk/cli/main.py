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
from doctalk.db.models import Base, Chapter, Chunk, File, Job, JobStatus, Link
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


@app.command()
def ingest(path: Path) -> None:
    """Ingest one file: hash -> upsert -> run the DAG."""
    if not path.is_file():
        raise typer.BadParameter(f"not a file: {path}")

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
def stats() -> None:
    """Print counts: files, jobs by status, and extracted structure."""
    with session_scope() as session:
        for table, label in ((File, "files"), (Chapter, "chapters"), (Chunk, "chunks"), (Link, "links")):
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
