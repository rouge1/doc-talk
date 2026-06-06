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
from doctalk.db.models import Base, File, Job, JobStatus
from doctalk.db.session import get_engine, session_scope
from doctalk.hashing import hash_file
from doctalk.ingest.dag import run_dag
from doctalk.ingest.pipeline import phase0_pipeline

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
    results = run_dag(content_hash, phase0_pipeline(), file_path=str(path))
    for r in results:
        typer.echo(f"  {r.stage:<12} {r.status}" + (f"  {r.error}" if r.error else ""))


@app.command()
def stats() -> None:
    """Print file count and job counts by status."""
    with session_scope() as session:
        n_files = session.scalar(select(func.count()).select_from(File))
        typer.echo(f"files: {n_files}")
        rows = session.execute(
            select(Job.status, func.count()).group_by(Job.status)
        ).all()
        if not rows:
            typer.echo("jobs:  (none)")
        for status, count in rows:
            label = status.value if isinstance(status, JobStatus) else status
            typer.echo(f"jobs:  {label:<8} {count}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
