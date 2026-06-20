"""wiki-merge: the shared merge mechanics, the slug-collision planner, and the batch CLI heal.

Model-free (no embeddings/LLM): the planner is pure DB logic, and the batch command's page writes +
git commit run against the temp wiki the ``db`` fixture isolates.
"""

from __future__ import annotations

import pytest

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.db.models import Entity, EntityMerge
from doctalk.db.session import session_scope
from doctalk.synth import merge, pages, wikirepo


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


def test_merge_key_is_underscore_and_space_insensitive():
    assert merge.merge_key("afh_channel_map") == merge.merge_key("afh channel map") == "afh channel map"
    assert merge.merge_key("c[t+1") != merge.merge_key("c[t-1")   # operators aren't spacing


def test_canonical_display_name_titlecases_and_keeps_acronyms():
    # underscore -> space, lowercase words capitalized, real acronyms left intact
    assert merge.canonical_display_name(["channel map", "Channel_Map"]) == "Channel Map"
    assert merge.canonical_display_name(["AFH channel map", "AFH_channel_map"]) == "AFH Channel Map"
    assert merge.canonical_display_name(["CS DRBG", "CS_DRBG"]) == "CS DRBG"
    assert merge.canonical_display_name(["Packet_Type", "Packet Type"]) == "Packet Type"


def test_plan_separates_formatting_dupes_from_genuine_collisions(db):
    with session_scope() as s:
        fid = _file(s)
        # underscore-vs-space dupes: same slug + same merge_key -> auto-mergeable, richer = survivor
        rich = _ent(s, "AFH_channel_map", "afh_channel_map", fid=fid, n_claims=3)
        thin = _ent(s, "AFH channel map", "afh channel map", fid=fid, n_claims=1)
        # same slug but genuinely distinct (the slugifier strips the operators) -> must be skipped
        cp = _ent(s, "C[t+1]", "c[t+1")
        cm = _ent(s, "C[t-1]", "c[t-1")
        # same slug, matching key, but incompatible type -> skipped (safety rail)
        comp = _ent(s, "Packet_Type", "packet_type", type_="component", fid=fid, n_claims=2)
        per = _ent(s, "Packet Type", "packet type", type_="person")
        ids = {n: e.id for n, e in
               {"rich": rich, "thin": thin, "cp": cp, "cm": cm, "comp": comp, "per": per}.items()}

    with session_scope() as s:
        mergeable, skipped = merge.plan_slug_collision_merges(s)
        merged = {(src.id, dst.id) for src, dst in mergeable}
        skips = {src.id: why for src, _dst, why in skipped}

    assert (ids["thin"], ids["rich"]) in merged          # thin folds into the claim-richer survivor
    assert "distinct norm_key" in skips[ids["cm"]] or "distinct norm_key" in skips[ids["cp"]]
    assert "incompatible types" in skips[ids["per"]]     # person !~ component, left manual
    assert ids["comp"] not in {s for s, _ in merged} and ids["comp"] not in skips  # comp is a survivor


def test_wiki_merge_slug_collisions_batch(db):
    from typer.testing import CliRunner

    from doctalk.cli.main import app

    wiki = get_settings().wiki_dir
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        fid = _file(s)
        rich = _ent(s, "Flush Timeout", "flush timeout", fid=fid, n_claims=3)
        thin = _ent(s, "Flush_Timeout", "flush_timeout", fid=fid, n_claims=1)
        path = f"entities/{pages.slug_for(rich)}.md"
        assert path == f"entities/{pages.slug_for(thin)}.md"     # collision
        wikirepo.write_page(path, pages.render_entity_page(s, rich))
        repo.upsert_wiki_page(s, path=path, title=rich.name, kind="entity", entity_id=rich.id)
        rich.wiki_path = thin.wiki_path = path
        rich_id, thin_id = rich.id, thin.id

    dry = CliRunner().invoke(app, ["wiki-merge", "--slug-collisions", "--dry-run"])
    assert dry.exit_code == 0 and "would merge" in dry.output
    with session_scope() as s:
        assert s.get(Entity, thin_id).status == "active"        # dry-run changed nothing

    run = CliRunner().invoke(app, ["wiki-merge", "--slug-collisions"])
    assert run.exit_code == 0
    with session_scope() as s:
        assert s.get(Entity, thin_id).status == "merged_into"   # folded into the survivor
        assert s.get(Entity, rich_id).status == "active"
        # the survivor inherited the merged entity's claim (3 + 1)
        assert len(repo.get_claims_for_entity(s, rich_id)) == 4
    assert "> merged" not in (wiki / path).read_text()          # survivor page not clobbered


# --- undo (unmerge): the reversibility the manifest buys -----------------------------------------


