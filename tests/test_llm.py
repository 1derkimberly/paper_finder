"""Offline tests for Phase 4 (Claude API) with a fake Anthropic client."""

import json
import re
from datetime import date
from types import SimpleNamespace

import pytest

from paper_finder import db
from paper_finder.config import CONFIG_DIR, load_topics
from paper_finder.dashboard_data import Filters, apply_filters, load_frame
from paper_finder.export import to_csv
from paper_finder.llm import ClaudeClient, LLMError, PaperText, judge_papers, suggest_terms
from paper_finder.pipeline import judge_topic
from paper_finder.process import process
from paper_finder.pubmed import Paper

TOPIC = load_topics(CONFIG_DIR / "topics.yaml")["transient_bioelectronics"]
GROUPS = {"tier1_general": ["Nature", "Sci Adv"]}


def _response(payload, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason,
                           content=[SimpleNamespace(type="thinking", thinking=""),
                                    SimpleNamespace(type="text", text=json.dumps(payload))])


class FakeAnthropic:
    """Records requests; `reply(kwargs)` builds each response."""

    def __init__(self, reply):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self._reply = reply

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._reply(kwargs)


def judge_reply(off_topic_words=("laser",)):
    def reply(kwargs):
        user = kwargs["messages"][0]["content"]
        out = []
        for pid, body in re.findall(r'<paper id="(\d+)">(.*?)</paper>', user, re.S):
            bad = any(w in body.lower() for w in off_topic_words)
            out.append({"id": pid, "relevant": not bad, "reason": "레이저 가공" if bad else "생분해 소자",
                        "summary": f"요약 {pid}"})
        return _response({"papers": out})
    return reply


def test_request_shape_and_judge():
    fake = FakeAnthropic(judge_reply())
    llm = ClaudeClient(client=fake)
    papers = [PaperText("1", "Bioresorbable sensor", "transient electronics implant", "Nature"),
              PaperText("2", "Laser drilling via transient electronic excitation", "", "Sci Adv")]
    v = judge_papers(llm, TOPIC, papers)
    assert v["1"].relevant and not v["2"].relevant
    assert v["1"].summary == "요약 1"
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert "Facet 'material'" in call["system"]
    assert "(no abstract available)" in call["messages"][0]["content"]


def test_refusal_and_bad_json_raise():
    llm = ClaudeClient(client=FakeAnthropic(lambda k: _response({}, stop_reason="refusal")))
    with pytest.raises(LLMError):
        judge_papers(llm, TOPIC, [PaperText("1", "t", "a")])
    broken = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="{not json")])
    llm = ClaudeClient(client=FakeAnthropic(lambda k: broken))
    with pytest.raises(LLMError):
        judge_papers(llm, TOPIC, [PaperText("1", "t", "a")])


def test_suggest_terms_dedupes_against_current():
    payload = {
        "facets": [{"name": "Material", "terms": ["bioresorbable", "Biodegradable polymer", "biodegradable polymer"]},
                   {"name": "power source", "terms": ["wireless power", " ", "NFC"]}],
        "exact_phrases": ["transient bioelectronics", "soft bioresorbable implant"],
        "exclude_phrases": ["transient absorption", "pump-probe"],
        "notes": "확인 필요",
    }
    llm = ClaudeClient(model="claude-sonnet-5-5", client=FakeAnthropic(lambda k: _response(payload)))
    s = suggest_terms(llm, "녹는 전자소자", TOPIC)
    assert s.facets == {"material": ["Biodegradable polymer"], "power_source": ["wireless power", "NFC"]}
    assert "transient absorption" not in s.exclude_phrases and "pump-probe" in s.exclude_phrases
    assert s.notes == "확인 필요"
    with pytest.raises(ValueError):
        suggest_terms(llm, "  ", None)


@pytest.fixture
def db_path(tmp_path):
    papers = [
        Paper(pmid="1", doi="10.1/a", title="Bioresorbable wireless implant", journal="Nature",
              pub_date="2026-01-07", abstract="transient electronics for nerve stimulation implant"),
        Paper(pmid="2", doi="10.1/b", title="Bioresorbable laser sensor", journal="Sci Adv",
              pub_date="2025-05", abstract="biodegradable implantable laser device"),
    ]
    kept, _ = process(papers, TOPIC)
    assert len(kept) == 2
    path = tmp_path / "t.db"
    conn = db.connect(path)
    with conn:
        tid = db.upsert_topic(conn, TOPIC)
        for s in kept:
            pid, _ = db.upsert_paper(conn, s, today=date(2026, 1, 1))
            db.link(conn, tid, pid, s)
    conn.close()
    return path


