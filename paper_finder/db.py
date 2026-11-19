"""Storage (SPEC §7): SQLite file locally, Postgres (e.g. Supabase) when deployed.

`connect(target)` takes a Path (SQLite) or a postgres:// URL. Queries are written once with `?` placeholders and
`RETURNING id`; the `Conn` wrapper adapts them to psycopg. Rows are dict-like on both backends (row["col"]).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Union

from paper_finder.config import Topic
from paper_finder.process import Scored

DBTarget = Union[Path, str]   # SQLite file path or postgres URL

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
    facets_json TEXT NOT NULL,     -- keywords the stored links were collected with
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
CREATE TABLE IF NOT EXISTS user_marks (         -- reading status / note per user ('' = local single user)
    user_id   TEXT NOT NULL DEFAULT '',
    paper_id  INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    status    TEXT CHECK (status IN ('to-read', 'read', 'starred')),
    note      TEXT,
    PRIMARY KEY (user_id, paper_id)
);
CREATE TABLE IF NOT EXISTS topic_defs (         -- topic definitions when they live in the DB (deployed)
    name        TEXT PRIMARY KEY,
    definition  TEXT NOT NULL,     -- JSON Topic.to_dict()
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_usage (          -- one row per Claude request (cost tracking / daily budget)
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    day           TEXT NOT NULL,   -- YYYY-MM-DD (local time)
    user_key      TEXT,            -- email / anon:<session> / '' (local or admin) / 'system' (scheduled)
    role          TEXT,            -- visitor | user | admin | system
    kind          TEXT,            -- suggest | next | judge
    model         TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost          REAL             -- estimated USD
);
CREATE TABLE IF NOT EXISTS app_settings (       -- limits the admin can change in the dashboard
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (           -- '의견 보내기' messages from the dashboard
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    user_key  TEXT,            -- email / anon:<session> / '' (local or admin)
    contact   TEXT,            -- optional reply address the sender typed
    page      TEXT,            -- topic or page open when it was sent
    message   TEXT NOT NULL
)
"""

# Columns added after the first release: (table, column, type)
_ADDED_COLUMNS = [
    ("papers", "pub_types", "TEXT"),
    ("papers", "source", "TEXT"),          # 'pubmed' | 'openalex'
    ("papers", "openalex_id", "TEXT"),
    ("papers", "enriched_at", "TEXT"),     # last OpenAlex lookup (date)
    ("papers", "summary", "TEXT"),         # Phase 4: Claude one-line Korean summary
    ("topic_defs", "owner", "TEXT NOT NULL DEFAULT ''"),  # '' = admin / public, email, or anon:<session id>
    ("topic_defs", "title", "TEXT"),       # display name chosen by the owner (name is the unique id)
    ("topic_defs", "created_at", "TEXT"),
]


PG_SCHEMES = ("postgres://", "postgresql://")


def is_postgres(target: DBTarget) -> bool:
    return isinstance(target, str) and target.startswith(PG_SCHEMES)


def exists(target: DBTarget | None) -> bool:
    """Is there a database to read? (A postgres URL always counts; tables are created on connect.)"""
    if target is None:
        return False
    return True if is_postgres(target) else Path(target).exists()


def describe(target: DBTarget) -> str:
    """Short label for the UI, without credentials."""
    if is_postgres(target):
        host = target.split("@")[-1].split("/")[0].split("?")[0]
        return f"Postgres ({host.split(':')[0]})"
    return Path(target).name


