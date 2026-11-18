"""Claude API features (SPEC Phase 4): keyword expansion, relevance check, one-line Korean summary."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic

from paper_finder.config import Topic

# Cost split: term suggestions are rare and need judgement -> Sonnet; relevance verdicts + summaries are
# high-volume classification -> Haiku (1/20 of Sonnet's per-token price). Override with CLAUDE_MODEL /
# CLAUDE_JUDGE_MODEL in .env.
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_JUDGE_MODEL = "claude-haiku-5-5"
# On a safety-classifier refusal the API retries the request on a fallback model in the same call.
# Haiku models have no server-side fallback, so the parameter is left off for them.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD per million tokens (input, output) for the cost estimate / daily budget. Unknown models count as Sonnet.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES.get(model, PRICES["claude-sonnet-5-5"])
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


class LLMError(RuntimeError):
    pass


@dataclass
class Verdict:
    relevant: bool
    reason: str      # Korean, why (ir)relevant
    summary: str     # Korean one-line summary


@dataclass
class Suggestion:
    facets: dict[str, list[str]]
    exact_phrases: list[str] = field(default_factory=list)
    exclude_phrases: list[str] = field(default_factory=list)
    notes: str = ""
    name: str = ""          # suggested snake_case topic name (for a new topic)


class ClaudeClient:
    """Thin wrapper: one structured-JSON request per call, errors mapped to LLMError."""

    def __init__(self, api_key: str | None = None, model: str | None = None, client: Any = None,
                 on_usage: Callable[[str, str, int, int, float], None] | None = None,
                 budget_check: Callable[[], str | None] | None = None):
        """on_usage(kind, model, input_tokens, output_tokens, cost_usd) is called after each request;
        budget_check() returning a message blocks the request with LLMError(message)."""
        self.model = model or DEFAULT_MODEL
        self._client = client if client is not None else anthropic.Anthropic(api_key=api_key)
        self.on_usage = on_usage
        self.budget_check = budget_check

    def json(self, system: str, user: str, schema: dict, effort: str = "low", max_tokens: int = 16000,
             kind: str = "other") -> dict:
        if self.budget_check is not None and (msg := self.budget_check()):
            raise LLMError(msg)
        kwargs: dict[str, Any] = {}
        if not self.model.startswith("claude-haiku"):
            kwargs = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        try:
            resp = self._client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
                **kwargs,
            )
        except anthropic.AuthenticationError as e:
            raise LLMError("ANTHROPIC_API_KEY가 올바르지 않습니다") from e
        except anthropic.PermissionDeniedError as e:
            raise LLMError(f"이 API 키로는 {self.model} 모델을 쓸 수 없습니다") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"모델 '{self.model}'을 찾을 수 없습니다 (CLAUDE_MODEL 확인)") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Claude API 요청 한도 초과. 잠시 뒤 다시 시도하세요") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API 오류 ({e.status_code}): {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Claude API 연결 실패: {e}") from e
        usage = getattr(resp, "usage", None)
        if self.on_usage is not None and usage is not None:
            tin, tout = int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0)
            self.on_usage(kind, self.model, tin, tout, estimate_cost(self.model, tin, tout))
        if resp.stop_reason == "refusal":
            raise LLMError("Claude가 요청을 처리하지 않았습니다 (refusal)")
        if resp.stop_reason == "max_tokens":
            raise LLMError("Claude 응답이 max_tokens에서 잘렸습니다")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise LLMError("Claude 응답에 텍스트가 없습니다")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"Claude 응답 JSON 파싱 실패: {e}") from e


# ------------------------------------------------------------------ keyword expansion

_SUGGEST_SYSTEM = """\
You help a biomedical engineering lab build PubMed searches for recent papers on a research topic.
The search tags every term with [tiab] (title/abstract). Terms inside one concept group (facet) are ORed;
facets are ANDed. Exact phrases match on their own (ORed with the facet block). Exclude phrases are applied
after retrieval to drop false positives whose title or abstract contains them.

Propose search terms:
- 2-4 facets, each one concept the topic requires (e.g. material, device, application). Facet names are short
  lowercase English words.
- Per facet, the synonyms, spelling and hyphenation variants, abbreviations, MeSH entry terms and newer coinages
  that authors actually write in titles and abstracts. Usually 4-12 terms per facet.
- Avoid single generic words that match unrelated fields on their own (e.g. "transient", "device", "sensor"),
  unless another facet constrains them. Prefer two- or three-word phrases.
- Exact phrases: a few highly specific phrases that identify the topic by themselves.
- Exclude phrases: phrases from other fields that would collide with the terms (e.g. "transient absorption"
  in spectroscopy when searching "transient electronics"). Only list ones you are confident about.
- Do not repeat terms that the current topic already has. When extending a current definition, put each term
  under the existing facet name for its concept; add a new facet only for a concept not covered yet.
