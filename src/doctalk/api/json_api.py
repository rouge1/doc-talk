"""JSON API for the Phase 3 React frontend.

A parallel ``/api`` surface over the same truth store + query functions the Jinja routes use — the
server-rendered pages stay intact, the SPA consumes JSON. Read-only in this slice (library + the
synthesis wiki); auth, search, chat, gallery, and the job dashboard land in later Phase-3 slices.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
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


@router.get("/search")
def search(q: str = "", k: int = 8) -> dict:
    """Hybrid retrieval (ANN + cross-encoder rerank) over the chunk index. Loads models lazily."""
    q = q.strip()
    if not q:
        return {"query": "", "hits": []}
    from doctalk.query.retriever import retrieve

    hits = retrieve(q, k=k)
    return {
        "query": q,
        "hits": [
            {
                "chunk_id": h.chunk_id,
                "file": h.file,
                "chapter": h.chapter,
                "page": h.page,
                "text": h.text,
                "score": h.score,
                "rerank_score": h.rerank_score,
                "content_hash": h.content_hash,
                "chapter_id": h.chapter_id,
            }
            for h in hits
        ],
    }


def _stem(path: str | None) -> str | None:
    return path.rsplit("/", 1)[-1][:-3] if path else None


@router.get("/chat")
def chat(q: str = "") -> dict:
    """Wiki-first chat: answered from the synthesized pages, chunk-RAG fills gaps. Blocks on the
    local LLM (~a minute) — the SPA shows a pending state. Wiki citations carry an entity stem so
    the frontend can deep-link to the folio."""
    q = q.strip()
    if not q:
        return {"query": "", "answer": "", "wiki_citations": [], "citations": []}
    from doctalk.query.wikichat import answer

    res = answer(q, k_chunks=6)
    return {
        "query": q,
        "answer": res["answer"],
        "wiki_citations": [
            {"name": w["name"], "type": w["type"], "stem": _stem(w.get("path"))}
            for w in res["wiki_citations"]
        ],
        "citations": res["citations"],
    }


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
        # Aliases accumulate the canonical surface too; don't echo the name back as an "also".
        aliases = [a for a in (entity.aliases or []) if a.strip().lower() != entity.name.strip().lower()]
        return {
            "name": entity.name,
            "type": entity.type,
            "aliases": aliases,
            "sources": entity.source_count,
            "claims": [
                {"text": c.text, "status": c.status, "sources": _claim_sources(s, c.id)}
                for c in claims
            ],
            "related": related,
        }


@router.get("/doc/{content_hash}")
def doc(content_hash: str) -> dict:
    """A document's outline (chapter tree) for the in-app reader."""
    with session_scope() as s:
        f = repo.get_file(s, content_hash)
        if f is None:
            raise HTTPException(status_code=404, detail="document not found")
        return {
            "hash": content_hash,
            "name": f.filename,
            "format": f.format,
            "chapters": [
                {"id": c.id, "title": c.title, "level": c.level, "page": c.page_start}
                for c in repo.get_chapters(s, f.id)
            ],
        }


@router.get("/doc/{content_hash}/chapter/{chapter_id}")
def doc_chapter(content_hash: str, chapter_id: int) -> dict:
    """One chapter's text (its chunks) for the reader, plus the chapter's place in the outline so
    the reader can offer prev/next. Each chunk carries its id so a search hit can be highlighted."""
    with session_scope() as s:
        f = repo.get_file(s, content_hash)
        if f is None:
            raise HTTPException(status_code=404, detail="document not found")
        chapter = s.get(Chapter, chapter_id)
        if chapter is None or chapter.file_id != f.id:
            raise HTTPException(status_code=404, detail="chapter not found")
        chunks = s.scalars(
            select(Chunk).where(Chunk.chapter_id == chapter_id).order_by(Chunk.ord)
        )
        outline = repo.get_chapters(s, f.id)
        ids = [c.id for c in outline]
        pos = ids.index(chapter_id) if chapter_id in ids else -1
        nav = {
            "prev": ids[pos - 1] if pos > 0 else None,
            "next": ids[pos + 1] if 0 <= pos < len(ids) - 1 else None,
        }
        return {
            "hash": content_hash,
            "doc_name": f.filename,
            "chapter": {"id": chapter.id, "title": chapter.title, "page": chapter.page_start},
            "chunks": [{"id": c.id, "page": c.page, "text": c.text} for c in chunks],
            "nav": nav,
        }


