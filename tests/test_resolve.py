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
from doctalk.synth.normalize import acronym_pair, strip_row_label


# --- units -----------------------------------------------------------------


def test_acronym_pair_detects_definitional_form():
    assert acronym_pair("Logical Link Control and Adaptation Protocol (L2CAP)") == (
        "logical link control and adaptation protocol",
        "l2cap",
    )
    assert acronym_pair("just a name") is None


def test_strip_row_label_salvages_the_subject():
    # A transaction-ID row label is dropped, leaving the cell's real subject to resolve normally.
    assert strip_row_label("T_ID 5 - RTT AA candidates") == "RTT AA candidates"
    assert strip_row_label("T_ID 0 - non-mode0 channel") == "non-mode0 channel"
    assert strip_row_label("T_ID 5") == ""                        # only a label, no subject
    # Conservative: a real name is never touched, even when it ends in a number.
    assert strip_row_label("RTT AA candidates") == "RTT AA candidates"
    assert strip_row_label("AES 128") == "AES 128"
    assert strip_row_label("IEEE 802.11") == "IEEE 802.11"


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


# --- (name, type) is identity: the create-collision guard -------------------


def test_exact_name_in_defer_band_matches_instead_of_minting(db, stub_resolve):
    # The re-drop crash: an entity with no stored name vector scores ~0.50 (alias + lexical only)
    # -> DEFER band -> the can't-tell path used to create_entity straight into a UNIQUE(name, type)
    # violation. (name, type) is identity: it must MATCH the existing row.
    with session_scope() as s:
        eid = repo.create_entity(s, name="Alpha Widget", type_="component",
                                 norm_key="alpha widget").id
    with session_scope() as s:
        r = _resolve(s, "Alpha Widget")
        assert r.decision == "MATCH" and r.entity.id == eid
        assert r.signals.get("exact_name") is True
        assert len(repo.get_entities(s)) == 1              # no duplicate row


def test_exact_name_follows_merge_to_survivor(db, stub_resolve):
    # Merged-away rows keep their name and still occupy the UNIQUE constraint — re-extracting the
    # merged name must land on the survivor, not crash on the redirect stub.
    with session_scope() as s:
        src = repo.create_entity(s, name="Batter", type_="component", norm_key="batter")
        dst = repo.create_entity(s, name="Cake", type_="component", norm_key="cake")
        repo.merge_entities(s, src.id, dst.id, reason="test")
        dst_id = dst.id
    with session_scope() as s:
        r = _resolve(s, "Batter")
        assert r.decision == "MATCH" and r.entity.id == dst_id


def test_exact_name_reactivates_pruned_entity(db, stub_resolve):
    # A same-name row pruned under an older gate is re-admitted when the gate passes it today.
    from doctalk.vector import store

    with session_scope() as s:
        pid = repo.create_entity(s, name="Alpha Widget", type_="component",
                                 norm_key="alpha widget", status="pruned").id
    with session_scope() as s:
        r = _resolve(s, "Alpha Widget")
        assert r.decision == "MATCH" and r.entity.id == pid
        assert r.entity.status == "active"                 # reactivated
        # Prune dropped its name vector; re-admit must restore it, or the entity is active but
        # unfindable — the NEXT mention can't match it (no candidate vector) and mints a duplicate.
        assert pid in store.get_entity_vectors([pid])


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


def test_wiki_merge_same_slug_preserves_survivor_page(db):
    # Two entities sharing a norm_key resolve to the SAME slug -> the same entities/<slug>.md. Merging
    # one into the other must not overwrite the just-written survivor page with the "merged into [[…]]"
    # redirect stub (the same-slug clobber bug). This is the heal path for the live slug collisions.
    from typer.testing import CliRunner

    from doctalk.cli.main import app
    from doctalk.config import get_settings
    from doctalk.db.models import Entity
    from doctalk.synth import pages, wikirepo

    wiki = get_settings().wiki_dir
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                         format="pdf", mime="x", byte_size=1)
        s.flush()
        fid = repo.get_file_id(s, "a" * 64)
        dst = repo.create_entity(s, name="Channel Map", type_="component", norm_key="channel map")
        src = repo.create_entity(s, name="channel map", type_="concept", norm_key="channel map")
        for e in (dst, src):
            c = repo.insert_claim(s, entity_id=e.id, file_id=fid, text=f"{e.name} claim.")
            repo.insert_claim_sources(s, c.id, [{"file_id": fid, "chunk_id": None}])
        path = f"entities/{pages.slug_for(dst)}.md"
        assert path == f"entities/{pages.slug_for(src)}.md"     # same slug -> the collision
        wikirepo.write_page(path, pages.render_entity_page(s, dst))
        repo.upsert_wiki_page(s, path=path, title=dst.name, kind="entity", entity_id=dst.id)
        dst.wiki_path = src.wiki_path = path
        src_id, dst_id = src.id, dst.id

    result = CliRunner().invoke(app, ["wiki-merge", str(src_id), str(dst_id)])
    assert result.exit_code == 0, result.output

    page = (wiki / path).read_text()
    assert "# Channel Map" in page and "> merged" not in page   # survivor intact, not clobbered
    with session_scope() as s:
        assert s.get(Entity, src_id).status == "merged_into"    # the merge still happened
        assert s.get(Entity, dst_id).status == "active"
        assert repo.get_wiki_page_by_path(s, path).entity_id == dst_id  # catalog points at survivor


def select_entity(model, entity_id):
    from sqlalchemy import select

    return select(model).where(model.entity_id == entity_id)
