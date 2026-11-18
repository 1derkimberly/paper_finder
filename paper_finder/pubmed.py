"""Minimal PubMed E-utilities client (esearch + efetch) with rate limiting."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import requests

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TOOL_NAME = "paper_finder"

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


@dataclass
class Paper:
    pmid: str
    title: str
    abstract: str = ""
    journal: str = ""
    pub_date: str = ""  # ISO-ish: YYYY, YYYY-MM or YYYY-MM-DD
    authors: list[str] = field(default_factory=list)
    doi: str | None = None
    mesh: list[str] = field(default_factory=list)


class PubMedClient:
    def __init__(self, api_key: str | None = None, email: str | None = None,
                 session: requests.Session | None = None, max_retries: int = 3):
        self.api_key = api_key
        self.email = email
        self.session = session or requests.Session()
        self.max_retries = max_retries
        # NCBI: 10 req/s with a key, 3 req/s without. Stay slightly under.
        self._min_interval = 0.11 if api_key else 0.35
        self._last_call = 0.0

    def _params(self, **extra) -> dict:
        p = {"db": "pubmed", "tool": TOOL_NAME, **extra}
        if self.api_key:
            p["api_key"] = self.api_key
        if self.email:
            p["email"] = self.email
        return p

    def _post(self, endpoint: str, data: dict) -> requests.Response:
        for attempt in range(self.max_retries + 1):
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            # POST keeps long queries / ID lists out of the URL.
            resp = self.session.post(f"{EUTILS}/{endpoint}", data=data, timeout=60)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError("unreachable")

    def esearch(self, term: str, retmax: int = 10000) -> tuple[int, list[str]]:
        """Return (total count, PMIDs). Raises on PubMed-reported query errors."""
        resp = self._post("esearch.fcgi", self._params(term=term, retmax=retmax, retmode="json"))
        result = resp.json()["esearchresult"]
        if "ERROR" in result:
            raise ValueError(f"PubMed esearch error: {result['ERROR']}")
        errors = result.get("errorlist") or {}
        if errors.get("phrasesnotfound") or errors.get("fieldsnotfound"):
            # Not fatal (e.g. a journal abbreviation not in PubMed) but worth surfacing.
            result["_warnings"] = errors
        self.last_warnings = result.get("_warnings")
        self.last_translation = result.get("querytranslation", "")
        return int(result["count"]), list(result.get("idlist", []))

    def efetch(self, pmids: list[str], batch_size: int = 200) -> list[Paper]:
        papers: list[Paper] = []
        for i in range(0, len(pmids), batch_size):
            chunk = pmids[i:i + batch_size]
            resp = self._post("efetch.fcgi", self._params(id=",".join(chunk), retmode="xml"))
            papers.extend(parse_pubmed_xml(resp.content))
        return papers


# ---------------------------------------------------------------- XML parsing

def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _date_from(el: ET.Element | None) -> str:
    if el is None:
        return ""
    year = _text(el.find("Year"))
    if not year:
        m = re.match(r"(\d{4})(?:\s+(\w{3}))?", _text(el.find("MedlineDate")))
        if not m:
            return ""
        year, mon = m.group(1), m.group(2)
        mm = _MONTHS.get((mon or "").lower())
        return f"{year}-{mm:02d}" if mm else year
    month = _text(el.find("Month"))
    mm = int(month) if month.isdigit() else _MONTHS.get(month[:3].lower())
    if not mm:
        return year
    day = _text(el.find("Day"))
    return f"{year}-{mm:02d}-{int(day):02d}" if day.isdigit() else f"{year}-{mm:02d}"


def _parse_article(node: ET.Element) -> Paper | None:
    mc = node.find("MedlineCitation")
    if mc is None:
        return None
    art = mc.find("Article")
    pmid = _text(mc.find("PMID"))
    if art is None or not pmid:
        return None

    abstract_parts = []
    for at in art.findall("Abstract/AbstractText"):
        label, txt = at.get("Label"), _text(at)
        abstract_parts.append(f"{label}: {txt}" if label else txt)

    authors = []
    for a in art.findall("AuthorList/Author"):
        last, fore = _text(a.find("LastName")), _text(a.find("ForeName"))
        name = f"{last} {fore}".strip() or _text(a.find("CollectiveName"))
        if name:
            authors.append(name)

    journal = (_text(mc.find("MedlineJournalInfo/MedlineTA"))
               or _text(art.find("Journal/ISOAbbreviation"))
               or _text(art.find("Journal/Title")))

    # Prefer the electronic publication date (more precise); fall back to the issue date.
    pub_date = _date_from(art.find("ArticleDate")) or _date_from(art.find("Journal/JournalIssue/PubDate"))

    doi = None
    for aid in node.findall("PubmedData/ArticleIdList/ArticleId"):
        if aid.get("IdType") == "doi" and aid.text:
            doi = aid.text.strip()
            break
    if not doi:
        for loc in art.findall("ELocationID"):
            if loc.get("EIdType") == "doi" and loc.text:
                doi = loc.text.strip()
                break

    mesh = [_text(d) for d in mc.findall("MeshHeadingList/MeshHeading/DescriptorName")]

    return Paper(
        pmid=pmid,
        title=_text(art.find("ArticleTitle")),
        abstract="\n".join(p for p in abstract_parts if p),
        journal=journal,
        pub_date=pub_date,
        authors=authors,
        doi=doi.lower() if doi else None,
        mesh=[m for m in mesh if m],
    )


def parse_pubmed_xml(content: bytes | str) -> list[Paper]:
    root = ET.fromstring(content)
    return [p for p in (_parse_article(n) for n in root.findall("PubmedArticle")) if p]