def test_judge_topic_stores_and_hides(db_path):
    fake = FakeAnthropic(judge_reply())
    llm = ClaudeClient(client=fake)
    stats = judge_topic(llm, TOPIC, db_path, batch_size=1)
    assert (stats.pending, stats.judged, stats.rejected, stats.error) == (2, 2, 1, None)
    assert len(fake.calls) == 2
    # already judged -> nothing to do
    assert judge_topic(llm, TOPIC, db_path).pending == 0
    assert judge_topic(llm, TOPIC, db_path, force=True).judged == 2

    df = load_frame(db_path, TOPIC.name, GROUPS)
    row = df.set_index("pmid").loc["2"]
    assert row["llm_relevant"] is False and row["llm_reason"] == "레이저 가공" and row["summary"] == "요약 2"
    assert set(apply_filters(df, Filters(hide_rejected=True))["pmid"]) == {"1"}
    assert len(apply_filters(df, Filters())) == 2
    csv = to_csv(df).decode("utf-8-sig")
    assert "summary_ko" in csv.splitlines()[0] and ",no," in csv

    # re-running the search keeps verdicts (link() only updates relevance)
    conn = db.connect(db_path)
    with conn:
        kept, _ = process([Paper(pmid="2", doi="10.1/b", title="Bioresorbable laser sensor", journal="Sci Adv",
                                 pub_date="2025-05", abstract="biodegradable implantable laser device")], TOPIC)
        pid, _ = db.upsert_paper(conn, kept[0])
        db.link(conn, db.upsert_topic(conn, TOPIC), pid, kept[0])
    conn.close()
    assert load_frame(db_path, TOPIC.name, GROUPS).set_index("pmid").loc["2", "llm_relevant"] is False


def test_app_hides_rejected(db_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    import paper_finder.config as cfg
    judge_topic(ClaudeClient(client=FakeAnthropic(judge_reply())), TOPIC, db_path)
    monkeypatch.setattr(cfg, "DEFAULT_DB", db_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    at = AppTest.from_file("../app.py", default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    assert {m.label: m.value for m in at.metric}["총 논문 수"] == "1"
    toggle = next(t for t in at.toggle if "관련 없다고" in t.label)
    toggle.set_value(True).run()
    assert not at.exception, [e.value for e in at.exception]
    assert {m.label: m.value for m in at.metric}["총 논문 수"] == "2"
    at.segmented_control[0].set_value("카드").run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("관련 없음" in w.value for w in at.warning)


def test_app_suggestion_flow(db_path, monkeypatch):
    """Suggested terms land in the keyword editor (not saved until '키워드 저장')."""
    from streamlit.testing.v1 import AppTest

    import paper_finder.config as cfg
    import paper_finder.llm as llm_mod
    monkeypatch.setattr(cfg, "DEFAULT_DB", db_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    monkeypatch.setattr(llm_mod, "suggest_terms", lambda llm, desc, cur: llm_mod.Suggestion(
        facets={"material": ["silk fibroin"], "power": ["wireless power transfer"]},
        exact_phrases=["transient bioelectronics"], exclude_phrases=["pump-probe"], notes="메모"))
    next_calls = []
    monkeypatch.setattr(llm_mod, "next_terms",
                        lambda llm, topic, facet, typed: next_calls.append((facet, tuple(typed))) or ["PLGA"])
    at = AppTest.from_file("../app.py", default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    at.button(key="suggest_transient_bioelectronics").click().run()
    assert not at.exception, [e.value for e in at.exception]
    at.button(key="apply_transient_bioelectronics").click().run()
    assert not at.exception, [e.value for e in at.exception]
    values = {ms.label: ms.value for ms in at.multiselect}
    assert "silk fibroin" in values["material"] and "bioresorbable" in values["material"]
    assert values["power"] == ["wireless power transfer"]
    assert "pump-probe" in values["제외 문구 (수집 후 제목·초록에서 걸러냄)"]
    # edited facets get Claude's "next term" chips (one call per term set, cached across reruns)
    rec = next(p for p in at.button_group if p.key == "rec_facet_transient_bioelectronics_material")
    assert "Claude" in rec.label and rec.options == ["PLGA"]
    n_calls = len(next_calls)
    rec.set_value("PLGA").run()
    assert not at.exception, [e.value for e in at.exception]
    assert "PLGA" in {ms.key: ms.value for ms in at.multiselect}["facet_transient_bioelectronics_material"]
    assert ("material", tuple(TOPIC.facets["material"] + ["silk fibroin"])) in next_calls
    assert len(next_calls) == n_calls + 1          # new term set -> one more call
    assert load_topics(CONFIG_DIR / "topics.yaml")["transient_bioelectronics"].to_dict() == TOPIC.to_dict()


def test_judge_topic_stops_on_error_and_keeps_saved(db_path):
    calls = {"n": 0}

    def reply(kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            return _response({}, stop_reason="refusal")
        return judge_reply()(kwargs)

    stats = judge_topic(ClaudeClient(client=FakeAnthropic(reply)), TOPIC, db_path, batch_size=1)
    assert stats.judged == 1 and stats.error
    remaining = judge_topic(ClaudeClient(client=FakeAnthropic(judge_reply())), TOPIC, db_path)
    assert remaining.pending == 1


def test_missing_ids_are_retried(db_path):
    def reply(kwargs):
        return _response({"papers": [{"id": "999", "relevant": True, "reason": "", "summary": ""}]})

    stats = judge_topic(ClaudeClient(client=FakeAnthropic(reply)), TOPIC, db_path)
    assert stats.judged == 0 and stats.pending == 2
    assert judge_topic(ClaudeClient(client=FakeAnthropic(judge_reply())), TOPIC, db_path).pending == 2
