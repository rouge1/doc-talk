"""synth_integrate: page rendering + the stage that materializes the wiki on disk.

The LLM lead-paragraph is monkeypatched off, so these cover the deterministic guarantees — every
claim carries provenance, pages interlink via [[wikilinks]], the catalog + index stay consistent,
and a re-run reproduces byte-identical pages (idempotency). git is best-effort and not required.
"""

from __future__ import annotations

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.ingest.dag import StageContext
from doctalk.ingest.stages import synth_entities, synth_integrate
from doctalk.synth import extract, pages


def _doc(s):
    repo.upsert_file(
        s, content_hash="a" * 64, path="/a", filename="a.pdf", format="pdf", mime="x", byte_size=1,
    )
    s.flush()
    fid = repo.get_file_id(s, "a" * 64)
    ch = repo.insert_chapters(
        s, fid, [{"level": 1, "ord": 0, "title": "Sec", "page_start": 1, "page_end": 1,
                  "source": "outline", "parent_ord": None}]
    )[0]
    repo.insert_chunks(s, fid, [
        {"chapter_id": ch.id, "page": 1, "ord": 0, "char_count": 40,
         "text": "The E0 cipher works with the Link Manager."},
    ])
    return fid


_EXTRACTED = [
    extract.ExtractedEntity("E0 cipher", "component", ["E0"], ["E0 is a stream cipher."]),
    extract.ExtractedEntity(
        "Link Manager", "component", [], ["Link Manager handles pairing.", "It runs over HCI."]
    ),
]


def _populate(monkeypatch, overview_chat=None):
    """Run synth_entities (mocked extractor) then synth_integrate (LLM summary disabled; the
    overview chat raises like an unreachable Ollama unless a fake is supplied)."""
    from doctalk.synth import overview

    def _down(messages, model=None, **kw):
        raise RuntimeError("ollama unreachable")

    monkeypatch.setattr(extract, "extract_entities",
                        lambda passage, model=None, timeout=None: _EXTRACTED)
    monkeypatch.setattr(synth_integrate, "_summarize", lambda name, claims, model: None)
    monkeypatch.setattr(overview, "_chat", overview_chat or _down)
    with session_scope() as s:
        synth_entities.run(StageContext("a" * 64, None, s))
    with session_scope() as s:
        synth_integrate.run(StageContext("a" * 64, None, s))


# --- pure rendering --------------------------------------------------------


def test_render_entity_page_has_claims_provenance_and_links(db, monkeypatch, stub_resolve):
    with session_scope() as s:
        _doc(s)
    _populate(monkeypatch)
    with session_scope() as s:
        e0 = repo.find_entity_by_norm_key(s, "e0 cipher", "component")
        md = pages.render_entity_page(s, e0)
    assert md.startswith("# E0 cipher")
    assert "## Claims" in md and "E0 is a stream cipher." in md
    assert "source: a.pdf p.1" in md                       # provenance is mandatory + resolved
    assert "[[link-manager|Link Manager]]" in md           # co-mention wikilink


def test_slug_for_is_stable_and_filesystem_safe():
    class _E:  # minimal stand-in
        norm_key = "all-purpose flour"
        name = "All-Purpose Flour"
        id = 1
    assert pages.slug_for(_E()) == "all-purpose-flour"


# --- the stage -------------------------------------------------------------


def test_integrate_writes_pages_index_log_and_catalog(db, monkeypatch, stub_resolve):
    with session_scope() as s:
        _doc(s)
    _populate(monkeypatch)

    wiki = get_settings().wiki_dir
    e0_page = wiki / "entities" / "e0-cipher.md"
    lm_page = wiki / "entities" / "link-manager.md"
    assert e0_page.exists() and lm_page.exists()
    assert "It runs over HCI." in lm_page.read_text()       # cumulative claims rendered

    index = (wiki / "index.md").read_text()
    assert "[[e0-cipher|E0 cipher]]" in index and "[[link-manager|Link Manager]]" in index
    assert "ingest | a.pdf" in (wiki / "log.md").read_text()

    with session_scope() as s:
        catalog = repo.get_wiki_pages_by_kind(s, "entity")
        assert {p.path for p in catalog} == {"entities/e0-cipher.md", "entities/link-manager.md"}
        assert all(p.md_hash and p.entity_id for p in catalog)
        e0 = repo.find_entity_by_norm_key(s, "e0 cipher", "component")
        assert e0.wiki_path == "entities/e0-cipher.md"


# --- the evolving overview ---------------------------------------------------


def test_overview_is_revised_with_previous_text_as_input(db, monkeypatch, stub_resolve):
    with session_scope() as s:
        _doc(s)
    prompts = []

    def fake_chat(messages, model=None, **kw):
        prompts.append(messages)
        return "A corpus about Bluetooth: [[e0-cipher|E0 cipher]] and [[link-manager|Link Manager]]."

    _populate(monkeypatch, overview_chat=fake_chat)

    md = (get_settings().wiki_dir / "overview.md").read_text()
    assert md.startswith("# Overview\n\n")                       # heading owned by us, not the model
    assert "[[e0-cipher|E0 cipher]]" in md
    user = prompts[0][1]["content"]
    assert "A running, high-level summary" in user               # previous (seed) text was the input
    assert "NEW SOURCE: a.pdf" in user
    # digest hands the model ready-made wikilinks + a grounding claim per entity
    assert "[[link-manager|Link Manager]] (component): Link Manager handles pairing." in user


def test_overview_untouched_when_model_unavailable(db, monkeypatch, stub_resolve):
    with session_scope() as s:
        _doc(s)
    _populate(monkeypatch)  # overview chat raises RuntimeError

    md = (get_settings().wiki_dir / "overview.md").read_text()
    assert "A running, high-level summary" in md                 # seed left in place, page not blanked


def test_overview_disabled_by_setting(db, monkeypatch, stub_resolve):
    monkeypatch.setenv("DOCTALK_SYNTH_OVERVIEW", "false")
    from doctalk.config import get_settings as gs
    gs.cache_clear()

    with session_scope() as s:
        _doc(s)
    called = []
    _populate(monkeypatch, overview_chat=lambda *a, **kw: called.append(1) or "text")
    assert not called


def test_integrate_is_idempotent(db, monkeypatch, stub_resolve):
    with session_scope() as s:
        _doc(s)
    _populate(monkeypatch)
    wiki = get_settings().wiki_dir
    before = (wiki / "entities" / "e0-cipher.md").read_text()
    with session_scope() as s:
        hash_before = repo.get_wiki_page_by_path(s, "entities/e0-cipher.md").md_hash

    with session_scope() as s:  # re-integrate
        synth_integrate.run(StageContext("a" * 64, None, s))

    assert (wiki / "entities" / "e0-cipher.md").read_text() == before  # byte-identical
    with session_scope() as s:
        assert len(repo.get_wiki_pages_by_kind(s, "entity")) == 2      # no duplicate catalog rows
        assert repo.get_wiki_page_by_path(s, "entities/e0-cipher.md").md_hash == hash_before
