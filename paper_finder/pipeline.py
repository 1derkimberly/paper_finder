"""search -> fetch -> dedupe -> filter -> score -> store (SPEC §6; expand/enrich come later)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from paper_finder import db
from paper_finder.config import Topic
from paper_finder.process import Scored, process
from paper_finder.pubmed import Paper, PubMedClient
from paper_finder.query import build_query


@dataclass
class GroupResult:
    group: str
    query: str
    count: int
    pmids: list[str]
    warnings: dict | None = None


@dataclass
class RunResult:
    groups: list[GroupResult] = field(default_factory=list)
    unique_pmids: list[str] = field(default_factory=list)
    kept: list[Scored] = field(default_factory=list)
    excluded: list[tuple[Paper, str]] = field(default_factory=list)
    new_count: int = 0


def search_groups(client: PubMedClient, topic: Topic, journal_groups: dict[str, list[str]],
                  start: date, end: date) -> list[GroupResult]:
    results = []
    for group, journals in journal_groups.items():
        q = build_query(topic, journals, start, end)
        count, pmids = client.esearch(q)
        results.append(GroupResult(group, q, count, pmids, getattr(client, "last_warnings", None)))
    return results


def run(client: PubMedClient, topic: Topic, journal_groups: dict[str, list[str]],
        start: date, end: date, db_path: Path | None = None, dry_run: bool = False) -> RunResult:
    res = RunResult(groups=search_groups(client, topic, journal_groups, start, end))
    seen: set[str] = set()
    for g in res.groups:
        for pmid in g.pmids:
            if pmid not in seen:
                seen.add(pmid)
                res.unique_pmids.append(pmid)
    if dry_run or not res.unique_pmids:
        return res

    papers = client.efetch(res.unique_pmids)
    res.kept, res.excluded = process(papers, topic)

    if db_path is not None:
        conn = db.connect(db_path)
        try:
            with conn:
                topic_id = db.upsert_topic(conn, topic)
                for s in res.kept:
                    paper_id, is_new = db.upsert_paper(conn, s)
                    db.link(conn, topic_id, paper_id, s)
                    res.new_count += is_new
        finally:
            conn.close()
    return res
