"""Offline tests for OpenAlex parsing, query building, supplement filtering and enrichment."""

from datetime import date

import pytest

from paper_finder import db
from paper_finder.config import CONFIG_DIR, OpenAlexSource, load_openalex_sources, load_topics
from paper_finder.openalex import OpenAlexClient, OpenAlexError, _abstract, work_to_paper
from paper_finder.pipeline import enrich_db, run
from paper_finder.process import matches_topic
from paper_finder.pubmed import Paper
from paper_finder.query import openalex_query

TOPIC = load_topics(CONFIG_DIR / "topics.yaml")["transient_bioelectronics"]
SRC = OpenAlexSource("Nat Electron", "tier1_bme", "2520-1131", "S1")


def work(wid, doi, title, abstract=None, source="S1", pmid=None, cited=5, wtype="article"):
    inv = None
    if abstract:
        inv = {}
        for i, w in enumerate(abstract.split()):
            inv.setdefault(w, []).append(i)
    return {
        "id": f"https://openalex.org/{wid}", "doi": f"https://doi.org/{doi}" if doi else None,
        "title": title, "type": wtype, "publication_date": "2025-03-04", "cited_by_count": cited,
        "open_access": {"oa_url": None}, "best_oa_location": {"pdf_url": f"https://oa/{wid}.pdf"},
        "primary_location": {"source": {"id": f"https://openalex.org/{source}"}},
        "authorships": [{"author": {"display_name": "Kento Yamagishi"}}],
        "abstract_inverted_index": inv,
        "ids": {"pmid": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}"} if pmid else {},
    }


class FakeOpenAlex:
    def __init__(self, search_results=(), by_doi=None, fail_search=False):
        self.search_results = list(search_results)
        self.by_doi = by_doi or {}
        self.fail_search = fail_search
        self.queries = []

    def search(self, query, source_ids, start, end):
        self.queries.append((query, source_ids))
        if self.fail_search:
            raise OpenAlexError("429 throttled")
        return self.search_results

    def works_by_doi(self, dois):
        return {d: self.by_doi[d] for d in dois if d in self.by_doi}

    def source_id_by_issn(self, issn):
        return "S1"


class FakePubMed:
    def __init__(self, papers):
        self.papers = papers

    def esearch(self, term):
        return len(self.papers), [p.pmid for p in self.papers]

    def efetch(self, pmids):
        return self.papers


def test_abstract_reconstruction_and_author_format():
    assert _abstract({"world": [1], "hello": [0, 2]}) == "hello world hello"
    p = work_to_paper(work("W1", "10.1/X", "T", "a b", pmid="123", wtype="review"), "Nat Electron")
    assert (p.doi, p.pmid, p.source, p.openalex_id) == ("10.1/x", "123", "openalex", "W1")
    assert p.authors == ["Yamagishi, Kento"]
    assert p.pub_types == ["Review"] and p.oa_url == "https://oa/W1.pdf" and p.cited_by == 5


def test_openalex_query_shape():
    q = openalex_query(TOPIC)
    assert q.startswith("((bioresorbable OR biodegradable")
    assert '"neural interface"' in q
    assert ') OR ("transient electronics" OR' in q
    assert "[tiab]" not in q


def test_matches_topic_is_stricter_than_stemming():
    good = Paper(pmid=None, title="Bioresorbable wireless stimulator", abstract="")
    stemmed_only = Paper(pmid=None, title="Transients in wireless sensors", abstract="electronic noise")
    assert matches_topic(good, TOPIC)
    assert not matches_topic(stemmed_only, TOPIC)  # 'transients' is not 'transient'


def test_load_openalex_sources():
    names = {s.name: s for s in load_openalex_sources(CONFIG_DIR / "openalex_sources.yaml")}
    assert names["Nat Electron"].group == "tier1_bme"
    assert names["npj Flex Electron"].openalex_id


