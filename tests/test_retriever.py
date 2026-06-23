"""Lexical (simple) search ranking and the RRF fusion math. The dense arm needs an embed model +
vector index, so it's exercised via the API tests with monkeypatched retrieval; here we cover the
pure/DB-backed pieces."""

from __future__ import annotations

from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.query import retriever


def _seed_chunks(texts: list[str]):
    with session_scope() as s:
        repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                         format="pdf", mime="x", byte_size=1)
        s.flush()
        fid = repo.get_file_id(s, "a" * 64)
        ch = repo.insert_chapters(s, fid, [{"level": 1, "ord": 0, "title": "Sec", "page_start": 1,
                                            "page_end": 1, "source": "outline", "parent_ord": None}])[0]
        repo.insert_chunks(s, fid, [
            {"chapter_id": ch.id, "page": i + 1, "ord": i, "char_count": len(t), "text": t}
            for i, t in enumerate(texts)
        ])


def _seed_image(filename: str, caption: str, content_hash: str):
    """Seed an image file + its VLM caption (the searchable text a photo gets indexed by)."""
    with session_scope() as s:
        repo.upsert_file(s, content_hash=content_hash, path=f"/{filename}", filename=filename,
                         format="png", mime="image/png", byte_size=1)
        s.flush()
        fid = repo.get_file_id(s, content_hash)
        repo.upsert_image(s, fid, vlm_description=caption)
        return fid


def test_terms_tokenizes_and_dedupes():
    assert retriever._terms("Bluetooth, bluetooth LE!") == ["bluetooth", "le"]
    assert retriever._terms("   ") == []


def test_terms_keeps_versions_and_codes_intact():
    assert retriever._terms("new features in 6.0") == ["new", "features", "in", "6.0"]  # 6.0 not split
    assert retriever._terms("v6.0 L2CAP bluetooth-le") == ["v6.0", "l2cap", "bluetooth-le"]


def test_parse_query_splits_phrases_and_drops_stopwords():
    phrases, words = retriever._parse_query('"new features added in 6.0" advertising')
    assert phrases == ["new features added in 6.0"]      # quoted span kept verbatim
    assert words == ["advertising"]                       # loose word survives
    # stopwords are dropped from loose words, but a phrase keeps its stopwords
    _ph, words2 = retriever._parse_query("the advertising channel in the spec")
    assert "the" not in words2 and "in" not in words2 and "advertising" in words2


def test_parse_query_all_stopwords_falls_back():
    phrases, words = retriever._parse_query("the in of")
    assert phrases == [] and words == ["the", "in", "of"]  # don't return nothing for a stopword query


def test_keyword_search_ranks_by_coverage_then_frequency(db):
    _seed_chunks([
        "A passage mentioning bluetooth once.",            # 1 term, 1 occurrence
        "bluetooth pairing and bluetooth bonding here.",   # 1 term, 2 occurrences
        "bluetooth low energy advertising channels.",      # 2 terms (bluetooth, channels)
        "completely unrelated baking content.",            # no match
    ])
    hits = retriever.keyword_search("bluetooth channels", k=8)
    texts = [h.text for h in hits]
    assert len(hits) == 3                                  # the unrelated chunk is excluded
    assert texts[0].startswith("bluetooth low energy")     # 2-term coverage wins
    assert hits[0].score == 1.0                            # matched both query terms
    assert all(h.source == "keyword" for h in hits)        # provenance tagged
    # of the single-term chunks, the higher frequency ranks above the single occurrence
    assert "bonding" in texts[1] and "once" in texts[2]


def test_keyword_search_empty_query_returns_nothing(db):
    _seed_chunks(["bluetooth here"])
    assert retriever.keyword_search("   ", k=8) == []


def test_keyword_search_phrase_beats_scattered_words(db):
    _seed_chunks([
        "new and improved features were added; see version notes.",  # all words, scattered
        "the new features added in this release are listed below.",  # contains the exact phrase
    ])
    hits = retriever.keyword_search('"new features added"', k=8)
    assert len(hits) == 1                                  # only the chunk with the contiguous phrase
    assert "listed below" in hits[0].text


