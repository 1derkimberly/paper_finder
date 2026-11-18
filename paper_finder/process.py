"""Dedupe, exclude-phrase filter, and facet-match relevance scoring (SPEC §6 steps 5-7)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from paper_finder.config import Topic
from paper_finder.pubmed import Paper

EXACT_PHRASE_BONUS = 1.0


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def dedupe(papers: list[Paper]) -> list[Paper]:
    """Drop duplicates by DOI, then PMID, then normalized title (first occurrence wins)."""
    seen_doi: set[str] = set()
    seen_pmid: set[str] = set()
    seen_title: set[str] = set()
    out: list[Paper] = []
    for p in papers:
        doi = (p.doi or "").lower()
        title = normalize_title(p.title)
        if (doi and doi in seen_doi) or (p.pmid and p.pmid in seen_pmid) or (title and title in seen_title):
            continue
        if doi:
            seen_doi.add(doi)
        if p.pmid:
            seen_pmid.add(p.pmid)
        if title:
            seen_title.add(title)
        out.append(p)
    return out


def _phrase_regex(phrase: str) -> re.Pattern:
    # Word-boundary, case-insensitive; whitespace/hyphen variations tolerated.
    words = [re.escape(w) for w in re.split(r"[\s\-]+", phrase.strip()) if w]
    return re.compile(r"\b" + r"[\s\-]+".join(words) + r"\b", re.IGNORECASE)


def _haystack(p: Paper) -> str:
    return f"{p.title}\n{p.abstract}"


def excluded_by(p: Paper, topic: Topic) -> str | None:
    text = _haystack(p)
    for phrase in topic.exclude_phrases:
        if _phrase_regex(phrase).search(text):
            return phrase
    return None


@dataclass
class Scored:
    paper: Paper
    relevance: float
    matched_terms: dict[str, list[str]] = field(default_factory=dict)


def score(p: Paper, topic: Topic) -> Scored:
    """relevance = number of facets with at least one hit + bonus if an exact phrase hits."""
    text = _haystack(p)
    matched: dict[str, list[str]] = {}
    for facet, terms in topic.facets.items():
        hits = [t for t in terms if _phrase_regex(t).search(text)]
        if hits:
            matched[facet] = hits
    exact = [ph for ph in topic.exact_phrases if _phrase_regex(ph).search(text)]
    if exact:
        matched["exact_phrases"] = exact
    relevance = sum(1 for k in matched if k != "exact_phrases") + (EXACT_PHRASE_BONUS if exact else 0.0)
    return Scored(paper=p, relevance=relevance, matched_terms=matched)


def matches_topic(p: Paper, topic: Topic) -> bool:
    """Re-apply the query logic on title+abstract with exact word boundaries:
    every facet has a hit, or any exact phrase hits. Used for OpenAlex results,
    whose search stems words (e.g. 'transient' also matches 'transients')."""
    text = _haystack(p)
    if any(_phrase_regex(ph).search(text) for ph in topic.exact_phrases):
        return True
    return bool(topic.facets) and all(
        any(_phrase_regex(t).search(text) for t in terms) for terms in topic.facets.values())


def title_has_any_term(p: Paper, topic: Topic) -> bool:
    """Lenient check for records without an abstract (some publishers withhold abstracts from
    OpenAlex, though OpenAlex's own search still saw them): one exact term or phrase in the title."""
    terms = [t for v in topic.facets.values() for t in v] + topic.exact_phrases
    return any(_phrase_regex(t).search(p.title) for t in terms)


def process(papers: list[Paper], topic: Topic) -> tuple[list[Scored], list[tuple[Paper, str]]]:
    """Return (kept papers sorted by relevance then date, excluded papers with reason)."""
    kept: list[Scored] = []
    excluded: list[tuple[Paper, str]] = []
    for p in dedupe(papers):
        reason = excluded_by(p, topic)
        if reason:
            excluded.append((p, reason))
        else:
            kept.append(score(p, topic))
    kept.sort(key=lambda s: (s.relevance, s.paper.pub_date), reverse=True)
    return kept, excluded
