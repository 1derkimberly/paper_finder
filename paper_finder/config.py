"""Loading of topics.yaml, journals.yaml and .env settings."""

from __future__ import annotations

import os
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