def test_keyword_search_excludes_toc_chunks(db):
    # A dotted-leader TOC chunk contains the words but is navigation filler — it must be filtered out.
    toc = ("Changes from v5.0 to v5.1 ......................... 375\n"
           "New features ....................................... 376\n"
           "Deprecated features ................................ 377\n"
           "Removed features ................................... 378")
    _seed_chunks([
        toc,
        "Section 10 describes the changes from v5.0 to v5.1 in detail.",  # real prose
    ])
    hits = retriever.keyword_search('"changes from v5.0"', k=8)
    assert len(hits) == 1 and "in detail" in hits[0].text   # only the prose chunk survives


def test_is_boilerplate_flags_page_furniture_only():
    from doctalk import textfilter
    assert textfilter.is_boilerplate("Version Date: 2024-08-27") is True
    assert textfilter.is_boilerplate("\nVersion Date: 2024-08-27") is True
    assert textfilter.is_boilerplate("BLUETOOTH CORE SPECIFICATION Version 6.0 | Vol 1, Part C\nPage 375") is True
    assert textfilter.is_boilerplate("y\nVersion Date: 2024-08-27") is True  # stray 1-char fragment ignored
    # real prose that merely ends with a footer line is NOT boilerplate
    assert textfilter.is_boilerplate("The ACL logical transport carries LMP.\nVersion Date: 2024-08-27") is False


def test_keyword_search_excludes_boilerplate(db):
    _seed_chunks([
        "Version Date: 2024-08-27",                              # pure page footer
        "The advertising channel map version 6.0 is described here.",
    ])
    hits = retriever.keyword_search("version", k=8)
    assert len(hits) == 1 and "advertising" in hits[0].text   # footer dropped, prose kept


def test_keyword_search_matches_version_token(db):
    _seed_chunks([
        "Compliant with version 6 of the zero spec.",      # has 6 and 0 but not the token 6.0
        "This applies to Bluetooth 6.0 devices.",          # has the literal 6.0
    ])
    hits = retriever.keyword_search("6.0", k=8)
    assert len(hits) == 1 and "devices" in hits[0].text    # 6.0 isn't split into 6 / 0


def test_keyword_search_surfaces_image_by_caption(db):
    # A photo is findable by what its caption depicts — the same keyword search that hits documents.
    _seed_chunks(["The advertising channel hopping sequence is defined here."])
    fid = _seed_image("cat.png", "A black and white cat wearing red headphones.", "c" * 64)
    hits = retriever.keyword_search("cat", k=8)
    assert len(hits) == 1
    h = hits[0]
    assert h.kind == "image" and h.file_id == fid     # surfaced as an image hit
    assert h.file == "cat.png" and "cat" in h.text.lower()
    assert h.source == "keyword"


def test_keyword_search_fuses_passages_and_images(db):
    # A query that matches both a passage and a caption returns both kinds in one ranking.
    _seed_chunks(["Headphones reduce ambient noise on the channel."])
    _seed_image("cat.png", "A cat wearing red headphones.", "d" * 64)
    hits = retriever.keyword_search("headphones", k=8)
    kinds = {h.kind for h in hits}
    assert kinds == {"passage", "image"}              # one fused list, both surfaces


def test_keyword_search_scoped_to_file_excludes_images(db):
    # Searching within one document must not pull in corpus-wide photos.
    _seed_chunks(["A cat naps on the spec."])
    _seed_image("cat.png", "A cat wearing red headphones.", "e" * 64)
    with session_scope() as s:
        doc_fid = repo.get_file_id(s, "a" * 64)
    hits = retriever.keyword_search("cat", k=8, file_id=doc_fid)
    assert all(h.kind == "passage" for h in hits)     # no image hits when scoped to a doc


def test_dedupe_image_clusters_keeps_canonical():
    from doctalk.query.retriever import Hit, _dedupe_image_clusters
    passage = Hit(chunk_id=1, file="d.pdf", chapter=None, page=5, text="t", score=0.9)
    dup = Hit(chunk_id=0, file="dup.jpg", chapter=None, page=0, text="c", score=0.8,
              kind="image", file_id=8, cluster_id=2)       # non-canonical, ranks higher
    orig = Hit(chunk_id=0, file="orig.png", chapter=None, page=0, text="c", score=0.7,
               kind="image", file_id=2, cluster_id=2)       # canonical (file_id == cluster_id)
    out = _dedupe_image_clusters([passage, dup, orig])
    assert [h.file for h in out] == ["d.pdf", "orig.png"]   # passage kept; cluster -> canonical member
    assert out[1].file_id == 2 and out[1].score == 0.8      # shown as canonical, ranked by the best member


