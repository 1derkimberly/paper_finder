"""CSV and BibTeX export of dashboard rows."""

from __future__ import annotations

import re
import unicodedata

import pandas as pd

CSV_COLUMNS = {
    "title": "title", "authors": "authors", "journal": "journal", "pub_date": "pub_date",
    "doi": "doi", "pmid": "pmid", "relevance": "relevance", "cited_by": "cited_by",
    "status": "status", "note": "note", "first_seen": "first_seen", "abstract": "abstract",
}


def to_csv(df: pd.DataFrame) -> bytes:
    out = df[list(CSV_COLUMNS)].copy()
    out["authors"] = out["authors"].map(lambda a: "; ".join(a))
    # utf-8-sig so Excel opens Korean/accents correctly
    return out.to_csv(index=False).encode("utf-8-sig")


_BIB_ESCAPE = str.maketrans({"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
                             "{": r"\{", "}": r"\}"})


def _bib(s: str) -> str:
    return str(s).translate(_BIB_ESCAPE)


def _ascii_word(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]", "", s)


_STOP = {"a", "an", "the", "of", "for", "and", "in", "on", "with", "to", "via", "by"}


def _cite_key(row: pd.Series, used: set[str]) -> str:
    first = row["authors"][0].split(",")[0] if row["authors"] else "anon"
    words = [w for w in re.findall(r"[A-Za-z0-9]+", row["title"]) if w.lower() not in _STOP]
    year = str(row["year"]) if pd.notna(row["year"]) else ""
    base = f"{_ascii_word(first).lower() or 'anon'}{year}{_ascii_word(words[0]).lower() if words else ''}"
    key, n = base, 1
    while key in used:
        key = f"{base}{chr(ord('a') + n)}" if n < 26 else f"{base}{n}"
        n += 1
    used.add(key)
    return key


def to_bibtex(df: pd.DataFrame) -> str:
    used: set[str] = set()
    entries = []
    for _, r in df.iterrows():
        fields = {
            "title": "{" + _bib(r["title"].rstrip(".")) + "}",
            "author": " and ".join(_bib(a) for a in r["authors"]),
            "journal": _bib(r["journal"]),
            "year": str(r["year"]) if pd.notna(r["year"]) else "",
            "doi": r["doi"] or "",
            "pmid": r["pmid"] or "",
            "url": f"https://doi.org/{r['doi']}" if r["doi"] else "",
        }
        body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields.items() if v)
        entries.append(f"@article{{{_cite_key(r, used)},\n{body}\n}}")
    return "\n\n".join(entries) + "\n"