All terms in English. Write `notes` in Korean: one or two sentences on the choices a researcher should check.
topic_name: a short lowercase snake_case English name for the topic (2-4 words, e.g. transient_bioelectronics)."""

_SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "facets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "terms": {"type": "array", "items": {"type": "string"}}},
                "required": ["name", "terms"],
                "additionalProperties": False,
            },
        },
        "exact_phrases": {"type": "array", "items": {"type": "string"}},
        "exclude_phrases": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
        "topic_name": {"type": "string"},
    },
    "required": ["facets", "exact_phrases", "exclude_phrases", "notes", "topic_name"],
    "additionalProperties": False,
}


def _topic_block(topic: Topic) -> str:
    lines = [f"Topic name: {topic.name}"]
    for facet, terms in topic.facets.items():
        lines.append(f"Facet '{facet}': {', '.join(terms)}")
    if topic.exact_phrases:
        lines.append(f"Exact phrases: {', '.join(topic.exact_phrases)}")
    if topic.exclude_phrases:
        lines.append(f"Exclude phrases: {', '.join(topic.exclude_phrases)}")
    return "\n".join(lines)


def _clean(terms: list[str], known: set[str]) -> list[str]:
    out, seen = [], set(known)
    for t in terms:
        t = " ".join(str(t).split())
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def suggest_terms(llm: ClaudeClient, description: str, current: Topic | None = None) -> Suggestion:
    """Ask Claude for facet terms. Terms already in `current` are removed from the result."""
    parts = []
    if description.strip():
        parts.append(f"Researcher's description of the topic:\n{description.strip()}")
    if current is not None:
        parts.append(f"Current search definition:\n{_topic_block(current)}")
    if not parts:
        raise ValueError("주제 설명이나 기존 키워드가 필요합니다")
    data = llm.json(_SUGGEST_SYSTEM, "\n\n".join(parts), _SUGGEST_SCHEMA, effort="medium", kind="suggest")

    known = set()
    if current is not None:
        known = {t.lower() for ts in current.facets.values() for t in ts}
        known |= {t.lower() for t in current.exact_phrases + current.exclude_phrases}
    facets: dict[str, list[str]] = {}
    for f in data["facets"]:
        name = " ".join(f["name"].split()).lower().replace(" ", "_")
        terms = _clean(f["terms"], known | {t.lower() for ts in facets.values() for t in ts})
        if name and terms:
            facets.setdefault(name, []).extend(terms)
    return Suggestion(
        facets=facets,
        exact_phrases=_clean(data["exact_phrases"], known),
        exclude_phrases=_clean(data["exclude_phrases"], known),
        notes=data["notes"].strip(),
        name=re.sub(r"[^a-z0-9_]+", "_", data.get("topic_name", "").strip().lower()).strip("_"),
    )


_NEXT_SYSTEM = """\
You help a researcher fill one concept group (facet) of a PubMed search. Terms in a facet are ORed and each is
searched in titles and abstracts ([tiab]). Given the topic and the terms the researcher has typed so far for
one facet, predict the next terms they would most likely add to that facet: synonyms, spelling or hyphenation
variants, abbreviations, MeSH entry terms, and closely related terms authors write for the same concept.
Do not repeat any term that is already in the topic. Avoid generic single words that would match unrelated
fields. Order from most to least useful. All terms in English."""

_NEXT_SCHEMA = {
    "type": "object",
    "properties": {"terms": {"type": "array", "items": {"type": "string"}}},
    "required": ["terms"],
    "additionalProperties": False,
}


def next_terms(llm: ClaudeClient, topic: Topic, facet: str, typed: list[str], top: int = 8) -> list[str]:
    """Likely next terms for one facet, given what the researcher has typed so far (for the keyword editor)."""
    if not typed:
        return []
    user = (f"Current search definition:\n{_topic_block(topic)}\n\nFacet being edited: '{facet}'\n"
            f"Terms typed so far for this facet: {', '.join(typed)}\n\nSuggest up to {top} next terms.")
    data = llm.json(_NEXT_SYSTEM, user, _NEXT_SCHEMA, effort="low", max_tokens=2000, kind="next")
    known = {t.lower() for ts in topic.facets.values() for t in ts}
    known |= {t.lower() for t in topic.exact_phrases + topic.exclude_phrases + typed}
    return _clean(data["terms"], known)[:top]


# ------------------------------------------------------------------ relevance + summary

_JUDGE_SYSTEM = """\
You screen papers retrieved by a keyword search for a biomedical engineering lab.

{topic}

For each paper decide whether it is really about this topic:
- relevant = true when the paper's own work (device, material, method, or a review of them) is about the topic
  defined by the concept groups above, with each concept present in substance.
- relevant = false when the keywords matched by coincidence with a different meaning (e.g. "transient" in
  spectroscopy or laser physics, "dissolvable" in pharmaceutics with no electronics), or when the topic is only
  mentioned in passing.
- If only the title is available, judge from the title and lean toward relevant when unsure.

reason: one short Korean phrase explaining the decision.
summary: one Korean sentence (under about 80 characters) on what the paper did and found. Keep technical terms
in English where Korean researchers would. Return one entry per paper, using the given id."""

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "papers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "relevant": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["id", "relevant", "reason", "summary"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["papers"],
    "additionalProperties": False,
}


@dataclass
class PaperText:
    id: str
    title: str
    abstract: str
    journal: str = ""


def judge_papers(llm: ClaudeClient, topic: Topic, papers: list[PaperText]) -> dict[str, Verdict]:
    """One request for a batch of papers. Papers missing from the reply are left out of the result."""
    if not papers:
        return {}
    blocks = []
    for p in papers:
        blocks.append(f"<paper id=\"{p.id}\">\nTitle: {p.title}\nJournal: {p.journal}\n"
                      f"Abstract: {p.abstract or '(no abstract available)'}\n</paper>")
    data = llm.json(_JUDGE_SYSTEM.format(topic=_topic_block(topic)), "\n\n".join(blocks), _JUDGE_SCHEMA,
                    effort="low", kind="judge")
    wanted = {p.id for p in papers}
    out = {}
    for e in data["papers"]:
        if e["id"] in wanted:
            out[e["id"]] = Verdict(bool(e["relevant"]), e["reason"].strip(), e["summary"].strip())
    return out
