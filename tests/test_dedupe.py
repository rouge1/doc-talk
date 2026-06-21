"""wiki-dedupe: the read-only duplicates triage and its plan cache.

Scoring all the near-duplicate pairs reads the whole entity-vector table plus a co-mention query per
entity — seconds of work that reruns on every maintenance-page visit. ``plan_duplicates`` memoizes the
result on a cheap ``(active-count, max-entity-id, max-mention-id)`` fingerprint; these tests pin that the
cache returns the same object on a repeat call and recomputes once the inputs (or an explicit
invalidation) move. Model-free, like the rest of the synth suite.
"""

from __future__ import annotations

from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.synth import dedupe


def _file(s):
    repo.upsert_file(s, content_hash="a" * 64, path="/a", filename="a.pdf",
                     format="pdf", mime="x", byte_size=1)
    s.flush()
    return repo.get_file_id(s, "a" * 64)


def _pair(s, fid):
    """Two same-type entities whose norm_keys share a token -> one near-duplicate candidate pair."""
    repo.create_entity(s, name="BR/EDR Controller", type_="component", norm_key="br edr controller")
    repo.create_entity(s, name="BR/EDR/LE Controller", type_="component", norm_key="br edr le controller")
    s.flush()


def test_plan_is_memoized_on_repeat_call(db):
    with session_scope() as s:
        fid = _file(s)
        _pair(s, fid)
        first = dedupe.plan_duplicates(s)
        second = dedupe.plan_duplicates(s)
        # Same fingerprint -> the very same object handed back, no rescore.
        assert second is first
        assert first["total"] == 1


def test_new_entity_busts_the_fingerprint(db):
    with session_scope() as s:
        fid = _file(s)
        _pair(s, fid)
        first = dedupe.plan_duplicates(s)
        # A new entity moves max-entity-id, so the cached plan must not be reused.
        repo.create_entity(s, name="LE Controller", type_="component", norm_key="le controller")
        s.flush()
        second = dedupe.plan_duplicates(s)
        assert second is not first
        assert second["total"] >= first["total"]


def test_invalidate_forces_recompute(db):
    with session_scope() as s:
        fid = _file(s)
        _pair(s, fid)
        first = dedupe.plan_duplicates(s)
        dedupe.invalidate_plan_cache()
        second = dedupe.plan_duplicates(s)
        # Equal in value, but a distinct object — proof it was recomputed, not served from cache.
        assert second is not first
        assert second == first
