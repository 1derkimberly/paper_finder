"""Offline tests: keyword suggestions, update + report + run log (Phase 5), toast XML, schedule script."""

from datetime import date
from types import SimpleNamespace

import pytest

from paper_finder import db, update as update_mod
from paper_finder.config import CONFIG_DIR, load_topics
from paper_finder.notify import toast_xml
from paper_finder.pipeline import RunResult
from paper_finder.process import process
from paper_finder.pubmed import Paper
from paper_finder.report import new_papers, to_html, to_markdown, write_report
from paper_finder.schedule import ScheduleError, install_script
from paper_finder.suggest import build_index, related_terms, vocabulary
from paper_finder.update import report_since, update_topic

TOPIC = load_topics(CONFIG_DIR / "topics.yaml")["transient_bioelectronics"]
GROUPS = {"tier1_general": ["Nature", "Sci Adv"]}

TEXTS = [
    "Bioresorbable silicon electronics for implants. Bioresorbable devices dissolve in the body.",
    "Degradable silicon electronics for nerve stimulation. Degradable devices vanish after use.",
    "Resorbable magnesium electronics for implants. Resorbable devices dissolve safely.",
    "Wireless power for implants. Wireless power transfer drives the stimulator.",
]


def test_related_terms_finds_words_used_alike():
    idx = build_index(TEXTS)
    recs = related_terms(idx, ["bioresorbable"], known=["implant"], min_docs=1)
    assert "degradable" in recs and "resorbable" in recs
    assert "bioresorbable" not in recs and "implant" not in recs
    assert related_terms(idx, ["unknownword"]) == []
    assert "wireless power" in vocabulary(idx, min_docs=1) and "devices" in vocabulary(idx)


def _paper(pmid, title, abstract):
    return Paper(pmid=pmid, doi=f"10.1/{pmid}", title=title, journal="Nature", pub_date="2026-01-07",
                 abstract=abstract)


GOOD = _paper("1", "Bioresorbable wireless implant", "transient electronics for nerve stimulation implant")
LASER = _paper("2", "Bioresorbable laser sensor", "biodegradable implantable laser device")


def fake_run_factory(papers):
    def fake_run(client, topic, groups, start, end, db_path=None, **kw):
        kept, excluded = process(papers, topic)
        res = RunResult(kept=kept, excluded=excluded)
        conn = db.connect(db_path)
        with conn:
            tid = db.upsert_topic(conn, topic)
            for s in kept:
                pid, new = db.upsert_paper(conn, s)
                db.link(conn, tid, pid, s)
                res.new_count += new
        conn.close()
        return res
    return fake_run


def test_update_writes_report_and_logs_run(tmp_path, monkeypatch):
    from test_llm import FakeAnthropic, judge_reply
    from paper_finder.llm import ClaudeClient

    path = tmp_path / "t.db"
    monkeypatch.setattr(update_mod, "run", fake_run_factory([GOOD, LASER]))
    llm = ClaudeClient(client=FakeAnthropic(judge_reply()))
    today = date.today()
    out = update_topic(TOPIC, GROUPS, path, pubmed=None, llm=llm, reports_dir=tmp_path / "reports", today=today)
    assert out.error is None and out.judge.judged == 2
    assert out.report.count == 1 and out.report.rejected == 1          # laser paper left out
    assert out.report_path.exists() and out.report_path.with_suffix(".md").exists()
    html = out.report_path.read_text(encoding="utf-8")
    assert "Bioresorbable wireless implant" in html and "laser" not in html and "요약 " in html

    conn = db.connect(path)
    r = db.last_run(conn, TOPIC.name)
    conn.close()
    assert (r["kept"], r["new_count"], r["rejected"], r["trigger"]) == (2, 2, 1, "cli")
    # next report starts at this run's day
    assert report_since(path, TOPIC.name, today) == today


def test_judge_limit_per_update(tmp_path, monkeypatch):
    from test_llm import FakeAnthropic, judge_reply
    from paper_finder.llm import ClaudeClient

    monkeypatch.setattr(update_mod, "run", fake_run_factory([GOOD, LASER]))
    monkeypatch.setattr(update_mod, "MAX_JUDGE_PER_UPDATE", 1)
    fake = FakeAnthropic(judge_reply())
    out = update_topic(TOPIC, GROUPS, tmp_path / "t.db", pubmed=None, llm=ClaudeClient(client=fake),
                       reports_dir=tmp_path)
    assert out.judge.judged == 1 and out.judge.skipped == 1 and any("다음 업데이트" in w for w in out.warnings)
    out = update_topic(TOPIC, GROUPS, tmp_path / "t.db", pubmed=None, llm=ClaudeClient(client=fake),
                       reports_dir=tmp_path)
    assert out.judge.judged == 1 and out.judge.skipped == 0


