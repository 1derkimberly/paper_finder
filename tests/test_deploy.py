"""Deployed mode: login roles (simulated with PAPER_FINDER_FAKE_LOGIN), per-user marks, DB-built report,
and (with TEST_DATABASE_URL) the same storage code on Postgres."""

import os
from datetime import date, datetime

import pytest

from conftest import FIXTURE_TOPICS
from paper_finder import db
from paper_finder.config import load_topics
from paper_finder.process import process
from paper_finder.pubmed import Paper
from paper_finder.report import last_update_report
from paper_finder.topics_store import TopicStore

TOPIC = load_topics(FIXTURE_TOPICS)["transient_bioelectronics"]
GROUPS = {"tier1_general": ["Nature", "Sci Adv"]}


def _fill(target):
    papers = [Paper(pmid=str(i), doi=f"10.1/{i}", title=f"Bioresorbable wireless implant {i}", journal="Nature",
                    pub_date="2026-01-07", abstract="transient electronics for nerve stimulation implant")
              for i in (1, 2)]
    kept, _ = process(papers, TOPIC)
    conn = db.connect(target)
    with conn:
        tid = db.upsert_topic(conn, TOPIC)
        ids = []
        for s in kept:
            pid, _ = db.upsert_paper(conn, s)
            db.link(conn, tid, pid, s)
            ids.append(pid)
        db.record_run(conn, TOPIC.name, datetime.now(), "cli", 2, 2)
        if not db.is_postgres(target):
            db.save_topic_def(conn, TOPIC, owner="")      # a public (admin) topic for the app tests
    conn.close()
    return ids


def _storage_roundtrip(target):
    ids = _fill(target)
    conn = db.connect(target)
    with conn:
        db.set_mark(conn, ids[0], "read", "memo", user="a@x.com")
        db.set_mark(conn, ids[0], "starred", None, user="b@x.com")
    marks = {u: {r["id"]: r["mark_status"] for r in db.topic_papers(conn, TOPIC.name, u)}
             for u in ("a@x.com", "b@x.com", "")}
    assert marks["a@x.com"][ids[0]] == "read" and marks["b@x.com"][ids[0]] == "starred"
    assert marks[""][ids[0]] is None
    assert db.last_run(conn, TOPIC.name)["kept"] == 2
    assert db.data_stamp(conn)
    with conn:
        db.save_topic_def(conn, TOPIC)
    assert db.load_topic_defs(conn)[TOPIC.name].to_dict() == TOPIC.to_dict()
    with conn:
        assert db.delete_topic(conn, TOPIC.name) == 1          # paper 2 unmarked -> gone; paper 1 marked -> kept
        assert db.remove_topic_def(conn, TOPIC.name)
    conn.close()


def test_storage_sqlite(tmp_path):
    _storage_roundtrip(tmp_path / "t.db")


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="set TEST_DATABASE_URL to an empty Postgres DB")
def test_storage_postgres():
    url = os.environ["TEST_DATABASE_URL"]
    conn = db.connect(url)
    with conn:
        conn.script("DROP TABLE IF EXISTS topic_papers, user_marks, runs, topic_defs, topics, papers CASCADE")
    conn.close()
    db._READY.discard(url)
    _storage_roundtrip(url)


def test_topic_store_starts_empty(tmp_path, monkeypatch):
    # treat a SQLite path as "in DB" to exercise the seeding logic without Postgres
    store = TopicStore(tmp_path / "t.db", FIXTURE_TOPICS)
    monkeypatch.setattr(TopicStore, "in_db", property(lambda self: True))
    assert store.load() == {}                                               # starts empty (YAML not copied)
    store.save(TOPIC, owner="")
    assert list(store.load()) == ["transient_bioelectronics"]
    store.remove("transient_bioelectronics")
    assert store.load() == {}


def test_last_update_report_from_db(tmp_path):
    path = tmp_path / "t.db"
    _fill(path)
    name, html = last_update_report(path, TOPIC.name, GROUPS)
    assert name == f"{date.today()}_transient_bioelectronics.html" and b"Bioresorbable wireless implant" in html
    assert last_update_report(tmp_path / "none.db", TOPIC.name, GROUPS) is None


