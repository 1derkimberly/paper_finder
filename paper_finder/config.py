"""Loading of topics.yaml, journals.yaml and .env settings."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DEFAULT_DB = ROOT / "data" / "papers.db"


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


@dataclass
class Settings:
    ncbi_api_key: str | None
    ncbi_email: str | None
    openalex_mailto: str | None
    anthropic_api_key: str | None


def _read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_topics(path: Path = CONFIG_DIR / "topics.yaml") -> dict[str, Topic]:
    raw = _read_yaml(path)
    topics: dict[str, Topic] = {}
    for name, body in raw.items():
        facets = {k: [str(t) for t in v] for k, v in (body.get("facets") or {}).items() if v}
        exact = [str(p) for p in body.get("exact_phrases") or []]
        if not facets and not exact:
            raise ValueError(f"topic '{name}' has neither facets nor exact_phrases")
        topics[name] = Topic(
            name=name,
            facets=facets,
            exact_phrases=exact,
            exclude_phrases=[str(p) for p in body.get("exclude_phrases") or []],
        )
    return topics


TOPIC_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

TOPICS_HEADER = """\
# 개념 그룹(facet) 구조: 같은 facet 안은 OR, facet끼리는 AND.
# exact_phrases 는 단독으로도 매칭, exclude_phrases 는 수집 후 제목+초록에서 걸러낸다.
# 대시보드에서 저장하면 이 파일이 다시 쓰인다 (주석은 이 머리말만 유지됨).
"""


def save_topic(topic: Topic, path: Path = CONFIG_DIR / "topics.yaml") -> None:
    """Add or replace one topic in topics.yaml."""
    if not TOPIC_NAME_RE.match(topic.name):
        raise ValueError("주제 이름은 영문, 숫자, _, - 만 쓸 수 있습니다")
    if not topic.facets and not topic.exact_phrases:
        raise ValueError("facet 또는 exact phrase가 하나 이상 있어야 합니다")
    raw = _read_yaml(path) if path.exists() else {}
    raw[topic.name] = topic.to_dict()
    body = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=None, width=120)
    path.write_text(TOPICS_HEADER + body, encoding="utf-8")


def load_journals(path: Path = CONFIG_DIR / "journals.yaml") -> dict[str, list[str]]:
    raw = _read_yaml(path)
    return {group: [str(j) for j in journals] for group, journals in raw.items() if journals}


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")

    def get(key: str) -> str | None:
        return os.getenv(key) or None

    return Settings(
        ncbi_api_key=get("NCBI_API_KEY"),
        ncbi_email=get("NCBI_EMAIL"),
        openalex_mailto=get("OPENALEX_MAILTO"),
        anthropic_api_key=get("ANTHROPIC_API_KEY"),
    )
