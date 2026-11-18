"""Build PubMed query strings from a facet-structured topic (SPEC §4)."""

from __future__ import annotations

from datetime import date

from paper_finder.config import Topic


def _term(t: str) -> str:
    """Tag a term with [tiab]; quote multi-word phrases to stop automatic term mapping."""
    t = t.strip().replace('"', "")
    if " " in t or "-" in t:
        t = f'"{t}"'
    return f"{t}[tiab]"


def _or_group(terms: list[str], tag) -> str:
    return "(" + " OR ".join(tag(t) for t in terms) + ")"


def topic_clause(topic: Topic) -> str:
    """(facet1 OR ..) AND (facet2 OR ..) ... OR (exact phrases)."""
    parts: list[str] = []
    if topic.facets:
        parts.append("(" + " AND ".join(_or_group(v, _term) for v in topic.facets.values()) + ")")
    if topic.exact_phrases:
        parts.append(_or_group(topic.exact_phrases, _term))
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def journal_clause(journals: list[str]) -> str:
    return _or_group(journals, lambda j: f'"{j}"[ta]')


def date_clause(start: date, end: date) -> str:
    return f"{start:%Y/%m/%d}:{end:%Y/%m/%d}[dp]"


def build_query(topic: Topic, journals: list[str], start: date, end: date) -> str:
    return f"{topic_clause(topic)} AND {journal_clause(journals)} AND {date_clause(start, end)}"


def _oa_term(t: str) -> str:
    t = t.strip().replace('"', "")
    return f'"{t}"' if (" " in t or "-" in t) else t


def openalex_query(topic: Topic) -> str:
    """Same facet logic for OpenAlex title_and_abstract.search (boolean, uppercase operators).
    OpenAlex stems words, so results are re-checked with process.matches_topic."""
    parts: list[str] = []
    if topic.facets:
        parts.append(" AND ".join("(" + " OR ".join(_oa_term(t) for t in v) + ")"
                                  for v in topic.facets.values()))
    if topic.exact_phrases:
        parts.append(" OR ".join(_oa_term(p) if " " in p else f'"{p}"' for p in topic.exact_phrases))
    if len(parts) == 1:
        return parts[0]
    return " OR ".join(f"({p})" for p in parts)


def years_ago(end: date, years: int) -> date:
    try:
        return end.replace(year=end.year - years)
    except ValueError:  # Feb 29
        return end.replace(year=end.year - years, day=28)
