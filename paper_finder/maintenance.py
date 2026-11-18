"""Housekeeping for the public service (run by the scheduled update and from the admin screen)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from paper_finder import db


@dataclass
class CleanupStats:
    visitor_topics: int = 0
    visitor_marks: int = 0
    papers: int = 0


def cleanup(target: db.DBTarget, visitor_topic_hours: int = 24, now: datetime | None = None) -> CleanupStats:
    """Delete visitor topics older than `visitor_topic_hours` (with their links / run log), visitor reading marks
    that no longer belong to a topic, and papers no topic or mark refers to."""
    stats = CleanupStats()
    if not db.exists(target):
        return stats
    conn = db.connect(target)
    try:
        with conn:
            for name in db.expired_visitor_topics(conn, (now or datetime.now()) - timedelta(hours=visitor_topic_hours)):
                db.delete_topic(conn, name)
                db.remove_topic_def(conn, name)
                stats.visitor_topics += 1
            stats.visitor_marks = db.delete_orphan_visitor_marks(conn)
            stats.papers = db.delete_orphan_papers(conn)
    finally:
        conn.close()
    return stats
