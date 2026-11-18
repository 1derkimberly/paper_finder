"""Instant, offline keyword suggestions from papers already collected for a topic.

`related_terms` finds words used *like* the seed terms: words whose neighbouring words (±2 positions) in titles
and abstracts resemble the seeds' neighbours (e.g. 'bioresorbable' → 'degradable', 'resorbable'). These are
candidates for the same facet (OR list). `vocabulary` lists frequent words and two-word phrases for type-ahead
in the keyword editor. No API calls; Claude (llm.next_terms / suggest_terms) gives better but slower suggestions.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

_WORD = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_WINDOW = (-2, -1, 1, 2)

STOPWORDS = frozenset("""
a about above abstract across after against all along also although among an and another any approach
approaches are around as at available based be been before being between both but by can capable could current
currently demonstrate demonstrated demonstrates demonstrating describe described design designed develop
developed developing development different do does due during each effect effective effectively effects either
enable enabled enables enabling enhanced et etc even ever every example first following for from further
furthermore given good great had has have here herein high higher highly however i if important improve improved
in including increase increased into is it its key large less low lower may method methods more moreover most
much must new next-generation no non not novel now of offer offers on one only or other our over overall paper
per performance potential present presented presents propose proposed provide provided provides providing rather
recent recently related remain remains report reported reports result resulting results review reviewed second
several shown shows show significant significantly similar since so some strategy strategies studies study such
suggest than that the their them then there these they this those three through thus to toward towards two under
unique up upon use used uses using various very via vivo vitro situ was we well were what when where whereas
which while who whose will with within without work works would yet able achieve achieved achieves achieving
addition additionally allow allows application applications applied compared comparison conventional critical
challenge challenges contrast approximately broad findings form forms future level levels main major multiple need
needs number numerous obtained possible process range real require required requires role specific state strong
successful successfully system systems type types wide widely day days week weeks month months year years time
times possess possesses replace capability capabilities excellent essential promising limited
""".split())


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _norm(tok: str) -> str:
    """Cheap plural folding so 'implants' and 'implant' count as one word."""
    return tok[:-1] if len(tok) > 4 and tok.endswith("s") and not tok.endswith("ss") else tok


def _ok(tok: str) -> bool:
    return len(tok) >= 3 and tok not in STOPWORDS and not tok.isdigit()


@dataclass
class TermIndex:
    contexts: dict[str, Counter] = field(default_factory=dict)  # word -> Counter{(side, neighbour): n}
    word_df: Counter = field(default_factory=Counter)             # normalised word -> documents
    phrase_df: Counter = field(default_factory=Counter)           # surface word / bigram -> documents
    size: int = 0


def build_index(texts: list[str]) -> TermIndex:
    idx = TermIndex()
    ctx: dict[str, Counter] = defaultdict(Counter)
    for text in texts:
        raw = _tokens(text or "")
        if not raw:
            continue
        idx.size += 1
        toks = [_norm(t) for t in raw]
        idx.word_df.update({t for t in toks if _ok(t)})
        phrases = {t for t in raw if _ok(t)}
        phrases |= {f"{a} {b}" for a, b in zip(raw, raw[1:]) if _ok(a) and _ok(b)}
        idx.phrase_df.update(phrases)
        for i, w in enumerate(toks):
            if not _ok(w):
                continue
            for d in _WINDOW:
                j = i + d
                if 0 <= j < len(toks) and _ok(toks[j]):
                    ctx[w][(d > 0, toks[j])] += 1
    idx.contexts = dict(ctx)
    return idx


def _cosine(a: Counter, b: Counter) -> float:
    if len(a) > len(b):
        a, b = b, a
    num = sum(v * b[k] for k, v in a.items() if k in b)
    if not num:
        return 0.0
    return num / (math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values())))


def related_terms(index: TermIndex, seeds: list[str], known: list[str] = (), top: int = 8,
                  min_docs: int = 2, min_score: float = 0.12) -> list[str]:
    """Words whose contexts resemble the seed terms' contexts, best first.

    `known` are terms already in the topic (any facet / exact / exclude); words from seeds or known terms are
    never suggested.
    """
    seed_words = {_norm(t) for s in seeds for t in _tokens(s)}
    target: Counter = Counter()
    for w in seed_words:
        target.update(index.contexts.get(w, {}))
    if not target:
        return []
    blocked = seed_words | {_norm(t) for k in known for t in _tokens(k)}
    scored = []
    for w, vec in index.contexts.items():
        if w in blocked or index.word_df[w] < min_docs:
            continue
        s = _cosine(vec, target)
        if s >= min_score:
            scored.append((s, w))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [w for _, w in scored[:top]]


def vocabulary(index: TermIndex, top: int = 300, min_docs: int = 2) -> list[str]:
    """Frequent words and two-word phrases (for type-ahead), most common first."""
    items = [(c, g) for g, c in index.phrase_df.items() if c >= min_docs]
    items.sort(key=lambda x: (-x[0], x[1]))
    return [g for _, g in items[:top]]
