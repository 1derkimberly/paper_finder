import pytest


@pytest.fixture(autouse=True)
def _clear_streamlit_cache():
    """AppTest runs share st.cache_data within one process; each test uses its own temp DB."""
    import streamlit as st
    st.cache_data.clear()
    yield
