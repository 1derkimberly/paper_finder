"""Live PubMed check of SPEC §10 reference data. Run with `pytest -m network`."""

from datetime import date

import pytest

from paper_finder.config import CONFIG_DIR, load_journals, load_settings, load_topics
from paper_finder.pipeline import run
from paper_finder.pubmed import PubMedClient

pytestmark = pytest.mark.network

MUST_FIND = {
    "10.1038/s41586-025-09874-3",  # Nature 2026, soft biodegradable implants
    "10.1126/science.adz5300",      # Science 2026, in-body networking
    "10.1038/s41551-025-01579-2",  # Nat Biomed Eng 2026, triboelectric nerve block
    "10.1038/s41467-026-74248-w",  # Nat Commun 2026, lactate sensors
}
MUST_NOT_KEEP = "10.1126/sciadv.adv4436"  # laser drilling via transient electronic excitation


@pytest.fixture(scope="module")
def tier1_result():
    topic = load_topics(CONFIG_DIR / "topics.yaml")["transient_bioelectronics"]
    journals = load_journals(CONFIG_DIR / "journals.yaml")
    groups = {g: journals[g] for g in ("tier1_general", "tier1_bme")}
    s = load_settings()
    client = PubMedClient(api_key=s.ncbi_api_key, email=s.ncbi_email)
    return run(client, topic, groups, date(2021, 11, 18), date(2026, 11, 18), db_path=None)


def test_reference_papers_found(tier1_result):
    dois = {s.paper.doi for s in tier1_result.kept}
    assert MUST_FIND <= dois, MUST_FIND - dois


def test_false_positive_not_kept(tier1_result):
    assert MUST_NOT_KEEP not in {s.paper.doi for s in tier1_result.kept}


def test_result_size_is_focused(tier1_result):
    # SPEC §3: ~31 hits in the Cowork test; allow drift but catch query blow-ups.
    assert 20 <= len(tier1_result.kept) <= 80
