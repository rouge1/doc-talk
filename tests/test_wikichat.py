"""Wiki-first chat: page retrieval, the pure prompt, promote-to-queries, and the orchestrator.

Models and the vector store are monkeypatched, so these cover the wiring + the deterministic pieces
(provenance carried into context, active-only page filtering, the promoted query page) without a
model or network.
"""

from __future__ import annotations

from types import SimpleNamespace

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.query import wiki, wikichat
from doctalk.query.wikiprompt import build_wiki_messages, format_wiki_citations


# --- pure prompt -----------------------------------------------------------


def _page():
    return SimpleNamespace(
        name="Cake", type="product", path="entities/cake.md",
        claims=[SimpleNamespace(text="Bake for 30 minutes.", sources=["a.pdf p.1"])],
    )


def test_build_wiki_messages_prefers_wiki_and_numbers_excerpts():
    chunk = SimpleNamespace(file="a.pdf", chapter="Sec", page=1, text="bake it")
    msgs = build_wiki_messages("how long?", [_page()], [chunk])
    assert [m["role"] for m in msgs] == ["system", "user"]
    user = msgs[1]["content"]
    assert "SYNTHESIZED KNOWLEDGE" in user and "Cake" in user and "Bake for 30 minutes." in user
    assert "SUPPORTING EXCERPTS" in user and "[1]" in user
    assert "prefer" in msgs[0]["content"].lower()


def test_format_wiki_citations():
    assert format_wiki_citations([_page()]) == [
        {"name": "Cake", "type": "product", "path": "entities/cake.md"}
    ]


# --- page retrieval --------------------------------------------------------


def test_retrieve_pages_filters_to_active_with_claims(db, monkeypatch):
    with session_scope() as s:
        repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                         format="pdf", mime="x", byte_size=1)
        s.flush()
        fid = repo.get_file_id(s, "a" * 64)
        ch = repo.insert_chapters(s, fid, [{"level": 1, "ord": 0, "title": "Sec", "page_start": 1,
                                            "page_end": 1, "source": "outline", "parent_ord": None}])[0]
        repo.insert_chunks(s, fid, [{"chapter_id": ch.id, "page": 1, "ord": 0,
                                     "char_count": 5, "text": "bake"}])
        chunk_id = repo.get_chunks(s, fid)[0].id
        good = repo.create_entity(s, name="Cake", type_="product", norm_key="cake")
        claim = repo.insert_claim(s, entity_id=good.id, file_id=fid, text="Bake for 30 minutes.")
        repo.insert_claim_sources(s, claim.id, [{"file_id": fid, "chunk_id": chunk_id}])
        empty = repo.create_entity(s, name="Empty", type_="concept", norm_key="empty")
        gone = repo.create_entity(s, name="Old", type_="concept", norm_key="old", status="merged_into")
        order = [good.id, empty.id, gone.id]

    monkeypatch.setattr(wiki, "_embed_query", lambda q: [1.0, 0.0])
    from doctalk.vector import store
    monkeypatch.setattr(
        store, "search_entity_names",
        lambda qv, k, type_=None: [{"entity_id": eid, "_distance": 0.1} for eid in order],
    )

    hits = wiki.retrieve_pages("cake baking", k=6)
    assert len(hits) == 1                                  # empty (no claims) + merged are dropped
    assert hits[0].name == "Cake"
    assert hits[0].claims[0].text == "Bake for 30 minutes."
    assert hits[0].claims[0].sources == ["a.pdf p.1"]      # provenance carried through


def test_retrieve_pages_gates_off_topic_pages(db, monkeypatch):
    # An off-topic wiki shouldn't be cited for an unrelated question just because it's all that exists.
    with session_scope() as s:
        repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                         format="pdf", mime="x", byte_size=1)
        s.flush()
        fid = repo.get_file_id(s, "a" * 64)
        ch = repo.insert_chapters(s, fid, [{"level": 1, "ord": 0, "title": "Sec", "page_start": 1,
                                            "page_end": 1, "source": "outline", "parent_ord": None}])[0]
        repo.insert_chunks(s, fid, [{"chapter_id": ch.id, "page": 1, "ord": 0,
                                     "char_count": 4, "text": "salt"}])
        cid = repo.get_chunks(s, fid)[0].id
        near = repo.create_entity(s, name="On", type_="concept", norm_key="on")
        far = repo.create_entity(s, name="Salt", type_="component", norm_key="salt")
        for e in (near, far):
            c = repo.insert_claim(s, entity_id=e.id, file_id=fid, text=f"{e.name} fact.")
            repo.insert_claim_sources(s, c.id, [{"file_id": fid, "chunk_id": cid}])
        ids = {"near": near.id, "far": far.id}

    monkeypatch.setattr(wiki, "_embed_query", lambda q: [1.0, 0.0])
    from doctalk.vector import store
    # near page: distance 0.5 -> score 0.5 (relevant); far page: distance 0.92 -> score 0.08 (off-topic)
    monkeypatch.setattr(
        store, "search_entity_names",
        lambda qv, k, type_=None: [{"entity_id": ids["near"], "_distance": 0.5},
                                   {"entity_id": ids["far"], "_distance": 0.92}],
    )

    hits = wiki.retrieve_pages("unrelated question", k=6)  # default gate = wiki_page_min_score (0.30)
    assert [h.name for h in hits] == ["On"]                # the 0.08 page is gated out
    # an explicit looser gate lets the off-topic page back in
    assert {h.name for h in wiki.retrieve_pages("q", k=6, min_score=0.0)} == {"On", "Salt"}


# --- orchestrator + promote ------------------------------------------------


def test_wikichat_answer_combines_and_saves(db, monkeypatch):
    monkeypatch.setattr(wikichat, "retrieve_pages", lambda q, k=6: [_page()])
    monkeypatch.setattr(wikichat, "retrieve", lambda q, k=6, file_id=None: [])
    import doctalk.models.chat as chatmod
    monkeypatch.setattr(chatmod, "chat", lambda messages, **kw: "Bake for 30 minutes. [wiki: Cake]")

    res = wikichat.answer("how long to bake?", save=True)
    assert "30 minutes" in res["answer"]
    assert res["wiki_citations"][0]["name"] == "Cake"

    saved = res["saved_path"]
    assert saved and saved.startswith("queries/")
    wiki_dir = get_settings().wiki_dir
    page = (wiki_dir / saved).read_text()
    assert "how long to bake?" in page and "[[cake|Cake]]" in page  # links back to the entity page
    assert "## Queries" in (wiki_dir / "index.md").read_text()
    with session_scope() as s:
        assert repo.get_wiki_page_by_path(s, saved).kind == "query"


def test_wikichat_empty_corpus_is_graceful(db, monkeypatch):
    monkeypatch.setattr(wikichat, "retrieve_pages", lambda q, k=6: [])
    monkeypatch.setattr(wikichat, "retrieve", lambda q, k=6, file_id=None: [])
    res = wikichat.answer("anything?")
    assert "don't find" in res["answer"] and res["citations"] == []
