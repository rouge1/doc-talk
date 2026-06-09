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
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select

from doctalk.api.wikimd import render
from doctalk.db import repo
from doctalk.db.models import Chapter, Chunk, Claim, Entity, File, Job, JobStatus, WikiPage
from doctalk.db.session import session_scope
from doctalk.ingest.pipeline import IMAGE_FORMATS, pipeline_for

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


def _status_value(status) -> str:
    return status.value if isinstance(status, JobStatus) else str(status)


@router.get("/jobs")
def jobs() -> dict:
    """Ingest dashboard: per-file pipeline progress against the resumable-DAG ledger, plus overall
    status counts and the current error list. A stage with no ledger row is 'pending' (not yet run
    — e.g. a source ingested before that stage existed)."""
    totals = {"done": 0, "running": 0, "pending": 0, "error": 0}
    files, errors = [], []
    with session_scope() as s:
        for status, n in s.execute(select(Job.status, func.count()).group_by(Job.status)):
            totals[_status_value(status)] = totals.get(_status_value(status), 0) + n

        for f in s.scalars(select(File).order_by(File.id)):
            try:
                stage_names = [st.name for st in pipeline_for(f.format)]
            except Exception:  # noqa: BLE001 - unknown format: just show the ledger we have
                stage_names = []
            ledger = {}
            for j in s.scalars(select(Job).where(Job.content_hash == f.content_hash).order_by(Job.id)):
                ledger[j.stage] = j  # last row per stage wins
            rows, done = [], 0
            for name in stage_names:
                j = ledger.get(name)
                st = _status_value(j.status) if j else "pending"
                if st == "done":
                    done += 1
                elif st == "error":
                    errors.append({"hash": f.content_hash, "name": f.filename, "stage": name,
                                   "error": (j.error or "")[:300]})
                rows.append({"name": name, "status": st})
            state = (
                "error" if any(r["status"] == "error" for r in rows)
                else "running" if any(r["status"] == "running" for r in rows)
                else "done" if (stage_names and done == len(stage_names))
                else "pending"
            )
            files.append({
                "hash": f.content_hash, "name": f.filename, "format": f.format,
                "stages": rows, "done": done, "total": len(stage_names), "state": state,
            })
    return {"totals": totals, "files": files, "errors": errors}


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
def search(q: str = "", k: int = 8, mode: str = "hybrid") -> dict:
    """Search the chunk index. ``mode='simple'`` is lexical keyword search; ``mode='hybrid'`` fuses
    the lexical and dense (ANN) arms with RRF then cross-encoder reranks. Loads models lazily."""
    q = q.strip()
    if not q:
        return {"query": "", "hits": [], "mode": mode}
    from doctalk.query.retriever import hybrid_search, keyword_search

    hits = keyword_search(q, k=k) if mode == "simple" else hybrid_search(q, k=k)
    return {
        "query": q,
        "mode": mode,
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
                "source": h.source,
            }
            for h in hits
        ],
    }


@router.get("/gallery")
def gallery(q: str = "", fmt: str = "", min_kb: float | None = None) -> dict:
    """Hybrid image search (CLIP text->image within a metadata prefilter), with near-duplicates
    collapsed to one card per cluster (the rest counted as '+N similar')."""
    from doctalk.query.hybrid import ImageFilter, find_images, list_images

    filt = ImageFilter(
        format=fmt or None, min_bytes=int(min_kb * 1024) if min_kb else None
    )
    hits = find_images(q, filt, k=24) if q.strip() else list_images(filt, limit=48)
    items: list[dict] = []
    by_cluster: dict[object, dict] = {}
    for h in hits:
        key = h.cluster_id if h.cluster_id is not None else f"f{h.file_id}"
        if key in by_cluster:
            by_cluster[key]["dups"] += 1
            continue
        item = {
            "file_id": h.file_id,
            "name": h.filename,
            "desc": h.description,
            "fmt": h.format,
            "kb": h.byte_size // 1024,
            "score": h.score,
            "when": h.exif_datetime.date().isoformat() if h.exif_datetime else None,
            "geo": h.geo_country,
            "dups": 0,
            "image": f"/api/image/{h.file_id}",
        }
        by_cluster[key] = item
        items.append(item)
    return {"query": q, "items": items}


