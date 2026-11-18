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
    return conn


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
        row = conn.execute("SELECT id FROM papers WHERE doi = ?", (p.doi,)).fetchone()
    if row is None:
        row = conn.execute("SELECT id FROM papers WHERE pmid = ?", (p.pmid,)).fetchone()

    values = (p.pmid, p.doi, p.title, p.abstract, p.journal, p.pub_date,
              json.dumps(p.authors, ensure_ascii=False), json.dumps(p.mesh, ensure_ascii=False))
    if row is None:
        cur = conn.execute(
            "INSERT INTO papers (pmid, doi, title, abstract, journal, pub_date, authors, mesh, "
            "first_seen, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*values, seen, now),
        )
        return cur.lastrowid, True
    conn.execute(
        "UPDATE papers SET pmid=?, doi=?, title=?, abstract=?, journal=?, pub_date=?, authors=?, "
        "mesh=?, updated_at=? WHERE id=?",
        (*values, now, row["id"]),
    )
    return row["id"], False


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
