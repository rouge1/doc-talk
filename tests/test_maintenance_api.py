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


def test_apply_then_undo_round_trips(client, stub_resolve):
    """The narrative's happy path over HTTP: apply heals the collision and hands back a sha; undo by
    that sha resurrects the folded entity and clears the batch — so the maintenance page's receipt +
    [Undo this batch] button actually reverse the change."""
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        _rich, thin = _collision(s)
    res = client.post("/api/maintenance/merge-collisions").json()
    assert res["merged"] == 1 and res["sha"]
    # recent-merges rehydrates the receipt after a reload
    recent = client.get("/api/maintenance/recent-merges").json()
    assert recent["sha"] == res["sha"] and recent["count"] == 1

    undo = client.post("/api/maintenance/undo-merge", json={"sha": res["sha"]}).json()
    assert undo["count"] == 1
    with session_scope() as s:
        from doctalk.db.models import Entity
        assert s.get(Entity, thin).status == "active"            # folded entity is back
    # the batch is gone — nothing reversible remains
    assert client.get("/api/maintenance/recent-merges").json()["sha"] is None


def _distinct_pair(s):
    """Two genuinely distinct-named near-dups (different norm_keys), richer one first — the kind a human
    reads in Compare, judges 'same', and folds."""
    repo.upsert_file(s, content_hash="b" * 64, path="/b", filename="b.pdf",
                     format="pdf", mime="x", byte_size=1)
    s.flush()
    fid = repo.get_file_id(s, "b" * 64)
    rich = repo.create_entity(s, name="Out of Band pairing", type_="concept", norm_key="out of band pairing")
    thin = repo.create_entity(s, name="Out Of Band", type_="concept", norm_key="out of band")
    for e, n in ((rich, 3), (thin, 1)):
        for i in range(n):
            c = repo.insert_claim(s, entity_id=e.id, file_id=fid, text=f"{e.name} {i}.")
            repo.insert_claim_sources(s, c.id, [{"file_id": fid, "chunk_id": None}])
    for e in (rich, thin):
        path = f"entities/{pages.slug_for(e)}.md"
        wikirepo.write_page(path, pages.render_entity_page(s, e))
        repo.upsert_wiki_page(s, path=path, title=e.name, kind="entity", entity_id=e.id)
        e.wiki_path = path
    return rich.id, thin.id


def test_fold_duplicate_then_undo_round_trips(client, stub_resolve):
    """Compare's 'Same → fold together': the richer entity survives whichever order the pair is sent,
    the thinner is merged into it, and /undo-merge by the returned sha resurrects it."""
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        rich, thin = _distinct_pair(s)
    from doctalk.db.models import Entity

    res = client.post("/api/maintenance/duplicates/fold", json={"a": thin, "b": rich}).json()
    assert res["into"] == "Out of Band pairing" and res["folded"] == "Out Of Band" and res["sha"]
    with session_scope() as s:
        assert s.get(Entity, thin).status == "merged_into"
        assert s.get(Entity, rich).status == "active"
    # the pair can't be folded twice — the thinner is already gone
    assert client.post("/api/maintenance/duplicates/fold", json={"a": thin, "b": rich}).status_code == 409

    undo = client.post("/api/maintenance/undo-merge", json={"sha": res["sha"]}).json()
    assert undo["count"] == 1
    with session_scope() as s:
        assert s.get(Entity, thin).status == "active"  # folded entity is back


def test_fold_is_admin_gated(client, monkeypatch):
    monkeypatch.setenv("DOCTALK_ADMIN_TOKEN", "s3cret")
    get_settings.cache_clear()
    blocked = client.post("/api/maintenance/duplicates/fold", json={"a": 1, "b": 2})
    assert blocked.status_code == 401
    get_settings.cache_clear()


def test_undo_is_admin_gated(client, monkeypatch):
    monkeypatch.setenv("DOCTALK_ADMIN_TOKEN", "s3cret")
    get_settings.cache_clear()
    blocked = client.post("/api/maintenance/undo-merge", json={"sha": "deadbeef"})
    assert blocked.status_code == 401
    get_settings.cache_clear()


