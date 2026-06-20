"""wiki-lint / wiki-audit: each finding type, and the safe materialize fix.

Seeds the truth store directly (no LLM/model) and writes minimal markdown on disk so the orphan and
catalog-drift checks have something real to read.
"""

from __future__ import annotations

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.synth import lint, wikirepo


def _file(s):
    repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                     format="pdf", mime="x", byte_size=1)
    s.flush()
    return repo.get_file_id(s, "a" * 64)


def _entity(s, name, norm, *, type_="component", status="active", source_count=1, wiki_path=None):
    e = repo.create_entity(s, name=name, type_=type_, norm_key=norm, status=status)
    e.source_count = source_count
    if wiki_path:
        e.wiki_path = wiki_path
    s.flush()
    return e


def test_unsupported_claim_is_flagged(db):
    with session_scope() as s:
        fid = _file(s)
        e = _entity(s, "Cake", "cake")
        repo.insert_claim(s, entity_id=e.id, file_id=fid, text="Unsupported assertion.")  # no sources
        findings = lint.lint(s, get_settings().wiki_dir)
    assert any(f.kind == "unsupported_claim" and f.ref == "Cake" for f in findings)


def test_unresolved_entity_and_review_queue_flagged(db):
    with session_scope() as s:
        fid = _file(s)
        _entity(s, "Ambiguous", "ambiguous", status="unresolved")
        repo.add_entity_review(s, mention_surface="Ambiguous", mention_type="component",
                               file_id=fid, entity_id=None, payload={})
        findings = lint.lint(s, get_settings().wiki_dir)
    kinds = [f for f in findings if f.kind == "unresolved"]
    assert any(f.ref == "Ambiguous" for f in kinds)
    assert any("review queue" in f.detail for f in kinds)


def test_missing_page_flagged(db):
    with session_scope() as s:
        _entity(s, "Orphaned Entity", "orphaned entity", source_count=3, wiki_path=None)
        findings = lint.lint(s, get_settings().wiki_dir)
    assert any(f.kind == "missing_page" and f.ref == "Orphaned Entity" for f in findings)


def test_deleted_page_flagged_and_healed(db):
    # The prune slug-collision bug left active entities whose page file was deleted out from under
    # them: wiki_path is set (non-null) but the file is gone. The old wiki_path-IS-NULL checks were
    # blind to this. lint must flag it ('deleted_page') and materialize_missing must regenerate it.
    wiki = get_settings().wiki_dir
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        fid = _file(s)
        e = _entity(s, "HCI", "hci", wiki_path="entities/hci.md")  # pointer set, but no file on disk
        claim = repo.insert_claim(s, entity_id=e.id, file_id=fid, text="The host controller interface.")
        repo.insert_claim_sources(s, claim.id, [{"file_id": fid, "chunk_id": None}])
        assert not (wiki / "entities" / "hci.md").exists()
        findings = lint.lint(s, wiki)
        assert any(f.kind == "deleted_page" and f.ref == "HCI" for f in findings)
        assert lint.materialize_missing(s, wiki) == ["HCI"]
    assert (wiki / "entities" / "hci.md").exists()                 # regenerated
    with session_scope() as s:
        assert not any(f.kind == "deleted_page" for f in lint.lint(s, wiki))  # healed -> no longer flagged


def test_slug_collision_flagged(db):
    # Two active entities whose norm_keys differ only by underscore-vs-space slugify to the same path,
    # so one's page silently overwrites the other. lint must flag the collision by slug.
    with session_scope() as s:
        _entity(s, "AFH_channel_map", "afh_channel_map")
        _entity(s, "AFH channel map", "afh channel map")
        findings = lint.lint(s, get_settings().wiki_dir)
    coll = [f for f in findings if f.kind == "slug_collision"]
    assert len(coll) == 1 and coll[0].ref == "afh-channel-map"
    assert "AFH channel map" in coll[0].detail and "AFH_channel_map" in coll[0].detail


def test_duplicate_candidates_suggested(db):
    with session_scope() as s:
        _entity(s, "Link Manager", "link manager", wiki_path="entities/link-manager.md")
        _entity(s, "Link Manager Protocol", "link manager protocol",
                wiki_path="entities/link-manager-protocol.md")
        findings = lint.lint(s, get_settings().wiki_dir)
    assert any(f.kind == "duplicate" for f in findings)  # share 2/3 norm_key tokens


def test_orphan_page_detected_from_disk(db):
    wiki = get_settings().wiki_dir
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        a = _entity(s, "Alpha", "alpha", wiki_path="entities/alpha.md")
        b = _entity(s, "Beta", "beta", wiki_path="entities/beta.md")
        repo.upsert_wiki_page(s, path="entities/alpha.md", title="Alpha", kind="entity", entity_id=a.id)
        repo.upsert_wiki_page(s, path="entities/beta.md", title="Beta", kind="entity", entity_id=b.id)
    # alpha links to beta; nothing links to alpha -> alpha is the orphan
    wikirepo.write_page("entities/alpha.md", "# Alpha\n\nSee [[beta|Beta]].\n")
    wikirepo.write_page("entities/beta.md", "# Beta\n")
    with session_scope() as s:
        findings = lint.lint(s, wiki)
    orphans = {f.ref for f in findings if f.kind == "orphan"}
    assert "Alpha" in orphans and "Beta" not in orphans


def test_audit_flags_dangling_chunk_and_catalog_drift(db):
    wiki = get_settings().wiki_dir
    with session_scope() as s:
        fid = _file(s)
        e = _entity(s, "Cake", "cake")
        claim = repo.insert_claim(s, entity_id=e.id, file_id=fid, text="Bake it.")
        repo.insert_claim_sources(s, claim.id, [{"file_id": fid, "chunk_id": 9999}])  # no such chunk
        repo.upsert_wiki_page(s, path="entities/ghost.md", title="Ghost", kind="entity", entity_id=e.id)
        findings = lint.audit(s, wiki)
    assert any(f.kind == "dangling_source" for f in findings)
    assert any(f.kind == "catalog_drift" for f in findings)


def test_materialize_missing_creates_pages(db):
    wiki = get_settings().wiki_dir
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        fid = _file(s)
        e = _entity(s, "Cake", "cake", wiki_path=None)
        claim = repo.insert_claim(s, entity_id=e.id, file_id=fid, text="Bake 30 min.")
        repo.insert_claim_sources(s, claim.id, [{"file_id": fid, "chunk_id": None}])
        created = lint.materialize_missing(s, wiki)
        assert created == ["Cake"]
        assert e.wiki_path == "entities/cake.md"
    assert (wiki / "entities" / "cake.md").exists()
    with session_scope() as s:
        assert repo.get_wiki_page_by_path(s, "entities/cake.md") is not None
