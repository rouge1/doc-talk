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


def test_rrf_merge_rewards_agreement_across_arms():
    dense = [10, 20, 30]   # ranks 0,1,2
    lexical = [30, 40]     # ranks 0,1
    fused = retriever._rrf_merge([dense, lexical])
    # 30 appears in both lists -> highest combined score; a doc in one list scores less
    assert max(fused, key=fused.get) == 30
    assert fused[30] > fused[10] and fused[10] > fused[40]
