"""Maintenance API: the findings dashboards, the slug-collision plan, and the gated batch heal."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from doctalk.api.app import app
from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.synth import pages, wikirepo


@pytest.fixture
def client(db):
    return TestClient(app)


def _collision(s):
    """Two entities that slug-collide (underscore vs space): a mergeable pair, richer one wins."""
    repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                     format="pdf", mime="x", byte_size=1)
    s.flush()
    fid = repo.get_file_id(s, "a" * 64)
    rich = repo.create_entity(s, name="Flush Timeout", type_="component", norm_key="flush timeout")
    thin = repo.create_entity(s, name="Flush_Timeout", type_="component", norm_key="flush_timeout")
    for e, n in ((rich, 3), (thin, 1)):
        for i in range(n):
            c = repo.insert_claim(s, entity_id=e.id, file_id=fid, text=f"{e.name} {i}.")
            repo.insert_claim_sources(s, c.id, [{"file_id": fid, "chunk_id": None}])
    path = f"entities/{pages.slug_for(rich)}.md"
    wikirepo.write_page(path, pages.render_entity_page(s, rich))
    repo.upsert_wiki_page(s, path=path, title=rich.name, kind="entity", entity_id=rich.id)
    rich.wiki_path = thin.wiki_path = path
    return rich.id, thin.id


def test_lint_endpoint_groups_findings(client):
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        _collision(s)
    data = client.get("/api/maintenance/lint").json()
    kinds = {g["kind"]: g for g in data["groups"]}
    assert "slug_collision" in kinds and kinds["slug_collision"]["count"] == 1
    assert data["total"] >= 1


def test_slug_collisions_plan_endpoint(client):
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        _collision(s)
    plan = client.get("/api/maintenance/slug-collisions").json()
    assert len(plan["mergeable"]) == 1
    pair = plan["mergeable"][0]
    assert pair["src"]["name"] == "Flush_Timeout" and pair["dst"]["name"] == "Flush Timeout"


def test_merge_collisions_action_heals(client):
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        _rich, thin = _collision(s)
    res = client.post("/api/maintenance/merge-collisions").json()
    assert res["merged"] == 1
    with session_scope() as s:
        from doctalk.db.models import Entity
        assert s.get(Entity, thin).status == "merged_into"
    # idempotent: nothing left to merge on a second run
    assert client.post("/api/maintenance/merge-collisions").json()["merged"] == 0


def test_admin_gate_blocks_mutations_when_token_set(client, monkeypatch):
    monkeypatch.setenv("DOCTALK_ADMIN_TOKEN", "s3cret")
    get_settings.cache_clear()
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        _collision(s)
    assert client.post("/api/maintenance/merge-collisions").status_code == 401   # no header
    assert client.get("/api/maintenance/slug-collisions").status_code == 200      # reads stay open
    ok = client.post("/api/maintenance/merge-collisions", headers={"X-Admin-Token": "s3cret"})
    assert ok.status_code == 200 and ok.json()["merged"] == 1
    get_settings.cache_clear()