def test_update_failure_is_logged(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("PubMed down")
    monkeypatch.setattr(update_mod, "run", boom)
    path = tmp_path / "t.db"
    out = update_topic(TOPIC, GROUPS, path, pubmed=None, reports_dir=tmp_path / "reports")
    assert "PubMed down" in out.error and out.report_path is None
    conn = db.connect(path)
    assert db.last_run(conn, TOPIC.name) is None                      # failed runs don't move the report window
    assert "PubMed down" in db.last_run(conn, TOPIC.name, successful=False)["error"]
    conn.close()


def test_report_handles_missing_values(tmp_path, monkeypatch):
    monkeypatch.setattr(update_mod, "run", fake_run_factory([GOOD]))
    out = update_topic(TOPIC, GROUPS, tmp_path / "t.db", pubmed=None, reports_dir=tmp_path)  # no OA url / cites
    assert out.report.count == 1 and "무료 전문" not in out.report_path.read_text(encoding="utf-8")


def test_report_without_new_papers(tmp_path):
    rep = new_papers(tmp_path / "missing.db", TOPIC.name, GROUPS, date(2026, 1, 1), date(2026, 1, 8))
    assert rep.count == 0 and "없습니다" in to_markdown(rep) and "없습니다" in to_html(rep)
    assert write_report(rep, tmp_path).name == "2026-01-08_transient_bioelectronics.html"


def test_toast_xml_escapes_and_links(tmp_path):
    xml = toast_xml("신규 <3>", "a & b", tmp_path / "r.html")
    assert "&lt;3&gt;" in xml and "a &amp; b" in xml and 'launch="file:///' in xml


def test_schedule_script():
    s = install_script("fri", "08:30")
    assert "-DaysOfWeek Friday" in s and "'08:30'" in s and "-StartWhenAvailable" in s
    assert "weekly_update.ps1" in s
    with pytest.raises(ScheduleError):
        install_script("XYZ")


def test_cli_update_and_report(tmp_path, monkeypatch, capsys):
    from paper_finder import cli
    import paper_finder.report as report_mod

    monkeypatch.setattr(update_mod, "run", fake_run_factory([GOOD]))
    monkeypatch.setattr(cli, "update_topic", lambda *a, **k: update_topic(*a, **{**k, "reports_dir": tmp_path}))
    monkeypatch.setattr(cli, "notify", lambda *a, **k: True)
    monkeypatch.setattr(report_mod, "REPORTS_DIR", tmp_path)
    db_path = tmp_path / "t.db"
    rc = cli.main(["--db", str(db_path), "update", "transient_bioelectronics", "--no-llm", "--notify"])
    out = capsys.readouterr().out
    assert rc == 0 and "1 new" in out and "notification shown" in out
    rc = cli.main(["--db", str(db_path), "report", "transient_bioelectronics", "--since", "2020-01-01"])
    assert rc == 0 and "1 new since 2020-01-01" in capsys.readouterr().out


def test_app_recommends_next_terms(tmp_path, monkeypatch):
    """Local suggestions appear under a facet; clicking one adds it to the facet's terms."""
    from streamlit.testing.v1 import AppTest

    import paper_finder.config as cfg
    papers = [_paper(str(i), t, a) for i, (t, a) in enumerate([
        ("Bioresorbable silicon implant", "Bioresorbable silicon electronics for nerve implants dissolve."),
        ("Degradable silicon implant", "Degradable silicon electronics for nerve implants dissolve."),
        ("Resorbable magnesium implant", "Resorbable magnesium electronics for nerve implants dissolve."),
    ], 1)]
    kept, _ = process(papers, TOPIC)
    path = tmp_path / "t.db"
    conn = db.connect(path)
    with conn:
        tid = db.upsert_topic(conn, TOPIC)
        for s in kept:
            pid, _ = db.upsert_paper(conn, s)
            db.link(conn, tid, pid, s)
    conn.close()
    monkeypatch.setattr(cfg, "DEFAULT_DB", path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    at = AppTest.from_file("../app.py", default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    assert not any(b.label == "Claude 판정·요약" for b in at.button)        # judging runs inside 업데이트 실행
    recs = [p for p in at.button_group if "다음 추천" in p.label]
    assert recs, "no recommendation chips"
    material = next(p for p in recs if p.key == "rec_facet_transient_bioelectronics_material")
    term = material.options[0]
    material.set_value(term).run()
    assert not at.exception, [e.value for e in at.exception]
    values = {ms.key: ms.value for ms in at.multiselect}
    assert term in values["facet_transient_bioelectronics_material"]