class Conn:
    """sqlite3 / psycopg connection with one interface: execute(sql with ?), commit, `with conn:` = transaction."""

    def __init__(self, raw: Any, pg: bool, pool: Any = None):
        self.raw, self.pg, self.pool = raw, pg, pool
        self._tx = None   # Postgres: explicit transaction for `with conn:` (connections are autocommit)

    def execute(self, sql: str, params: tuple | list = ()) -> Any:
        if not self.pg:
            return self.raw.execute(sql, params)
        cur = self.raw.cursor()
        if params:   # psycopg placeholders: ? -> %s, and a literal % (e.g. LIKE 'anon:%') must be doubled
            cur.execute(sql.replace("%", "%%").replace("?", "%s"), params)
        else:
            cur.execute(sql)
        return cur

    def script(self, sql: str) -> None:
        for stmt in sql.split(";"):
            if stmt.strip():
                self.execute(stmt)

    def columns(self, table: str) -> set[str]:
        cur = self.execute(f"SELECT * FROM {table} LIMIT 0")
        return {d[0] for d in cur.description}

    def commit(self) -> None:
        if not (self.pg and self.raw.autocommit):
            self.raw.commit()

    def rollback(self) -> None:
        if not (self.pg and self.raw.autocommit):
            self.raw.rollback()

    def close(self) -> None:
        """SQLite: close. Postgres: end any open (read) transaction and hand the connection back to the pool."""
        if self.pool is None:
            self.raw.close()
            return
        self.pool.putconn(self.raw)

    def __enter__(self) -> Conn:
        if self.pg and self.raw.autocommit:
            self._tx = self.raw.transaction()
            self._tx.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._tx is not None:
            tx, self._tx = self._tx, None
            tx.__exit__(exc_type, exc, tb)      # commit, or roll back on error
            return False
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


_READY: set[str] = set()   # targets whose schema was checked in this process
_POOLS: dict[str, Any] = {}


def _pool(url: str) -> Any:
    """One small connection pool per Postgres URL and process. Opening a TLS connection to the database costs
    several round trips (0.2 s locally, ~0.7 s from a US server to Tokyo); a pooled one is reused."""
    pool = _POOLS.get(url)
    if pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        # prepare_threshold=None: works through Supabase's transaction pooler (port 6543)
        # autocommit: plain reads need no BEGIN/ROLLBACK round trips; `with conn:` opens a real transaction.
        pool = ConnectionPool(url, min_size=1, max_size=5, max_idle=300, open=True, timeout=30,
                              check=ConnectionPool.check_connection,
                              kwargs={"row_factory": dict_row, "prepare_threshold": None, "connect_timeout": 15,
                                      "autocommit": True})
        _POOLS[url] = pool
        if len(_POOLS) == 1:
            import atexit
            atexit.register(close_pools)
    return pool


def close_pools() -> None:
    for pool in _POOLS.values():
        pool.close()
    _POOLS.clear()


def connect(target: DBTarget) -> Conn:
    if is_postgres(target):
        pool = _pool(target)
        conn = Conn(pool.getconn(), pg=True, pool=pool)
    else:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(path)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        conn = Conn(raw, pg=False)
    key = str(target)
    if key not in _READY or not conn.pg:   # SQLite files are cheap to check (and tests reuse paths)
        _ensure_schema(conn)
        _READY.add(key)
    return conn


