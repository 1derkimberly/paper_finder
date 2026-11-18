"""Loading of topics.yaml, journals.yaml and .env settings."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
# PAPER_FINDER_CONFIG / PAPER_FINDER_DB point a separate copy (e.g. a demo or test dataset) elsewhere.
CONFIG_DIR = Path(os.environ.get("PAPER_FINDER_CONFIG") or ROOT / "config")
DEFAULT_DB = Path(os.environ.get("PAPER_FINDER_DB") or ROOT / "data" / "papers.db")


@dataclass
class Topic:
    name: str
    facets: dict[str, list[str]]
    exact_phrases: list[str] = field(default_factory=list)
    exclude_phrases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "facets": self.facets,
            "exact_phrases": self.exact_phrases,
            "exclude_phrases": self.exclude_phrases,
        }

    @classmethod
    def from_dict(cls, name: str, body: dict) -> Topic:
        facets = {k: [str(t) for t in v] for k, v in (body.get("facets") or {}).items() if v}
        exact = [str(p) for p in body.get("exact_phrases") or []]
        if not facets and not exact:
            raise ValueError(f"topic '{name}' has neither facets nor exact_phrases")
        return cls(name=name, facets=facets, exact_phrases=exact,
                   exclude_phrases=[str(p) for p in body.get("exclude_phrases") or []])


@dataclass
class Settings:
    ncbi_api_key: str | None
    ncbi_email: str | None
    openalex_mailto: str | None
    openalex_api_key: str | None
    anthropic_api_key: str | None
    claude_model: str | None = None         # term suggestions (default llm.DEFAULT_MODEL)
    claude_judge_model: str | None = None   # relevance verdicts + summaries (default llm.DEFAULT_JUDGE_MODEL)
    database_url: str | None = None         # postgres://… (deployed); empty = local SQLite file
    admin_emails: tuple[str, ...] = ()      # who may manage topics / run updates when login is on


@dataclass
class OpenAlexSource:
    name: str              # journal name shown in the dashboard (journals.yaml spelling)
    group: str             # journals.yaml group it belongs to
    issn: str | None = None
    openalex_id: str | None = None


def _read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_topics(path: Path = CONFIG_DIR / "topics.yaml") -> dict[str, Topic]:
    return {name: Topic.from_dict(name, body or {}) for name, body in _read_yaml(path).items()}


TOPIC_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

TOPICS_HEADER = """\
# 개념 그룹(facet) 구조: 같은 facet 안은 OR, facet끼리는 AND.
# exact_phrases 는 단독으로도 매칭, exclude_phrases 는 수집 후 제목+초록에서 걸러낸다.
# 대시보드에서 저장하면 이 파일이 다시 쓰인다 (주석은 이 머리말만 유지됨).
"""


def validate_topic(topic: Topic) -> None:
    if not TOPIC_NAME_RE.match(topic.name):
        raise ValueError("주제 이름은 영문, 숫자, _, - 만 쓸 수 있습니다")
    if not topic.facets and not topic.exact_phrases:
        raise ValueError("facet 또는 exact phrase가 하나 이상 있어야 합니다")


def save_topic(topic: Topic, path: Path = CONFIG_DIR / "topics.yaml") -> None:
    """Add or replace one topic in topics.yaml."""
    validate_topic(topic)
    raw = _read_yaml(path) if path.exists() else {}
    raw[topic.name] = topic.to_dict()
    body = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=None, width=120)
    path.write_text(TOPICS_HEADER + body, encoding="utf-8")


def remove_topic(name: str, path: Path = CONFIG_DIR / "topics.yaml") -> bool:
    """Remove one topic from topics.yaml. Returns False if it was not there."""
    raw = _read_yaml(path) if path.exists() else {}
    if name not in raw:
        return False
    del raw[name]
    body = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=None, width=120) if raw else ""
    path.write_text(TOPICS_HEADER + body, encoding="utf-8")
    return True


def load_journals(path: Path = CONFIG_DIR / "journals.yaml") -> dict[str, list[str]]:
    raw = _read_yaml(path)
    return {group: [str(j) for j in journals] for group, journals in raw.items() if journals}


def load_openalex_sources(path: Path = CONFIG_DIR / "openalex_sources.yaml") -> list[OpenAlexSource]:
    if not path.exists():
        return []
    out = []
    for name, body in _read_yaml(path).items():
        body = body or {}
        if not body.get("group") or not (body.get("issn") or body.get("openalex_id")):
            raise ValueError(f"openalex source '{name}' needs group and issn or openalex_id")
        out.append(OpenAlexSource(name=str(name), group=str(body["group"]),
                                  issn=body.get("issn"), openalex_id=body.get("openalex_id")))
    return out


def load_settings() -> Settings:
    # Read .env on every call without copying it into os.environ, so a long-running dashboard picks up keys
    # saved after it started. A variable set in the real environment (even empty) still wins.
    file_values = dotenv_values(ROOT / ".env") if (ROOT / ".env").exists() else {}

    def get(key: str) -> str | None:
        value = os.environ[key] if key in os.environ else file_values.get(key)
        return (value or "").strip() or None

    return Settings(
        ncbi_api_key=get("NCBI_API_KEY"),
        ncbi_email=get("NCBI_EMAIL"),
        openalex_mailto=get("OPENALEX_MAILTO"),
        openalex_api_key=get("OPENALEX_API_KEY"),
        anthropic_api_key=get("ANTHROPIC_API_KEY"),
        claude_model=get("CLAUDE_MODEL"),
        claude_judge_model=get("CLAUDE_JUDGE_MODEL"),
        database_url=get("DATABASE_URL"),
        admin_emails=tuple(e.strip().lower() for e in (get("ADMIN_EMAILS") or "").split(",") if e.strip()),
    )
