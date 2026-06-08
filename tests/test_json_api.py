"""JSON API (/api) for the React frontend: shapes, the structured entity, and not-found guards."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from doctalk.api.app import app


@pytest.fixture
def client(db):
    return TestClient(app)


def _seed(name="Cake", norm="cake", type_="product"):
    from doctalk.db import repo
    from doctalk.db.session import session_scope

    with session_scope() as s:
        repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                         format="pdf", mime="x", byte_size=1)
        s.flush()
        fid = repo.get_file_id(s, "a" * 64)
        ch = repo.insert_chapters(s, fid, [{"level": 1, "ord": 0, "title": "Sec", "page_start": 1,
                                            "page_end": 1, "source": "outline", "parent_ord": None}])[0]
        repo.insert_chunks(s, fid, [{"chapter_id": ch.id, "page": 3, "ord": 0,
                                     "char_count": 4, "text": "bake"}])
        cid = repo.get_chunks(s, fid)[0].id
        e = repo.create_entity(s, name=name, type_=type_, norm_key=norm)
        e.source_count = 1
        e.wiki_path = f"entities/{norm}.md"
        claim = repo.insert_claim(s, entity_id=e.id, file_id=fid, text="Bake 30 min.")
        repo.insert_claim_sources(s, claim.id, [{"file_id": fid, "chunk_id": cid}])


def test_stats_and_library_shapes(client):
    _seed()
    stats = client.get("/api/stats").json()
    assert stats["documents"] == 1 and stats["entities"] == 1 and stats["claims"] == 1
    lib = client.get("/api/library").json()
    assert lib["documents"][0]["name"] == "a.pdf" and lib["images"] == 0


def test_wiki_index_groups_by_type(client):
    _seed()
    data = client.get("/api/wiki").json()
    assert data["totals"]["entities"] == 1
    assert data["groups"][0]["type"] == "product"
    assert data["groups"][0]["entities"][0]["stem"] == "cake"


def test_wiki_entity_is_structured_with_provenance(client):
    _seed()
    e = client.get("/api/wiki/entity/cake").json()
    assert e["name"] == "Cake" and e["type"] == "product"
    assert e["claims"][0]["text"] == "Bake 30 min."
    assert e["claims"][0]["sources"] == ["a.pdf p.3"]   # resolved provenance


def test_entity_not_found_and_bad_slug(client):
    assert client.get("/api/wiki/entity/nope").status_code == 404
    assert client.get("/api/wiki/entity/Bad_Slug").status_code == 404


def test_search_empty_and_results(client, monkeypatch):
    assert client.get("/api/search").json() == {"query": "", "hits": []}

    from types import SimpleNamespace
    import doctalk.query.retriever as retr
    hit = SimpleNamespace(chunk_id=42, file="a.pdf", chapter="Sec", page=3, text="bake the cake",
                          score=0.81, rerank_score=0.93, content_hash="abc", chapter_id=5)
    monkeypatch.setattr(retr, "retrieve", lambda q, k=8: [hit])
    data = client.get("/api/search?q=cake").json()
    assert data["query"] == "cake" and len(data["hits"]) == 1
    assert data["hits"][0]["rerank_score"] == 0.93 and data["hits"][0]["chunk_id"] == 42


def test_doc_outline_and_chapter_reader(client):
    _seed()
    from doctalk.db.session import session_scope
    from doctalk.db.models import Chapter
    from sqlalchemy import select
    with session_scope() as s:
        ch_id = s.scalar(select(Chapter.id))

    outline = client.get("/api/doc/" + "a" * 64).json()
    assert outline["name"] == "a.pdf" and outline["chapters"][0]["id"] == ch_id

    reader = client.get(f"/api/doc/{'a' * 64}/chapter/{ch_id}").json()
    assert reader["chapter"]["title"] == "Sec"
    assert reader["chunks"][0]["text"] == "bake" and "id" in reader["chunks"][0]
    assert reader["nav"] == {"prev": None, "next": None}

    assert client.get("/api/doc/deadbeef").status_code == 404
    assert client.get(f"/api/doc/{'a' * 64}/chapter/999999").status_code == 404


def test_pdf_page_render_and_highlight_rects(client, tmp_path):
    fitz = pytest.importorskip("fitz")
    pdf_path = tmp_path / "real.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "alpha beta highlighted phrase gamma")
    doc.save(str(pdf_path))
    doc.close()

    from doctalk.db import repo
    from doctalk.db.session import session_scope
    with session_scope() as s:
        repo.upsert_file(s, content_hash="p" * 64, path=str(pdf_path), filename="real.pdf",
                         format="pdf", mime="application/pdf", byte_size=pdf_path.stat().st_size)
        s.flush()
        fid = repo.get_file_id(s, "p" * 64)
        ch = repo.insert_chapters(s, fid, [{"level": 1, "ord": 0, "title": "S", "page_start": 1,
                                            "page_end": 1, "source": "outline", "parent_ord": None}])[0]
        repo.insert_chunks(s, fid, [{"chapter_id": ch.id, "page": 1, "ord": 0,
                                     "char_count": 18, "text": "highlighted phrase"}])
        cid = repo.get_chunks(s, fid)[0].id

    info = client.get(f"/api/doc/{'p' * 64}/page/1?chunk_id={cid}").json()
    assert info["page_count"] == 1 and info["width"] > 0
    assert len(info["rects"]) >= 1                       # found "highlighted phrase" on the page
    r = info["rects"][0]
    assert 0 <= r["x"] <= 1 and 0 <= r["y"] <= 1 and r["w"] > 0   # normalized

    png = client.get(f"/api/doc/{'p' * 64}/page/1.png")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"        # real PNG bytes

    assert client.get(f"/api/doc/{'p' * 64}/page/99").status_code == 404   # out of range


def test_page_view_rejects_missing_or_unknown(client):
    _seed()  # a.pdf is registered with a non-existent path
    assert client.get("/api/doc/deadbeef/page/1").status_code == 404
    assert client.get(f"/api/doc/{'a' * 64}/page/1").status_code == 404    # path not on disk


def test_gallery_lists_and_collapses_clusters(client):
    from doctalk.db import repo
    from doctalk.db.session import session_scope
    with session_scope() as s:
        for i, h in enumerate(["c" * 64, "d" * 64]):  # two near-dups in one cluster
            repo.upsert_file(s, content_hash=h, path=f"/{h}", filename=f"img{i}.png",
                             format="png", mime="image/png", byte_size=2048)
            s.flush()
            fid = repo.get_file_id(s, h)
            repo.upsert_image(s, fid, width=10, height=10, cluster_id=99)

    data = client.get("/api/gallery").json()
    assert len(data["items"]) == 1                 # cluster collapsed to one card
    item = data["items"][0]
    assert item["dups"] == 1 and item["kb"] == 2 and item["image"].startswith("/api/image/")


def test_image_endpoint_404_for_missing(client):
    assert client.get("/api/image/999999").status_code == 404


def test_office_doc_renders_and_locates_page(client, tmp_path):
    import shutil
    pytest.importorskip("docx")
    if not (shutil.which("soffice") or shutil.which("libreoffice")):
        pytest.skip("LibreOffice not available for docx->pdf rendering")

    import docx
    docx_path = tmp_path / "note.docx"
    d = docx.Document()
    d.add_paragraph("Preheat the oven and bake the cake thoroughly.")
    d.save(str(docx_path))

    from doctalk.config import get_settings
    get_settings().rendered_dir = tmp_path / "rendered"   # isolate the conversion cache
    from doctalk.db import repo
    from doctalk.db.session import session_scope
    with session_scope() as s:
        repo.upsert_file(s, content_hash="d" * 64, path=str(docx_path), filename="note.docx",
                         format="docx", mime="x", byte_size=docx_path.stat().st_size)
        s.flush()
        fid = repo.get_file_id(s, "d" * 64)
        ch = repo.insert_chapters(s, fid, [{"level": 1, "ord": 0, "title": "S", "page_start": 1,
                                            "page_end": 1, "source": "outline", "parent_ord": None}])[0]
        repo.insert_chunks(s, fid, [{"chapter_id": ch.id, "page": 7, "ord": 0,  # block index, not a page
                                     "char_count": 40, "text": "bake the cake thoroughly"}])
        cid = repo.get_chunks(s, fid)[0].id

    # find resolves the chunk to a real rendered page (not the stored block index 7)
    found = client.get(f"/api/doc/{'d' * 64}/find?chunk_id={cid}").json()
    assert found["page"] == 1
    info = client.get(f"/api/doc/{'d' * 64}/page/1?chunk_id={cid}").json()
    assert len(info["rects"]) >= 1                          # the docx words located on the rendered page
    png = client.get(f"/api/doc/{'d' * 64}/page/1.png")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"


def test_entity_aliases_drop_the_canonical_name(client):
    from doctalk.db.session import session_scope
    from doctalk.db import repo
    with session_scope() as s:
        repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                         format="pdf", mime="x", byte_size=1)
        s.flush()
        fid = repo.get_file_id(s, "a" * 64)
        e = repo.create_entity(s, name="Cake", type_="product", norm_key="cake",
                               aliases=["Cake", "the cake"])
        e.wiki_path = "entities/cake.md"
        c = repo.insert_claim(s, entity_id=e.id, file_id=fid, text="x")
        repo.insert_claim_sources(s, c.id, [{"file_id": fid, "chunk_id": None}])
    aliases = client.get("/api/wiki/entity/cake").json()["aliases"]
    assert aliases == ["the cake"]   # the canonical "Cake" is filtered out


def test_chat_empty_and_wiki_citations_carry_stem(client, monkeypatch):
    assert client.get("/api/chat").json()["answer"] == ""

    import doctalk.query.wikichat as wc
    monkeypatch.setattr(wc, "answer", lambda q, k_chunks=6: {
        "answer": "Bake 30 min.",
        "wiki_citations": [{"name": "Cake", "type": "product", "path": "entities/cake.md"}],
        "citations": [{"n": 1, "file": "a.pdf", "chapter": "Sec", "page": 3,
                       "content_hash": "abc", "chapter_id": 5}],
        "pages": [], "hits": [], "saved_path": None,
    })
    data = client.get("/api/chat?q=how long").json()
    assert data["answer"] == "Bake 30 min."
    assert data["wiki_citations"][0]["stem"] == "cake"   # deep-link target for the SPA
    assert data["citations"][0]["page"] == 3