def _ensure_schema(conn: Conn) -> None:
    schema = SCHEMA
    if conn.pg:
        schema = (schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")
                  .replace("INTEGER NOT NULL REFERENCES", "BIGINT NOT NULL REFERENCES")
                  .replace("REAL", "DOUBLE PRECISION"))
    conn.script(schema)
    for table, column, coltype in _ADDED_COLUMNS:
        if column not in conn.columns(table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    if "user_id" not in conn.columns("user_marks"):   # old single-user table (paper_id primary key)
        conn.script("""
            ALTER TABLE user_marks RENAME TO user_marks_old;
            CREATE TABLE user_marks (
                user_id TEXT NOT NULL DEFAULT '',
                paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                status TEXT CHECK (status IN ('to-read', 'read', 'starred')),
                note TEXT,
                PRIMARY KEY (user_id, paper_id));
            INSERT INTO user_marks (user_id, paper_id, status, note)
                SELECT '', paper_id, status, note FROM user_marks_old;
            DROP TABLE user_marks_old""")
    conn.execute("UPDATE papers SET source = 'pubmed' WHERE source IS NULL AND pmid IS NOT NULL")
    conn.commit()


def data_stamp(conn: Conn) -> str:
    """Changes whenever an update, verdict or topic edit lands (cache key for the dashboard). One round trip."""
    r = conn.execute(
        "SELECT (SELECT COUNT(*) FROM runs) AS rn, (SELECT MAX(finished_at) FROM runs) AS rt, "
        "(SELECT COUNT(*) FROM papers) AS pn, (SELECT MAX(updated_at) FROM papers) AS pt, "
        "(SELECT COUNT(*) FROM topic_defs) AS tn, (SELECT MAX(updated_at) FROM topic_defs) AS tt, "
        "(SELECT COUNT(*) FROM topic_papers WHERE llm_verdict IS NOT NULL) AS vn").fetchone()
    return ".".join(str(r[k]) for k in ("rn", "rt", "pn", "pt", "tn", "tt", "vn"))


def set_enrichment(conn: Conn, paper_id: int, openalex_id: str | None, cited_by: int | None,
                   oa_url: str | None, today: date | None = None) -> None:
    conn.execute(
        "UPDATE papers SET openalex_id = COALESCE(?, openalex_id), cited_by = COALESCE(?, cited_by), "
        "oa_url = COALESCE(?, oa_url), enriched_at = ? WHERE id = ?",
        (openalex_id, cited_by, oa_url, (today or date.today()).isoformat(), paper_id),
    )


def papers_with_doi(conn: Conn) -> list:
    return conn.execute("SELECT id, doi FROM papers WHERE doi IS NOT NULL").fetchall()


def upsert_topic(conn: Conn, topic: Topic) -> int:
    """Insert or update a topic. If its keywords changed, drop old paper links
    (they were matched by the old query); user_marks are per paper and survive."""
    now = datetime.now().isoformat(timespec="seconds")
    facets_json = json.dumps(topic.to_dict(), ensure_ascii=False, sort_keys=True)
    row = conn.execute("SELECT id, facets_json FROM topics WHERE name = ?", (topic.name,)).fetchone()
    if row is None:
        cur = conn.execute("INSERT INTO topics (name, facets_json, created_at) VALUES (?, ?, ?) RETURNING id",
                           (topic.name, facets_json, now))
        return cur.fetchone()["id"]
    if json.loads(row["facets_json"]) != json.loads(facets_json):
        conn.execute("DELETE FROM topic_papers WHERE topic_id = ?", (row["id"],))
        conn.execute("UPDATE topics SET facets_json = ? WHERE id = ?", (facets_json, row["id"]))
    return row["id"]


def upsert_paper(conn: Conn, s: Scored, today: date | None = None) -> tuple[int, bool]:
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
            "source, first_seen, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (*values, seen, now),
        )
        paper_id, is_new = cur.fetchone()["id"], True
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


def link(conn: Conn, topic_id: int, paper_id: int, s: Scored) -> None:
    conn.execute(
        "INSERT INTO topic_papers (topic_id, paper_id, relevance, matched_terms) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(topic_id, paper_id) DO UPDATE SET relevance = excluded.relevance, "
        "matched_terms = excluded.matched_terms",
        (topic_id, paper_id, s.relevance, json.dumps(s.matched_terms, ensure_ascii=False)),
    )


def topic_papers(conn: Conn, topic_name: str, user: str = "") -> list:
    """Papers of a topic with `user`'s reading status / note ('' = local single user)."""
    return conn.execute(
        """SELECT p.*, tp.relevance, tp.matched_terms, tp.llm_verdict,
                  um.status AS mark_status, um.note AS mark_note
           FROM papers p
           JOIN topic_papers tp ON tp.paper_id = p.id
           JOIN topics t ON t.id = tp.topic_id
           LEFT JOIN user_marks um ON um.paper_id = p.id AND um.user_id = ?
           WHERE t.name = ?
           ORDER BY tp.relevance DESC, p.pub_date DESC""",
        (user, topic_name),
    ).fetchall()


def papers_to_judge(conn: Conn, topic_name: str, force: bool = False) -> list:
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


