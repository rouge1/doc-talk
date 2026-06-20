"""Phase 4 synthesis foundation: name normalization, LLM-output parsing, the wiki scaffold, and
the synth_entities stage (extraction persisted with deterministic provenance + idempotency).

The LLM is never called: ``extract.extract_entities`` is monkeypatched in the stage test and
``parse_entities`` is exercised directly on raw strings — so these cover the *logic*, not a model.
"""

from __future__ import annotations

from sqlalchemy import select

from doctalk.db import repo
from doctalk.db.models import ClaimSource
from doctalk.db.session import session_scope
from doctalk.ingest.dag import StageContext
from doctalk.ingest.stages import synth_entities
from doctalk.synth import extract
from doctalk.synth.extract import ExtractedEntity, parse_entities
from doctalk.synth.normalize import norm_key


# --- normalization ---------------------------------------------------------


def test_norm_key_strips_articles_qualifiers_and_collapses_ws():
    assert norm_key("The E0 Procedure") == "e0"
    assert norm_key("  Bluetooth   Low  Energy ") == "bluetooth low energy"
    assert norm_key("E0") == "e0"
    assert norm_key("HCI.") == "hci"
    assert norm_key("the Link Manager") == "link manager"
    assert norm_key("   ") == ""
    # Underscores are separators too: the model writes one concept both ways, so they must key alike
    # (else AFH_channel_map and "AFH channel map" fragment into slug-colliding duplicate pages).
    assert norm_key("AFH_channel_map") == norm_key("AFH channel map") == "afh channel map"
    # But operators are NOT spacing — C[t+1] and C[t-1] stay distinct (only the slugifier conflates).
    assert norm_key("C[t+1]") != norm_key("C[t-1]")


# --- pageworthiness gate ----------------------------------------------------


def test_gate_rejects_data_values():
    from doctalk.synth.gate import is_pageworthy

    for junk in ("0", "0x0009", "0x1F40", "3.2.1", "1,000", "350 ms", "2.4 GHz", "100%",
                 "Section 2.3", "Table 5-1", "Figure 3.2a", "0009", "a1b2", "—", "ab", "  "):
        assert is_pageworthy(junk) is False, junk


def test_gate_keeps_real_entities():
    from doctalk.synth.gate import is_pageworthy

    # "E0" and "AES" are short + hex-shaped but acronym-cased; "cafe" is hex-shaped but digit-free;
    # "h3"/"f4"/"s1" are the spec's named crypto functions (letter+digit, lowercase).
    for name in ("E0", "AES", "HCI", "L2CAP", "Link Manager", "Bluetooth Low Energy",
                 "cake", "cafe", "piconet", "IEEE 802.11", "h3", "f4", "s1"):
        assert is_pageworthy(name) is True, name


# --- extractor output parsing ----------------------------------------------


def test_parse_entities_handles_fences_and_object_wrapper():
    raw = '```json\n{"entities": [{"name": "E0", "type": "protocol", "claims": ["E0 is a cipher."]}]}\n```'
    ents = parse_entities(raw)
    assert len(ents) == 1 and ents[0].name == "E0" and ents[0].type == "protocol"


def test_parse_entities_tolerates_bare_list_and_coerces_unknown_type():
    ents = parse_entities('[{"name": "Foo", "type": "nonsense", "claims": ["c"]}]')
    assert len(ents) == 1 and ents[0].type == "concept"  # unknown -> concept


def test_parse_entities_drops_claimless_and_malformed():
    raw = '{"entities": [{"name": "NoClaims", "claims": []}, {"name": "", "claims": ["x"]}, 7]}'
    assert parse_entities(raw) == []


def test_parse_entities_returns_empty_on_garbage():
    assert parse_entities("not json at all") == []


def test_parse_entities_wraps_bare_string_claims():
    # Found live: the model returned "claims" as one string; iterating it exploded the claim into
    # per-character rows ("S","A","L","T",…) that polluted the salt entity page.
    raw = ('{"entities": [{"name": "SALT", "type": "concept", '
           '"claims": "SALT is the 128-bit value.", "aliases": "the salt"}]}')
    ents = parse_entities(raw)
    assert len(ents) == 1
    assert ents[0].claims == ["SALT is the 128-bit value."]
    assert ents[0].aliases == ["the salt"]


def test_parse_entities_gates_data_values():
    # A sloppy model emits hex literals as entities — the gate drops them at the parse boundary.
    raw = ('{"entities": [{"name": "0x0009", "type": "concept", "claims": ["A PSM value."]},'
           '{"name": "E0", "type": "protocol", "claims": ["E0 is a cipher."]}]}')
    ents = parse_entities(raw)
    assert [e.name for e in ents] == ["E0"]


