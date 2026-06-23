"""The query loop: evaluator gate, snapshot/merge filing, auto-promote wiring, stale-query lint.

LLM and embeddings are monkeypatched throughout — these cover the gate logic, the append-vs-create
decision, near-duplicate merging, and the lint check, all deterministically.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.query import wikichat
from doctalk.synth import evaluate, lint, promote, wikirepo
from doctalk.synth.evaluate import Verdict, parse_verdict, should_save


def _page_hit(name="Cake", path="entities/cake.md"):
    return SimpleNamespace(name=name, path=path)


def _chunk_hit():
    return SimpleNamespace(file="a.pdf", chapter="Sec", page=1)


# --- the evaluator gate ------------------------------------------------------


def test_parse_verdict_handles_fences_and_garbage():
    assert parse_verdict('```json\n{"save": true, "reason": "synthesis"}\n```') == Verdict(True, "synthesis")
    assert parse_verdict('{"save": "yes"}') is None        # save must be a real bool
    assert parse_verdict("not json") is None


def test_should_save_prefilters_trivial_without_llm(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("LLM must not be called for trivial answers")
    monkeypatch.setattr(evaluate, "_chat", boom)
    v = should_save("q?", "Short answer.", n_pages=1, n_chunks=0)
    assert not v.save and "trivial" in v.reason


def test_should_save_follows_llm_verdict(monkeypatch):
    monkeypatch.setattr(evaluate, "_chat",
                        lambda *a, **kw: '{"save": true, "reason": "cross-source synthesis"}')
    v = should_save("q?", "long " * 60, n_pages=3, n_chunks=2)
    assert v.save and v.reason == "cross-source synthesis"


def test_should_save_is_conservative_on_failure(monkeypatch):
    def down(*a, **kw):
        raise RuntimeError("ollama unreachable")
    monkeypatch.setattr(evaluate, "_chat", down)
    v = should_save("q?", "long " * 60, n_pages=3, n_chunks=2)
    assert not v.save and "unavailable" in v.reason


# --- snapshot + merge filing -------------------------------------------------


def test_repeat_question_appends_dated_update(db, monkeypatch):
    monkeypatch.setattr(promote, "_embed_titles", lambda q, t: None)  # no dedup model needed
    p1 = promote.promote_query("How long to bake?", "30 minutes.", [_page_hit()], [_chunk_hit()])
    p2 = promote.promote_query("How long to bake?", "Now 35 minutes (new source).", [_page_hit()], [])
    assert p1 == p2
    md = (get_settings().wiki_dir / p1).read_text()
    assert md.count("# How long to bake?") == 1            # one page, not overwritten
    assert "## Update" in md and "35 minutes" in md        # snapshot appended
    assert "30 minutes." in md                             # history preserved
    with session_scope() as s:
        assert len(repo.get_wiki_pages_by_kind(s, "query")) == 1
    log = (get_settings().wiki_dir / "log.md").read_text()
    assert "query-update | How long to bake?" in log


def test_identical_answer_is_a_noop(db, monkeypatch):
    monkeypatch.setattr(promote, "_embed_titles", lambda q, t: None)
    p1 = promote.promote_query("How long to bake?", "30 minutes.", [_page_hit()], [])
    before = (get_settings().wiki_dir / p1).read_text()
    promote.promote_query("How long to bake?", "30 minutes.", [_page_hit()], [])
    assert (get_settings().wiki_dir / p1).read_text() == before


def test_rephrased_question_merges_into_existing_page(db, monkeypatch):
    monkeypatch.setattr(promote, "_embed_titles", lambda q, t: None)
    p1 = promote.promote_query("How long to bake?", "30 minutes.", [_page_hit()], [])

    monkeypatch.setattr(promote, "_embed_titles", lambda q, titles: [0.95] * len(titles))
    monkeypatch.setattr(promote, "_same_subject", lambda a, b, model: True)
    p2 = promote.promote_query("What is the baking time?", "About 30 min.", [_page_hit()], [])
    assert p2 == p1                                        # filed into the equivalent page
    md = (get_settings().wiki_dir / p1).read_text()
    assert "About 30 min." in md and "## Update" in md
    with session_scope() as s:
        pages = repo.get_wiki_pages_by_kind(s, "query")
        assert len(pages) == 1 and pages[0].title == "How long to bake?"  # original title kept


def test_same_shape_different_subject_forks_a_new_page(db, monkeypatch):
    monkeypatch.setattr(promote, "_embed_titles", lambda q, t: None)
    promote.promote_query("How many chapters?", "About 500.", [_page_hit()], [])

    monkeypatch.setattr(promote, "_embed_titles", lambda q, titles: [0.9] * len(titles))
    monkeypatch.setattr(promote, "_same_subject", lambda a, b, model: False)  # judge: same shape only
    p2 = promote.promote_query("How many figures?", "About 4,600.", [_page_hit()], [])
    with session_scope() as s:
        assert len(repo.get_wiki_pages_by_kind(s, "query")) == 2
    assert "figures" in p2


# --- auto-promote wiring -----------------------------------------------------


def _stub_chat_stack(monkeypatch):
    monkeypatch.setattr(wikichat, "retrieve_pages",
                        lambda q, k=6: [SimpleNamespace(name="Cake", type="product",
                                                        path="entities/cake.md", claims=[],
                                                        score=0.9, rerank_score=0.9)])
    monkeypatch.setattr(wikichat, "retrieve", lambda q, k=6, file_id=None: [])
    import doctalk.models.chat as chatmod
    monkeypatch.setattr(chatmod, "chat", lambda messages, **kw: "A long synthesized answer.")


def test_wikichat_auto_files_when_evaluator_approves(db, monkeypatch):
    _stub_chat_stack(monkeypatch)
    import doctalk.synth.evaluate as ev
    monkeypatch.setattr(ev, "should_save", lambda *a, **kw: Verdict(True, "synthesizes"))
    monkeypatch.setattr(promote, "_embed_titles", lambda q, t: None)
    res = wikichat.answer("how long to bake?", save="auto")
    assert res["saved_path"] and res["save_reason"] == "synthesizes"


def test_wikichat_auto_skips_when_evaluator_declines(db, monkeypatch):
    _stub_chat_stack(monkeypatch)
    import doctalk.synth.evaluate as ev
    monkeypatch.setattr(ev, "should_save", lambda *a, **kw: Verdict(False, "single-page lookup"))
    res = wikichat.answer("how long to bake?", save="auto")
    assert res["saved_path"] is None and res["save_reason"] == "single-page lookup"


# --- stale-query lint --------------------------------------------------------


def test_lint_flags_query_whose_entities_gained_claims(db):
    wiki = get_settings().wiki_dir
    wikirepo.ensure_scaffold()
    with session_scope() as s:
        repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                         format="pdf", mime="x", byte_size=1)
        s.flush()
        fid = repo.get_file_id(s, "a" * 64)
        cake = repo.create_entity(s, name="Cake", type_="product", norm_key="cake")
        repo.upsert_wiki_page(s, path="entities/cake.md", title="Cake", kind="entity",
                              entity_id=cake.id)
        wikirepo.write_page("queries/how-long.md", "# How long?\n\nSee [[cake|Cake]].\n")
        repo.upsert_wiki_page(s, path="queries/how-long.md", title="How long?", kind="query",
                              entity_id=None, last_synth_at=datetime(2020, 1, 1))
        findings = lint.lint(s, wiki)
        assert not any(f.kind == "stale_query" for f in findings)  # no claims yet -> not stale

        claim = repo.insert_claim(s, entity_id=cake.id, file_id=fid, text="New fact.")
        repo.insert_claim_sources(s, claim.id, [{"file_id": fid, "chunk_id": None}])
        findings = lint.lint(s, wiki)
    assert any(f.kind == "stale_query" and f.ref == "How long?" for f in findings)