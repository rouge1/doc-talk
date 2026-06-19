"""Smoke tests for the Phase 1 web UI: pages render and routing works against an empty DB.

These avoid query params, so no embedding/LLM models are loaded — the data-backed query paths
(search/chat/find) are covered by their own slices' verification.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from doctalk.api.app import app


@pytest.fixture
def client(db):
    return TestClient(app)


def _seed_wiki_entity(name="Cake", norm="cake", type_="product"):
    from doctalk.db import repo
    from doctalk.db.session import session_scope
    from doctalk.synth import wikirepo

    with session_scope() as s:
        repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                         format="pdf", mime="x", byte_size=1)
        s.flush()
        fid = repo.get_file_id(s, "a" * 64)
        e = repo.create_entity(s, name=name, type_=type_, norm_key=norm)
        e.source_count = 1
        e.wiki_path = f"entities/{norm}.md"
        claim = repo.insert_claim(s, entity_id=e.id, file_id=fid, text="Bake 30 min.")
        repo.insert_claim_sources(s, claim.id, [{"file_id": fid, "chunk_id": None}])
        repo.upsert_wiki_page(s, path=f"entities/{norm}.md", title=name, kind="entity", entity_id=e.id,
                              source_count=1)
    wikirepo.write_page(f"entities/{norm}.md", f"# {name}\n\n> **{type_}** · 1 source(s)\n\n## Claims\n\n- Bake 30 min.\n")


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "doctalk" in r.text and "Documents" in r.text


def test_static_pages_render(client):
    for path in ("/search", "/chat", "/gallery"):
        r = client.get(path)
        assert r.status_code == 200
        assert "doctalk" in r.text


def test_missing_doc_is_404(client):
    assert client.get("/doc/nope").status_code == 404


def test_gallery_empty_lists_nothing(client):
    r = client.get("/gallery")
    assert r.status_code == 200
    assert "No images match" in r.text


def test_gallery_blank_form_params_dont_422(client):
    """The gallery form submits empty fields as `fmt=&min_kb=`; blanks must be treated as
    "no filter", not a parse error (regression for the float_parsing 422)."""
    for qs in ("fmt=&min_kb=", "q=cat&fmt=&min_kb=", "q=cat&fmt=png&min_kb=100", "min_kb=notanumber"):
        r = client.get(f"/gallery?{qs}")
        assert r.status_code == 200, f"gallery?{qs} -> {r.status_code}: {r.text[:200]}"


def test_missing_figure_is_404(client):
    assert client.get("/figure/999999").status_code == 404


def test_missing_image_is_404(client):
    assert client.get("/image/999999").status_code == 404


def test_wiki_index_renders_empty(client):
    r = client.get("/wiki")
    assert r.status_code == 200 and "synthesis wiki" in r.text


def test_wiki_index_lists_entities_and_page_renders(client):
    _seed_wiki_entity()
    r = client.get("/wiki")
    assert r.status_code == 200 and "Cake" in r.text and "product" in r.text
    page = client.get("/wiki/page/cake")
    assert page.status_code == 200
    assert 'class="wiki-title">Cake</h1>' in page.text and "Bake 30 min." in page.text


def test_wiki_index_lists_sources_and_topics(client):
    # the catalog front door must surface the rungs above entities (document profiles + topics),
    # each clickable through to its /wiki/page render.
    from doctalk.db import repo
    from doctalk.db.session import session_scope

    with session_scope() as s:
        repo.upsert_wiki_page(s, path="sources/spec-v1.md", title="Spec v1.pdf",
                              kind="source", entity_id=None)
        repo.upsert_wiki_page(s, path="topics/spec-v1--security.md", title="Security",
                              kind="topic", entity_id=None)
    r = client.get("/wiki")
    assert r.status_code == 200
    assert "sources · 1" in r.text and 'href="/wiki/page/spec-v1"' in r.text
    assert "topics · 1" in r.text and 'href="/wiki/page/spec-v1--security"' in r.text


def test_wiki_page_renders_source_kind(client):
    # synth_source writes sources/<stem>.md; the page route must resolve that kind (regression:
    # the kinds map originally omitted "sources", so source profiles 404'd in the viewer).
    from doctalk.synth import wikirepo

    wikirepo.write_page("sources/spec-v1.md", "# Spec v1.pdf\n\n> **source** · pdf\n")
    page = client.get("/wiki/page/spec-v1")
    assert page.status_code == 200
    assert 'class="wiki-title">Spec v1.pdf</h1>' in page.text


def test_wiki_page_missing_is_404(client):
    assert client.get("/wiki/page/nonexistent").status_code == 404


def test_wiki_page_rejects_non_slug_targets(client):
    # uppercase/underscore are not slug chars -> 404 before any filesystem touch (traversal guard)
    assert client.get("/wiki/page/Bad_Name").status_code == 404


def test_wiki_review_renders(client):
    r = client.get("/wiki/review")
    assert r.status_code == 200 and "Resolution review" in r.text