def test_parse_entities_salvages_prose_wrapped_json():
    # A small model under load ignores JSON mode and pads with prose — salvage the embedded payload.
    raw = ('Here is the breakdown of the passage:\n'
           '{"entities": [{"name": "E0", "type": "protocol", "claims": ["E0 is a cipher."]}]}\n'
           'Hope this helps!')
    ents = parse_entities(raw)
    assert len(ents) == 1 and ents[0].name == "E0"


def test_parse_entities_pure_prose_still_empty():
    assert parse_entities("Here is a summary and breakdown of the provided text.") == []


# --- TOC/index noise filtering + windowing ---------------------------------


def test_is_noise_chunk_flags_table_of_contents():
    toc = ("7.3.41 Read Link Supervision Timeout command ................ 2098\n"
           "7.3.42 Write Link Supervision Timeout command ............... 2100\n"
           "7.3.43 Read Number Of Supported IAC command ................. 2102\n"
           "7.3.44 Read Current IAC LAP command ......................... 2103")
    assert synth_entities._is_noise_chunk(toc) is True
    prose = ("The E0 cipher is a stream cipher used by Bluetooth for link encryption. "
             "It is keyed by the link key and the device clock.")
    assert synth_entities._is_noise_chunk(prose) is False


def test_windows_split_consecutively_and_drop_noise():
    class C:
        def __init__(self, i, text):
            self.id, self.text = i, text
    toc = "A ...... 1\nB ...... 2\nC ...... 3\nD ...... 4"
    chunks = [C(0, "alpha prose one"), C(1, toc), C(2, "beta prose two"), C(3, "gamma prose three")]
    wins = synth_entities._windows(chunks, 2)
    flat = [c.id for w in wins for c in w]
    assert 1 not in flat                     # the TOC chunk was filtered out
    assert flat == [0, 2, 3] and len(wins) == 2  # consecutive windows of size 2


# --- wiki scaffold ---------------------------------------------------------


def test_wiki_scaffold_creates_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCTALK_WIKI_DIR", str(tmp_path / "wiki"))
    from doctalk.config import get_settings

    get_settings.cache_clear()
    from doctalk.synth import wikirepo

    root = wikirepo.ensure_scaffold()
    assert (root / "index.md").exists() and (root / "log.md").exists()
    for sub in ("entities", "concepts", "topics", "queries"):
        assert (root / sub).is_dir()
    wikirepo.append_log("## [2026-06-07] ingest | test")
    assert "ingest | test" in (root / "log.md").read_text()
    h = wikirepo.write_page("entities/foo.md", "# Foo\n")
    assert (root / "entities" / "foo.md").read_text() == "# Foo\n" and len(h) == 64
    get_settings.cache_clear()


# --- the synth_entities stage ----------------------------------------------


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
        {"chapter_id": ch.id, "page": 1, "ord": 0, "char_count": 50,
         "text": "The E0 cipher is a stream cipher used by Bluetooth."},
        {"chapter_id": ch.id, "page": 1, "ord": 1, "char_count": 40,
         "text": "Pairing uses the Link Manager component."},
    ])
    return fid


_EXTRACTED = [
    ExtractedEntity("E0 cipher", "component", ["E0"], ["E0 is a stream cipher."]),
    ExtractedEntity("Link Manager", "component", [], ["Link Manager handles pairing."]),
    ExtractedEntity("Bluetooth SIG", "organization", [], ["The SIG maintains the spec."]),  # not in text
]


def _run(monkeypatch):
    monkeypatch.setattr(extract, "extract_entities",
                        lambda passage, model=None, timeout=None: _EXTRACTED)
    with session_scope() as s:
        synth_entities.run(StageContext("a" * 64, None, s))


def test_synth_entities_persists_with_provenance(db, monkeypatch, stub_resolve):
    with session_scope() as s:
        fid = _doc(s)
    _run(monkeypatch)

    with session_scope() as s:
        ents = {e.norm_key: e for e in repo.get_entities(s)}
        assert set(ents) == {"e0 cipher", "link manager", "bluetooth sig"}

        e0 = ents["e0 cipher"]
        claims = repo.get_claims_for_entity(s, e0.id)
        assert len(claims) == 1 and claims[0].file_id == fid
        # provenance points at the chunk that actually names "e0 cipher" (chunk ord 0), not null
        srcs = s.scalars(
            select(ClaimSource.chunk_id).where(ClaimSource.claim_id == claims[0].id)
        ).all()
        assert srcs and all(cid is not None for cid in srcs)

        # an entity absent from the text still records a claim, with a null-chunk (file-level) source
        sig = ents["bluetooth sig"]
        sig_claim = repo.get_claims_for_entity(s, sig.id)[0]
        sig_srcs = s.scalars(
            select(ClaimSource.chunk_id).where(ClaimSource.claim_id == sig_claim.id)
        ).all()
        assert sig_srcs == [None]

        assert all(e.source_count == 1 for e in ents.values())


