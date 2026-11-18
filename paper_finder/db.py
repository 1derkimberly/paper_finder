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
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO topics (name, facets_json, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET facets_json = excluded.facets_json",
        (topic.name, json.dumps(topic.to_dict(), ensure_ascii=False), now),
    )
    return conn.execute("SELECT id FROM topics WHERE name = ?", (topic.name,)).fetchone()["id"]


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
        """SELECT p.*, tp.relevance, tp.matched_terms, tp.llm_verdict
           FROM papers p
           JOIN topic_papers tp ON tp.paper_id = p.id
           JOIN topics t ON t.id = tp.topic_id
           WHERE t.name = ?
           ORDER BY tp.relevance DESC, p.pub_date DESC""",
        (topic_name,),
    ).fetchall()
