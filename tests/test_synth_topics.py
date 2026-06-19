"""synth_topics: chapter-rollup clustering, topic page rendering, catalog/index reconciliation.

The LLM is monkeypatched (fixed prose or a simulated outage); these cover the clustering and
bookkeeping guarantees — mentions roll up to top-level chapters, thin chapters get no page, slugs
are file-prefixed, stale topic rows are reconciled, and a failed call skips its topic only.
"""

from __future__ import annotations

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.ingest.dag import StageContext
from doctalk.ingest.stages import synth_topics


def _corpus(s, *, rich_entities=3, thin_entities=1):
    """One file, two top-level chapters: 'Security' (with a nested subsection holding the chunks)
    and 'Appendix'. Entities are mentioned via chunks so they cluster by chapter rollup."""
    repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="Spec v1.pdf",
                     format="pdf", mime="x", byte_size=1)
    s.flush()
    fid = repo.get_file_id(s, "a" * 64)
    sec, sub, app = repo.insert_chapters(s, fid, [
        {"level": 1, "ord": 0, "title": "Security", "page_start": 1, "page_end": 9,
         "source": "outline", "parent_ord": None},
        {"level": 2, "ord": 1, "title": "Encryption", "page_start": 2, "page_end": 5,
         "source": "outline", "parent_ord": 0},
        {"level": 1, "ord": 2, "title": "Appendix", "page_start": 10, "page_end": 12,
         "source": "outline", "parent_ord": None},
    ])
    repo.insert_chunks(s, fid, [
        {"chapter_id": sub.id, "page": 2, "ord": 0, "char_count": 10, "text": "security text"},
        {"chapter_id": app.id, "page": 10, "ord": 1, "char_count": 10, "text": "appendix text"},
    ])
    sub_chunk, app_chunk = repo.get_chunks(s, fid)

    def _ent(name, chunk):
        e = repo.create_entity(s, name=name, type_="concept", norm_key=name.lower())
        claim = repo.insert_claim(s, entity_id=e.id, file_id=fid, text=f"{name} does things.")
        repo.insert_claim_sources(s, claim.id, [{"file_id": fid, "chunk_id": chunk.id}])
        repo.insert_mentions(s, fid, [{"entity_id": e.id, "chunk_id": chunk.id}])
        return e

    rich = [_ent(f"Rich{i}", sub_chunk) for i in range(rich_entities)]   # nested -> rolls up
    thin = [_ent(f"Thin{i}", app_chunk) for i in range(thin_entities)]
    return fid, sec, app, rich, thin


def _run(monkeypatch, chat=None):
    def _down(messages, model=None, **kw):
        raise RuntimeError("ollama unreachable")

    monkeypatch.setattr(synth_topics, "_chat", chat or _down)
    with session_scope() as s:
        ctx = StageContext("a" * 64, None, s)
        synth_topics.run(ctx)
        return dict(ctx.scratch)


def test_cluster_rolls_nested_chunks_up_to_top_level(db):
    with session_scope() as s:
        fid, sec, _app, rich, thin = _corpus(s)
        clusters = synth_topics.cluster_entities(s, fid)
        assert {e.id for e in rich} == clusters[sec.id]  # via the level-2 'Encryption' subsection


def test_topic_pages_written_for_rich_chapters_only(db, monkeypatch):
    monkeypatch.setenv("DOCTALK_SYNTH_TOPIC_MIN_ENTITIES", "2")
    get_settings.cache_clear()
    with session_scope() as s:
        _corpus(s)

    prompts = []

    def fake_chat(messages, model=None, **kw):
        prompts.append(messages)
        return "Security rests on [[rich0|Rich0]] and friends."

    scratch = _run(monkeypatch, chat=fake_chat)
    assert scratch["synth_topics"] == 1 and scratch["synth_topics_failed"] == 0

    wiki = get_settings().wiki_dir
    page = wiki / "topics" / "spec-v1--security.md"
    assert page.exists()                                          # file-stem-prefixed slug
    md = page.read_text()
    assert md.startswith("# Security\n")
    assert "Security rests on [[rich0|Rich0]]" in md
    assert "## Drawn from" in md and "[[rich1|Rich1]]" in md       # provenance via entity links
    assert not (wiki / "topics" / "spec-v1--appendix.md").exists() # 1 entity < min 2

    user = prompts[0][1]["content"]
    assert "CHAPTER: Security" in user
    assert "[[rich0|Rich0]] (concept): Rich0 does things." in user  # claim-grounded digest

    index = (wiki / "index.md").read_text()
    assert "[[spec-v1--security|Security]]" in index               # Topics section is live
    assert "topics | Spec v1.pdf (1 pages)" in (wiki / "log.md").read_text()
    with session_scope() as s:
        rows = repo.get_wiki_pages_by_kind(s, "topic")
        assert [p.path for p in rows] == ["topics/spec-v1--security.md"]


