"""Data shaping for the Streamlit dashboard (kept free of Streamlit so it can be tested)."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from paper_finder import db
from paper_finder.pubmed import is_review

NEW_DAYS = 7
TOP_GROUP_PREFIX = "tier1"  # journal groups counted as "상위 저널"

COLUMNS = ["id", "pmid", "doi", "title", "abstract", "journal", "group", "pub_date", "pub_dt", "year",
           "authors", "mesh", "pub_types", "is_review", "cited_by", "oa_url", "first_seen", "is_new", "relevance",
           "matched_terms", "status", "note"]


def journal_to_group(journal_groups: dict[str, list[str]]) -> dict[str, str]:
    return {j.lower(): g for g, js in journal_groups.items() for j in js}


def _pub_dt(s: str | None) -> pd.Timestamp:
    """'2026', '2026-01', '2026-01-07' -> Timestamp (missing parts = 1)."""
    if not s:
        return pd.NaT
    parts = (s.split("-") + ["01", "01"])[:3]
    return pd.to_datetime("-".join(parts), errors="coerce")


def load_frame(db_path: Path, topic_name: str, journal_groups: dict[str, list[str]],
               today: date | None = None) -> pd.DataFrame:
    if not db_path.exists():
        return pd.DataFrame(columns=COLUMNS)
    conn = db.connect(db_path)
    try:
        rows = db.topic_papers(conn, topic_name)
    finally:
        conn.close()
    if not rows:
        return pd.DataFrame(columns=COLUMNS)

    j2g = journal_to_group(journal_groups)
    cutoff = ((today or date.today()) - timedelta(days=NEW_DAYS)).isoformat()
    records = []
    for r in rows:
        pub_dt = _pub_dt(r["pub_date"])
        pub_types = json.loads(r["pub_types"] or "[]")
        records.append({
            "id": r["id"], "pmid": r["pmid"], "doi": r["doi"], "title": r["title"],
            "abstract": r["abstract"] or "", "journal": r["journal"] or "",
            "group": j2g.get((r["journal"] or "").lower(), "기타"),
            "pub_date": r["pub_date"] or "", "pub_dt": pub_dt,
            "year": pub_dt.year if pd.notna(pub_dt) else None,
            "authors": json.loads(r["authors"] or "[]"), "mesh": json.loads(r["mesh"] or "[]"),
            "pub_types": pub_types, "is_review": is_review(pub_types),
            "cited_by": r["cited_by"], "oa_url": r["oa_url"], "first_seen": r["first_seen"],
            "is_new": r["first_seen"] >= cutoff, "relevance": r["relevance"] or 0.0,
            "matched_terms": json.loads(r["matched_terms"] or "{}"),
            "status": r["mark_status"] or "", "note": r["mark_note"] or "",
        })
    df = pd.DataFrame.from_records(records, columns=COLUMNS)
    df["year"] = df["year"].astype("Int64")
    return df


REVIEW_PREFIX = "(review) "


def display_title(title: str, review: bool) -> str:
    return REVIEW_PREFIX + title if review else title


@dataclass
class Filters:
    groups: list[str] | None = None       # None = all
    since: date | None = None
    min_relevance: float = 0.0
    text: str = ""
    statuses: list[str] | None = None     # "" means unmarked; None = all
    new_only: bool = False


def apply_filters(df: pd.DataFrame, f: Filters) -> pd.DataFrame:
    m = pd.Series(True, index=df.index)
    if f.groups is not None:
        m &= df["group"].isin(f.groups)
    if f.since is not None:
        m &= df["pub_dt"] >= pd.Timestamp(f.since)
    if f.min_relevance:
        m &= df["relevance"] >= f.min_relevance
    if f.text.strip():
        needle = f.text.strip()
        m &= (df["title"].str.contains(needle, case=False, regex=False)
              | df["abstract"].str.contains(needle, case=False, regex=False))
    if f.statuses is not None:
        m &= df["status"].isin(f.statuses)
    if f.new_only:
        m &= df["is_new"]
    return df[m]


def metrics(df: pd.DataFrame) -> dict[str, float | int]:
    total = len(df)
    top = int(df["group"].astype(str).str.startswith(TOP_GROUP_PREFIX).sum()) if total else 0
    return {
        "total": total,
        "new": int(df["is_new"].sum()) if total else 0,
        "top_ratio": top / total if total else 0.0,
    }


def per_year(df: pd.DataFrame) -> pd.DataFrame:
    s = df.dropna(subset=["year"]).groupby("year").size()
    return s.rename("논문 수").reset_index().rename(columns={"year": "연도"})


def per_journal(df: pd.DataFrame, top: int = 15) -> pd.DataFrame:
    s = df.groupby("journal").size().sort_values(ascending=False).head(top)
    return s.rename("논문 수").reset_index().rename(columns={"journal": "저널"})


def term_frequency(df: pd.DataFrame, kind: str, top: int = 20) -> pd.DataFrame:
    """kind='mesh' for MeSH descriptors, 'keywords' for matched topic terms."""
    c: Counter[str] = Counter()
    for v in df["mesh"] if kind == "mesh" else df["matched_terms"]:
        if kind == "mesh":
            c.update(set(v))
        else:
            c.update({t for terms in v.values() for t in terms})
    return pd.DataFrame(c.most_common(top), columns=["용어", "논문 수"])
