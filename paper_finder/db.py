"""SQLite storage (SPEC §7)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from paper_finder.config import Topic
from paper_finder.process import Scored

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pmid        TEXT UNIQUE,
    doi         TEXT UNIQUE,
    title       TEXT NOT NULL,
    abstract    TEXT,
    journal     TEXT,
    pub_date    TEXT,
    authors     TEXT,              -- JSON list
    mesh        TEXT,              -- JSON list
    pub_types   TEXT,              -- JSON list (PubMed PublicationType)
    cited_by    INTEGER,           -- Phase 3 (OpenAlex)
    oa_url      TEXT,              -- Phase 3 (OpenAlex)
    first_seen  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    facets_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topic_papers (
    topic_id      INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    paper_id      INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    relevance     REAL,
    matched_terms TEXT,            -- JSON {facet: [terms]}
    llm_verdict   TEXT,            -- Phase 4
    PRIMARY KEY (topic_id, paper_id)
);
CREATE TABLE IF NOT EXISTS runs (               -- Phase 5: one row per update of a topic
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic       TEXT NOT NULL,
    started_at  TEXT NOT NULL,      -- ISO datetime
    finished_at TEXT,
    trigger     TEXT,               -- 'dashboard' | 'cli' | 'schedule'
    kept        INTEGER,
    new_count   INTEGER,
    rejected    INTEGER,            -- Claude off-topic verdicts among new papers
    error       TEXT,
    report_path TEXT
);
CREATE TABLE IF NOT EXISTS user_marks (
    paper_id  INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    status    TEXT CHECK (status IN ('to-read', 'read', 'starred')),
    note      TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Columns added after the first release: (table, column, type)
_ADDED_COLUMNS = [
    ("papers", "pub_types", "TEXT"),
    ("papers", "source", "TEXT"),          # 'pubmed' | 'openalex'
    ("papers", "openalex_id", "TEXT"),
    ("papers", "enriched_at", "TEXT"),     # last OpenAlex lookup (date)
    ("papers", "summary", "TEXT"),         # Phase 4: Claude one-line Korean summary
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, coltype in _ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    conn.execute("UPDATE papers SET source = 'pubmed' WHERE source IS NULL AND pmid IS NOT NULL")
    conn.commit()


def set_enrichment(conn: sqlite3.Connection, paper_id: int, openalex_id: str | None, cited_by: int | None,
                   oa_url: str | None, today: date | None = None) -> None:
    conn.execute(
        "UPDATE papers SET openalex_id = COALESCE(?, openalex_id), cited_by = COALESCE(?, cited_by), "
        "oa_url = COALESCE(?, oa_url), enriched_at = ? WHERE id = ?",
        (openalex_id, cited_by, oa_url, (today or date.today()).isoformat(), paper_id),
    )


def papers_with_doi(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, doi FROM papers WHERE doi IS NOT NULL").fetchall()


def upsert_topic(conn: sqlite3.Connection, topic: Topic) -> int:
    """Insert or update a topic. If its keywords changed, drop old paper links
    (they were matched by the old query); user_marks are per paper and survive."""
    now = datetime.now().isoformat(timespec="seconds")
    facets_json = json.dumps(topic.to_dict(), ensure_ascii=False, sort_keys=True)
    row = conn.execute("SELECT id, facets_json FROM topics WHERE name = ?", (topic.name,)).fetchone()
    if row is None:
        cur = conn.execute("INSERT INTO topics (name, facets_json, created_at) VALUES (?, ?, ?)",
                           (topic.name, facets_json, now))
        return cur.lastrowid
    if json.loads(row["facets_json"]) != json.loads(facets_json):
        conn.execute("DELETE FROM topic_papers WHERE topic_id = ?", (row["id"],))
        conn.execute("UPDATE topics SET facets_json = ? WHERE id = ?", (facets_json, row["id"]))
    return row["id"]


def upsert_paper(conn: sqlite3.Connection, s: Scored, today: date | None = None) -> tuple[int, bool]:
    """Insert or update a paper. Returns (paper_id, is_new). first_seen is never overwritten."""
    p = s.paper
    now = datetime.now().isoformat(timespec="seconds")
    seen = (today or date.today()).isoformat()
    row = None
    if p.doi:
        row = conn.execute("SELECT id, source FROM papers WHERE doi = ?", (p.doi,)).fetchone()
    if row is None and p.pmid:
        row = conn.execute("SELECT id, source FROM papers WHERE pmid = ?", (p.pmid,)).fetchone()

    values = (p.pmid, p.doi, p.title, p.abstract, p.journal, p.pub_date,
              json.dumps(p.authors, ensure_ascii=False), json.dumps(p.mesh, ensure_ascii=False),
              json.dumps(p.pub_types, ensure_ascii=False), p.source)
    if row is None:
        cur = conn.execute(
            "INSERT INTO papers (pmid, doi, title, abstract, journal, pub_date, authors, mesh, pub_types, "
            "source, first_seen, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*values, seen, now),
        )
        paper_id, is_new = cur.lastrowid, True
    elif p.source == "openalex" and row["source"] == "pubmed":
        # Never replace a PubMed record (PMID, MeSH, publication types) with OpenAlex metadata.
        paper_id, is_new = row["id"], False
    else:
        conn.execute(
            "UPDATE papers SET pmid=COALESCE(?, pmid), doi=COALESCE(?, doi), title=?, abstract=?, journal=?, "
            "pub_date=?, authors=?, mesh=?, pub_types=?, source=?, updated_at=? WHERE id=?",
            (*values, now, row["id"]),
        )
        paper_id, is_new = row["id"], False
    if p.openalex_id or p.cited_by is not None:
        set_enrichment(conn, paper_id, p.openalex_id, p.cited_by, p.oa_url, today)
    return paper_id, is_new


def link(conn: sqlite3.Connection, topic_id: int, paper_id: int, s: Scored) -> None:
    conn.execute(
        "INSERT INTO topic_papers (topic_id, paper_id, relevance, matched_terms) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(topic_id, paper_id) DO UPDATE SET relevance = excluded.relevance, "
        "matched_terms = excluded.matched_terms",
        (topic_id, paper_id, s.relevance, json.dumps(s.matched_terms, ensure_ascii=False)),
    )


def topic_papers(conn: sqlite3.Connection, topic_name: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT p.*, tp.relevance, tp.matched_terms, tp.llm_verdict,
                  um.status AS mark_status, um.note AS mark_note
           FROM papers p
           JOIN topic_papers tp ON tp.paper_id = p.id
           JOIN topics t ON t.id = tp.topic_id
           LEFT JOIN user_marks um ON um.paper_id = p.id
           WHERE t.name = ?
           ORDER BY tp.relevance DESC, p.pub_date DESC""",
        (topic_name,),
    ).fetchall()


