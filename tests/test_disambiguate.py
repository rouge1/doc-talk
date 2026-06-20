"""slug disambiguation: the mechanical fix for genuinely-distinct entities the slugifier collides.

Model-free — like the merge tests, this is pure DB + page-write logic against the temp wiki the ``db``
fixture isolates. ``C[t+1]`` and ``C[t-1]`` are the canonical case: same base slug (the slugifier
flattens ``+``/``-``), distinct norm_keys, so a merge would conflate them and the right fix is to give
each its own page.
"""

from __future__ import annotations

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.db.models import Entity
from doctalk.db.session import session_scope
from doctalk.synth import disambiguate, pages, wikirepo


def _file(s):
    repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                     format="pdf", mime="x", byte_size=1)
    s.flush()
    return repo.get_file_id(s, "a" * 64)


def _ent(s, name, norm, *, type_="component", fid=None, n_claims=0):
    e = repo.create_entity(s, name=name, type_=type_, norm_key=norm)
    for i in range(n_claims):
        c = repo.insert_claim(s, entity_id=e.id, file_id=fid, text=f"{name} claim {i}.")
        repo.insert_claim_sources(s, c.id, [{"file_id": fid, "chunk_id": None}])
    s.flush()
    return e


def test_disc_is_stable_and_distinct():
    """The discriminator is a pure function of the norm_key: same key -> same suffix (idempotent across
    re-ingests), different key -> different suffix (the two siblings never re-collide)."""
    a = Entity(name="C[t+1]", type="component", norm_key="c[t+1")
    a2 = Entity(name="C[t+1] (other surface)", type="component", norm_key="c[t+1")
    b = Entity(name="C[t-1]", type="component", norm_key="c[t-1")
    assert disambiguate._disc(a) == disambiguate._disc(a2)
    assert disambiguate._disc(a) != disambiguate._disc(b)


def test_slug_override_wins_over_derived_slug(db):
    with session_scope() as s:
        e = _ent(s, "C[t+1]", "c[t+1")
        assert pages.base_slug_for(e) == "c-t-1"
        assert pages.slug_for(e) == "c-t-1"            # no override yet -> derived
        e.slug = "c-t-1-abc123"
        s.flush()
        assert pages.slug_for(e) == "c-t-1-abc123"     # override wins
        assert pages.base_slug_for(e) == "c-t-1"       # base slug still ignores the override


def test_plan_disambiguates_genuine_collision_not_dupes(db):
    """The planner splits genuinely-distinct siblings (distinct merge_key) and leaves fold-able
    duplicates (same merge_key) for the merge heal. The lower-id sibling keeps the bare slug."""
    with session_scope() as s:
        fid = _file(s)
        cm = _ent(s, "C[t-1]", "c[t-1", fid=fid, n_claims=1)   # lower id -> winner, keeps base slug
        cp = _ent(s, "C[t+1]", "c[t+1", fid=fid, n_claims=1)   # gets its own slug
        a = _ent(s, "AFH_map", "afh_map", fid=fid, n_claims=2)  # underscore/space dupes: a merge, not a
        b = _ent(s, "AFH map", "afh map", fid=fid, n_claims=1)  # disambiguation
        cm_id, cp_id, a_id, b_id = cm.id, cp.id, a.id, b.id

    with session_scope() as s:
        plan = {e.id: (base, slug) for e, base, slug in disambiguate.plan_disambiguations(s)}

    assert cp_id in plan and cm_id not in plan            # the higher-id distinct sibling is split out
    base, new_slug = plan[cp_id]
    assert base == "c-t-1" and new_slug.startswith("c-t-1-")
    assert a_id not in plan and b_id not in plan          # the mergeable dupes are untouched here


def test_disambiguate_then_undo_round_trips(db):
    """End-to-end: split the collision (each gets its own file), confirm it's idempotent, then undo —
    the override clears, the standalone page is retired, and the collision is faithfully restored."""
    wiki = get_settings().wiki_dir
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        fid = _file(s)
        cm = _ent(s, "C[t-1]", "c[t-1", fid=fid, n_claims=1)
        cp = _ent(s, "C[t+1]", "c[t+1", fid=fid, n_claims=1)
        assert pages.slug_for(cm) == pages.slug_for(cp) == "c-t-1"   # they collide today
        cm_id, cp_id = cm.id, cp.id

    with session_scope() as s:
        applied = disambiguate.disambiguate_collisions(s)
        assert wikirepo.commit("disambiguate")
        assert len(applied) == 1
        name, base, new_slug, eid = applied[0]
        assert eid == cp_id and base == "c-t-1" and new_slug.startswith("c-t-1-")

    with session_scope() as s:
        cm, cp = s.get(Entity, cm_id), s.get(Entity, cp_id)
        assert cm.slug is None and cp.slug == new_slug               # winner bare, loser split
        assert pages.slug_for(cm) != pages.slug_for(cp)              # no longer collide
        assert (wiki / "entities/c-t-1.md").exists()                 # winner keeps the base file
        assert (wiki / f"entities/{new_slug}.md").exists()           # loser has its own
        assert disambiguate.plan_disambiguations(s) == []            # idempotent

    with session_scope() as s:
        undone = disambiguate.undo_disambiguations(s, [cp_id])
        assert wikirepo.commit("undo")
        assert undone == [("C[t+1]", "c-t-1")]

    with session_scope() as s:
        cp = s.get(Entity, cp_id)
        assert cp.slug is None                                       # override cleared
        assert pages.slug_for(cp) == "c-t-1"                         # collision restored, warts and all
        assert not (wiki / f"entities/{new_slug}.md").exists()       # standalone page retired
        assert repo.get_wiki_page_by_path(s, f"entities/{new_slug}.md") is None  # catalog row gone