def test_keep_unresolved_then_reopen_round_trips(client):
    """Resolving an unresolved entity: Keep promotes it to active (it leaves the lint queue and the
    unresolved finding carries its id to act on), and reopen sends it back."""
    from doctalk.db.models import Entity

    wikirepo.ensure_scaffold()
    with session_scope() as s:
        eid = repo.create_entity(s, name="Channel Sounding", type_="concept",
                                 norm_key="channel sounding", status="unresolved").id

    # the lint endpoint surfaces it with its entity_id, so the dashboard can Keep it in place
    groups = {g["kind"]: g for g in client.get("/api/maintenance/lint").json()["groups"]}
    item = next(i for i in groups["unresolved"]["items"] if i["entity_id"] == eid)
    assert item["entity_id"] == eid

    res = client.post("/api/maintenance/unresolved/keep", json={"entity_id": eid}).json()
    assert res["name"] == "Channel Sounding"
    with session_scope() as s:
        assert s.get(Entity, eid).status == "active"
    # can't keep what's already kept
    assert client.post("/api/maintenance/unresolved/keep", json={"entity_id": eid}).status_code == 409

    client.post("/api/maintenance/unresolved/reopen", json={"entity_id": eid})
    with session_scope() as s:
        assert s.get(Entity, eid).status == "unresolved"


def _unresolved_with_candidate(s):
    """An active candidate (with claims + a page) and an unresolved entity whose norm_key overlaps it —
    the parked twin the resolver couldn't place. ``best_candidate`` should pair them."""
    repo.upsert_file(s, content_hash="c" * 64, path="/c", filename="c.pdf",
                     format="pdf", mime="x", byte_size=1)
    s.flush()
    fid = repo.get_file_id(s, "c" * 64)
    cand = repo.create_entity(s, name="Channel Sounding", type_="concept", norm_key="channel sounding")
    for i in range(3):
        c = repo.insert_claim(s, entity_id=cand.id, file_id=fid, text=f"Channel Sounding {i}.")
        repo.insert_claim_sources(s, c.id, [{"file_id": fid, "chunk_id": None}])
    prov = repo.create_entity(s, name="Channel Sounding (CS)", type_="concept",
                              norm_key="channel sounding cs", status="unresolved")
    path = f"entities/{pages.slug_for(cand)}.md"
    wikirepo.write_page(path, pages.render_entity_page(s, cand))
    repo.upsert_wiki_page(s, path=path, title=cand.name, kind="entity", entity_id=cand.id)
    cand.wiki_path = path
    return prov.id, cand.id


def test_merge_unresolved_into_candidate_then_undo_restores_unresolved(client, stub_resolve):
    """The unification's happy path: the lint finding carries the candidate, /unresolved/merge folds the
    provisional entity into it (the directional fold — the provisional always loses), and /undo-merge
    sends it back to the *unresolved* queue, not a blanket 'active'."""
    from doctalk.db.models import Entity

    wikirepo.ensure_scaffold()
    with session_scope() as s:
        prov, cand = _unresolved_with_candidate(s)

    # the lint endpoint pairs the unresolved entity with its top candidate, so the card can offer merge
    groups = {g["kind"]: g for g in client.get("/api/maintenance/lint").json()["groups"]}
    item = next(i for i in groups["unresolved"]["items"] if i["entity_id"] == prov)
    assert item["candidate"] and item["candidate"]["id"] == cand

    res = client.post("/api/maintenance/unresolved/merge",
                      json={"entity_id": prov, "into_id": cand}).json()
    assert res["folded"] == "Channel Sounding (CS)" and res["into"] == "Channel Sounding" and res["sha"]
    with session_scope() as s:
        assert s.get(Entity, prov).status == "merged_into"
        assert s.get(Entity, cand).status == "active"
    # can't fold it twice — the provisional one is already gone
    assert client.post("/api/maintenance/unresolved/merge",
                       json={"entity_id": prov, "into_id": cand}).status_code == 409

    undo = client.post("/api/maintenance/undo-merge", json={"sha": res["sha"]}).json()
    assert undo["count"] == 1
    with session_scope() as s:
        assert s.get(Entity, prov).status == "unresolved"  # back in the queue, not silently resolved
    # and it's surfaced by lint again, candidate and all
    groups = {g["kind"]: g for g in client.get("/api/maintenance/lint").json()["groups"]}
    assert any(i["entity_id"] == prov for i in groups["unresolved"]["items"])


def test_merge_unresolved_is_admin_gated(client, monkeypatch):
    monkeypatch.setenv("DOCTALK_ADMIN_TOKEN", "s3cret")
    get_settings.cache_clear()
    blocked = client.post("/api/maintenance/unresolved/merge", json={"entity_id": 1, "into_id": 2})
    assert blocked.status_code == 401
    get_settings.cache_clear()


def test_keep_unresolved_is_admin_gated(client, monkeypatch):
    monkeypatch.setenv("DOCTALK_ADMIN_TOKEN", "s3cret")
    get_settings.cache_clear()
    blocked = client.post("/api/maintenance/unresolved/keep", json={"entity_id": 1})
    assert blocked.status_code == 401
    get_settings.cache_clear()


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
