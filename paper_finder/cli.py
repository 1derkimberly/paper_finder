"""Command-line entry point: `python -m paper_finder ...`."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import yaml

from paper_finder import db
from paper_finder.config import (CONFIG_DIR, DEFAULT_DB, load_journals, load_openalex_sources, load_settings,
                                 load_topics)
from paper_finder.llm import ClaudeClient, LLMError, suggest_terms
from paper_finder.openalex import OpenAlexClient, OpenAlexError
from paper_finder.pipeline import JudgeStats, enrich_db, judge_topic, run
from paper_finder.dashboard_data import display_title
from paper_finder.pubmed import PubMedClient, is_review
from paper_finder.query import years_ago


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="paper_finder", description="Recent top-journal papers by topic.")
    ap.add_argument("--topics", type=Path, default=CONFIG_DIR / "topics.yaml")
    ap.add_argument("--journals", type=Path, default=CONFIG_DIR / "journals.yaml")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="search PubMed and store results")
    s.add_argument("topic")
    s.add_argument("--groups", nargs="+", help="journal groups to use (default: all)")
    s.add_argument("--years", type=int, default=5, help="look-back window in years (default 5)")
    s.add_argument("--start", type=_parse_date, help="YYYY-MM-DD (overrides --years)")
    s.add_argument("--end", type=_parse_date, help="YYYY-MM-DD (default today)")
    s.add_argument("--dry-run", action="store_true", help="only print queries and hit counts")
    s.add_argument("--show-query", action="store_true", help="print the full query per group")
    s.add_argument("--no-openalex", action="store_true",
                   help="skip OpenAlex supplement search and citation lookup")
    s.add_argument("--quiet", action="store_true", help="do not print the paper list")
    s.add_argument("--no-llm", action="store_true",
                   help="skip the Claude relevance check (it runs only when ANTHROPIC_API_KEY is set)")

    ls = sub.add_parser("list", help="list stored papers for a topic")
    ls.add_argument("topic")
    ls.add_argument("--limit", type=int, default=50)

    j = sub.add_parser("judge", help="Claude relevance verdict + one-line Korean summary for stored papers")
    j.add_argument("topic")
    j.add_argument("--force", action="store_true", help="re-judge papers that already have a verdict")

    e = sub.add_parser("expand", help="ask Claude for facet / exact / exclude term suggestions")
    e.add_argument("description", nargs="?", default="", help="free-text description of the topic")
    e.add_argument("--topic", help="existing topic to extend (its terms are sent and not repeated)")

    sub.add_parser("enrich", help="refresh OpenAlex citation counts / OA links for all stored papers")
    sub.add_parser("topics", help="list configured topics and journal groups")
    return ap


def _openalex_client() -> OpenAlexClient:
    s = load_settings()
    return OpenAlexClient(api_key=s.openalex_api_key, mailto=s.openalex_mailto)


def _claude_client() -> ClaudeClient | None:
    s = load_settings()
    if not s.anthropic_api_key:
        return None
    return ClaudeClient(api_key=s.anthropic_api_key, model=s.claude_model)


def _judge(llm: ClaudeClient, topic, db_path: Path, force: bool = False) -> JudgeStats:
    def progress(done: int, total: int) -> None:
        print(f"  Claude: {done}/{total}", end="\r", flush=True)

    stats = judge_topic(llm, topic, db_path, force=force, progress=progress)
    print(f"Claude ({llm.model}) judged {stats.judged}/{stats.pending}: {stats.rejected} off-topic" + " " * 10)
    if stats.error:
        print(f"WARNING: Claude judging stopped: {stats.error}")
    return stats


def _print_enrich(stats) -> None:
    print(f"OpenAlex DOI match: {stats.matched}/{stats.with_doi} ({stats.rate:.1%})")


def _cmd_search(args) -> int:
    topics = load_topics(args.topics)
    if args.topic not in topics:
        print(f"unknown topic '{args.topic}'. available: {', '.join(topics)}", file=sys.stderr)
        return 2
    all_groups = load_journals(args.journals)
    names = args.groups or list(all_groups)
    missing = [g for g in names if g not in all_groups]
    if missing:
        print(f"unknown journal group(s): {', '.join(missing)}. available: {', '.join(all_groups)}",
              file=sys.stderr)
        return 2
    groups = {g: all_groups[g] for g in names}

    end = args.end or date.today()
    start = args.start or years_ago(end, args.years)
    settings = load_settings()
    client = PubMedClient(api_key=settings.ncbi_api_key, email=settings.ncbi_email)

    print(f"topic={args.topic}  period={start}..{end}  groups={', '.join(groups)}")
    res = run(client, topics[args.topic], groups, start, end,
              db_path=None if args.dry_run else args.db, dry_run=args.dry_run,
              openalex=None if args.no_openalex else _openalex_client(),
              openalex_sources=load_openalex_sources())

    for g in res.groups:
        print(f"  [{g.group}] {g.count} hits")
        if g.warnings:
            print(f"     warnings: {g.warnings}")
        if args.show_query:
            print(f"     {g.query}")
    print(f"unique PMIDs: {len(res.unique_pmids)}")
    if args.dry_run:
        return 0

    if res.openalex_found:
        print(f"OpenAlex supplement: {res.openalex_found} hits -> {res.openalex_added} added (not in PubMed results)")
    print(f"kept: {len(res.kept)}  excluded: {len(res.excluded)}  new in DB: {res.new_count}")
    if res.enrich:
        _print_enrich(res.enrich)
    for w in res.warnings:
        print(f"WARNING: {w}")
    for p, reason in res.excluded:
        print(f"  - excluded ({reason}): {p.title[:90]}  [{p.journal}]")
    if not args.no_llm and res.kept:
        llm = _claude_client()
        if llm is None:
            print("Claude check skipped (no ANTHROPIC_API_KEY)")
        else:
            _judge(llm, topics[args.topic], args.db)
    if not args.quiet:
        print()
        for i, s in enumerate(res.kept, 1):
            p = s.paper
            cites = f"  cited:{p.cited_by}" if p.cited_by is not None else ""
            print(f"{i:3d}. [{s.relevance:.0f}] {display_title(p.title, is_review(p.pub_types))[:100]}")
            print(f"      {p.journal}, {p.pub_date}  doi:{p.doi or '-'}  pmid:{p.pmid or '-'}{cites}")
    print(f"\nsaved to {args.db}")
    return 0


def _cmd_judge(args) -> int:
    topics = load_topics(args.topics)
    if args.topic not in topics:
        print(f"unknown topic '{args.topic}'. available: {', '.join(topics)}", file=sys.stderr)
        return 2
    llm = _claude_client()
    if llm is None:
        print("ANTHROPIC_API_KEY is not set (.env)", file=sys.stderr)
        return 1
    stats = _judge(llm, topics[args.topic], args.db, force=args.force)
    return 1 if stats.error else 0


def _cmd_expand(args) -> int:
    current = None
    if args.topic:
        topics = load_topics(args.topics)
        if args.topic not in topics:
            print(f"unknown topic '{args.topic}'", file=sys.stderr)
            return 2
        current = topics[args.topic]
    if not args.description and current is None:
        print("give a description or --topic", file=sys.stderr)
        return 2
    llm = _claude_client()
    if llm is None:
        print("ANTHROPIC_API_KEY is not set (.env)", file=sys.stderr)
        return 1
    try:
        sug = suggest_terms(llm, args.description, current)
    except LLMError as e:
        print(f"Claude error: {e}", file=sys.stderr)
        return 1
    out = {"facets": sug.facets, "exact_phrases": sug.exact_phrases, "exclude_phrases": sug.exclude_phrases}
    print(yaml.safe_dump(out, allow_unicode=True, sort_keys=False, width=120))
    if sug.notes:
        print(f"# {sug.notes}")
    return 0


def _cmd_enrich(args) -> int:
    if not args.db.exists():
        print(f"no database at {args.db}; run `search` first", file=sys.stderr)
        return 1
    try:
        stats = enrich_db(_openalex_client(), args.db)
    except OpenAlexError as e:
        print(f"OpenAlex error: {e}", file=sys.stderr)
        return 1
    _print_enrich(stats)
    return 0


def _cmd_list(args) -> int:
    if not args.db.exists():
        print(f"no database at {args.db}; run `search` first", file=sys.stderr)
        return 1
    conn = db.connect(args.db)
    try:
        rows = db.topic_papers(conn, args.topic)
    finally:
        conn.close()
    if not rows:
        print(f"no papers stored for topic '{args.topic}'")
        return 0
    week_ago = date.fromordinal(date.today().toordinal() - 7).isoformat()
    for i, r in enumerate(rows[: args.limit], 1):
        badge = " NEW" if r["first_seen"] >= week_ago else ""
        verdict = json.loads(r["llm_verdict"]) if r["llm_verdict"] else {}
        off = " [off-topic]" if verdict.get("relevant") is False else ""
        review = is_review(json.loads(r["pub_types"] or "[]"))
        print(f"{i:3d}. [{r['relevance']:.0f}]{badge}{off} {display_title(r['title'], review)[:100]}")
        print(f"      {r['journal']}, {r['pub_date']}  doi:{r['doi'] or '-'}")
        if r["summary"]:
            print(f"      {r['summary']}")
        if off:
            print(f"      Claude: {verdict.get('reason', '')}")
    print(f"\n{len(rows)} papers total")
    return 0


def _cmd_topics(args) -> int:
    for name, t in load_topics(args.topics).items():
        print(f"{name}: facets={list(t.facets)}  exact={len(t.exact_phrases)}  exclude={len(t.exclude_phrases)}")
    for g, js in load_journals(args.journals).items():
        print(f"  journals/{g}: {len(js)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp949; titles contain non-ASCII characters.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _build_parser().parse_args(argv)
    return {"search": _cmd_search, "list": _cmd_list, "enrich": _cmd_enrich, "judge": _cmd_judge,
            "expand": _cmd_expand, "topics": _cmd_topics}[args.cmd](args)
