"""Phase 5: one full update of a topic = search + store + Claude check + new-paper report + run log."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from paper_finder import db
from paper_finder.config import OpenAlexSource, Topic
from paper_finder.llm import ClaudeClient
from paper_finder.openalex import OpenAlexClient
from paper_finder.pipeline import JudgeStats, RunResult, judge_topic, run
from paper_finder.pubmed import PubMedClient
from paper_finder.query import years_ago
from paper_finder.report import REPORTS_DIR, Report, new_papers, write_report

FIRST_REPORT_DAYS = 7
# Claude cost guard: a too-broad topic (e.g. a single generic word as an exact phrase) can return thousands of
# papers. Judge at most this many per update; the rest are judged by later updates.
MAX_JUDGE_PER_UPDATE = 300
BROAD_RESULT_WARNING = 1000


@dataclass
class UpdateResult:
    topic: str
    since: date                         # report covers papers first stored on/after this date
    run: RunResult | None = None
    judge: JudgeStats | None = None     # None: no Claude key or nothing stored
    report: Report | None = None
    report_path: Path | None = None
    error: str | None = None            # search failed; nothing stored
    warnings: list[str] = field(default_factory=list)


def report_since(db_path: Path, topic: str, today: date) -> date:
    """Start of the next report: the day of the previous successful update (first time: one week back)."""
    if db_path.exists():
        conn = db.connect(db_path)
        try:
            prev = db.last_run(conn, topic)
        finally:
            conn.close()
        if prev is not None:
            return date.fromisoformat(prev["started_at"][:10])
    return today - timedelta(days=FIRST_REPORT_DAYS)


def update_topic(topic: Topic, journal_groups: dict[str, list[str]], db_path: Path, pubmed: PubMedClient,
                 openalex: OpenAlexClient | None = None, openalex_sources: list[OpenAlexSource] | None = None,
                 llm: ClaudeClient | None = None, years: int = 5, trigger: str = "cli",
                 today: date | None = None, reports_dir: Path = REPORTS_DIR,
                 judge_progress: Callable[[int, int], None] | None = None) -> UpdateResult:
    today = today or date.today()
    started = datetime.now()
    out = UpdateResult(topic=topic.name, since=report_since(db_path, topic.name, today))

    try:
        out.run = run(pubmed, topic, journal_groups, years_ago(today, years), today, db_path=db_path,
                      openalex=openalex, openalex_sources=openalex_sources)
    except Exception as e:  # network / PubMed errors: log the failed run, keep the DB as it was
        out.error = f"{type(e).__name__}: {e}"
        conn = db.connect(db_path)
        try:
            with conn:
                db.record_run(conn, topic.name, started, trigger, None, None, error=out.error)
        finally:
            conn.close()
        return out
    out.warnings += out.run.warnings
    if len(out.run.kept) > BROAD_RESULT_WARNING:
        out.warnings.append(f"검색 결과가 {len(out.run.kept)}건으로 너무 많습니다. 키워드가 너무 넓은지 확인하세요 "
                            "(정확한 문구에 단어 하나만 넣으면 그 단어가 들어간 모든 논문이 걸립니다).")

    if llm is not None and out.run.kept:
        out.judge = judge_topic(llm, topic, db_path, progress=judge_progress, limit=MAX_JUDGE_PER_UPDATE)
        if out.judge.skipped:
            out.warnings.append(f"Claude 판정은 비용 때문에 한 번에 {MAX_JUDGE_PER_UPDATE}건까지만 합니다. "
                                f"남은 {out.judge.skipped}건은 다음 업데이트 때 판정합니다.")
        if out.judge.error:
            out.warnings.append(f"Claude 판정 중단 (남은 논문은 다음 업데이트 때 판정): {out.judge.error}")

    out.report = new_papers(db_path, topic.name, journal_groups, out.since, today)
    out.report_path = write_report(out.report, reports_dir)
    conn = db.connect(db_path)
    try:
        with conn:
            db.record_run(conn, topic.name, started, trigger, len(out.run.kept), out.run.new_count,
                          rejected=out.report.rejected, report_path=str(out.report_path))
    finally:
        conn.close()
    return out
