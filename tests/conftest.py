import shutil
from pathlib import Path

import pytest

import paper_finder.config as cfg

# Tests use their own copy of the topic definitions, never the user's config/topics.yaml (which the dashboard edits).
FIXTURE_TOPICS = Path(__file__).parent / "fixtures" / "topics.yaml"
_REAL_CONFIG = cfg.CONFIG_DIR


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path_factory, monkeypatch):
    """Every test (and every AppTest run of app.py) sees a temp config dir: fixture topics + real journal lists."""
    d = tmp_path_factory.mktemp("config")
    shutil.copy(FIXTURE_TOPICS, d / "topics.yaml")
    for name in ("journals.yaml", "openalex_sources.yaml"):
        shutil.copy(_REAL_CONFIG / name, d / name)
    monkeypatch.setattr(cfg, "CONFIG_DIR", d)
    # Never touch the real service: a DATABASE_URL / ADMIN_EMAILS in .env must not reach tests (an empty
    # environment variable wins over .env in load_settings).
    for var in ("DATABASE_URL", "ADMIN_EMAILS", "PAPER_FINDER_FAKE_LOGIN"):
        monkeypatch.setenv(var, "")
    monkeypatch.delenv("PAPER_FINDER_FAKE_LOGIN")
    # Second guard: refuse any Postgres connection except the explicit TEST_DATABASE_URL.
    import os
    from paper_finder import db
    real_connect = db.connect

    def guarded(target):
        if db.is_postgres(target) and target != os.environ.get("TEST_DATABASE_URL"):
            raise RuntimeError("tests must not connect to a real Postgres database")
        return real_connect(target)
    monkeypatch.setattr(db, "connect", guarded)
    yield d


@pytest.fixture(autouse=True)
def _clear_streamlit_cache():
    """AppTest runs share st.cache_data within one process; each test uses its own temp DB."""
    import streamlit as st
    st.cache_data.clear()
    yield
