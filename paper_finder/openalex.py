"""Minimal OpenAlex client: DOI lookup (citations, OA links) and supplementary search."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date

import requests

from paper_finder.pubmed import Paper

API = "https://api.openalex.org"
WORK_FIELDS = ("id,doi,title,type,publication_date,cited_by_count,open_access,best_oa_location,"
               "primary_location,authorships,abstract_inverted_index,ids")
# OpenAlex work types that are not research content.
SKIP_TYPES = {"editorial", "erratum", "paratext", "retraction", "peer-review", "supplementary-materials"}


class OpenAlexError(RuntimeError):
    pass


@dataclass
class Enrichment:
    openalex_id: str
    cited_by: int
    oa_url: str | None


def _norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi.strip(), flags=re.I).lower()


def _short_id(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def _oa_url(w: dict) -> str | None:
    loc = w.get("best_oa_location") or {}
    return loc.get("pdf_url") or loc.get("landing_page_url") or (w.get("open_access") or {}).get("oa_url")


def _abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    pos = {i: word for word, idxs in inv.items() for i in idxs}
    return " ".join(pos[i] for i in sorted(pos))


def _author_name(display: str) -> str:
    """'Kento Yamagishi' -> 'Yamagishi, Kento' (matches the PubMed 'Last, First' format)."""
    parts = display.strip().rsplit(" ", 1)
    return f"{parts[1]}, {parts[0]}" if len(parts) == 2 else display.strip()


def enrichment_from(w: dict) -> Enrichment:
    return Enrichment(_short_id(w["id"]), int(w.get("cited_by_count") or 0), _oa_url(w))


def work_to_paper(w: dict, journal: str) -> Paper:
    pmid_url = (w.get("ids") or {}).get("pmid") or ""
    pmid = re.search(r"(\d+)\s*$", pmid_url)
    e = enrichment_from(w)
    return Paper(
        pmid=pmid.group(1) if pmid else None,
        title=re.sub(r"\s+", " ", w.get("title") or "").strip(),
        abstract=_abstract(w.get("abstract_inverted_index")),
        journal=journal,
        pub_date=w.get("publication_date") or "",
        authors=[_author_name(a["author"]["display_name"]) for a in w.get("authorships") or []
                 if (a.get("author") or {}).get("display_name")],
        doi=_norm_doi(w.get("doi")),
        pub_types=["Review"] if w.get("type") == "review" else [],
        source="openalex",
        openalex_id=e.openalex_id,
        cited_by=e.cited_by,
        oa_url=e.oa_url,
    )


class OpenAlexClient:
    def __init__(self, api_key: str | None = None, mailto: str | None = None,
                 session: requests.Session | None = None, max_retries: int = 3,
                 sleep=time.sleep):
        self.api_key = api_key
        self.mailto = mailto
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self._sleep = sleep
        self._last_call = 0.0

    def _get(self, path: str, params: dict) -> dict:
        params = dict(params)
        if self.api_key:
            params["api_key"] = self.api_key
        if self.mailto:
            params["mailto"] = self.mailto
        for attempt in range(self.max_retries + 1):
            wait = 0.11 - (time.monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
            self._last_call = time.monotonic()
            try:
                resp = self.session.get(f"{API}{path}", params=params, timeout=60)
            except requests.RequestException as e:
                if attempt < self.max_retries:
                    self._sleep(2 ** attempt)
                    continue
                raise OpenAlexError(f"OpenAlex 접속 실패: {e}") from e
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < self.max_retries:
                    # Anonymous search is throttled under load and asks for Retry-After (~30 s).
                    retry_after = resp.headers.get("Retry-After", "")
                    self._sleep(min(float(retry_after), 60) if retry_after.isdigit() else 2 ** attempt)
                    continue
                msg = resp.json().get("message", "") if "json" in resp.headers.get("content-type", "") else ""
                raise OpenAlexError(f"OpenAlex {resp.status_code}: {msg or resp.text[:200]}")
            if resp.status_code != 200:
                raise OpenAlexError(f"OpenAlex {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        raise OpenAlexError("unreachable")

    def works_by_doi(self, dois: list[str], batch_size: int = 50) -> dict[str, dict]:
        """Look up works by DOI. Returns {normalized doi: work}."""
        found: dict[str, dict] = {}
        clean = sorted({d for d in (_norm_doi(x) for x in dois) if d and "|" not in d and "," not in d})
        for i in range(0, len(clean), batch_size):
            chunk = clean[i:i + batch_size]
            data = self._get("/works", {"filter": "doi:" + "|".join(chunk), "select": WORK_FIELDS,
                                        "per-page": 100})
            for w in data.get("results", []):
                d = _norm_doi(w.get("doi"))
                if d:
                    found[d] = w
        return found

    def source_id_by_issn(self, issn: str) -> str:
        return _short_id(self._get(f"/sources/issn:{issn}", {"select": "id"})["id"])

    def search(self, query: str, source_ids: list[str], start: date, end: date,
               max_results: int = 1000) -> list[dict]:
        """Boolean title+abstract search restricted to sources and a date window."""
        flt = (f"primary_location.source.id:{'|'.join(source_ids)},"
               f"from_publication_date:{start.isoformat()},to_publication_date:{end.isoformat()},"
               f"title_and_abstract.search:{query}")
        out: list[dict] = []
        cursor = "*"
        while cursor and len(out) < max_results:
            data = self._get("/works", {"filter": flt, "select": WORK_FIELDS, "per-page": 100,
                                        "cursor": cursor})
            out.extend(w for w in data.get("results", []) if w.get("type") not in SKIP_TYPES)
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not data.get("results"):
                break
        return out[:max_results]