def _app(tmp_path, monkeypatch, login, admins="", key=""):
    from streamlit.testing.v1 import AppTest

    import paper_finder.config as cfg
    path = tmp_path / "t.db"
    if not path.exists():
        _fill(path)
    monkeypatch.setattr(cfg, "DEFAULT_DB", path)
    monkeypatch.setenv("PAPER_FINDER_FAKE_LOGIN", login)
    monkeypatch.setenv("ADMIN_EMAILS", admins)
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    at = AppTest.from_file("../app.py", default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _suggest(monkeypatch, name="zinc_battery"):
    """Fake Claude suggestion; names get a counter suffix after the first (t1, t1_2, ...)."""
    import paper_finder.llm as llm_mod
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    n = {"i": 0}

    def fake(llm, desc, cur):
        n["i"] += 1
        return llm_mod.Suggestion(facets={"material": ["biodegradable"], "power": ["battery"]},
                                  exact_phrases=["transient battery"], exclude_phrases=[], notes="",
                                  name=name if n["i"] == 1 else f"{name}_{n['i']}")
    monkeypatch.setattr(llm_mod, "suggest_terms", fake)


def _create(at, desc="녹는 배터리"):
    at.sidebar.selectbox[0].set_value("＋ 새 주제 만들기").run()
    at.text_area(key="nt_desc").input(desc).run()
    next(b for b in at.button if b.label == "Claude 용어 제안 받기").click().run()
    next(b for b in at.button if b.label == "이 키워드로 주제 만들기").click().run()
    assert not at.exception, [e.value for e in at.exception]


def test_visitor_tastes_everything_with_limits(tmp_path, monkeypatch):
    at = _app(tmp_path, monkeypatch, "")
    labels = [b.label for b in at.button]
    assert "업데이트 실행" not in labels and "구글로 로그인" in labels            # public topic: read-only
    assert any("맛보기" in i.value for i in at.info)
    assert "⚙ 관리 (유지 보수)" not in at.sidebar.selectbox[0].options
    assert any("검색 키워드 보기" in e.label for e in at.expander)
    assert {m.label: m.value for m in at.metric}["총 논문 수"] == "2"

    _suggest(monkeypatch)
    at = AppTest_rerun(at)
    _create(at)
    conn = db.connect(tmp_path / "t.db")
    row = conn.execute("SELECT name, owner, title FROM topic_defs WHERE owner LIKE 'anon:%'").fetchone()
    conn.close()
    assert row["title"] == "zinc_battery" and row["name"].startswith("zinc_battery-")
    assert at.title[0].value == "Zinc Battery" or "zinc" in at.title[0].value.lower()
    assert "업데이트 실행" in [b.label for b in at.button]                     # own topic: manageable
    assert any("최근 1년" in c.value for c in at.caption)                         # 1-year search limit
    # quota: a second topic is refused
    at.sidebar.selectbox[0].set_value("＋ 새 주제 만들기").run()
    assert any("더 만들 수 없습니다" in w.value for w in at.warning)


def AppTest_rerun(at):
    return at.run()


def test_signed_in_user_keeps_own_marks(tmp_path, monkeypatch):
    at = _app(tmp_path, monkeypatch, "a@x.com", admins="boss@x.com")
    assert "업데이트 실행" not in [b.label for b in at.button] and "로그아웃" in [b.label for b in at.button]
    sel = next(s for s in at.selectbox if s.key and s.key.startswith("st_"))
    sel.set_value("read").run()
    assert not at.exception, [e.value for e in at.exception]
    conn = db.connect(tmp_path / "t.db")
    rows = conn.execute("SELECT user_id, status FROM user_marks").fetchall()
    conn.close()
    assert [(r["user_id"], r["status"]) for r in rows] == [("a@x.com", "read")]
    at2 = _app(tmp_path, monkeypatch, "c@x.com", admins="boss@x.com")
    assert all(s.value == "" for s in at2.selectbox if s.key and s.key.startswith("st_"))


def test_user_topic_limit_and_privacy(tmp_path, monkeypatch):
    at = _app(tmp_path, monkeypatch, "a@x.com", admins="boss@x.com")
    _suggest(monkeypatch, "t1")
    for _ in range(3):
        _create(at.run())
    at.sidebar.selectbox[0].set_value("＋ 새 주제 만들기").run()
    assert any("더 만들 수 없습니다" in w.value for w in at.warning)
    other = _app(tmp_path, monkeypatch, "c@x.com", admins="boss@x.com")
    assert other.sidebar.selectbox[0].options == ["transient_bioelectronics", "＋ 새 주제 만들기"]


def test_daily_budget_blocks_visitors_not_admin(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    _fill(path)
    conn = db.connect(path)
    with conn:
        db.record_usage(conn, "anon:x", "visitor", "judge", "claude-haiku-5-5", 1, 1, 5.0)
    conn.close()
    at = _app(tmp_path, monkeypatch, "", key="test-key-not-used")
    at.sidebar.selectbox[0].set_value("＋ 새 주제 만들기").run()
    assert any("사용 한도" in w.value for w in at.warning)
    admin = _app(tmp_path, monkeypatch, "boss@x.com", admins="boss@x.com", key="test-key-not-used")
    admin.sidebar.selectbox[0].set_value("＋ 새 주제 만들기").run()
    assert not any("사용 한도" in w.value for w in admin.warning)


def test_admin_maintenance_page(tmp_path, monkeypatch):
    at = _app(tmp_path, monkeypatch, "Boss@X.com", admins="boss@x.com")
    assert "업데이트 실행" in [b.label for b in at.button]
    assert any("편집" in e.label for e in at.expander)
    assert not any(m.value == "None" for m in at.markdown)          # no stray magic output
    at.sidebar.selectbox[0].set_value("⚙ 관리 (유지 보수)").run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title[0].value == "관리"
    at.number_input[0].set_value(2.5)
    next(b for b in at.button if b.label == "저장").click().run()
    assert not at.exception, [e.value for e in at.exception]
    from paper_finder.quota import load_limits
    assert load_limits(tmp_path / "t.db").daily_budget_usd == 2.5
    # deleting a topic from the admin page needs its ID typed in
    assert at.button(key="admin_delete").disabled
    at.text_input(key="admin_delete_confirm").input("transient_bioelectronics").run()
    assert not at.button(key="admin_delete").disabled
    next(b for b in at.button if "정리" in b.label).click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("지웠습니다" in s.value for s in at.success)


def test_cleanup_removes_expired_visitor_topics(tmp_path):
    from datetime import timedelta
    from paper_finder.config import Topic
    from paper_finder.maintenance import cleanup
    path = tmp_path / "t.db"
    _fill(path)
    conn = db.connect(path)
    with conn:
        db.save_topic_def(conn, Topic("v-1", {}, ["x y"]), owner="anon:abc", title="v")
        db.save_topic_def(conn, Topic("u-1", {}, ["x y"]), owner="a@x.com", title="u")
    conn.close()
    assert cleanup(path, 24).visitor_topics == 0
    st = cleanup(path, 24, now=datetime.now() + timedelta(hours=25))
    assert st.visitor_topics == 1
    conn = db.connect(path)
    assert {r["name"] for r in db.topic_def_rows(conn)} == {"u-1", "transient_bioelectronics"}
    conn.close()


def test_empty_service_starts_with_new_topic(tmp_path, monkeypatch):
    """No topics yet: everyone lands on the welcome / new-topic screen and can still log in."""
    from streamlit.testing.v1 import AppTest

    import paper_finder.config as cfg
    monkeypatch.setattr(cfg, "DEFAULT_DB", tmp_path / "empty.db")
    monkeypatch.setenv("PAPER_FINDER_FAKE_LOGIN", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    at = AppTest.from_file("../app.py", default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.sidebar.selectbox[0].options == ["＋ 새 주제 만들기"]
    assert at.title[0].value == "Paper Finder"
    assert "구글로 로그인" in [b.label for b in at.button]