def test_merge_records_manifest_then_unmerge_restores_exactly(db):
    """The merge records which claims/aliases it moved; unmerge repoints *exactly* those back and
    leaves the survivor with only its own — the round-trip the maintenance-page Undo depends on."""
    with session_scope() as s:
        fid = _file(s)
        a = _ent(s, "Alpha", "alpha", fid=fid, n_claims=2)
        b = _ent(s, "Beta", "beta", fid=fid, n_claims=3)
        a_id, b_id = a.id, b.id
        a_claims = {c.id for c in repo.get_claims_for_entity(s, a_id)}

    with session_scope() as s:
        m = repo.merge_entities(s, a_id, b_id, reason="t")
        assert sorted(m.moved["claims"]) == sorted(a_claims)     # manifest = exactly a's claims
        assert "Alpha" in m.moved["aliases_added"]               # a's name contributed to b
        assert len(repo.get_claims_for_entity(s, b_id)) == 5     # 3 + 2 folded in
        assert "Alpha" in (s.get(Entity, b_id).aliases or [])

    with session_scope() as s:
        repo.unmerge_entities(s, repo.get_entity_merges(s)[-1])

    with session_scope() as s:
        a, b = s.get(Entity, a_id), s.get(Entity, b_id)
        assert a.status == "active" and b.status == "active"
        assert {c.id for c in repo.get_claims_for_entity(s, a_id)} == a_claims   # exact claims back
        assert len(repo.get_claims_for_entity(s, b_id)) == 3                     # survivor un-fattened
        assert "Alpha" not in (b.aliases or [])                                  # contributed alias stripped
        assert repo.get_entity_merges(s) == []                                  # record consumed


def test_unmerge_refuses_manifestless_merge(db):
    """A merge row from before undo tracking has no manifest — unmerge refuses rather than guess
    which of the survivor's claims to peel off (guessing would corrupt both entities)."""
    with session_scope() as s:
        fid = _file(s)
        a, b = _ent(s, "A", "a", fid=fid), _ent(s, "B", "b", fid=fid)
        m = EntityMerge(from_id=a.id, into_id=b.id, reason="legacy", moved=None)
        s.add(m)
        s.flush()
        mid = m.id

    with session_scope() as s:
        with pytest.raises(ValueError, match="predates undo tracking"):
            repo.unmerge_entities(s, s.get(EntityMerge, mid))


def test_undo_batch_round_trips_a_slug_collision(db, stub_resolve):
    """End-to-end at the merge layer: apply the collision heal, stamp the commit, then undo_batch by
    that handle — the folded entity comes back active with its claims and the collision is restored."""
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        fid = _file(s)
        rich = _ent(s, "Flush Timeout", "flush timeout", fid=fid, n_claims=3)
        thin = _ent(s, "Flush_Timeout", "flush_timeout", fid=fid, n_claims=1)
        path = f"entities/{pages.slug_for(rich)}.md"
        wikirepo.write_page(path, pages.render_entity_page(s, rich))
        repo.upsert_wiki_page(s, path=path, title=rich.name, kind="entity", entity_id=rich.id)
        rich.wiki_path = thin.wiki_path = path
        rich_id, thin_id = rich.id, thin.id
        thin_claims = {c.id for c in repo.get_claims_for_entity(s, thin_id)}

    with session_scope() as s:
        applied, _ = merge.merge_slug_collisions(s)
        assert wikirepo.commit("merge")
        sha = wikirepo.head_sha()
        repo.set_merge_committed_sha(s, [mid for *_, mid in applied], sha)
    with session_scope() as s:
        assert s.get(Entity, thin_id).status == "merged_into"

    with session_scope() as s:
        undone = merge.undo_batch(s, sha)
        assert len(undone) == 1

    with session_scope() as s:
        assert s.get(Entity, thin_id).status == "active"                        # resurrected
        assert {c.id for c in repo.get_claims_for_entity(s, thin_id)} == thin_claims  # claims back
        assert len(repo.get_claims_for_entity(s, rich_id)) == 3                  # survivor un-fattened
        assert repo.get_merges_by_sha(s, sha) == []                             # batch fully reversed


def test_slug_collision_prettifies_survivor_then_undo_restores_name(db, stub_resolve):
    """Option B: the richest entity stays the data survivor (least churn), but its title is cleaned to
    a consistent spaced/Title-Case form drawn from both spellings; the slug never moves, and undo puts
    the original name back."""
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        fid = _file(s)
        # the lowercase spaced spelling is richer -> it survives, but its title gets prettified
        rich = _ent(s, "channel map", "channel map", fid=fid, n_claims=3)
        thin = _ent(s, "Channel_Map", "channel map", fid=fid, n_claims=1)
        path = f"entities/{pages.slug_for(rich)}.md"
        assert path == f"entities/{pages.slug_for(thin)}.md"     # collision
        wikirepo.write_page(path, pages.render_entity_page(s, rich))
        repo.upsert_wiki_page(s, path=path, title=rich.name, kind="entity", entity_id=rich.id)
        rich.wiki_path = thin.wiki_path = path
        rich_id, slug_before = rich.id, pages.slug_for(rich)

    with session_scope() as s:
        applied, _ = merge.merge_slug_collisions(s)
        assert any(dname == "Channel Map" for _s, dname, _m in applied)  # receipt shows clean title
        assert wikirepo.commit("merge")
        sha = wikirepo.head_sha()
        repo.set_merge_committed_sha(s, [mid for *_, mid in applied], sha)

    with session_scope() as s:
        surv = s.get(Entity, rich_id)
        assert surv.name == "Channel Map"                       # prettified survivor title
        assert "channel map" in (surv.aliases or [])            # original spelling preserved as alias
        assert "Channel_Map" in (surv.aliases or [])            # folded entity's spelling too
        assert pages.slug_for(surv) == slug_before              # the file never moved

    with session_scope() as s:
        merge.undo_batch(s, sha)

    with session_scope() as s:
        surv = s.get(Entity, rich_id)
        assert surv.name == "channel map"                       # original title restored
        assert "Channel Map" not in (surv.aliases or [])        # the synthetic name is gone
        assert "Channel_Map" not in (surv.aliases or [])        # folded entity's alias stripped too