def _pdf_path(content_hash: str) -> str:
    """Resolve a PDF's on-disk path, or raise 404/415. Reading the original is the point — we
    rasterize the real page, not a re-render of the extracted text."""
    with session_scope() as s:
        f = repo.get_file(s, content_hash)
    if f is None:
        raise HTTPException(status_code=404, detail="document not found")
    if f.format != "pdf":
        raise HTTPException(status_code=415, detail="page view is available for PDFs only")
    if not os.path.isfile(f.path):
        raise HTTPException(status_code=404, detail="original file is no longer on disk")
    return f.path


@router.get("/doc/{content_hash}/page/{page}.png")
def doc_page_png(content_hash: str, page: int, zoom: float = 2.0) -> Response:
    """Rasterize one page of the original PDF to PNG (the real document, not reflowed text)."""
    import fitz

    path = _pdf_path(content_hash)
    zoom = max(1.0, min(zoom, 4.0))
    with fitz.open(path) as doc:
        if not (1 <= page <= doc.page_count):
            raise HTTPException(status_code=404, detail="page out of range")
        pix = doc[page - 1].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        png = pix.tobytes("png")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


# Running headers/footers live in these top/bottom fractions of the page; a chunk's extracted text
# sometimes includes them, so we drop matches landing in these bands to avoid highlighting chrome.
_MARGIN_BAND = 0.09


def _highlight_rects(pg, text: str, w: float, h: float) -> list[dict]:
    """Locate a chunk's words on a page, line by line, so the highlight follows the section even
    onto the next page when it spills across a page break (a full-text search only matches the page
    that holds the whole passage). Short lines and running-header/footer bands are skipped; dedup."""
    rects: list[dict] = []
    seen: set[tuple] = set()
    for line in text.split("\n"):
        line = line.strip()
        if len(line) < 8:
            continue
        for r in pg.search_for(line):
            cy = (r.y0 + r.y1) / 2 / h
            if cy < _MARGIN_BAND or cy > 1 - _MARGIN_BAND:  # running header / footer
                continue
            key = (round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1))
            if key in seen:
                continue
            seen.add(key)
            rects.append({"x": r.x0 / w, "y": r.y0 / h, "w": (r.x1 - r.x0) / w, "h": (r.y1 - r.y0) / h})
    return rects


@router.get("/doc/{content_hash}/page/{page}")
def doc_page(content_hash: str, page: int, chunk_id: int | None = None) -> dict:
    """Page metadata for the viewer: dimensions, page count, and (for a search/citation chunk) the
    highlight rectangles of the matched words — normalized to 0..1 so the client scales them to the
    rendered image. The match is per-line, so carrying the chunk across page nav keeps a spanning
    section highlighted."""
    import fitz

    path = _pdf_path(content_hash)
    chunk_text = None
    if chunk_id is not None:
        with session_scope() as s:
            chunk = s.get(Chunk, chunk_id)
            chunk_text = chunk.text if chunk is not None else None
    with fitz.open(path) as doc:
        if not (1 <= page <= doc.page_count):
            raise HTTPException(status_code=404, detail="page out of range")
        pg = doc[page - 1]
        w, h = pg.rect.width, pg.rect.height
        rects = _highlight_rects(pg, chunk_text, w, h) if chunk_text else []
        page_count = doc.page_count
    with session_scope() as s:
        f = repo.get_file(s, content_hash)
        name = f.filename if f else content_hash
    return {
        "hash": content_hash, "doc_name": name, "page": page, "page_count": page_count,
        "width": w, "height": h, "image": f"/api/doc/{content_hash}/page/{page}.png",
        "rects": rects,
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