def test_linkify_links_first_plain_occurrence_only():
    refs = [("salt", "salt"), ("unsalted-butter", "unsalted butter")]
    prose = "Mix unsalted butter with salt; more salt to taste. [[salt|salt]] stays linked."
    out = synth_topics._linkify("Mix unsalted butter with salt; more salt to taste.", refs)
    assert "[[unsalted-butter|unsalted butter]]" in out
    assert out.count("[[salt|salt]]") == 1                      # first occurrence only
    # a name the model already linked is left alone
    assert synth_topics._linkify(prose, [("salt", "salt")]).count("[[salt|salt]]") == 1


def test_linkify_repairs_model_bracket_slips():
    """Live with qwen3.5: names ending in a paren get a one-bracket link, and some links omit the
    display half. linkify strips the model's markup and re-links deterministically, so both heal."""
    refs = [("lmp", "Link Manager protocol (LMP)"), ("sssm", "Sounding sequence marker signal")]
    # malformed single-bracket link (name ends in a paren) -> repaired to a well-formed [[..]]
    out = synth_topics._linkify("The [[lmp|Link Manager protocol (LMP)], covered later.", refs)
    assert "[[lmp|Link Manager protocol (LMP)]]" in out
    assert "(LMP)]," not in out                                 # the broken single bracket is gone
    # slug-only link (no display) -> re-expanded to the proper [[slug|Name]]
    out2 = synth_topics._linkify("See [[sssm]] for details.", refs)
    assert "[[sssm|Sounding sequence marker signal]]" in out2


def test_failed_call_skips_topic_not_stage(db, monkeypatch):
    monkeypatch.setenv("DOCTALK_SYNTH_TOPIC_MIN_ENTITIES", "2")
    get_settings.cache_clear()
    with session_scope() as s:
        _corpus(s)
    scratch = _run(monkeypatch)  # chat raises
    assert scratch["synth_topics"] == 0 and scratch["synth_topics_failed"] == 1
    assert not (get_settings().wiki_dir / "topics" / "spec-v1--security.md").exists()


def test_rerun_reconciles_stale_topic_pages(db, monkeypatch):
    monkeypatch.setenv("DOCTALK_SYNTH_TOPIC_MIN_ENTITIES", "2")
    get_settings.cache_clear()
    with session_scope() as s:
        _corpus(s)
    from doctalk.synth import wikirepo

    wikirepo.ensure_scaffold()
    stale = "topics/spec-v1--removed-chapter.md"
    wikirepo.write_page(stale, "# Removed\n")
    with session_scope() as s:
        repo.upsert_wiki_page(s, path=stale, title="Removed", kind="topic", entity_id=None)

    _run(monkeypatch, chat=lambda m, model=None, **kw: "Prose.")
    assert not (get_settings().wiki_dir / stale).exists()          # stale page + row reconciled
    with session_scope() as s:
        assert [p.path for p in repo.get_wiki_pages_by_kind(s, "topic")] == [
            "topics/spec-v1--security.md"
        ]


def test_topics_disabled_by_setting(db, monkeypatch):
    monkeypatch.setenv("DOCTALK_SYNTH_TOPICS", "false")
    get_settings.cache_clear()
    with session_scope() as s:
        _corpus(s)
    called = []
    scratch = _run(monkeypatch, chat=lambda m, model=None, **kw: called.append(1) or "x")
    assert not called and scratch == {}