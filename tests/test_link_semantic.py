"""link_semantic: aggregation to chapters, threshold, self-exclusion, image bridge, idempotency.

The embedder and the vector search are monkeypatched, so no models load and the search results are
deterministic — the test exercises the linking *logic*, not retrieval quality.
"""

from __future__ import annotations

from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.ingest.dag import StageContext
from doctalk.ingest.stages import link_semantic


def _file(s, content_hash, fmt="pdf"):
    repo.upsert_file(
        s, content_hash=content_hash, path=f"/{content_hash}", filename=f"{content_hash}.{fmt}",
        format=fmt, mime="x", byte_size=1,
    )
    s.flush()  # autoflush is off; assign the id before we read it
    return repo.get_file_id(s, content_hash)


def _chapter(s, file_id, ord_, title):
    rows = [{"level": 1, "ord": ord_, "title": title, "page_start": 1, "page_end": 1,
             "source": "outline", "parent_ord": None}]
    return repo.insert_chapters(s, file_id, rows)[0]


def _setup_corpus(s):
    """File A (two chapters, with text) is the source; File B (two chapters) is the target."""
    a = _file(s, "a" * 64)
    a1 = _chapter(s, a, 0, "A-one")
    a2 = _chapter(s, a, 1, "A-two")
    repo.insert_chunks(s, a, [
        {"chapter_id": a1.id, "page": 1, "ord": 0, "text": "alpha text", "char_count": 10},
        {"chapter_id": a2.id, "page": 1, "ord": 1, "text": "beta text", "char_count": 9},
    ])
    b = _file(s, "b" * 64)
    b_hi = _chapter(s, b, 0, "B-related")     # strong match
    b_lo = _chapter(s, b, 1, "B-unrelated")   # below threshold
    return a, a1.id, a2.id, b, b_hi.id, b_lo.id


def test_links_above_threshold_and_excludes_self(db, monkeypatch):
    with session_scope() as s:
        a, a1, a2, b, b_hi, b_lo = _setup_corpus(s)

    # Same synthetic hits for every source: strong target b_hi, the source-file chapter a1
    # (self for a1, cross for a2), a below-threshold b_lo, and a chapterless hit.
    hits = [
        {"chapter_id": b_hi, "file_id": b, "_distance": 0.2},   # sim 0.80 -> link
        {"chapter_id": a1, "file_id": a, "_distance": 0.3},     # sim 0.70 -> self for a1
        {"chapter_id": b_lo, "file_id": b, "_distance": 0.6},   # sim 0.40 -> below 0.55
        {"chapter_id": -1, "file_id": a, "_distance": 0.05},    # chapterless -> skip
    ]
    monkeypatch.setattr(link_semantic, "embed_passages", lambda texts: [[0.0]] * len(texts))
    monkeypatch.setattr(link_semantic.store, "search_text", lambda qv, k, file_id=None: hits)

    with session_scope() as s:
        link_semantic.run(StageContext("a" * 64, None, s))

    with session_scope() as s:
        rels = repo.get_relations_for_file(s, a)
    pairs = {(r.src_chapter_id, r.dst_chapter_id) for r in rels}
    assert (a1, b_hi) in pairs and (a2, b_hi) in pairs   # both source chapters link to strong target
    assert (a2, a1) in pairs                              # cross-chapter link kept
    assert (a1, a1) not in pairs                          # self excluded
    assert all(r.dst_chapter_id != b_lo for r in rels)   # below-threshold excluded
    assert all(r.dst_chapter_id != -1 for r in rels)     # chapterless excluded
    assert all(r.src_file_id == a for r in rels)


def test_image_links_via_description(db, monkeypatch):
    with session_scope() as s:
        b = _file(s, "b" * 64)
        b_ch = _chapter(s, b, 0, "Target")
        img = _file(s, "c" * 64, fmt="png")
        repo.upsert_image(s, img, vlm_description="a photo of a thing")

    monkeypatch.setattr(link_semantic, "embed_passages", lambda texts: [[0.0]] * len(texts))
    monkeypatch.setattr(
        link_semantic.store, "search_text",
        lambda qv, k, file_id=None: [{"chapter_id": b_ch.id, "file_id": b, "_distance": 0.1}],
    )
    with session_scope() as s:
        link_semantic.run(StageContext("c" * 64, None, s))
        rels = repo.get_relations_for_file(s, repo.get_file_id(s, "c" * 64))
    assert len(rels) == 1
    assert rels[0].src_image_id is not None and rels[0].src_chapter_id is None
    assert rels[0].dst_chapter_id == b_ch.id


def test_rerun_is_idempotent(db, monkeypatch):
    with session_scope() as s:
        a, a1, a2, b, b_hi, b_lo = _setup_corpus(s)
    monkeypatch.setattr(link_semantic, "embed_passages", lambda texts: [[0.0]] * len(texts))
    monkeypatch.setattr(
        link_semantic.store, "search_text",
        lambda qv, k, file_id=None: [{"chapter_id": b_hi, "file_id": b, "_distance": 0.2}],
    )
    with session_scope() as s:
        link_semantic.run(StageContext("a" * 64, None, s))
    with session_scope() as s:
        first = len(repo.get_relations_for_file(s, a))
    with session_scope() as s:
        link_semantic.run(StageContext("a" * 64, None, s))
    with session_scope() as s:
        assert len(repo.get_relations_for_file(s, a)) == first
