"""Phase 5: new-paper report (HTML + Markdown) for one topic."""

from __future__ import annotations

import html
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from paper_finder import db
from paper_finder.config import ROOT
from paper_finder.dashboard_data import display_title, load_frame, rejected_mask

REPORTS_DIR = Path(os.environ.get("PAPER_FINDER_REPORTS") or ROOT / "reports")


@dataclass
class Report:
    topic: str
    since: date
    today: date
    papers: pd.DataFrame          # new, not rejected by Claude; best first
    rejected: int                 # new papers Claude judged off-topic (left out)
    total: int                    # all papers stored for the topic
    label: str | None = None      # name shown to people (stored IDs of user topics carry an owner hash)

    @property
    def shown(self) -> str:
        return self.label or self.topic

    @property
    def count(self) -> int:
        return len(self.papers)


def new_papers(db_path: "db.DBTarget", topic: str, journal_groups: dict[str, list[str]], since: date,
               today: date | None = None) -> Report:
    """Papers of `topic` first stored on or after `since`."""
    today = today or date.today()
    df = load_frame(db_path, topic, journal_groups, today=today)
    new = df[df["first_seen"] >= since.isoformat()] if not df.empty else df
    rej = rejected_mask(new) if not new.empty else pd.Series(dtype=bool)
    kept = new[~rej] if not new.empty else new
    if not kept.empty:
        kept = kept.assign(_top=kept["group"].astype(str).str.startswith("tier1"))
        kept = kept.sort_values(["_top", "pub_dt", "relevance"], ascending=[False, False, False],
                                na_position="last", kind="stable").drop(columns="_top")
    return Report(topic=topic, since=since, today=today, papers=kept, rejected=int(rej.sum()), total=len(df))


def _s(v) -> str:
    """Missing values from the DataFrame (None / NaN) as ''."""
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)


def _link(r) -> str:
    if _s(r["doi"]):
        return f"https://doi.org/{r['doi']}"
    if _s(r["pmid"]):
        return f"https://pubmed.ncbi.nlm.nih.gov/{r['pmid']}/"
    return f"https://openalex.org/{r['openalex_id']}" if _s(r["openalex_id"]) else ""


def _authors(authors: list[str]) -> str:
    return "; ".join(authors[:3]) + (" 외" if len(authors) > 3 else "")


def to_markdown(rep: Report) -> str:
    lines = [f"# {rep.shown} 신규 논문 리포트", "",
             f"{rep.since} ~ {rep.today} 사이에 처음 수집된 논문 **{rep.count}건**"
             + (f" (Claude가 관련 없다고 판정한 {rep.rejected}건 제외)" if rep.rejected else "")
             + f" · 전체 저장 {rep.total}건", ""]
    if not rep.count:
        lines.append("새로 수집된 논문이 없습니다.")
    for i, (_, r) in enumerate(rep.papers.iterrows(), 1):
        title = display_title(r["title"], r["is_review"])
        link = _link(r)
        lines.append(f"{i}. **[{title}]({link})**" if link else f"{i}. **{title}**")
        meta = f"   {r['journal']}, {r['pub_date']}"
        if r["authors"]:
            meta += f" · {_authors(r['authors'])}"
        if pd.notna(r["cited_by"]):
            meta += f" · 인용 {r['cited_by']}"
        if _s(r["oa_url"]):
            meta += f" · [무료 전문]({r['oa_url']})"
        lines.append(meta)
        if r["summary"]:
            lines.append(f"   {r['summary']}")
    return "\n".join(lines) + "\n"


