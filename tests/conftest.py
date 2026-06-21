"""Point the truth store at a throwaway SQLite file and create the schema from the models.

This keeps the Phase 0 idempotency tests fast and dependency-free while production runs on MySQL
(the schema is intentionally portable). DB URL is injected via DOCTALK_DB_URL before any engine
is built, then caches are reset.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCTALK_DB_URL", f"sqlite:///{tmp_path / 'truth.db'}")
    monkeypatch.setenv("DOCTALK_LANCE_DIR", str(tmp_path / "lance"))  # isolate the derived index
    monkeypatch.setenv("DOCTALK_FIGURES_DIR", str(tmp_path / "figures"))  # isolate extracted rasters
    monkeypatch.setenv("DOCTALK_WIKI_DIR", str(tmp_path / "wiki"))  # isolate the synthesis wiki repo

    from doctalk.config import get_settings
    from doctalk.db import session as session_mod
    from doctalk.db.models import Base

    get_settings.cache_clear()
    session_mod.reset_engine()
    from doctalk.vector import store as vstore

    vstore.reset_db_cache()  # the lancedb connection is cached; without this, vectors stored by an
    # earlier test leak into this test's (re-used) entity ids and skew resolver scores
    from doctalk.synth import dedupe

    dedupe.invalidate_plan_cache()  # the duplicates plan is memoized by a (count, max-id) fingerprint;
    # two tests' fresh DBs can collide on that, so clear it between tests

    Base.metadata.create_all(session_mod.get_engine())
    yield
    session_mod.reset_engine()
    vstore.reset_db_cache()
    dedupe.invalidate_plan_cache()
    get_settings.cache_clear()


@pytest.fixture
def stub_resolve(monkeypatch):
    """Deterministic, model-free entity resolver for synth tests: a constant name-embedding (so the
    embed signal is fixed) and no LLM adjudication (DEFERs fall straight to the unresolved queue).
    Lets the resolution *logic* — block/score/decide/merge — be tested without loading a model."""
    from doctalk.synth import resolve

    monkeypatch.setattr(resolve, "_embed", lambda text: [1.0, 0.0, 0.0])
    monkeypatch.setattr(resolve, "_adjudicate", lambda *a, **k: None)
