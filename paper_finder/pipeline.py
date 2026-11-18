"""search -> fetch -> (OpenAlex supplement) -> dedupe -> filter -> score -> enrich -> store (SPEC §6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from paper_finder import db
from paper_finder.config import OpenAlexSource, Topic
from paper_finder.openalex import OpenAlexClient, OpenAlexError, enrichment_from, work_to_paper
from paper_finder.process import Scored, matches_topic, process, title_has_any_term
from paper_finder.pubmed import Paper, PubMedClient
from paper_finder.query import build_query, openalex_query


@dataclass
class GroupResult:
    group: str
    query: str
    count: int
    pmids: list[str]
    warnings: dict | None = None


@dataclass
class EnrichStats:
    with_doi: int = 0
    matched: int = 0

    @property
    def rate(self) -> float:
        return self.matched / self.with_doi if self.with_doi else 0.0


@dataclass
class RunResult:
    groups: list[GroupResult] = field(default_factory=list)
    unique_pmids: list[str] = field(default_factory=list)
    openalex_found: int = 0          # supplement search hits (before topic re-check)
    openalex_added: int = 0          # supplement papers kept after dedupe/filter, not in PubMed results
    kept: list[Scored] = field(default_factory=list)
    excluded: list[tuple[Paper, str]] = field(default_factory=list)
    new_count: int = 0
    enrich: EnrichStats | None = None
    warnings: list[str] = field(default_factory=list)


def search_groups(client: PubMedClient, topic: Topic, journal_groups: dict[str, list[str]],
                  start: date, end: date) -> list[GroupResult]:
    results = []
    for group, journals in journal_groups.items():
        q = build_query(topic, journals, start, end)
        count, pmids = client.esearch(q)
        results.append(GroupResult(group, q, count, pmids, getattr(client, "last_warnings", None)))
    return results


def openalex_supplement(oa: OpenAlexClient, topic: Topic, sources: list[OpenAlexSource],
                        start: date, end: date) -> tuple[int, list[Paper]]:
    """Search OpenAlex in journals PubMed covers poorly. Returns (raw hits, papers passing topic check)."""
    ids = {}
    for s in sources:
        sid = s.openalex_id or oa.source_id_by_issn(s.issn)
        ids[sid] = s.name
    works = oa.search(openalex_query(topic), list(ids), start, end)
    papers = []
    for w in works:
        src = ((w.get("primary_location") or {}).get("source") or {}).get("id", "")
        p = work_to_paper(w, ids.get(src.rsplit("/", 1)[-1], ""))
        if not p.title:
            continue
        if matches_topic(p, topic) if p.abstract else title_has_any_term(p, topic):
            papers.append(p)
    return len(works), papers


def enrich_papers(oa: OpenAlexClient, papers: list[Paper]) -> EnrichStats:
    """Fill openalex_id / cited_by / oa_url in place by DOI. Papers that came from OpenAlex already have them."""
    stats = EnrichStats()
    todo = [p for p in papers if p.doi and not p.openalex_id]
    found = oa.works_by_doi([p.doi for p in todo]) if todo else {}
    for p in papers:
        if not p.doi:
            continue
        stats.with_doi += 1
        if p.openalex_id:
            stats.matched += 1
            continue
        w = found.get(p.doi.lower())
        if w:
            e = enrichment_from(w)
            p.openalex_id, p.cited_by, p.oa_url = e.openalex_id, e.cited_by, e.oa_url
            stats.matched += 1
    return stats


def run(client: PubMedClient, topic: Topic, journal_groups: dict[str, list[str]],
        start: date, end: date, db_path: Path | None = None, dry_run: bool = False,
        openalex: OpenAlexClient | None = None,
        openalex_sources: list[OpenAlexSource] | None = None) -> RunResult:
    res = RunResult(groups=search_groups(client, topic, journal_groups, start, end))
    seen: set[str] = set()
    for g in res.groups:
        for pmid in g.pmids:
            if pmid not in seen:
                seen.add(pmid)
                res.unique_pmids.append(pmid)
    if dry_run:
        return res

    papers = client.efetch(res.unique_pmids) if res.unique_pmids else []

    sources = [s for s in openalex_sources or [] if s.group in journal_groups]
    if openalex and sources:
        try:
            res.openalex_found, extra = openalex_supplement(openalex, topic, sources, start, end)
            papers = papers + extra  # PubMed first: dedupe keeps the PubMed record
        except OpenAlexError as e:
            res.warnings.append(f"OpenAlex 보완 검색 실패 (PubMed 결과만 저장): {e}")

    res.kept, res.excluded = process(papers, topic)
    res.openalex_added = sum(1 for s in res.kept if s.paper.source == "openalex")

    if openalex and res.kept:
        try:
            res.enrich = enrich_papers(openalex, [s.paper for s in res.kept])
        except OpenAlexError as e:
            res.warnings.append(f"OpenAlex 인용 수 조회 실패 (나중에 `enrich`로 다시 시도): {e}")

    if db_path is not None and res.kept:
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


def enrich_db(oa: OpenAlexClient, db_path: Path) -> EnrichStats:
    """Refresh citation counts / OA links for every paper with a DOI in the database."""
    conn = db.connect(db_path)
    try:
        rows = db.papers_with_doi(conn)
        found = oa.works_by_doi([r["doi"] for r in rows])
        stats = EnrichStats(with_doi=len(rows))
        with conn:
            for r in rows:
                w = found.get(r["doi"].lower())
                if w:
                    e = enrichment_from(w)
                    db.set_enrichment(conn, r["id"], e.openalex_id, e.cited_by, e.oa_url)
                    stats.matched += 1
        return stats
    finally:
        conn.close()