@router.get("/image/{file_id}")
def image(file_id: int) -> FileResponse:
    """Serve an image's original bytes (for the gallery + any image reference)."""
    with session_scope() as s:
        f = s.get(File, file_id)
        if f is None or not os.path.isfile(f.path):
            raise HTTPException(status_code=404, detail="image not found")
        path, mime = f.path, f.mime
    return FileResponse(path, media_type=mime)


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
        "answer": res.get("formatted") or res["answer"],  # the typeset dispatch (presenter pass)
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
        # First chunk per chapter (one pass), so the outline can open the ORIGINAL page at the
        # section start and highlight it — same as a search hit.
        first_chunk: dict[int, int] = {}
        for cid, chid in s.execute(
            select(Chunk.id, Chunk.chapter_id).where(Chunk.file_id == f.id).order_by(Chunk.ord)
        ):
            if chid is not None and chid not in first_chunk:
                first_chunk[chid] = cid
        return {
            "hash": content_hash,
            "name": f.filename,
            "format": f.format,
            "chapters": [
                {"id": c.id, "title": c.title, "level": c.level, "page": c.page_start,
                 "first_chunk": first_chunk.get(c.id)}
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


# Formats LibreOffice can render to PDF — so the page viewer shows the *real* document (layout,
# tables, figures), not reflowed text, for office docs as well as native PDFs.
_RENDERABLE = {"pdf", "docx", "doc", "odt", "rtf", "pptx", "ppt", "xlsx"}


def _convert_to_pdf(src_path: str, content_hash: str) -> str | None:
    """Render an office document to PDF via headless LibreOffice, cached by content_hash. Returns
    the cached PDF path, or None if conversion isn't possible (binary missing / failure)."""
    import shutil
    import subprocess
    import tempfile

    from doctalk.config import get_settings

    out_dir = get_settings().rendered_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{content_hash}.pdf"
    if target.is_file():
        return str(target)
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run(
                [soffice, "--headless", f"-env:UserInstallation=file://{tmp}/profile",
                 "--convert-to", "pdf", "--outdir", tmp, src_path],
                check=True, capture_output=True, timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return None
        produced = Path(tmp) / (Path(src_path).stem + ".pdf")
        if not produced.is_file():
            return None
        shutil.move(str(produced), str(target))
    return str(target)


def _render_pdf_path(content_hash: str) -> tuple[str, bool]:
    """Path to a renderable PDF for a document, and whether it's a *native* PDF (vs converted).
    Native PDFs keep their real page numbers; converted office docs need chunk→page location."""
    with session_scope() as s:
        f = repo.get_file(s, content_hash)
    if f is None:
        raise HTTPException(status_code=404, detail="document not found")
    if f.format not in _RENDERABLE:
        raise HTTPException(status_code=415, detail="page view is unavailable for this format")
    if not os.path.isfile(f.path):
        raise HTTPException(status_code=404, detail="original file is no longer on disk")
    if f.format == "pdf":
        return f.path, True
    pdf = _convert_to_pdf(f.path, content_hash)
    if pdf is None:
        raise HTTPException(status_code=503, detail="could not render this document for viewing")
    return pdf, False


def _locate_page(doc, text: str) -> int:
    """The 1-based page of a rendered PDF that best contains a chunk's text — needed for office
    docs, whose stored 'page' is a block index, not a rendered page."""
    lines = [ln.strip() for ln in text.split("\n") if len(ln.strip()) >= 8]
    if not lines:
        return 1
    best_page, best_hits = 1, -1
    for i in range(doc.page_count):
        hits = sum(1 for ln in lines if doc[i].search_for(ln))
        if hits > best_hits:
            best_hits, best_page = hits, i + 1
    return best_page


@router.get("/doc/{content_hash}/find")
def doc_find(content_hash: str, chunk_id: int) -> dict:
    """Resolve a chunk to the page that holds it in the rendered document (native page for PDFs,
    located page for office docs). The SPA calls this before opening the page viewer for non-PDFs."""
    with session_scope() as s:
        f = repo.get_file(s, content_hash)
        chunk = s.get(Chunk, chunk_id)
        if f is None or chunk is None:
            raise HTTPException(status_code=404, detail="not found")
        if f.format == "pdf":
            return {"page": chunk.page}
        chunk_text = chunk.text
    import fitz

    path, _ = _render_pdf_path(content_hash)
    with fitz.open(path) as doc:
        return {"page": _locate_page(doc, chunk_text)}


@router.get("/doc/{content_hash}/page/{page}.png")
def doc_page_png(content_hash: str, page: int, zoom: float = 2.0) -> Response:
    """Rasterize one page of the original PDF to PNG (the real document, not reflowed text)."""
    import fitz

    path, _ = _render_pdf_path(content_hash)
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


def _rects_for(pg, needles: list[str], w: float, h: float) -> list[dict]:
    """Search the page for each needle and return normalized rects, skipping header/footer bands and
    de-duplicating. Shared by chunk-line highlighting and query-term highlighting."""
    rects: list[dict] = []
    seen: set[tuple] = set()
    for needle in needles:
        for r in pg.search_for(needle):
            cy = (r.y0 + r.y1) / 2 / h
            if cy < _MARGIN_BAND or cy > 1 - _MARGIN_BAND:  # running header / footer
                continue
            key = (round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1))
            if key in seen:
                continue
            seen.add(key)
            rects.append({"x": r.x0 / w, "y": r.y0 / h, "w": (r.x1 - r.x0) / w, "h": (r.y1 - r.y0) / h})
    return rects


def _anchor_rects(pg, text: str, w: float, h: float) -> list[dict]:
    """Anchor a cited chunk: highlight just its opening line(s), not every line. A chunk is a big
    retrieval unit (~40 lines ≈ a whole page), so highlighting all of it floods the page and gives no
    focal point. Highlighting the start marks *where* the cited passage begins; the viewer scrolls to
    it and the reader continues from there. Up to the first two substantial lines are anchored."""
    anchor: list[str] = []
    for ln in text.split("\n"):
        ln = ln.strip()
        if len(ln) >= 8:
            anchor.append(ln)
        if len(anchor) == 2:
            break
    return _rects_for(pg, anchor, w, h)


def _query_rects(pg, query: str, w: float, h: float) -> list[dict]:
    """Highlight what a *search* matched: the query's phrase(s) and content word(s), not the whole
    chunk — so clicking a result lights up the words you searched, where they appear on the page."""
    from doctalk.query.retriever import _parse_query

    phrases, words = _parse_query(query)
    needles = phrases + [w_ for w_ in words if len(w_) >= 2]  # drop 1-char tokens (e.g. "6.0" -> 6/0)
    return _rects_for(pg, needles, w, h)


@router.get("/doc/{content_hash}/page/{page}")
def doc_page(content_hash: str, page: int, chunk_id: int | None = None, q: str = "") -> dict:
    """Page metadata for the viewer: dimensions, page count, and highlight rectangles (normalized to
    0..1 for the client). ``q`` highlights a *search query*'s terms (clicking a search hit lights up
    what you searched); ``chunk_id`` highlights a whole cited chunk (Ask citations show the source
    passage). ``q`` takes precedence. Both are carried across page nav so the highlight persists."""
    import fitz

    path, _ = _render_pdf_path(content_hash)
    chunk_text = None
    if not q and chunk_id is not None:
        with session_scope() as s:
            chunk = s.get(Chunk, chunk_id)
            chunk_text = chunk.text if chunk is not None else None
    with fitz.open(path) as doc:
        if not (1 <= page <= doc.page_count):
            raise HTTPException(status_code=404, detail="page out of range")
        pg = doc[page - 1]
        w, h = pg.rect.width, pg.rect.height
        if q:
            rects = _query_rects(pg, q, w, h)
        elif chunk_text:
            rects = _anchor_rects(pg, chunk_text, w, h)
        else:
            rects = []
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
