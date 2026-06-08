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