def set_verdict(conn: Conn, topic_id: int, paper_id: int, relevant: bool, reason: str,
                summary: str, model: str, today: date | None = None) -> None:
    verdict = {"relevant": relevant, "reason": reason, "model": model,
               "date": (today or date.today()).isoformat()}
    conn.execute("UPDATE topic_papers SET llm_verdict = ? WHERE topic_id = ? AND paper_id = ?",
                 (json.dumps(verdict, ensure_ascii=False), topic_id, paper_id))
    if summary:
        conn.execute("UPDATE papers SET summary = ? WHERE id = ?", (summary, paper_id))


def record_run(conn: Conn, topic: str, started_at: datetime, trigger: str, kept: int | None,
               new_count: int | None, rejected: int | None = None, error: str | None = None,
               report_path: str | None = None, finished_at: datetime | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO runs (topic, started_at, finished_at, trigger, kept, new_count, rejected, error, report_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (topic, started_at.isoformat(timespec="seconds"),
         (finished_at or datetime.now()).isoformat(timespec="seconds"),
         trigger, kept, new_count, rejected, error, report_path),
    )
    return cur.fetchone()["id"]


def last_run(conn: Conn, topic: str, successful: bool = True) -> Any | None:
    """Most recent run of a topic (only runs without an error when `successful`)."""
    return conn.execute(
        f"SELECT * FROM runs WHERE topic = ? {'AND error IS NULL' if successful else ''} "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (topic,),
    ).fetchone()


def delete_topic(conn: Conn, topic_name: str) -> int:
    """Delete a topic, its paper links and run log. Papers no other topic links to are deleted too, unless the
    user marked them (status / note). Returns the number of deleted papers."""
    conn.execute("DELETE FROM runs WHERE topic = ?", (topic_name,))
    conn.execute("DELETE FROM topics WHERE name = ?", (topic_name,))  # topic_papers cascade
    cur = conn.execute(
        "DELETE FROM papers WHERE id NOT IN (SELECT paper_id FROM topic_papers) "
        "AND id NOT IN (SELECT paper_id FROM user_marks)")
    return cur.rowcount


MARK_STATUSES = ("to-read", "read", "starred")


def set_mark(conn: Conn, paper_id: int, status: str | None, note: str | None, user: str = "") -> None:
    """Set `user`'s reading status/note for a paper. Both empty removes the mark."""
    status = status or None
    note = (note or "").strip() or None
    if status is not None and status not in MARK_STATUSES:
        raise ValueError(f"invalid status {status!r}")
    if status is None and note is None:
        conn.execute("DELETE FROM user_marks WHERE user_id = ? AND paper_id = ?", (user, paper_id))
        return
    conn.execute(
        "INSERT INTO user_marks (user_id, paper_id, status, note) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, paper_id) DO UPDATE SET status = excluded.status, note = excluded.note",
        (user, paper_id, status, note),
    )


# ------------------------------------------------------------------ topic definitions in the DB (deployed mode)

def load_topic_defs(conn: Conn) -> dict[str, Topic]:
    rows = conn.execute("SELECT name, definition FROM topic_defs ORDER BY name").fetchall()
    return {r["name"]: Topic.from_dict(r["name"], json.loads(r["definition"])) for r in rows}


def topic_def_rows(conn: Conn) -> list:
    """name, owner, title, created_at, updated_at of every stored topic."""
    return conn.execute("SELECT name, owner, title, created_at, updated_at FROM topic_defs "
                        "ORDER BY created_at, name").fetchall()


def save_topic_def(conn: Conn, topic: Topic, owner: str | None = None, title: str | None = None) -> None:
    """Insert or update a topic definition. owner/title are set on insert and kept on update unless given."""
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO topic_defs (name, definition, updated_at, owner, title, created_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET definition = excluded.definition, updated_at = excluded.updated_at",
        (topic.name, json.dumps(topic.to_dict(), ensure_ascii=False), now, owner or "", title, now),
    )
    if owner is not None or title is not None:
        conn.execute("UPDATE topic_defs SET owner = COALESCE(?, owner), title = COALESCE(?, title) WHERE name = ?",
                     (owner, title, topic.name))


def count_topics(conn: Conn, owner: str) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM topic_defs WHERE owner = ?", (owner,)).fetchone()["n"]


def expired_visitor_topics(conn: Conn, before: datetime) -> list[str]:
    """Visitor (anon:*) topics created before `before`."""
    rows = conn.execute("SELECT name FROM topic_defs WHERE owner LIKE 'anon:%' AND created_at < ?",
                        (before.isoformat(timespec="seconds"),)).fetchall()
    return [r["name"] for r in rows]


def delete_orphan_visitor_marks(conn: Conn) -> int:
    """Reading marks of visitor sessions that no longer own a topic."""
    return conn.execute("DELETE FROM user_marks WHERE user_id LIKE 'anon:%' AND user_id NOT IN "
                        "(SELECT owner FROM topic_defs WHERE owner LIKE 'anon:%')").rowcount


def delete_orphan_papers(conn: Conn) -> int:
    return conn.execute("DELETE FROM papers WHERE id NOT IN (SELECT paper_id FROM topic_papers) "
                        "AND id NOT IN (SELECT paper_id FROM user_marks)").rowcount


# ------------------------------------------------------------------ Claude usage, settings, size

def record_usage(conn: Conn, user_key: str, role: str, kind: str, model: str, input_tokens: int,
                 output_tokens: int, cost: float, when: datetime | None = None) -> None:
    when = when or datetime.now()
    conn.execute("INSERT INTO api_usage (ts, day, user_key, role, kind, model, input_tokens, output_tokens, cost) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (when.isoformat(timespec="seconds"), when.date().isoformat(), user_key, role, kind, model,
                  input_tokens, output_tokens, cost))