def test_dedupe_image_clusters_passes_through_unclustered():
    from doctalk.query.retriever import Hit, _dedupe_image_clusters
    a = Hit(chunk_id=0, file="a.png", chapter=None, page=0, text="", score=0.9, kind="image", file_id=3, cluster_id=None)
    b = Hit(chunk_id=0, file="b.png", chapter=None, page=0, text="", score=0.8, kind="image", file_id=4, cluster_id=None)
    out = _dedupe_image_clusters([a, b])
    assert [h.file for h in out] == ["a.png", "b.png"]      # no cluster => both stand alone


def test_keyword_search_collapses_image_clusters(db):
    # Two near-duplicate photos in one cluster collapse to a single hit — the canonical member, even
    # though the duplicate (more "cat" occurrences) ranks higher on its own.
    f_orig = _seed_image("kat.png", "A cat wearing red headphones.", "c" * 64)
    f_dup = _seed_image("kat_dup.jpg", "A cat. A cat. A cat wearing red headphones.", "d" * 64)
    with session_scope() as s:
        repo.upsert_image(s, f_orig, cluster_id=f_orig)     # canonical: file_id == cluster_id
        repo.upsert_image(s, f_dup, cluster_id=f_orig)      # duplicate folds into the same cluster
    imgs = [h for h in retriever.keyword_search("cat", k=8) if h.kind == "image"]
    assert len(imgs) == 1 and imgs[0].file_id == f_orig     # one card, the canonical photo


def test_relevance_floor_drops_weak_filler():
    from doctalk.query.retriever import Hit, apply_relevance_floor
    def h(score, rr=None):
        return Hit(chunk_id=0, file="f", chapter=None, page=1, text="t", score=score, rerank_score=rr)
    # Normal "cats" shape: one strong hit, then a weak tail the ANN returned only to fill k.
    hits = [h(0.5, rr=0.53), h(0.6, rr=0.07), h(0.6, rr=0.005)]   # ranked by rerank, not raw score
    kept = apply_relevance_floor(hits, 0.25, 0.01)
    assert len(kept) == 1 and kept[0].rerank_score == 0.53        # only the relevant chunk survives
    # A broad query where everything is relevant keeps the lot.
    broad = [h(0, rr=0.9), h(0, rr=0.6), h(0, rr=0.4)]
    assert len(apply_relevance_floor(broad, 0.25, 0.01)) == 3
    # No rerank scores: falls back to the raw similarity (well above the absolute min, so relative wins).
    raw = [h(0.8), h(0.7), h(0.1)]
    assert [x.score for x in apply_relevance_floor(raw, 0.25, 0.01)] == [0.8, 0.7]
    # Disabled / empty are no-ops.
    assert apply_relevance_floor(hits, 0, 0) == hits
    assert apply_relevance_floor([], 0.25, 0.01) == []


def test_relevance_floor_absolute_min_handles_flat_rerank():
    from doctalk.query.retriever import Hit, apply_relevance_floor
    def h(rr):
        return Hit(chunk_id=0, file="f", chapter=None, page=1, text="t", score=0.6, rerank_score=rr)
    # The "cat?" pathology: the reranker is flat and unsure (everything near zero), so the relative
    # ratio is meaningless. The absolute min isolates the single best hit; the top always survives
    # even though it's itself below the min.
    flat = [h(0.003), h(0.002), h(0.001), h(0.0001)]
    kept = apply_relevance_floor(flat, 0.25, 0.01)
    assert len(kept) == 1 and kept[0].rerank_score == 0.003
    # keep_top=False (the wiki-pages mode): when even the top is below the bar, drop everything —
    # there's simply no relevant page, so none should reach the LLM.
    assert apply_relevance_floor(flat, 0.25, 0.01, keep_top=False) == []
    # but a genuinely relevant set still survives under keep_top=False.
    strong = [h(0.9), h(0.6), h(0.02)]
    assert len(apply_relevance_floor(strong, 0.25, 0.01, keep_top=False)) == 2


def test_rrf_merge_rewards_agreement_across_arms():
    dense = [10, 20, 30]   # ranks 0,1,2
    lexical = [30, 40]     # ranks 0,1
    fused = retriever._rrf_merge([dense, lexical])
    # 30 appears in both lists -> highest combined score; a doc in one list scores less
    assert max(fused, key=fused.get) == 30
    assert fused[30] > fused[10] and fused[10] > fused[40]
