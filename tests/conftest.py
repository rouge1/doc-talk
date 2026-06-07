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

    Base.metadata.create_all(session_mod.get_engine())
    yield
    session_mod.reset_engine()
    get_settings.cache_clear()
