"""Who may do what, and the limits that keep the public service cheap.

Roles (only when Google login is configured; local use is a single admin):
- visitor: not signed in. Papers of the last `visitor_years` year(s) only; `visitor_topics` own topic(s) that live
  for the browser session (deleted after `visitor_topic_hours`).
- user: signed in. Everything, up to `user_topics` own topics.
- admin: ADMIN_EMAILS. No limits, plus the maintenance screen.
Claude calls by visitors and users together stop for the day at `daily_budget_usd` (estimated); admin and the
scheduled update are not blocked but are counted.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, fields
from datetime import date

from paper_finder import db
from paper_finder.llm import ClaudeClient

DEFAULT_LIMITS = {"daily_budget_usd": 1.0, "visitor_years": 1, "visitor_topics": 1, "user_topics": 3,
                  "visitor_topic_hours": 24, "max_years": 10}


@dataclass
class Limits:
    daily_budget_usd: float = 1.0
    visitor_years: int = 1
    visitor_topics: int = 1
    user_topics: int = 3
    visitor_topic_hours: int = 24
    max_years: int = 10


def load_limits(target: db.DBTarget) -> Limits:
    if not db.exists(target):
        return Limits()
    conn = db.connect(target)
    try:
        vals = {}
        for f in fields(Limits):
            raw = db.get_setting(conn, f.name, str(DEFAULT_LIMITS[f.name]))
            vals[f.name] = float(raw) if f.type in ("float", float) else int(float(raw))
        return Limits(**vals)
    finally:
        conn.close()


def save_limits(target: db.DBTarget, limits: Limits) -> None:
    conn = db.connect(target)
    try:
        with conn:
            for k, v in asdict(limits).items():
                db.set_setting(conn, k, str(v))
    finally:
        conn.close()


@dataclass
class Role:
    kind: str                 # visitor | user | admin
    key: str                  # owner / user_marks id: anon:<session>, email, or '' (admin / local)
    email: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin"

    def max_topics(self, limits: Limits) -> int | None:
        return {"visitor": limits.visitor_topics, "user": limits.user_topics}.get(self.kind)

    def max_years(self, limits: Limits) -> int:
        return limits.visitor_years if self.kind == "visitor" else limits.max_years

    @property
    def label(self) -> str:
        return {"visitor": "방문자", "user": "로그인 사용자", "admin": "관리자"}[self.kind]


def topic_id(name: str, owner: str) -> str:
    """Unique stored name: admin topics keep their name; others get a short owner hash (names may repeat)."""
    if not owner:
        return name
    return f"{name}-{hashlib.sha1(owner.encode()).hexdigest()[:6]}"


def spent_today(target: db.DBTarget) -> float:
    """Estimated Claude spend today by visitors and signed-in users (what the daily budget limits)."""
    if not db.exists(target):
        return 0.0
    conn = db.connect(target)
    try:
        return db.spent_on(conn, date.today(), exclude_roles=("admin", "system"))
    finally:
        conn.close()


def budget_message(target: db.DBTarget, role: Role, limits: Limits) -> str | None:
    if role.is_admin:
        return None
    if spent_today(target) >= limits.daily_budget_usd:
        return ("오늘 서비스 전체의 Claude 사용 한도에 도달했습니다. 내일 다시 시도해 주세요 "
                "(검색·수집은 되지만 관련성 판정·요약·용어 제안은 쉽니다).")
    return None


def claude_for(target: db.DBTarget, role_kind: str, user_key: str, api_key: str, model: str,
               limits: Limits | None = None) -> ClaudeClient:
    """ClaudeClient that logs every request to api_usage and (for visitors / users) obeys the daily budget."""
    def on_usage(kind: str, mdl: str, tin: int, tout: int, cost: float) -> None:
        conn = db.connect(target)
        try:
            with conn:
                db.record_usage(conn, user_key, role_kind, kind, mdl, tin, tout, cost)
        finally:
            conn.close()

    check = None
    if role_kind in ("visitor", "user"):
        lim = limits or load_limits(target)
        role = Role(role_kind, user_key)
        check = lambda: budget_message(target, role, lim)  # noqa: E731
    return ClaudeClient(api_key=api_key, model=model, on_usage=on_usage, budget_check=check)
