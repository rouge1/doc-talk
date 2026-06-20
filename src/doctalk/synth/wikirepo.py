"""The ``wiki/`` markdown git repo — the synthesis layer's second source of truth.

Unlike LanceDB, the wiki is *not* regenerable: it holds accumulated reasoning + human edits, so it
is durable via **git, one commit per ingested source** (``CLAUDE.md`` → Wiki conventions). This
module owns the on-disk layout: the scaffold (dirs + ``index.md``/``log.md``/``overview.md``), the
grep-parseable append-only ``log.md``, page writes (returning the body's blake3 for the
``wiki_pages.md_hash`` catalog), and commits. ``synth_integrate`` (next slice) drives page writes
and commits; this slice wires only the scaffold (``doctalk wiki-init``).

git is best-effort: if the binary is missing the markdown is still written, just unversioned.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from doctalk.config import get_settings
from doctalk.hashing import hash_bytes

SUBDIRS = ("entities", "concepts", "topics", "queries")

_INDEX_SEED = """# Wiki index

The content catalog for this knowledge base. Updated every ingest.

## Entities

_None yet._

## Concepts

_None yet._

## Topics

_None yet._
"""

_LOG_SEED = "# Synthesis log\n\nAppend-only, one line per event (grep-parseable).\n"

_OVERVIEW_SEED = "# Overview\n\nA running, high-level summary of the corpus. Rewritten as sources arrive.\n"


def repo_dir() -> Path:
    return get_settings().wiki_dir


def _git(args: list[str], cwd: Path) -> bool:
    """Run a git command in the repo; return True on success, False if git is unavailable/fails."""
    try:
        subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def ensure_scaffold() -> Path:
    """Create the wiki dir tree + seed files (idempotent) and ``git init`` it if needed."""
    root = repo_dir()
    root.mkdir(parents=True, exist_ok=True)
    for sub in SUBDIRS:
        (root / sub).mkdir(exist_ok=True)
    for name, seed in (
        ("index.md", _INDEX_SEED),
        ("log.md", _LOG_SEED),
        ("overview.md", _OVERVIEW_SEED),
    ):
        f = root / name
        if not f.exists():
            f.write_text(seed, encoding="utf-8")
    if not (root / ".git").exists():
        if _git(["init", "-q"], root):
            _git(["add", "-A"], root)
            _git(["commit", "-q", "-m", "wiki: scaffold"], root)
    return root


def append_log(line: str) -> None:
    """Append one line to ``log.md`` (caller supplies the ``## [date] op | title`` form)."""
    log = repo_dir() / "log.md"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


def write_page(relpath: str, content: str) -> str:
    """Write a page body under the wiki dir and return its blake3 (for ``wiki_pages.md_hash``)."""
    path = repo_dir() / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8")
    path.write_text(content, encoding="utf-8")
    return hash_bytes(data)


def remove_page(relpath: str) -> None:
    """Delete a page file from the wiki (best-effort). Used by undo paths that retire a standalone page
    (e.g. un-disambiguate folds a split entity back onto the shared slug, orphaning its own file)."""
    try:
        (repo_dir() / relpath).unlink()
    except FileNotFoundError:
        pass


def commit(message: str) -> bool:
    """Stage everything and commit (one commit per ingested source). No-op-safe; best-effort."""
    root = repo_dir()
    if not (root / ".git").exists():
        return False
    _git(["add", "-A"], root)
    return _git(["commit", "-q", "-m", message], root)


def head_sha() -> str | None:
    """Current wiki HEAD commit sha (to stamp ``entity_merges.committed_sha``). None if no git."""
    root = repo_dir()
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
        )
        return out.stdout.strip() or None
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