def papers_to_judge(conn: sqlite3.Connection, topic_name: str, force: bool = False) -> list[sqlite3.Row]:
    """Linked papers without an LLM verdict for this topic (all of them with force)."""
    return conn.execute(
        f"""SELECT tp.topic_id, p.id, p.title, p.abstract, p.journal
            FROM papers p
            JOIN topic_papers tp ON tp.paper_id = p.id
            JOIN topics t ON t.id = tp.topic_id
            WHERE t.name = ? {'' if force else 'AND tp.llm_verdict IS NULL'}
            ORDER BY p.id""",
        (topic_name,),
    ).fetchall()


def set_verdict(conn: sqlite3.Connection, topic_id: int, paper_id: int, relevant: bool, reason: str,
                summary: str, model: str, today: date | None = None) -> None:
    verdict = {"relevant": relevant, "reason": reason, "model": model,
               "date": (today or date.today()).isoformat()}
    conn.execute("UPDATE topic_papers SET llm_verdict = ? WHERE topic_id = ? AND paper_id = ?",
                 (json.dumps(verdict, ensure_ascii=False), topic_id, paper_id))
    if summary:
        conn.execute("UPDATE papers SET summary = ? WHERE id = ?", (summary, paper_id))


def record_run(conn: sqlite3.Connection, topic: str, started_at: datetime, trigger: str, kept: int | None,
               new_count: int | None, rejected: int | None = None, error: str | None = None,
               report_path: str | None = None, finished_at: datetime | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO runs (topic, started_at, finished_at, trigger, kept, new_count, rejected, error, report_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (topic, started_at.isoformat(timespec="seconds"),
         (finished_at or datetime.now()).isoformat(timespec="seconds"),
         trigger, kept, new_count, rejected, error, report_path),
    )
    return cur.lastrowid


def last_run(conn: sqlite3.Connection, topic: str, successful: bool = True) -> sqlite3.Row | None:
    """Most recent run of a topic (only runs without an error when `successful`)."""
    return conn.execute(
        f"SELECT * FROM runs WHERE topic = ? {'AND error IS NULL' if successful else ''} "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (topic,),
    ).fetchone()


def delete_topic(conn: sqlite3.Connection, topic_name: str) -> int:
    """Delete a topic, its paper links and run log. Papers no other topic links to are deleted too, unless the
    user marked them (status / note). Returns the number of deleted papers."""
    conn.execute("DELETE FROM runs WHERE topic = ?", (topic_name,))
    conn.execute("DELETE FROM topics WHERE name = ?", (topic_name,))  # topic_papers cascade
    cur = conn.execute(
        "DELETE FROM papers WHERE id NOT IN (SELECT paper_id FROM topic_papers) "
        "AND id NOT IN (SELECT paper_id FROM user_marks)")
    return cur.rowcount


MARK_STATUSES = ("to-read", "read", "starred")


def set_mark(conn: sqlite3.Connection, paper_id: int, status: str | None, note: str | None) -> None:
    """Set a paper's reading status/note. Both empty removes the mark."""
    status = status or None
    note = (note or "").strip() or None
    if status is not None and status not in MARK_STATUSES:
        raise ValueError(f"invalid status {status!r}")
    if status is None and note is None:
        conn.execute("DELETE FROM user_marks WHERE paper_id = ?", (paper_id,))
        return
    conn.execute(
        "INSERT INTO user_marks (paper_id, status, note) VALUES (?, ?, ?) "
        "ON CONFLICT(paper_id) DO UPDATE SET status = excluded.status, note = excluded.note",
        (paper_id, status, note),
    )
