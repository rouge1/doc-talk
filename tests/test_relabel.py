"""wiki-relabel: repair entity names that swallowed a test-vector row label ("T_ID 5 - X").

Seeds the store directly (no LLM) to mirror the real corpus disposition: a clean "RTT AA candidates"
beside its labeled twin (folds in), a labeled entity with no twin (renames in place), and a bare
"T_ID 5" husk (prunes). Asserts the plan classifies each correctly and the apply repairs them — with
the fold reversible by the entity_merges row, exactly like any merge.
"""

from __future__ import annotations

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.db.models import Entity, EntityMerge
from doctalk.db.session import session_scope
from doctalk.synth import merge, pages, relabel, wikirepo


def _seed(s, name, norm, *, claims=0, fid=None):
    """Seed an entity with a page + ``claims`` grounded claims; returns its id."""
    e = repo.create_entity(s, name=name, type_="component", norm_key=norm)
    path = f"entities/{pages.slug_for(e)}.md"
    wikirepo.write_page(path, pages.render_entity_page(s, e))
    repo.upsert_wiki_page(s, path=path, title=name, kind="entity", entity_id=e.id)
    e.wiki_path = path
    for i in range(claims):
        c = repo.insert_claim(s, entity_id=e.id, file_id=fid, text=f"{name} {i}.")
        repo.insert_claim_sources(s, c.id, [{"file_id": fid, "chunk_id": None}])
    s.flush()
    return e.id


def _seed_corpus(s):
    repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                     format="pdf", mime="x", byte_size=1)
    s.flush()
    fid = repo.get_file_id(s, "a" * 64)
    return {
        "clean": _seed(s, "RTT AA candidates", "rtt aa candidates", claims=3, fid=fid),
        "twin": _seed(s, "T_ID 5 - RTT AA candidates", "t_id 5 - rtt aa candidates", claims=1, fid=fid),
        "rename": _seed(s, "T_ID 1 - mode0 channel", "t_id 1 - mode0 channel", claims=1, fid=fid),
        "husk": _seed(s, "T_ID 5", "t_id 5", claims=1, fid=fid),
    }


def test_plan_relabel_classifies_fold_rename_prune(db):
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        _seed_corpus(s)
    with session_scope() as s:
        plan = {r.raw: r for r in relabel.plan_relabel(s)}

    assert plan["T_ID 5 - RTT AA candidates"].action == "fold"
    assert plan["T_ID 5 - RTT AA candidates"].into_name == "RTT AA candidates"
    assert plan["T_ID 1 - mode0 channel"].action == "rename"
    assert plan["T_ID 1 - mode0 channel"].clean == "mode0 channel"
    assert plan["T_ID 5"].action == "prune"
    assert "RTT AA candidates" not in plan  # an already-clean entity is never touched


def test_apply_relabel_folds_renames_prunes_and_fold_undoes(db, stub_resolve):
    wiki = get_settings().wiki_dir
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        ids = _seed_corpus(s)
        old_rename_path = s.get(Entity, ids["rename"]).wiki_path

    with session_scope() as s:
        out = relabel.apply_relabel(s, wiki)

    assert out["folds"] == [("T_ID 5 - RTT AA candidates", "RTT AA candidates")]
    assert out["renames"] == [("T_ID 1 - mode0 channel", "mode0 channel")]
    assert out["prunes"] == ["T_ID 5"]

    with session_scope() as s:
        # fold: twin folded into the clean entity, which absorbed its claim (3 -> 4)
        assert s.get(Entity, ids["twin"]).status == "merged_into"
        assert s.get(Entity, ids["clean"]).status == "active"
        assert len(repo.get_claims_for_entity(s, ids["clean"])) == 4
        # rename: name + key cleaned, page moved to the clean slug
        renamed = s.get(Entity, ids["rename"])
        assert renamed.name == "mode0 channel" and renamed.norm_key == "mode0 channel"
        assert renamed.wiki_path == "entities/mode0-channel.md"
        # prune: husk gone, its claim kept (truth store stays auditable)
        assert s.get(Entity, ids["husk"]).status == "pruned"
        assert len(repo.get_claims_for_entity(s, ids["husk"])) == 1

    assert (wiki / "entities/mode0-channel.md").exists()   # rename moved the page on disk
    assert not (wiki / old_rename_path).exists()           # stale file retired

    # the fold reverses by its entity_merges row, like any merge
    with session_scope() as s:
        merge.undo_merge(s, s.get(EntityMerge, out["merge_ids"][0]))
        assert s.get(Entity, ids["twin"]).status == "active"