def test_run_with_supplement_and_enrichment(tmp_path):
    pub = [Paper(pmid="1", doi="10.1/a", title="Bioresorbable wireless implant", journal="Nature",
                 pub_date="2025-01-01", abstract="sensor")]
    oa = FakeOpenAlex(
        search_results=[
            # duplicate of the PubMed paper -> PubMed record wins
            work("W1", "10.1/a", "Bioresorbable wireless implant", "sensor"),
            # abstract present, all facets -> kept
            work("W2", "10.1/b", "Biodegradable electronics patch", "an implantable sensor"),
            # abstract present but only stemmed match -> dropped
            work("W3", "10.1/c", "Transients in displays", "electronic noise in sensors"),
            # no abstract (publisher withholds), title has a topic term -> kept
            work("W4", "10.1/d", "A bioresorbable stimulation system"),
            # no abstract, no exact topic term in title -> dropped
            work("W5", "10.1/e", "MEMS that can dissolve in the body"),
        ],
        by_doi={"10.1/a": work("W1", "10.1/a", "x", cited=42)},
    )
    path = tmp_path / "t.db"
    res = run(FakePubMed(pub), TOPIC, {"tier1_bme": ["Nat Electron"]}, date(2021, 1, 1), date(2026, 1, 1),
              db_path=path, openalex=oa, openalex_sources=[SRC])
    assert oa.queries and oa.queries[0][1] == ["S1"]
    dois = {s.paper.doi: s.paper for s in res.kept}
    assert set(dois) == {"10.1/a", "10.1/b", "10.1/d"}
    assert dois["10.1/a"].source == "pubmed" and dois["10.1/a"].cited_by == 42
    assert res.openalex_found == 5 and res.openalex_added == 2
    assert (res.enrich.matched, res.enrich.with_doi) == (3, 3)

    conn = db.connect(path)
    rows = {r["doi"]: r for r in conn.execute("SELECT * FROM papers")}
    assert rows["10.1/a"]["pmid"] == "1" and rows["10.1/a"]["cited_by"] == 42
    assert rows["10.1/d"]["pmid"] is None and rows["10.1/d"]["source"] == "openalex"
    assert rows["10.1/d"]["journal"] == "Nat Electron"
    conn.close()


def test_supplement_skipped_when_group_not_selected(tmp_path):
    oa = FakeOpenAlex()
    run(FakePubMed([]), TOPIC, {"tier1_general": ["Nature"]}, date(2021, 1, 1), date(2026, 1, 1),
        openalex=oa, openalex_sources=[SRC])
    assert oa.queries == []


def test_search_failure_keeps_pubmed_results(tmp_path):
    pub = [Paper(pmid="1", doi="10.1/a", title="Bioresorbable wireless implant", abstract="sensor")]
    res = run(FakePubMed(pub), TOPIC, {"tier1_bme": ["Nat Electron"]}, date(2021, 1, 1), date(2026, 1, 1),
              db_path=tmp_path / "t.db", openalex=FakeOpenAlex(fail_search=True), openalex_sources=[SRC])
    assert len(res.kept) == 1 and res.warnings and "보완 검색 실패" in res.warnings[0]


def test_openalex_record_never_overwrites_pubmed(tmp_path):
    from paper_finder.process import score
    path = tmp_path / "t.db"
    conn = db.connect(path)
    with conn:
        pm = Paper(pmid="1", doi="10.1/a", title="PubMed title", mesh=["M"])
        db.upsert_paper(conn, score(pm, TOPIC))
        oa = work_to_paper(work("W1", "10.1/a", "OpenAlex title", cited=7), "J")
        db.upsert_paper(conn, score(oa, TOPIC))
    r = conn.execute("SELECT * FROM papers").fetchone()
    assert (r["title"], r["pmid"], r["mesh"], r["cited_by"], r["openalex_id"]) == \
        ("PubMed title", "1", '["M"]', 7, "W1")
    conn.close()


def test_enrich_db(tmp_path):
    from paper_finder.process import score
    path = tmp_path / "t.db"
    conn = db.connect(path)
    with conn:
        for i, d in enumerate(["10.1/a", "10.1/b", None]):
            db.upsert_paper(conn, score(Paper(pmid=str(i), doi=d, title=f"t{i}"), TOPIC))
    conn.close()
    stats = enrich_db(FakeOpenAlex(by_doi={"10.1/a": work("W1", "10.1/a", "x", cited=3)}), path)
    assert (stats.matched, stats.with_doi) == (1, 2) and stats.rate == 0.5


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body = status, body or {}
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.text = str(self._body)

    def json(self):
        return self._body


class _Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        return self.responses.pop(0)


def test_client_honors_retry_after_and_api_key():
    sleeps = []
    sess = _Session([_Resp(429, {"message": "busy"}, {"Retry-After": "30"}),
                     _Resp(200, {"results": [], "meta": {"next_cursor": None}})])
    c = OpenAlexClient(api_key="k", mailto="m@x", session=sess, sleep=sleeps.append)
    assert c.search("q", ["S1"], date(2021, 1, 1), date(2022, 1, 1)) == []
    assert 30.0 in sleeps
    assert sess.calls[0]["api_key"] == "k" and sess.calls[0]["mailto"] == "m@x"


def test_client_gives_up_with_error():
    sess = _Session([_Resp(429, {"message": "busy"}, {"Retry-After": "1"})] * 3)
    c = OpenAlexClient(session=sess, sleep=lambda s: None, max_retries=2)
    with pytest.raises(OpenAlexError, match="busy"):
        c.works_by_doi(["10.1/a"])