def test_synth_entities_is_idempotent(db, monkeypatch, stub_resolve):
    with session_scope() as s:
        _doc(s)
    _run(monkeypatch)
    _run(monkeypatch)  # re-synth: clears + reinserts, no duplication

    with session_scope() as s:
        ents = repo.get_entities(s)
        assert len(ents) == 3
        total_claims = sum(len(repo.get_claims_for_entity(s, e.id)) for e in ents)
        assert total_claims == 3
        for e in ents:
            assert len(repo.get_mentions_for_file(s, repo.get_file_id(s, "a" * 64))) == 3
            assert e.source_count == 1


def test_synth_entities_full_sweep_merges_across_windows(db, monkeypatch, stub_resolve):
    # Same entity surfaces in two windows with different facts/aliases -> one merged candidate.
    monkeypatch.setenv("DOCTALK_SYNTH_WINDOW_CHUNKS", "2")
    from doctalk.config import get_settings
    get_settings.cache_clear()

    with session_scope() as s:
        repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                         format="pdf", mime="x", byte_size=1)
        s.flush()
        fid = repo.get_file_id(s, "a" * 64)
        ch = repo.insert_chapters(s, fid, [{"level": 1, "ord": 0, "title": "Sec", "page_start": 1,
                                            "page_end": 1, "source": "outline", "parent_ord": None}])[0]
        repo.insert_chunks(s, fid, [
            {"chapter_id": ch.id, "page": 1, "ord": 0, "char_count": 30,
             "text": "The E0 cipher is defined here."},
            {"chapter_id": ch.id, "page": 1, "ord": 1, "char_count": 30, "text": "E0 cipher keystream."},
            {"chapter_id": ch.id, "page": 2, "ord": 2, "char_count": 30, "text": "The E0 cipher again."},
            {"chapter_id": ch.id, "page": 2, "ord": 3, "char_count": 30, "text": "More E0 cipher detail."},
        ])

    def fake_extract(passage, model=None, timeout=None):  # window A "concept", window B "component"
        if "defined here" in passage:
            return [ExtractedEntity("E0 cipher", "concept", ["E0"], ["E0 is a stream cipher."])]
        return [ExtractedEntity("E0 cipher", "component", ["E0 algorithm"], ["E0 keys off the link key."])]
    monkeypatch.setattr(extract, "extract_entities", fake_extract)

    with session_scope() as s:
        synth_entities.run(StageContext("a" * 64, None, s))

    with session_scope() as s:
        ents = repo.get_entities(s)
        assert len(ents) == 1                       # merged across windows, not duplicated
        e = ents[0]
        assert e.type == "component"                # a specific type supersedes the catch-all "concept"
        claims = repo.get_claims_for_entity(s, e.id)
        assert len(claims) == 2                      # union of both windows' claims
        srcs = set()
        for c in claims:
            srcs.update(
                s.scalars(select(ClaimSource.chunk_id).where(ClaimSource.claim_id == c.id)).all()
            )
        assert len([x for x in srcs if x is not None]) == 4   # provenance spans chunks from both windows


def test_synth_entities_sweep_survives_a_failed_window(db, monkeypatch, stub_resolve):
    # A timeout/error on one window must not abort the sweep — the other windows still persist.
    monkeypatch.setenv("DOCTALK_SYNTH_WINDOW_CHUNKS", "1")
    from doctalk.config import get_settings
    get_settings.cache_clear()

    with session_scope() as s:
        repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                         format="pdf", mime="x", byte_size=1)
        s.flush()
        fid = repo.get_file_id(s, "a" * 64)
        ch = repo.insert_chapters(s, fid, [{"level": 1, "ord": 0, "title": "Sec", "page_start": 1,
                                            "page_end": 1, "source": "outline", "parent_ord": None}])[0]
        repo.insert_chunks(s, fid, [
            {"chapter_id": ch.id, "page": 1, "ord": 0, "char_count": 20, "text": "Good window text."},
            {"chapter_id": ch.id, "page": 1, "ord": 1, "char_count": 20, "text": "BOOM window text."},
        ])

    def flaky(passage, model=None, timeout=None):
        if "BOOM" in passage:
            raise TimeoutError("timed out")
        return [ExtractedEntity("Survivor", "concept", [], ["A surviving claim."])]
    monkeypatch.setattr(extract, "extract_entities", flaky)

    with session_scope() as s:
        synth_entities.run(StageContext("a" * 64, None, s))

    with session_scope() as s:
        ents = repo.get_entities(s)
        assert [e.name for e in ents] == ["Survivor"]   # the good window persisted despite the failure


