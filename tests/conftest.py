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
    yield d


@pytest.fixture(autouse=True)
def _clear_streamlit_cache():
    """AppTest runs share st.cache_data within one process; each test uses its own temp DB."""
    import streamlit as st
    st.cache_data.clear()
    yield
