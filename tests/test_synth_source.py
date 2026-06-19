"""synth_source: per-document profile page — structure, lead-paragraph best-effort, index/log.

The LLM is monkeypatched (fixed prose or a simulated outage). These cover the document-rung
guarantees: the page maps the source's covered chapters (linking to their topic page when one
exists), lists key entities, carries an authored lead only when the model answers, and registers
in the ``## Sources`` index section — all provenance-safe (links chain to entity pages -> chunks).
"""

from __future__ import annotations

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.ingest.dag import StageContext
from doctalk.ingest.stages import synth_source


def _corpus(s):
    """One file 'Spec v1.pdf', two top-level chapters: 'Security' (with a nested 'Encryption'
    subsection holding the chunks, so its entities roll up) and 'Appendix'. Three rich entities
    under Security, one thin under Appendix — both chapters end up covered (have >=1 entity)."""
    repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="Spec v1.pdf",
                     format="pdf", mime="x", byte_size=2_500_000)
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

    rich = [_ent(f"Rich{i}", sub_chunk) for i in range(3)]
    thin = [_ent("Thin0", app_chunk)]
    return fid, sec, app, rich, thin


def _seed_topic_page(s, path="topics/spec-v1--security.md", title="Security"):
    """A pre-existing topic page so the source's Contents list links it (synth_topics ran first)."""
    from doctalk.synth import wikirepo

    wikirepo.ensure_scaffold()
    wikirepo.write_page(path, f"# {title}\n")
    repo.upsert_wiki_page(s, path=path, title=title, kind="topic", entity_id=None)


def _run(monkeypatch, chat=None):
    def _down(messages, model=None, **kw):
        raise RuntimeError("ollama unreachable")

    monkeypatch.setattr(synth_source, "_chat", chat or _down)
    with session_scope() as s:
        ctx = StageContext("a" * 64, None, s)
        synth_source.run(ctx)
        return dict(ctx.scratch)


def test_source_page_maps_structure_and_authors_lead(db, monkeypatch):
    with session_scope() as s:
        _corpus(s)
        _seed_topic_page(s)  # Security has a topic page; Appendix does not

    prompts = []

    def fake_chat(messages, model=None, **kw):
        prompts.append(messages)
        return "The spec covers [[rich0|Rich0]] and related material."

    scratch = _run(monkeypatch, chat=fake_chat)
    assert scratch == {"synth_source": 1, "synth_source_authored": 1}

    wiki = get_settings().wiki_dir
    page = wiki / "sources" / "spec-v1.md"
    assert page.exists()
    md = page.read_text()
    assert md.startswith("# Spec v1.pdf\n")
    assert "> **source** · pdf · 2.4 MB · 2 chapters · 4 entities" in md
    # lead paragraph, linkified
    assert "The spec covers [[rich0|Rich0]] and related material." in md
    # Contents IS the topic index: covered chapters in doc order; Security links its topic page,
    # Appendix (no topic page) stays plain text. No separate Topics section repeats these.
    assert "## Contents" in md
    assert "- [[spec-v1--security|Security]] — 3 entities" in md
    assert "- Appendix — 1 entities" in md
    assert "## Key entities" in md and "[[rich0|Rich0]]" in md
    assert "## Topics" not in md

    # the prompt was grounded in the TOC + claim digest
    user = prompts[0][1]["content"]
    assert "TABLE OF CONTENTS" in user and "- Security" in user and "- Appendix" in user
    assert "[[rich0|Rich0]] (concept): Rich0 does things." in user

    index = (wiki / "index.md").read_text()
    assert "## Sources" in index and "- [[spec-v1|Spec v1.pdf]]" in index
    assert "source | Spec v1.pdf" in (wiki / "log.md").read_text()
    with session_scope() as s:
        rows = repo.get_wiki_pages_by_kind(s, "source")
        assert [p.path for p in rows] == ["sources/spec-v1.md"]


def test_lead_is_best_effort_when_model_down(db, monkeypatch):
    with session_scope() as s:
        _corpus(s)
    scratch = _run(monkeypatch)  # chat raises -> no lead, structural page still written
    assert scratch == {"synth_source": 1, "synth_source_authored": 0}

    page = get_settings().wiki_dir / "sources" / "spec-v1.md"
    assert page.exists()
    md = page.read_text()
    assert "## Contents" in md and "## Key entities" in md  # structure survives the outage
    assert "[[rich0|Rich0]]" in md                          # key entities still present


def test_rerun_overwrites_in_place(db, monkeypatch):
    with session_scope() as s:
        _corpus(s)
    chat = lambda m, model=None, **kw: "Lead."  # noqa: E731
    _run(monkeypatch, chat=chat)
    _run(monkeypatch, chat=chat)  # idempotent: one page, one catalog row
    with session_scope() as s:
        assert [p.path for p in repo.get_wiki_pages_by_kind(s, "source")] == ["sources/spec-v1.md"]


def test_no_page_when_nothing_extracted(db, monkeypatch):
    with session_scope() as s:  # file + chapters but no entities/mentions
        repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="Empty.pdf",
                         format="pdf", mime="x", byte_size=10)
    called = []
    scratch = _run(monkeypatch, chat=lambda m, model=None, **kw: called.append(1) or "x")
    assert scratch == {} and not called
    assert not (get_settings().wiki_dir / "sources" / "empty.md").exists()


def test_sources_disabled_by_setting(db, monkeypatch):
    monkeypatch.setenv("DOCTALK_SYNTH_SOURCES", "false")
    get_settings.cache_clear()
    with session_scope() as s:
        _corpus(s)
    called = []
    scratch = _run(monkeypatch, chat=lambda m, model=None, **kw: called.append(1) or "x")
    assert not called and scratch == {}