def spent_on(conn: Conn, day: date, exclude_roles: tuple[str, ...] = ()) -> float:
    """Estimated Claude spend (USD) on `day`, optionally leaving some roles out."""
    rows = conn.execute("SELECT role, SUM(cost) AS c FROM api_usage WHERE day = ? GROUP BY role",
                        (day.isoformat(),)).fetchall()
    return float(sum((r["c"] or 0) for r in rows if r["role"] not in exclude_roles))


def usage_rows(conn: Conn, since: date) -> list:
    return conn.execute("SELECT day, role, kind, model, user_key, input_tokens, output_tokens, cost "
                        "FROM api_usage WHERE day >= ? ORDER BY ts", (since.isoformat(),)).fetchall()


def all_settings(conn: Conn) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_settings").fetchall()}


def get_setting(conn: Conn, key: str, default: str) -> str:
    r = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(conn: Conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def add_feedback(conn: Conn, user_key: str, message: str, contact: str = "", page: str = "",
                 when: datetime | None = None) -> None:
    when = when or datetime.now()
    conn.execute("INSERT INTO feedback (ts, user_key, contact, page, message) VALUES (?, ?, ?, ?, ?)",
                 (when.isoformat(timespec="seconds"), user_key, contact, page, message))


def feedback_rows(conn: Conn, limit: int = 200) -> list:
    return conn.execute("SELECT id, ts, user_key, contact, page, message FROM feedback "
                        "ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()


def delete_feedback(conn: Conn, ids: list[int]) -> int:
    return sum(conn.execute("DELETE FROM feedback WHERE id = ?", (i,)).rowcount for i in ids)


def size_bytes(conn: Conn, target: DBTarget) -> int | None:
    if conn.pg:
        return conn.execute("SELECT pg_database_size(current_database()) AS n").fetchone()["n"]
    return Path(target).stat().st_size if Path(target).exists() else None


def table_counts(conn: Conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in ("papers", "topic_defs", "topic_papers", "user_marks", "runs", "api_usage")}


def remove_topic_def(conn: Conn, name: str) -> bool:
    return conn.execute("DELETE FROM topic_defs WHERE name = ?", (name,)).rowcount > 0
