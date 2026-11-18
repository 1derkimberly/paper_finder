"""Offline tests for dashboard data shaping, marks, export, topic saving, and an app smoke test."""

import json
from datetime import date

import pytest

from paper_finder import db
from paper_finder.config import CONFIG_DIR, Topic, load_topics, save_topic
from paper_finder.dashboard_data import Filters, apply_filters, display_title, load_frame, metrics, per_year, term_frequency
from paper_finder.export import to_bibtex, to_csv
from paper_finder.process import process
from paper_finder.pubmed import Paper

TOPIC = load_topics(CONFIG_DIR / "topics.yaml")["transient_bioelectronics"]
GROUPS = {"tier1_general": ["Nature", "Sci Adv"], "materials_devices": ["Adv Mater"]}

PAPERS = [
    Paper(pmid="1", doi="10.1/a", title="Bioresorbable wireless implant & sensor_x", journal="Nature",
          pub_date="2026-01-07", authors=["Kim, A", "Lee, B"], mesh=["Absorbable Implants"],
          abstract="transient electronics for nerve"),
    Paper(pmid="2", doi="10.1/b", title="Biodegradable piezoelectric sensor", journal="Adv Mater",
          pub_date="2023-05", authors=["Kim, A"], abstract="implantable",
          pub_types=["Journal Article", "Review"]),
    Paper(pmid="3", doi=None, title="Transient electronic stimulator", journal="Some J",
          pub_date="2022", authors=[], abstract="implant"),
]


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    kept, _ = process(PAPERS, TOPIC)
    conn = db.connect(path)
    with conn:
        tid = db.upsert_topic(conn, TOPIC)
        for i, s in enumerate(kept):
            pid, _ = db.upsert_paper(conn, s, today=date(2026, 11, 15) if i == 0 else date(2026, 1, 1))
            db.link(conn, tid, pid, s)
    conn.close()
    return path


def test_load_frame_and_metrics(db_path):
    df = load_frame(db_path, TOPIC.name, GROUPS, today=date(2026, 11, 18))
    assert len(df) == 3
    by_pmid = df.set_index("pmid")
    assert by_pmid.loc["1", "group"] == "tier1_general"
    assert by_pmid.loc["3", "group"] == "기타"
    assert by_pmid.loc["1", "is_new"] and not by_pmid.loc["2", "is_new"]
    assert by_pmid.loc["2", "is_review"] and not by_pmid.loc["1", "is_review"]
    assert display_title(by_pmid.loc["2", "title"], True) == "(review) Biodegradable piezoelectric sensor"
    m = metrics(df)
    assert m == {"total": 3, "new": 1, "top_ratio": pytest.approx(1 / 3)}
    assert per_year(df).set_index("연도")["논문 수"].to_dict() == {2022: 1, 2023: 1, 2026: 1}
    assert term_frequency(df, "mesh").iloc[0].tolist() == ["Absorbable Implants", 1]
    assert "implant" in set(term_frequency(df, "keywords")["용어"])


def test_filters(db_path):
    df = load_frame(db_path, TOPIC.name, GROUPS, today=date(2026, 11, 18))
    assert set(apply_filters(df, Filters(groups=["tier1_general"]))["pmid"]) == {"1"}
    assert set(apply_filters(df, Filters(since=date(2023, 1, 1)))["pmid"]) == {"1", "2"}
    assert set(apply_filters(df, Filters(text="PIEZO"))["pmid"]) == {"2"}
    assert set(apply_filters(df, Filters(new_only=True))["pmid"]) == {"1"}
    assert len(apply_filters(df, Filters(statuses=[""]))) == 3


def test_marks_roundtrip(db_path):
    conn = db.connect(db_path)
    pid = conn.execute("SELECT id FROM papers WHERE pmid='2'").fetchone()["id"]
    with conn:
        db.set_mark(conn, pid, "starred", " good ")
    conn.close()
    df = load_frame(db_path, TOPIC.name, GROUPS)
    row = df.set_index("pmid").loc["2"]
    assert (row["status"], row["note"]) == ("starred", "good")
    assert set(apply_filters(df, Filters(statuses=["starred"]))["pmid"]) == {"2"}
    conn = db.connect(db_path)
    with conn:
        db.set_mark(conn, pid, "", "")
        with pytest.raises(ValueError):
            db.set_mark(conn, pid, "bogus", None)
    assert conn.execute("SELECT COUNT(*) FROM user_marks").fetchone()[0] == 0
    conn.close()


def test_keyword_change_clears_links(db_path):
    conn = db.connect(db_path)
    with conn:
        db.upsert_topic(conn, TOPIC)  # unchanged -> links stay
    assert len(db.topic_papers(conn, TOPIC.name)) == 3
    changed = Topic(TOPIC.name, {**TOPIC.facets, "material": ["bioresorbable"]},
                    TOPIC.exact_phrases, TOPIC.exclude_phrases)
    with conn:
        db.upsert_topic(conn, changed)
    assert db.topic_papers(conn, TOPIC.name) == []
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 3
    conn.close()


def test_exports(db_path):
    df = load_frame(db_path, TOPIC.name, GROUPS)
    csv = to_csv(df).decode("utf-8-sig")
    assert csv.splitlines()[0].startswith("title,article_type,authors,journal")
    assert "Biodegradable piezoelectric sensor,review," in csv
    assert "Kim, A; Lee, B" in csv
    bib = to_bibtex(df)
    assert bib.count("@article{") == 3
    assert "@article{kim2026bioresorbable," in bib
    assert "@article{kim2023biodegradable," in bib
    assert "@article{anon2022transient," in bib
    assert r"implant \& sensor\_x" in bib
    assert "author = {Kim, A and Lee, B}" in bib
    assert "doi = {10.1/a}" in bib


def test_save_topic_roundtrip(tmp_path):
    path = tmp_path / "topics.yaml"
    save_topic(TOPIC, path)
    save_topic(Topic("new_one", {}, ["soft neural probe"]), path)
    loaded = load_topics(path)
    assert loaded[TOPIC.name].to_dict() == TOPIC.to_dict()
    assert loaded["new_one"].exact_phrases == ["soft neural probe"]
    with pytest.raises(ValueError):
        save_topic(Topic("bad name!", {}, ["x"]), path)
    with pytest.raises(ValueError):
        save_topic(Topic("empty", {}, []), path)


def test_app_smoke(db_path, monkeypatch):
    """Render the app against the temp DB with no exceptions."""
    from streamlit.testing.v1 import AppTest

    import paper_finder.config as cfg
    monkeypatch.setattr(cfg, "DEFAULT_DB", db_path)
    at = AppTest.from_file("../app.py", default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title[0].value == "transient_bioelectronics"
    labels = {m.label: m.value for m in at.metric}
    assert labels["총 논문 수"] == "3"
    # switch to card view and back without errors
    at.segmented_control[0].set_value("카드").run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.expander) >= 3