def _salience_doc(s, n_chunks):
    repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                     format="pdf", mime="x", byte_size=1)
    s.flush()
    fid = repo.get_file_id(s, "a" * 64)
    ch = repo.insert_chapters(s, fid, [{"level": 1, "ord": 0, "title": "Sec", "page_start": 1,
                                        "page_end": 1, "source": "outline", "parent_ord": None}])[0]
    repo.insert_chunks(s, fid, [
        {"chapter_id": ch.id, "page": 1, "ord": i, "char_count": 20, "text": f"w{i} prose text"}
        for i in range(n_chunks)
    ])


def _salience_extract(passage, model=None, timeout=None):
    # w0+w1: recurring 1-claim entity. w2: single-window 2-claim. w3: single-window 1-claim
    # (noise). w4: single-window 1-claim that norm-key-matches a pre-existing entity.
    out = []
    if "w0" in passage or "w1 " in passage:
        out.append(ExtractedEntity("Recurring", "concept", [], ["Recurring is seen twice."]))
    if "w2" in passage:
        out.append(ExtractedEntity("Rich", "concept", [], ["Rich claim one.", "Rich claim two."]))
    if "w3" in passage:
        out.append(ExtractedEntity("Noise", "concept", [], ["A one-off noise claim."]))
    if "w4" in passage:
        out.append(ExtractedEntity("Known", "concept", [], ["Known gains a source."]))
    return out


def test_salience_drops_one_window_one_claim_candidates(db, monkeypatch, stub_resolve):
    monkeypatch.setenv("DOCTALK_SYNTH_WINDOW_CHUNKS", "1")  # 6 chunks -> 6 windows (>= gate floor)
    from doctalk.config import get_settings
    get_settings.cache_clear()

    with session_scope() as s:
        _salience_doc(s, 6)
        repo.create_entity(s, name="Known", type_="concept", norm_key="known")
    monkeypatch.setattr(extract, "extract_entities", _salience_extract)

    with session_scope() as s:
        ctx = StageContext("a" * 64, None, s)
        synth_entities.run(ctx)
        assert ctx.scratch["synth_entities_low_salience"] == 1  # only "Noise"

    with session_scope() as s:
        names = {e.name for e in repo.get_entities(s)}
        assert "Noise" not in names                      # one window + one claim + unknown -> dropped
        assert {"Recurring", "Rich", "Known"} <= names   # recurs / says more / already known -> kept


def test_salience_skipped_on_small_documents(db, monkeypatch, stub_resolve):
    # 2 windows < synth_salience_min_windows (5): "appeared once" carries no signal, keep everything.
    monkeypatch.setenv("DOCTALK_SYNTH_WINDOW_CHUNKS", "2")
    from doctalk.config import get_settings
    get_settings.cache_clear()

    with session_scope() as s:
        _salience_doc(s, 4)
    monkeypatch.setattr(extract, "extract_entities", _salience_extract)

    with session_scope() as s:
        ctx = StageContext("a" * 64, None, s)
        synth_entities.run(ctx)
        assert ctx.scratch["synth_entities_low_salience"] == 0

    with session_scope() as s:
        assert "Noise" in {e.name for e in repo.get_entities(s)}


def test_empty_sweep_refuses_to_wipe_prior_synthesis(db, monkeypatch, stub_resolve):
    # Found live: a re-synth whose sweep extracts nothing (model regression / all windows failed)
    # used to clear the file's prior claims+mentions and get marked done. It must fail instead.
    import pytest

    with session_scope() as s:
        _doc(s)
    _run(monkeypatch)  # establish a real synthesis first

    monkeypatch.setattr(extract, "extract_entities",
                        lambda passage, model=None, timeout=None: [])
    with session_scope() as s:
        with pytest.raises(RuntimeError, match="refusing to clear"):
            synth_entities.run(StageContext("a" * 64, None, s))

    with session_scope() as s:  # prior synthesis intact
        fid = repo.get_file_id(s, "a" * 64)
        assert len(repo.get_mentions_for_file(s, fid)) == 3


def test_empty_sweep_on_fresh_source_is_benign(db, monkeypatch, stub_resolve):
    with session_scope() as s:
        _doc(s)
    monkeypatch.setattr(extract, "extract_entities",
                        lambda passage, model=None, timeout=None: [])
    with session_scope() as s:
        ctx = StageContext("a" * 64, None, s)
        synth_entities.run(ctx)  # nothing prior, no failures -> no raise
        assert ctx.scratch["synth_entities"] == 0


def test_sample_chunks_is_evenly_spaced():
    items = list(range(100))
    sample = synth_entities._sample_chunks(items, 10)
    assert len(sample) == 10 and sample[0] == 0 and sample == sorted(sample)
    assert synth_entities._sample_chunks(items[:5], 10) == items[:5]  # fewer than limit -> all
