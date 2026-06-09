"""wiki-prune: retroactively apply the pageworthiness gate to already-synthesized entities.

Seeds the truth store + wiki dir directly (no LLM): one junk entity ("0x0009") and one real one
("Cake"), both with pages on disk and catalog rows. Prune must drop exactly the junk — reversibly
(status flip, claims kept), with the page file and catalog row gone and the real entity untouched.
"""

from __future__ import annotations

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.synth import prune, wikirepo


def _entity_with_page(s, name, norm, *, status="active"):
    e = repo.create_entity(s, name=name, type_="concept", norm_key=norm, status=status)
    path = f"entities/{norm}.md"
    wikirepo.write_page(path, f"# {name}\n")
    repo.upsert_wiki_page(s, path=path, title=name, kind="entity", entity_id=e.id)
    e.wiki_path = path
    s.flush()
    return e.id, path


def test_prune_drops_junk_keeps_real(db):
    wiki = get_settings().wiki_dir
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                         format="pdf", mime="x", byte_size=1)
        s.flush()
        fid = repo.get_file_id(s, "a" * 64)
        junk_id, junk_path = _entity_with_page(s, "0x0009", "0x0009")
        cake_id, cake_path = _entity_with_page(s, "Cake", "cake")
        # the junk entity's claims survive the prune (truth store stays auditable)
        claim = repo.insert_claim(s, entity_id=junk_id, file_id=fid, text="A PSM value.")
        repo.insert_claim_sources(s, claim.id, [{"file_id": fid, "chunk_id": None}])

    with session_scope() as s:
        assert [e.name for e in prune.junk_entities(s)] == ["0x0009"]
        assert prune.prune(s, wiki) == ["0x0009"]

    assert not (wiki / junk_path).exists()        # page file removed
    assert (wiki / cake_path).exists()            # real entity untouched
    with session_scope() as s:
        assert repo.get_wiki_page_by_path(s, junk_path) is None   # catalog matches disk
        assert repo.get_wiki_page_by_path(s, cake_path) is not None
        from doctalk.db.models import Entity

        junk = s.get(Entity, junk_id)
        assert junk.status == "pruned" and junk.wiki_path is None  # reversible, not deleted
        assert len(repo.get_claims_for_entity(s, junk_id)) == 1    # claims kept
        assert s.get(Entity, cake_id).status == "active"


def test_prune_is_idempotent_and_noop_safe(db):
    wiki = get_settings().wiki_dir
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        _entity_with_page(s, "0x0009", "0x0009")

    with session_scope() as s:
        assert prune.prune(s, wiki) == ["0x0009"]
    with session_scope() as s:
        assert prune.prune(s, wiki) == []   # already pruned -> nothing to do
        assert prune.junk_entities(s) == []


def test_pruned_entities_leave_the_index(db):
    from doctalk.synth import pages

    wiki = get_settings().wiki_dir
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        _entity_with_page(s, "0x0009", "0x0009")
        _entity_with_page(s, "Cake", "cake")
        assert "0x0009" in pages.render_index(s)
        prune.prune(s, wiki)
        index = pages.render_index(s)
        assert "0x0009" not in index and "Cake" in index