"""JSON API for the Phase 3 React frontend.

A parallel ``/api`` surface over the same truth store + query functions the Jinja routes use — the
server-rendered pages stay intact, the SPA consumes JSON. Read-only in this slice (library + the
synthesis wiki); auth, search, chat, gallery, and the job dashboard land in later Phase-3 slices.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select

from doctalk.api.wikimd import render
from doctalk.db import repo
from doctalk.db.models import Chapter, Chunk, Claim, Entity, File, WikiPage
from doctalk.db.session import session_scope
from doctalk.ingest.pipeline import IMAGE_FORMATS

router = APIRouter(prefix="/api")
_SLUG = re.compile(r"^[a-z0-9-]+$")


def _count(s, model, *where) -> int:
    q = select(func.count()).select_from(model)
    for w in where:
        q = q.where(w)
    return s.scalar(q) or 0


@router.get("/stats")
def stats() -> dict:
    with session_scope() as s:
        docs = _count(s, File) - _count(s, File, File.format.in_(IMAGE_FORMATS))
        return {
            "documents": docs,
            "images": _count(s, File, File.format.in_(IMAGE_FORMATS)),
            "entities": _count(s, Entity, Entity.status == "active"),
            "claims": _count(s, Claim),
            "queries": _count(s, WikiPage, WikiPage.kind == "query"),
            "reviews": len(repo.get_open_reviews(s)),
        }


@router.get("/library")
def library() -> dict:
    docs, images = [], 0
    with session_scope() as s:
        for f in s.scalars(select(File).order_by(File.id)):
            if f.format in IMAGE_FORMATS:
                images += 1
                continue
            docs.append({
                "hash": f.content_hash,
                "name": f.filename,
                "format": f.format,
                "chapters": _count(s, Chapter, Chapter.file_id == f.id),
                "chunks": _count(s, Chunk, Chunk.file_id == f.id),
            })
    return {"documents": docs, "images": images}


@router.get("/wiki")
def wiki() -> dict:
    groups: dict[str, list[dict]] = {}
    with session_scope() as s:
        for e in repo.get_entities(s):
            if e.status != "active":
                continue
            groups.setdefault(e.type, []).append({
                "name": e.name,
                "stem": Path(e.wiki_path).stem if e.wiki_path else None,
                "claims": len(repo.get_claims_for_entity(s, e.id)),
                "sources": e.source_count,
            })
        queries = [
            {"title": p.title, "stem": Path(p.path).stem}
            for p in repo.get_wiki_pages_by_kind(s, "query")
        ]
        reviews = len(repo.get_open_reviews(s))
    for items in groups.values():
        items.sort(key=lambda it: it["name"].lower())
    ordered = [{"type": t, "entities": items} for t, items in sorted(groups.items())]
    totals = {
        "entities": sum(len(g["entities"]) for g in ordered),
        "claims": sum(it["claims"] for g in ordered for it in g["entities"]),
        "queries": len(queries),
    }
    return {"groups": ordered, "queries": queries, "reviews": reviews, "totals": totals}


def _claim_sources(s, claim_id: int) -> list[str]:
    out: set[str] = set()
    for cs in repo.get_claim_sources(s, claim_id):
        file = s.get(File, cs.file_id)
        name = file.filename if file else f"file:{cs.file_id}"
        if cs.chunk_id is not None:
            chunk = s.get(Chunk, cs.chunk_id)
            out.add(f"{name} p.{chunk.page}" if chunk else name)
        else:
            out.add(name)
    return sorted(out)


@router.get("/wiki/entity/{stem}")
def wiki_entity(stem: str) -> dict:
    if not _SLUG.match(stem):
        raise HTTPException(status_code=404, detail="not found")
    with session_scope() as s:
        entity = s.scalar(
            select(Entity).where(
                Entity.wiki_path == f"entities/{stem}.md", Entity.status == "active"
            )
        )
        if entity is None:
            raise HTTPException(status_code=404, detail="entity not found")
        claims = repo.get_claims_for_entity(s, entity.id)
        related = []
        for rid in repo.get_comention_entity_ids(s, entity.id):
            r = s.get(Entity, rid)
            if r is not None and r.status == "active" and r.wiki_path:
                related.append({"name": r.name, "stem": Path(r.wiki_path).stem})
        return {
            "name": entity.name,
            "type": entity.type,
            "aliases": entity.aliases or [],
            "sources": entity.source_count,
            "claims": [
                {"text": c.text, "status": c.status, "sources": _claim_sources(s, c.id)}
                for c in claims
            ],
            "related": related,
        }


@router.get("/wiki/query/{stem}")
def wiki_query(stem: str) -> dict:
    if not _SLUG.match(stem):
        raise HTTPException(status_code=404, detail="not found")
    from doctalk.config import get_settings

    path = get_settings().wiki_dir / "queries" / f"{stem}.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="query not found")
    with session_scope() as s:
        page = repo.get_wiki_page_by_path(s, f"queries/{stem}.md")
        title = page.title if page else stem
    return {"title": title, "html": str(render(path.read_text(encoding="utf-8")))}
