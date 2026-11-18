"""Where topic definitions live: config/topics.yaml for local single-user use, the database otherwise.

In the database each topic has an owner ('' = admin / public, an email, or anon:<session> for a visitor).
The first time the database has no topics, the ones in topics.yaml (from the repository) are copied in as
public topics.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from paper_finder import db
from paper_finder.config import Topic, load_topics, remove_topic, save_topic, validate_topic


@dataclass
class TopicInfo:
    topic: Topic
    owner: str = ""
    title: str | None = None
    created_at: str | None = None

    @property
    def is_public(self) -> bool:
        return self.owner == ""

    @property
    def is_visitor(self) -> bool:
        return self.owner.startswith("anon:")


class TopicStore:
    def __init__(self, db_target: db.DBTarget, yaml_path: Path, use_db: bool | None = None):
        self.db_target = db_target
        self.yaml_path = yaml_path
        self._use_db = use_db

    @property
    def in_db(self) -> bool:
        return self._use_db if self._use_db is not None else db.is_postgres(self.db_target)

    def infos(self) -> dict[str, TopicInfo]:
        if not self.in_db:
            topics = load_topics(self.yaml_path) if self.yaml_path.exists() else {}
            return {n: TopicInfo(t) for n, t in topics.items()}
        conn = db.connect(self.db_target)
        try:
            topics = db.load_topic_defs(conn)
            if not topics and self.yaml_path.exists():
                with conn:
                    for t in load_topics(self.yaml_path).values():
                        db.save_topic_def(conn, t, owner="")
                topics = db.load_topic_defs(conn)
            meta = {r["name"]: r for r in db.topic_def_rows(conn)}
        finally:
            conn.close()
        return {n: TopicInfo(t, meta[n]["owner"] or "", meta[n]["title"], meta[n]["created_at"])
                for n, t in topics.items()}

    def load(self, include_visitors: bool = True) -> dict[str, Topic]:
        return {n: i.topic for n, i in self.infos().items() if include_visitors or not i.is_visitor}

    def save(self, topic: Topic, owner: str | None = None, title: str | None = None) -> None:
        if not self.in_db:
            save_topic(topic, self.yaml_path)
            return
        validate_topic(topic)
        conn = db.connect(self.db_target)
        try:
            with conn:
                db.save_topic_def(conn, topic, owner=owner, title=title)
        finally:
            conn.close()

    def remove(self, name: str) -> bool:
        if not self.in_db:
            return remove_topic(name, self.yaml_path)
        conn = db.connect(self.db_target)
        try:
            with conn:
                return db.remove_topic_def(conn, name)
        finally:
            conn.close()

    def count_owned(self, owner: str) -> int:
        if not self.in_db:
            return 0
        conn = db.connect(self.db_target)
        try:
            return db.count_topics(conn, owner)
        finally:
            conn.close()
