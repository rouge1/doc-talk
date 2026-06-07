"""synth_resolve: the block → score → two-threshold decision, and the reversible merge.

Resolution is exercised through ``stub_resolve`` (constant name vector, no LLM) so the decision
*logic* is deterministic and model-free. The cases mirror the spec's failure modes: distinct things
stay distinct (NEW), an exact re-mention links (MATCH), two equally-strong candidates DEFER instead
of guessing (conflation guard), and a wrong merge is reversible.
"""

from __future__ import annotations

from doctalk.db import repo
from doctalk.db.models import Claim, Mention
from doctalk.db.session import session_scope
from doctalk.synth import resolve
from doctalk.synth.normalize import acronym_pair


# --- units -----------------------------------------------------------------


def test_acronym_pair_detects_definitional_form():
    assert acronym_pair("Logical Link Control and Adaptation Protocol (L2CAP)") == (
        "logical link control and adaptation protocol",
        "l2cap",
    )
    assert acronym_pair("just a name") is None


def test_types_compatible_gate():
    assert resolve._types_compatible("component", "component")
    assert resolve._types_compatible("component", "concept")   # concept is the wildcard
    assert not resolve._types_compatible("component", "person")


def _resolve(s, name, type_="component", aliases=None, definition="def", comention=None):
    return resolve.resolve_candidate(
        s, name=name, type_=type_, aliases=aliases or [], definition=definition,
        context_text=definition, comention_keys=comention or set(),
    )


# --- the decision band -----------------------------------------------------


def test_first_candidate_is_new(db, stub_resolve):
    with session_scope() as s:
        r = _resolve(s, "Alpha Widget")
        assert r.decision == "NEW" and r.entity.norm_key == "alpha widget"


def test_exact_rementions_match(db, stub_resolve):
    with session_scope() as s:
        first = _resolve(s, "Alpha Widget").entity.id
    with session_scope() as s:
        r = _resolve(s, "Alpha Widget")            # same surface again
        assert r.decision == "MATCH" and r.entity.id == first


def test_distinct_surfaces_stay_separate(db, stub_resolve):
    with session_scope() as s:
        _resolve(s, "Alpha Widget")
    with session_scope() as s:
        r = _resolve(s, "Beta Gadget")             # disjoint tokens -> below tau_low
        assert r.decision == "NEW" and r.entity.norm_key == "beta gadget"


def test_thin_margin_between_two_strong_candidates_defers(db, stub_resolve):
    # Two existing entities share the norm_key "le" — the classic conflation trap. A new "LE"
    # scores high against BOTH with ~zero margin, so the band must DEFER, not guess.
    with session_scope() as s:
        repo.create_entity(s, name="LE radio", type_="component", norm_key="le", aliases=["LE"])
        repo.create_entity(s, name="LE other", type_="component", norm_key="le", aliases=["LE"])
    with session_scope() as s:
        r = _resolve(s, "LE")
        assert r.decision == "DEFER" and r.entity.status == "unresolved"


# --- the recovery half -----------------------------------------------------


def test_merge_repoints_and_is_reversible(db, stub_resolve):
    with session_scope() as s:
        repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                         format="pdf", mime="x", byte_size=1)
        s.flush()
        fid = repo.get_file_id(s, "a" * 64)
        a = repo.create_entity(s, name="E0", type_="component", norm_key="e0")
        b = repo.create_entity(s, name="E0 cipher", type_="component", norm_key="e0 cipher")
        repo.insert_mentions(s, fid, [{"entity_id": a.id}])
        repo.insert_claim(s, entity_id=a.id, file_id=fid, text="E0 is a cipher.")
        a_id, b_id = a.id, b.id

    with session_scope() as s:
        merge = repo.merge_entities(s, a_id, b_id, reason="same thing")
        assert merge.from_id == a_id and merge.into_id == b_id

    with session_scope() as s:
        # mentions + claims repointed to the survivor; the merged entity is a redirect, not deleted
        assert s.scalars(select_entity(Mention, a_id)).all() == []
        assert s.scalars(select_entity(Claim, a_id)).all() == []
        assert len(s.scalars(select_entity(Mention, b_id)).all()) == 1
        merged = repo.get_entity_merges(s)
        assert len(merged) == 1
        assert s.get(repo.Entity, a_id).status == "merged_into"
        assert "E0" in s.get(repo.Entity, b_id).aliases       # survivor absorbed the alias


def select_entity(model, entity_id):
    from sqlalchemy import select

    return select(model).where(model.entity_id == entity_id)