_CSS = """
:root { --bg:#ffffff; --fg:#1f2328; --muted:#59636e; --line:#d1d9e0; --accent:#2a78d6; --chip:#eef4fc; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1115; --fg:#e6e8eb; --muted:#9aa4af; --line:#2c323a; --accent:#5aa2f0; --chip:#16263b; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font: 15px/1.55 -apple-system, "Segoe UI", "Malgun Gothic", "Apple SD Gothic Neo", sans-serif; }
main { max-width: 860px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 1.5rem; margin: 0 0 4px; }
.sub { color: var(--muted); margin: 0 0 24px; }
ol { padding-left: 1.4em; }
li { padding: 14px 0; border-top: 1px solid var(--line); }
li a.t { color: var(--fg); font-weight: 600; text-decoration: none; }
li a.t:hover { color: var(--accent); text-decoration: underline; }
.meta { color: var(--muted); font-size: .88rem; margin-top: 2px; }
.meta a { color: var(--accent); }
.sum { margin-top: 6px; }
.chip { display:inline-block; font-size:.75rem; padding:1px 7px; border-radius:10px; background:var(--chip);
        color:var(--accent); margin-right:6px; vertical-align:1px; }
"""


def to_html(rep: Report) -> str:
    e = html.escape
    items = []
    for _, r in rep.papers.iterrows():
        title = e(display_title(r["title"], r["is_review"]))
        link = _link(r)
        head = f'<a class="t" href="{e(link)}">{title}</a>' if link else f"<strong>{title}</strong>"
        chip = f'<span class="chip">{e(str(r["group"]))}</span>' if r["group"] != "기타" else ""
        meta = [f"{e(r['journal'])}, {e(r['pub_date'])}"]
        if r["authors"]:
            meta.append(e(_authors(r["authors"])))
        if pd.notna(r["cited_by"]):
            meta.append(f"인용 {int(r['cited_by'])}")
        if _s(r["oa_url"]):
            meta.append(f'<a href="{e(r["oa_url"])}">무료 전문</a>')
        summary = f'<div class="sum">{e(r["summary"])}</div>' if r["summary"] else ""
        items.append(f"<li>{chip}{head}<div class=\"meta\">{' · '.join(meta)}</div>{summary}</li>")
    body = f"<ol>{''.join(items)}</ol>" if items else "<p>새로 수집된 논문이 없습니다.</p>"
    sub = (f"{rep.since} ~ {rep.today} 사이에 처음 수집된 논문 <strong>{rep.count}건</strong>"
           + (f" (Claude가 관련 없다고 판정한 {rep.rejected}건 제외)" if rep.rejected else "")
           + f" · 전체 저장 {rep.total}건")
    return (f"<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{e(rep.shown)} 신규 논문</title><style>{_CSS}</style></head><body><main>"
            f"<h1>{e(rep.shown)} 신규 논문</h1><p class=\"sub\">{sub}</p>{body}</main></body></html>")


def write_report(rep: Report, out_dir: Path = REPORTS_DIR) -> Path:
    """Write `<date>_<topic>.html` and `.md`; returns the HTML path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / f"{rep.today.isoformat()}_{rep.topic}"
    stem.with_suffix(".md").write_text(to_markdown(rep), encoding="utf-8")
    path = stem.with_suffix(".html")
    path.write_text(to_html(rep), encoding="utf-8")
    return path


def last_update_report(target: db.DBTarget, topic: str, journal_groups: dict[str, list[str]],
                       label: str | None = None) -> tuple[str, bytes] | None:
    """Report of the last successful update, rebuilt from the database (a deployed app keeps no files):
    papers first stored since the update before it. Returns (file name, HTML bytes)."""
    if not db.exists(target):
        return None
    conn = db.connect(target)
    try:
        runs = conn.execute("SELECT started_at FROM runs WHERE topic = ? AND error IS NULL "
                            "ORDER BY started_at DESC, id DESC LIMIT 2", (topic,)).fetchall()
    finally:
        conn.close()
    if not runs:
        return None
    today = date.fromisoformat(runs[0]["started_at"][:10])
    since = date.fromisoformat(runs[1]["started_at"][:10]) if len(runs) > 1 else today - timedelta(days=7)
    rep = new_papers(target, topic, journal_groups, since, today)
    rep.label = label
    return f"{today.isoformat()}_{rep.shown}.html", to_html(rep).encode("utf-8")


def latest_report(topic: str, out_dir: Path = REPORTS_DIR) -> Path | None:
    files = sorted(out_dir.glob(f"*_{topic}.html")) if out_dir.exists() else []
    return files[-1] if files else None
