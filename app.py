"""Streamlit dashboard (SPEC §8). Run: streamlit run app.py"""

from __future__ import annotations

import os
import uuid
from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

from paper_finder import db
from paper_finder.config import (CONFIG_DIR, DEFAULT_DB, Topic, load_journals, load_openalex_sources,
                                 load_settings)
from paper_finder.dashboard_data import (NEW_DAYS, SORTS, Filters, apply_filters, citation_coverage, display_title,
                                         load_frame, metrics, per_journal, per_year, rejected_mask, sort_frame,
                                         term_frequency)
from paper_finder.export import to_bibtex, to_csv
from paper_finder.llm import DEFAULT_JUDGE_MODEL, DEFAULT_MODEL, ClaudeClient, LLMError, next_terms, suggest_terms
from paper_finder.openalex import OpenAlexClient, OpenAlexError
from paper_finder.pipeline import enrich_db, judge_topic
from paper_finder.pubmed import PubMedClient
from paper_finder.report import last_update_report
from paper_finder.topics_store import TopicInfo, TopicStore
from paper_finder.quota import Limits, Role, budget_message, claude_for, load_limits, save_limits, spent_today, topic_id
from paper_finder.maintenance import cleanup
from paper_finder.update import update_topic
from paper_finder.query import build_query, years_ago
from paper_finder.suggest import TermIndex, build_index, related_terms, vocabulary

def _secrets_to_env() -> None:
    """Streamlit secrets (Cloud 'Secrets' box or .streamlit/secrets.toml): copy top-level strings such as
    DATABASE_URL / ANTHROPIC_API_KEY / ADMIN_EMAILS into the environment, where load_settings() reads them."""
    try:
        items = dict(st.secrets)
    except Exception:  # no secrets file
        return
    for k, v in items.items():
        if isinstance(v, str):
            os.environ.setdefault(k, v)


_secrets_to_env()
TOPICS_PATH = CONFIG_DIR / "topics.yaml"
JOURNALS_PATH = CONFIG_DIR / "journals.yaml"
_DB_URL = load_settings().database_url
# Postgres URL when deployed; anything else in DATABASE_URL is a configuration mistake (reported below).
DB_PATH: db.DBTarget = _DB_URL if (_DB_URL and db.is_postgres(_DB_URL)) else DEFAULT_DB
NEW_TOPIC = "＋ 새 주제 만들기"
ADMIN_PAGE = "⚙ 관리 (유지 보수)"
STATUS_LABELS = {"": "읽지 않음","to-read": "읽을 예정", "read": "읽음", "starred": "★ 즐겨찾기"}
LABEL_TO_STATUS = {v: k for k, v in STATUS_LABELS.items()}
PAGE_SIZE = 20

st.set_page_config(page_title="Paper Finder", page_icon="📄", layout="wide")

if _DB_URL and not db.is_postgres(_DB_URL):
    st.error("DATABASE_URL 설정이 Postgres 연결 주소가 아닙니다. `postgresql://` 로 시작하는 Supabase의 "
             "**Transaction pooler** 주소(포트 6543, 비밀번호 포함)를 넣어 주세요. "
             f"지금 값의 시작 부분: `{_DB_URL[:14]}…`")
    st.stop()


# ------------------------------------------------------------------ who is using the app

def auth_enabled() -> bool:
    """Google login is on when .streamlit/secrets.toml (or Streamlit Cloud secrets) has an [auth] section."""
    if os.environ.get("PAPER_FINDER_FAKE_LOGIN") is not None:   # tests: "" = anonymous, else an email
        return True
    try:
        return "auth" in st.secrets
    except Exception:  # no secrets file at all
        return False


def login_email() -> str | None:
    fake = os.environ.get("PAPER_FINDER_FAKE_LOGIN")
    if fake is not None:
        return fake.lower() or None
    return (st.user.get("email") or "").lower() or None if st.user.is_logged_in else None


AUTH = auth_enabled()
EMAIL = login_email() if AUTH else None
SESSION_ID = st.session_state.setdefault("anon_id", uuid.uuid4().hex[:12])
# Local use (no login configured): one admin who owns everything. Public service: visitors (taster limits,
# session-only topic), signed-in users (own topics up to a limit) and ADMIN_EMAILS (no limits + maintenance).
if not AUTH:
    ROLE = Role("admin", "")
elif EMAIL and EMAIL in load_settings().admin_emails:
    ROLE = Role("admin", "", EMAIL)
elif EMAIL:
    ROLE = Role("user", EMAIL, EMAIL)
else:
    ROLE = Role("visitor", f"anon:{SESSION_ID}")
USER_KEY: str = ROLE.key            # owner of new topics and user_marks.user_id
IS_ADMIN = ROLE.is_admin
CAN_MARK = True
STORE = TopicStore(DB_PATH, TOPICS_PATH, use_db=AUTH or db.is_postgres(DB_PATH))
@st.cache_data(ttl=60, show_spinner=False)
def cached_limits(target: str) -> Limits:
    return load_limits(target)


LIMITS = cached_limits(str(DB_PATH)) if AUTH else Limits()


# ------------------------------------------------------------------ helpers

def bar_color() -> str:
    # Single-series bars: one hue, stepped for the active theme.
    theme = getattr(getattr(st, "context", None), "theme", None)
    return "#3987e5" if getattr(theme, "type", None) == "dark" else "#2a78d6"


def bar_chart(df: pd.DataFrame, x: str, y: str, horizontal: bool = False) -> alt.Chart:
    color = bar_color()
    if horizontal:
        return alt.Chart(df).mark_bar(color=color, cornerRadiusEnd=4, size=14).encode(
            x=alt.X(f"{y}:Q", title=None, axis=alt.Axis(tickMinStep=1, grid=True)),
            y=alt.Y(f"{x}:N", sort="-x", title=None, axis=alt.Axis(labelOverlap=False, labelLimit=170)),
            tooltip=[alt.Tooltip(f"{x}:N"), alt.Tooltip(f"{y}:Q")],
        ).properties(height=max(120, 22 * len(df)))
    return alt.Chart(df).mark_bar(color=color, cornerRadiusEnd=4, size=28).encode(
        x=alt.X(f"{x}:O", title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y(f"{y}:Q", title=None, axis=alt.Axis(tickMinStep=1)),
        tooltip=[alt.Tooltip(f"{x}:O"), alt.Tooltip(f"{y}:Q")],
    ).properties(height=260)


def split_lines(s: str) -> list[str]:
    return [t.strip() for t in s.replace(",", "\n").splitlines() if t.strip()]


@st.cache_data(show_spinner=False)
def cached_frame(topic_name: str, version: str, user: str | None = "") -> pd.DataFrame:
    return load_frame(DB_PATH, topic_name, load_journals(JOURNALS_PATH), user=user or "")


@st.cache_data(show_spinner=False)
def term_index(topic_name: str, version: str) -> TermIndex:
    """Word index over the topic's collected papers (Claude-rejected ones left out) for keyword suggestions."""
    df = cached_frame(topic_name, version, USER_KEY)
    if df.empty:
        return TermIndex()
    df = df[~rejected_mask(df)]
    return build_index((df["title"] + ". " + df["abstract"]).tolist())


@st.cache_data(ttl=30, show_spinner=False)
def _db_stamp(target: str) -> str:
    conn = db.connect(target)
    try:
        return db.data_stamp(conn)
    finally:
        conn.close()


def data_version() -> str:
    """Cache key for DB-derived data: this session's own writes + a stamp of the DB contents, so changes made
    outside the dashboard (CLI, weekly scheduled update) show up without restarting it."""
    if db.is_postgres(DB_PATH):
        stamp = _db_stamp(DB_PATH)
    else:
        stamp = str(DB_PATH.stat().st_mtime_ns) if DB_PATH.exists() else "0"
    return f"{st.session_state.setdefault('data_version', 0)}-{stamp}"


def bump_data_version() -> None:
    st.session_state["data_version"] = st.session_state.setdefault("data_version", 0) + 1


def save_mark(paper_id: int, status: str, note: str) -> None:
    conn = db.connect(DB_PATH)
    try:
        with conn:
            db.set_mark(conn, paper_id, status, note, user=USER_KEY or "")
    finally:
        conn.close()
    bump_data_version()


def openalex_client() -> OpenAlexClient:
    s = load_settings()
    return OpenAlexClient(api_key=s.openalex_api_key, mailto=s.openalex_mailto)


def _claude(model: str) -> ClaudeClient | None:
    s = load_settings()
    if not s.anthropic_api_key:
        return None
    if not AUTH and not db.is_postgres(DB_PATH):   # local single user: still log usage for the cost table
        return claude_for(DB_PATH, "admin", "", s.anthropic_api_key, model)
    return claude_for(DB_PATH, ROLE.kind, USER_KEY, s.anthropic_api_key, model, LIMITS)


def claude_client() -> ClaudeClient | None:
    """For term suggestions (Sonnet by default). Visitors / users are held to the daily budget."""
    return _claude(load_settings().claude_model or DEFAULT_MODEL)


def judge_client() -> ClaudeClient | None:
    """For relevance verdicts + summaries (Haiku by default: high volume, low price)."""
    return _claude(load_settings().claude_judge_model or DEFAULT_JUDGE_MODEL)


@st.cache_data(ttl=30, show_spinner=False)
def _budget_note(kind: str, key: str, budget: float) -> str | None:
    return budget_message(DB_PATH, Role(kind, key), LIMITS)


def budget_note() -> str | None:
    """Why Claude features are paused for this user right now (daily budget), or None. Re-checked every 30 s;
    each Claude request also checks the budget itself."""
    return _budget_note(ROLE.kind, USER_KEY, LIMITS.daily_budget_usd) if AUTH else None


def run_update(topic: Topic, groups: dict[str, list[str]], years: int) -> None:
    """Search + store + Claude check + new-paper report (same steps as the weekly automatic update)."""
    settings = load_settings()
    client = PubMedClient(api_key=settings.ncbi_api_key, email=settings.ncbi_email)
    llm = judge_client()
    if llm is not None and (note := budget_note()):
        st.warning(note)
        llm = None
    with st.status("PubMed·OpenAlex 검색 중… (OpenAlex가 붐비면 최대 2분)", expanded=True) as status:
        bar = st.empty()
        out = update_topic(topic, groups, DB_PATH, client, openalex=openalex_client(),
                           openalex_sources=load_openalex_sources(), llm=llm, years=years, trigger="dashboard",
                           judge_progress=lambda d, n: bar.progress(d / n, text=f"Claude 관련성 판정·요약 {d}/{n}"))
        bar.empty()
        if out.error:
            status.update(label=f"업데이트 실패: {out.error}", state="error")
            return
        res = out.run
        for g in res.groups:
            st.write(f"**{g.group}**: {g.count}건" + (f" — 경고: {g.warnings}" if g.warnings else ""))
        if res.openalex_found:
            st.write(f"OpenAlex 보완 검색: {res.openalex_found}건 → PubMed 결과에 없던 {res.openalex_added}건 추가")
        st.write(f"저장 {len(res.kept)}건, 제외 {len(res.excluded)}건, 신규 {res.new_count}건")
        if res.enrich:
            st.write(f"OpenAlex DOI 매칭: {res.enrich.matched}/{res.enrich.with_doi} ({res.enrich.rate:.0%})")
        if out.judge and out.judge.pending:
            st.write(f"Claude 판정: {out.judge.judged}/{out.judge.pending}건, 관련 없음 {out.judge.rejected}건 "
                     f"({llm.model})")
        for w in out.warnings:
            st.warning(w)
        for p, reason in res.excluded:
            st.caption(f"제외 ({reason}): {p.title}")
        st.write(f"신규 논문 리포트: {out.report.count}건 ({out.since} 이후 처음 수집)")
        status.update(label=f"업데이트 완료: {len(res.kept)}건 (신규 {res.new_count}건)",
                      state="complete" if not out.warnings else "error", expanded=bool(out.warnings))
    bump_data_version()


def delete_topic(name: str) -> None:
    removed = 0
    if db.exists(DB_PATH):
        conn = db.connect(DB_PATH)
        try:
            with conn:
                removed = db.delete_topic(conn, name)
        finally:
            conn.close()
    STORE.remove(name)
    for k in [k for k in st.session_state if str(k).endswith(f"_{name}") or f"_{name}_" in str(k)]:
        del st.session_state[k]
    st.session_state.pop("selected_topic", None)
    st.session_state["deleted_msg"] = f"'{name}' 주제를 삭제했습니다 (어느 주제에도 속하지 않게 된 논문 {removed}건 삭제)."
    bump_data_version()
    st.rerun()


TRIGGER_LABELS ={"dashboard": "대시보드", "cli": "명령줄", "schedule": "자동"}


@st.cache_data(show_spinner=False)
def last_update(topic_name: str, version: str) -> str | None:
    if not db.exists(DB_PATH):
        return None
    conn = db.connect(DB_PATH)
    try:
        r = db.last_run(conn, topic_name, successful=False)
    finally:
        conn.close()
    if r is None:
        return None
    when = r["started_at"].replace("T", " ")[:16]
    how = TRIGGER_LABELS.get(r["trigger"], r["trigger"] or "")
    if r["error"]:
        return f"{when} ({how}) — 실패: {r['error'][:80]}"
    return f"{when} ({how}) — 신규 {r['new_count']}건"


@st.cache_data(show_spinner=False)
def cached_report(topic_name: str, version: str) -> tuple[str, bytes] | None:
    """(file name, HTML) of the new-paper report of the last update, built from the database."""
    return last_update_report(DB_PATH, topic_name, load_journals(JOURNALS_PATH))


def refresh_citations() -> None:
    with st.spinner("OpenAlex에서 인용 수 갱신 중…"):
        try:
            stats = enrich_db(openalex_client(), DB_PATH)
        except OpenAlexError as e:
            st.error(str(e))
            return
    st.success(f"인용 수 갱신: {stats.matched}/{stats.with_doi}건 ({stats.rate:.0%})")
    bump_data_version()


# ------------------------------------------------------------------ sidebar

@st.cache_data(ttl=300, show_spinner=False)
def cached_infos(version: str) -> dict[str, TopicInfo]:
    """All topic definitions; refreshed when the DB stamp changes (topic edits, updates) or this session writes."""
    return STORE.infos()


INFOS: dict[str, TopicInfo] = cached_infos(data_version())
# Everyone (the admin included) sees only their own topics; the admin reaches others' from the 관리 screen.
VISIBLE = {n: i for n, i in INFOS.items() if i.owner == USER_KEY}
topics = {n: i.topic for n, i in VISIBLE.items()}
journal_groups = load_journals(JOURNALS_PATH)
if msg := st.session_state.pop("deleted_msg", None):
    st.toast(msg, icon="🗑️")


def topic_label(name: str) -> str:
    if name in (NEW_TOPIC, ADMIN_PAGE):
        return name
    info = VISIBLE[name]
    return info.title or name


def owned_count() -> int:
    return sum(1 for i in INFOS.values() if i.owner == USER_KEY) if ROLE.max_topics(LIMITS) is not None else 0

with st.sidebar:
    st.markdown("### :material/menu_book: Paper Finder")
    st.caption("상위 저널 최신 논문 대시보드")
    options = list(topics) + [NEW_TOPIC] + ([ADMIN_PAGE] if IS_ADMIN else [])
    default_topic = st.session_state.get("selected_topic")
    topic_name = st.selectbox("주제", options, format_func=topic_label,
                              index=options.index(default_topic) if default_topic in options else 0)
    if AUTH and not EMAIL:
        st.info(f"맛보기 모드: 최근 {LIMITS.visitor_years}년 논문, 내 주제 {LIMITS.visitor_topics}개 "
                f"(창을 닫으면 사라짐). 로그인하면 내 주제 {LIMITS.user_topics}개를 계속 쓸 수 있습니다.",
                icon=":material/visibility:")


def create_topic(t: Topic) -> None:
    limit = ROLE.max_topics(LIMITS)
    if limit is not None and owned_count() >= limit:
        st.error(f"내 주제는 {limit}개까지 만들 수 있습니다. 기존 주제를 지운 뒤 다시 만들어 주세요.")
        return
    title = t.name
    t = Topic(name=topic_id(t.name, USER_KEY), facets=t.facets, exact_phrases=t.exact_phrases,
              exclude_phrases=t.exclude_phrases)
    if t.name in INFOS:
        st.error("같은 이름의 주제가 이미 있습니다. 주제 이름을 바꿔 주세요.")
        return
    try:
        STORE.save(t, owner=USER_KEY, title=title)
        bump_data_version()
    except ValueError as e:
        st.error(str(e))
        return
    for k in [k for k in st.session_state if str(k).startswith("nt_")]:
        del st.session_state[k]
    st.session_state["selected_topic"] = t.name
    st.rerun()


def sidebar_footer() -> None:
    """Login / logout, DB label and privacy link: shown on every screen (also with no topics yet)."""
    if AUTH:
        st.divider()
        if EMAIL:
            st.caption(f":material/account_circle: {EMAIL}" + (" · 관리자" if IS_ADMIN else ""))
            st.button("로그아웃", on_click=st.logout, width="stretch", icon=":material/logout:")
        else:
            st.caption("구글 계정으로 로그인하면 내 주제를 계속 쓰고 읽음 표시와 메모를 저장할 수 있습니다.")
            st.button("구글로 로그인", on_click=st.login, type="primary", width="stretch", icon=":material/login:")
    st.caption(f"DB: `{db.describe(DB_PATH)}`")
    st.page_link("pages/privacy.py", label="개인정보처리방침", icon=":material/policy:")


def new_topic_page() -> None:
    """New topic: describe it in words -> Claude proposes the search terms -> pick -> create."""
    if not topics:
        st.title("Paper Finder")
        st.markdown("관심 있는 연구 주제를 문장으로 설명하면 Claude가 검색 키워드를 만들고, 상위 저널의 최신 논문을 "
                    "모아 관련성 판정과 한국어 한 줄 요약까지 붙여 줍니다. 아래에서 첫 주제를 만들어 보세요.")
        st.subheader("새 주제")
    else:
        st.title("새 주제")
    limit = ROLE.max_topics(LIMITS)
    if limit is not None:
        used = owned_count()
        st.caption(f"{ROLE.label}는 내 주제를 {limit}개까지 만들 수 있습니다 (지금 {used}개)."
                   + (f" 방문자 주제는 최근 {LIMITS.visitor_years}년 논문만 모으고, 창을 닫으면 사라집니다."
                      if ROLE.kind == "visitor" else ""))
        if used >= limit:
            st.warning("더 만들 수 없습니다. 사이드바에서 내 주제를 고른 뒤 '주제 삭제'로 지우면 새로 만들 수 있습니다."
                       + (" 로그인하면 더 많이 만들 수 있습니다." if ROLE.kind == "visitor" else ""))
            return
    llm = claude_client()
    if llm is None:
        st.warning("새 주제는 주제 설명을 보고 Claude가 검색 키워드를 만들어 줍니다. "
                   "`.env`에 ANTHROPIC_API_KEY를 넣은 뒤 다시 열어 주세요.")
        return
    if note := budget_note():
        st.warning(note)
        return

    st.caption("어떤 논문을 찾고 싶은지 문장으로 설명하면 Claude가 검색 키워드(개념 그룹·정확한 문구·제외 문구)를 제안합니다. "
               "고른 키워드로 주제를 만든 뒤 사이드바의 **업데이트 실행**으로 논문을 수집합니다.")
    # A form sends what is typed when the button is pressed, so the button works right away
    # (a plain text_area only reports its value after focus leaves it).
    with st.form("nt_form", border=False):
        desc = st.text_area("주제 설명 (한국어 가능)", key="nt_desc", height=100,
                            placeholder="예: 몸 안에서 녹아 없어지는 배터리나 전원 장치. 이식형 의료기기에 전력을 공급하는 연구")
        asked = st.form_submit_button("Claude 용어 제안 받기", type="primary", icon=":material/auto_awesome:")
    if asked and not desc.strip():
        st.warning("주제 설명을 먼저 입력해 주세요.")
    elif asked:
        with st.spinner("Claude가 검색 키워드를 만드는 중…"):
            try:
                sug = suggest_terms(llm, desc.strip(), None)
            except (LLMError, ValueError) as e:
                st.error(str(e))
                sug = None
        if sug is not None:
            for k in [k for k in st.session_state if str(k).startswith("nt_pick_")]:
                del st.session_state[k]
            st.session_state["nt_sug"] = sug
            st.session_state["nt_name"] = sug.name

    sug = st.session_state.get("nt_sug")
    if sug is not None:
        st.divider()
        if sug.notes:
            st.info(sug.notes)
        name = st.text_input("주제 이름 (영문·숫자·_)", key="nt_name")
        st.caption("뺄 용어는 눌러서 선택을 해제하세요. 같은 개념 그룹 안은 OR, 그룹끼리는 AND로 검색합니다.")
        facets = {}
        for facet, terms in sug.facets.items():
            facets[facet] = st.pills(f"개념 그룹: {facet}", terms, selection_mode="multi", default=terms,
                                     key=f"nt_pick_{facet}") or []
        exact = (st.pills("정확한 문구 (단독으로도 매칭)", sug.exact_phrases, selection_mode="multi",
                          default=sug.exact_phrases, key="nt_pick__exact") or []) if sug.exact_phrases else []
        exclude = (st.pills("제외 문구", sug.exclude_phrases, selection_mode="multi", default=sug.exclude_phrases,
                            key="nt_pick__exclude") or []) if sug.exclude_phrases else []
        t = Topic(name=name.strip(), facets={k: v for k, v in facets.items() if v},
                  exact_phrases=exact, exclude_phrases=exclude)
        with st.expander("검색식 미리보기"):
            first_group = next(iter(journal_groups.values()), [])
            st.code(build_query(t, first_group, years_ago(date.today(), 5), date.today())
                    if (t.facets or t.exact_phrases) else "(키워드 없음)", language="text", wrap_lines=True)
        st.caption("만든 뒤에도 '검색 키워드' 패널에서 언제든 고칠 수 있습니다.")
        if st.button("이 키워드로 주제 만들기", type="primary", disabled=not (t.facets or t.exact_phrases)):
            create_topic(t)


def _query(sql: str, params: tuple = ()) -> list:
    conn = db.connect(DB_PATH)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def admin_page() -> None:
    """Maintenance: cost, limits, every topic, update log, cleanup and health checks."""
    st.title("관리")
    st.caption("유지 보수 화면은 관리자에게만 보입니다.")
    t_usage, t_limits, t_topics, t_runs, t_health = st.tabs(
        [":material/payments: 사용량·비용", ":material/tune: 한도 설정", ":material/topic: 주제",
         ":material/history: 업데이트 기록", ":material/health_and_safety: 정리·상태"])

    # -------------------------------------------------- usage / cost
    with t_usage:
        today = date.today()
        conn = db.connect(DB_PATH)
        try:
            rows = [dict(r) for r in db.usage_rows(conn, today.replace(day=1) if today.day > 1 else today)]
            rows30 = [dict(r) for r in db.usage_rows(conn, date.fromordinal(today.toordinal() - 29))]
        finally:
            conn.close()
        u = pd.DataFrame(rows30, columns=["day", "role", "kind", "model", "user_key", "input_tokens",
                                          "output_tokens", "cost"])
        month = pd.DataFrame(rows, columns=u.columns)
        today_df = u[u["day"] == today.isoformat()]
        limited = float(today_df[today_df["role"].isin(["visitor", "user"])]["cost"].sum())
        c1, c2, c3 = st.columns(3)
        c1.metric("오늘 (전체)", f"${today_df['cost'].sum():.3f}", border=True)
        c2.metric("오늘 방문자·사용자", f"${limited:.3f}", border=True,
                  help=f"하루 한도 ${LIMITS.daily_budget_usd:.2f}에 포함되는 사용량")
        c3.metric("이번 달", f"${month['cost'].sum():.2f}", border=True)
        st.progress(min(1.0, limited / LIMITS.daily_budget_usd) if LIMITS.daily_budget_usd else 1.0,
                    text=f"오늘 한도 사용 {limited / LIMITS.daily_budget_usd:.0%}" if LIMITS.daily_budget_usd else "")
        st.caption("금액은 토큰 수와 공시 단가로 계산한 추정치입니다. 실제 청구액은 Anthropic 콘솔에서 확인하세요.")
        if u.empty:
            st.info("최근 30일 동안 기록된 Claude 사용이 없습니다.")
        else:
            daily = u.groupby("day", as_index=False)["cost"].sum().rename(columns={"day": "날짜", "cost": "비용($)"})
            st.altair_chart(alt.Chart(daily).mark_bar(color=bar_color(), cornerRadiusEnd=4).encode(
                x=alt.X("날짜:O", title=None), y=alt.Y("비용($):Q", title=None),
                tooltip=["날짜", alt.Tooltip("비용($):Q", format=".4f")]).properties(height=220), width="stretch")
            by = (u.groupby(["role", "kind", "model"], as_index=False)
                  .agg(요청=("cost", "size"), 입력토큰=("input_tokens", "sum"), 출력토큰=("output_tokens", "sum"),
                       비용=("cost", "sum")).sort_values("비용", ascending=False))
            st.markdown("**최근 30일 · 역할·용도별**")
            st.dataframe(by, hide_index=True, width="stretch",
                         column_config={"비용": st.column_config.NumberColumn(format="$%.4f")})
            top = (u[u["role"].isin(["visitor", "user"])].groupby("user_key", as_index=False)["cost"].sum()
                   .sort_values("cost", ascending=False).head(10))
            if not top.empty:
                st.markdown("**최근 30일 · 많이 쓴 사용자**")
                st.dataframe(top.rename(columns={"user_key": "사용자", "cost": "비용"}), hide_index=True,
                             width="stretch", column_config={"비용": st.column_config.NumberColumn(format="$%.4f")})

    # -------------------------------------------------- limits
    with t_limits:
        with st.form("limits"):
            a, b = st.columns(2)
            budget = a.number_input("하루 Claude 비용 한도 (USD, 방문자+로그인 사용자 합계)", 0.0, 100.0,
                                    float(LIMITS.daily_budget_usd), step=0.5)
            v_years = b.number_input("방문자 검색 기간 (년)", 1, 10, LIMITS.visitor_years)
            v_topics = a.number_input("방문자 주제 수", 0, 5, LIMITS.visitor_topics)
            u_topics = b.number_input("로그인 사용자 주제 수", 0, 20, LIMITS.user_topics)
            hours = a.number_input("방문자 주제 보관 시간 (시간)", 1, 168, LIMITS.visitor_topic_hours)
            max_years = b.number_input("로그인 사용자 최대 검색 기간 (년)", 1, 20, LIMITS.max_years)
            if st.form_submit_button("저장", type="primary"):
                save_limits(DB_PATH, Limits(budget, int(v_years), int(v_topics), int(u_topics), int(hours),
                                            int(max_years)))
                cached_limits.clear()
                _budget_note.clear()
                st.success("저장했습니다.")
                st.rerun()
        st.caption("관리자와 자동 업데이트는 한도에 막히지 않지만 사용량에는 집계됩니다. "
                   "Anthropic 콘솔의 월 지출 한도도 함께 걸어 두세요.")

    # -------------------------------------------------- topics
    with t_topics:
        counts = {r["name"]: r["n"] for r in _query(
            "SELECT t.name AS name, COUNT(*) AS n FROM topic_papers tp JOIN topics t ON t.id = tp.topic_id "
            "GROUP BY t.name")}
        last_runs = {r["topic"]: r["t"] for r in _query(
            "SELECT topic, MAX(started_at) AS t FROM runs WHERE error IS NULL GROUP BY topic")}
        table = pd.DataFrame([{
            "주제": i.title or n, "ID": n,
            "소유": "관리자" if i.is_public else ("방문자" if i.is_visitor else i.owner),
            "만든 날": (i.created_at or "")[:10], "논문": counts.get(n, 0),
            "마지막 업데이트": (last_runs.get(n) or "").replace("T", " ")[:16],
        } for n, i in INFOS.items()])
        st.dataframe(table, hide_index=True, width="stretch")
        if not table.empty:
            with st.expander("주제 삭제", icon=":material/delete:"):
                target = st.selectbox("지울 주제", list(INFOS), format_func=lambda n: f"{INFOS[n].title or n} ({n})")
                st.caption("주제와 그 주제의 논문 연결·업데이트 기록이 지워지며 되돌릴 수 없습니다.")
                confirm = st.text_input("확인을 위해 주제 ID를 그대로 입력하세요", key="admin_delete_confirm",
                                        placeholder=target)
                if st.button("삭제", key="admin_delete", type="primary", disabled=confirm.strip() != target):
                    delete_topic(target)

    # -------------------------------------------------- runs
    with t_runs:
        runs = pd.DataFrame(_query("SELECT topic, started_at, trigger, kept, new_count, rejected, error FROM runs "
                                   "ORDER BY started_at DESC, id DESC LIMIT 50"),
                            columns=["topic", "started_at", "trigger", "kept", "new_count", "rejected", "error"])
        n_err = int(runs["error"].notna().sum()) if not runs.empty else 0
        if n_err:
            st.warning(f"최근 50회 중 실패 {n_err}회. 아래 '오류' 열을 확인하세요.")
        st.dataframe(runs.rename(columns={"topic": "주제", "started_at": "시작", "trigger": "실행", "kept": "저장",
                                          "new_count": "신규", "rejected": "관련 없음", "error": "오류"}),
                     hide_index=True, width="stretch")

    # -------------------------------------------------- cleanup / health
    with t_health:
        conn = db.connect(DB_PATH)
        try:
            size, counts = db.size_bytes(conn, DB_PATH), db.table_counts(conn)
            unjudged = conn.execute("SELECT COUNT(*) AS n FROM topic_papers WHERE llm_verdict IS NULL").fetchone()["n"]
        finally:
            conn.close()
        c1, c2, c3 = st.columns(3)
        c1.metric("DB 크기", f"{(size or 0) / 1e6:.1f} MB", border=True,
                  help="Supabase 무료 등급은 500 MB까지" if db.is_postgres(DB_PATH) else None)
        c2.metric("논문", f"{counts['papers']:,}", border=True)
        c3.metric("미판정 논문", f"{unjudged:,}", border=True, help="Claude 관련성 판정이 아직 없는 주제-논문 연결")
        if db.is_postgres(DB_PATH) and size:
            st.progress(min(1.0, size / 500e6), text=f"Supabase 무료 용량 {size / 500e6:.0%} 사용")
        st.caption(" · ".join(f"{k} {v:,}" for k, v in counts.items()))

        st.markdown("**정리**")
        if st.button("만료된 방문자 주제·안 쓰는 논문 정리", icon=":material/cleaning_services:"):
            stc = cleanup(DB_PATH, LIMITS.visitor_topic_hours)
            st.success(f"방문자 주제 {stc.visitor_topics}개, 방문자 읽음 표시 {stc.visitor_marks}건, "
                       f"어느 주제에도 안 쓰이는 논문 {stc.papers}건을 지웠습니다.")
            bump_data_version()
        st.caption(f"방문자 주제는 만든 지 {LIMITS.visitor_topic_hours}시간이 지나면 지웁니다. "
                   "자동 업데이트가 돌 때도 같은 정리를 합니다.")

        st.markdown("**API 키 상태**")
        s = load_settings()
        for label, ok in [("Claude (ANTHROPIC_API_KEY)", s.anthropic_api_key), ("NCBI (NCBI_API_KEY)", s.ncbi_api_key),
                          ("OpenAlex (OPENALEX_API_KEY)", s.openalex_api_key),
                          ("관리자 이메일 (ADMIN_EMAILS)", s.admin_emails)]:
            st.markdown(f"{':material/check_circle:' if ok else ':material/cancel:'} {label}"
                        + ("" if ok else " — 설정 안 됨"))
        if unjudged and st.button(f"미판정 논문 {unjudged}건 판정", icon=":material/gavel:"):
            llm = judge_client()
            if llm is None:
                st.error("ANTHROPIC_API_KEY가 없습니다.")
            else:
                for name, info in INFOS.items():
                    with st.spinner(f"{info.title or name} 판정 중…"):
                        stats = judge_topic(llm, info.topic, DB_PATH)
                    if stats.pending:
                        st.write(f"{info.title or name}: {stats.judged}/{stats.pending}건, 관련 없음 {stats.rejected}건"
                                 + (f" — 중단: {stats.error}" if stats.error else ""))
                bump_data_version()


if topic_name in (NEW_TOPIC, ADMIN_PAGE):
    with st.sidebar:
        sidebar_footer()
    if topic_name == NEW_TOPIC:
        new_topic_page()
    else:
        admin_page()
    st.stop()

with st.sidebar:

    topic = topics[topic_name]
    st.session_state["selected_topic"] = topic_name
    CAN_MANAGE = VISIBLE[topic_name].owner == USER_KEY   # only own topics are listed, so always true
    MAX_YEARS = ROLE.max_years(LIMITS)

    if CAN_MANAGE:
        st.markdown("**수집 조건**")
        sel_groups = st.multiselect("저널 그룹", list(journal_groups), default=list(journal_groups))
        if MAX_YEARS > 1:
            years = st.slider("기간 (최근 N년)", 1, MAX_YEARS, min(5, MAX_YEARS))
        else:
            years = 1
            st.caption(f"기간: 최근 {years}년 (방문자 맛보기 제한)")
        if st.button("업데이트 실행", type="primary", width="stretch", disabled=not sel_groups,
                     icon=":material/sync:", help="검색 → 저장 → Claude 관련성 판정·요약 → 신규 논문 리포트"):
            run_update(topic, {g: journal_groups[g] for g in sel_groups}, years)
        if st.button("인용 수 갱신", width="stretch", icon=":material/format_quote:",
                     help="저장된 모든 논문의 인용 수·OA 링크를 OpenAlex에서 다시 받음"):
            refresh_citations()
    else:
        sel_groups, years = list(journal_groups), min(5, MAX_YEARS)

    st.divider()
    last = last_update(topic_name, data_version())
    st.caption(f":material/schedule: 마지막 업데이트  \n{last}" if last else ":material/schedule: 아직 업데이트하지 않음")
    report = cached_report(topic_name, data_version())
    if report is not None:
        name, html_bytes = report
        st.download_button(f"신규 논문 리포트 ({name[:10]})", html_bytes, file_name=name, mime="text/html",
                           width="stretch", icon=":material/description:",
                           help="마지막 업데이트 때 새로 수집된 논문을 정리한 HTML 리포트")
    if IS_ADMIN and not load_settings().anthropic_api_key:
        st.caption(":material/warning: Claude API 키 없음: 오탐 판정·요약·용어 제안이 꺼져 있습니다 "
                   "(`.env`의 ANTHROPIC_API_KEY).")
    if IS_ADMIN and not load_settings().openalex_api_key:
        st.caption(":material/warning: OpenAlex API 키 없음: 검색이 몰리면 보완 검색이 실패할 수 있습니다 "
                   "(`.env`의 OPENALEX_API_KEY).")

    if CAN_MANAGE:
        st.divider()
        with st.popover("주제 삭제", width="stretch", icon=":material/delete:"):
            n_stored = len(cached_frame(topic_name, data_version(), USER_KEY))
            st.markdown(f"**{topic_name}** 주제를 지웁니다.")
            st.caption(f"검색 키워드와 저장된 논문 {n_stored}건의 연결, 업데이트 기록이 삭제되며 되돌릴 수 없습니다. "
                       "다른 주제에도 속한 논문과 읽음 상태·메모를 단 논문은 남습니다.")
            confirm = st.text_input("확인을 위해 주제 이름을 그대로 입력하세요", key=f"del_confirm_{topic_name}",
                                    placeholder=topic_name)
            if st.button("삭제", type="primary", disabled=confirm.strip() != topic_name, key=f"del_{topic_name}"):
                delete_topic(topic_name)

    sidebar_footer()


# ------------------------------------------------------------------ keywords

def pretty_name(name: str) -> str:
    """'transient_bioelectronics' -> 'Transient Bioelectronics' (headings only; the id stays as is)."""
    return " ".join(w[:1].upper() + w[1:] for w in name.replace("-", "_").split("_") if w)


st.title(pretty_name(topic_name))
st.caption(f"`{topic_name}` · 개념 그룹 {len(topic.facets)}개"
           + (f" · 정확한 문구 {len(topic.exact_phrases)}개" if topic.exact_phrases else "")
           + f" · 최근 {years}년 · 저널 그룹 {len(sel_groups)}개")

def pending_terms(name: str) -> dict:
    """Claude suggestions the user accepted but has not saved yet (shown as extra options)."""
    return st.session_state.setdefault(f"pending_{name}", {"facets": {}, "exact": [], "exclude": []})


def term_select(label: str, base: list[str], extra: list[str], key: str, vocab: list[str]) -> list[str]:
    """Multiselect of terms. `vocab` (frequent words in collected papers) makes the dropdown a type-ahead."""
    st.session_state.setdefault(key, list(base))
    options = list(dict.fromkeys(base + extra + st.session_state[key] + vocab))
    return st.multiselect(label, options, accept_new_options=True, key=key,
                          placeholder="용어를 입력하면 수집한 논문의 단어가 자동완성됩니다")


def add_recommended(select_key: str, pick_key: str) -> None:
    term = st.session_state.get(pick_key)
    if term:
        cur = list(st.session_state.get(select_key, []))
        if term not in cur:
            st.session_state[select_key] = cur + [term]
    st.session_state[pick_key] = None


def recommend_next(topic: Topic, facet: str, current: list[str], select_key: str, index: TermIndex) -> None:
    """'Next term' chips under a facet. After the user edits the facet, Claude predicts the next terms (cached per
    term set); otherwise, or without a key, words used like these terms in the collected papers are shown."""
    if not current:
        return
    recs, source = [], "수집한 논문 기준"
    llm = claude_client()
    if llm is not None and sorted(current) != sorted(topic.facets.get(facet, [])):
        cache = st.session_state.setdefault("next_terms_cache", {})
        ck = (topic.name, facet, tuple(sorted(current)))
        if ck not in cache:
            try:
                with st.spinner("Claude가 다음 용어를 예상하는 중…"):
                    cache[ck] = next_terms(llm, topic, facet, current)
            except LLMError as e:
                cache[ck] = []
                st.caption(f"Claude 추천 실패: {e}")
        if cache[ck]:
            recs, source = cache[ck], "Claude"
    if not recs:
        known = [t for ts in topic.facets.values() for t in ts] + topic.exact_phrases + topic.exclude_phrases
        recs = related_terms(index, current, known)
    recs = [r for r in recs if r not in current]
    if recs:
        pick_key = f"rec_{select_key}"
        st.pills(f"다음 추천 ({source}) · 누르면 추가", recs, selection_mode="single", key=pick_key,
                 on_change=add_recommended, args=(select_key, pick_key))


def apply_suggestion(name: str) -> None:
    sug = st.session_state.pop(f"sug_{name}", None)
    if sug is None:
        return
    pend = pending_terms(name)

    def add(key: str, picked: list[str], bucket: list[str]) -> None:
        cur = list(st.session_state.get(key, []))
        new = [t for t in picked if t not in cur]
        st.session_state[key] = cur + new
        bucket.extend(t for t in new if t not in bucket)

    for facet in sug.facets:
        picked = st.session_state.pop(f"sugpick_{name}_{facet}", None) or []
        if picked:
            add(f"facet_{name}_{facet}", picked, pend["facets"].setdefault(facet, []))
    add(f"exact_{name}", st.session_state.pop(f"sugpick_{name}__exact", None) or [], pend["exact"])
    add(f"exclude_{name}", st.session_state.pop(f"sugpick_{name}__exclude", None) or [], pend["exclude"])


def keyword_editor() -> None:
    """Admin only: edit / save the topic's search keywords, with Claude and offline suggestions."""
    with st.expander("검색 키워드 편집 (facet 안은 OR, facet끼리는 AND)", expanded=False, icon=":material/tune:"):
        st.caption("체크를 빼거나 새 용어를 입력해 추가한 뒤 저장하세요. 저장 후 '업데이트 실행'을 누르면 새 검색식으로 다시 수집합니다.")
        pend = pending_terms(topic_name)
        index = term_index(topic_name, data_version())
        vocab = vocabulary(index)
        new_facets: dict[str, list[str]] = {}
        for facet in list(topic.facets) + [f for f in pend["facets"] if f not in topic.facets]:
            key = f"facet_{topic_name}_{facet}"
            new_facets[facet] = term_select(facet, topic.facets.get(facet, []), pend["facets"].get(facet, []),
                                            key=key, vocab=vocab)
            recommend_next(topic, facet, new_facets[facet], key, index)
        c1, c2 = st.columns(2)
        add_facet_name = c1.text_input("새 facet 이름", key=f"newfacet_name_{topic_name}")
        add_facet_terms = c2.text_input("새 facet 용어 (쉼표로 구분)", key=f"newfacet_terms_{topic_name}")
        exact = term_select("정확한 문구 (단독으로도 매칭)", topic.exact_phrases, pend["exact"], key=f"exact_{topic_name}",
                            vocab=vocab)
        exclude = term_select("제외 문구 (수집 후 제목·초록에서 걸러냄)", topic.exclude_phrases, pend["exclude"],
                              key=f"exclude_{topic_name}", vocab=vocab)

        if add_facet_name.strip() and split_lines(add_facet_terms):
            new_facets[add_facet_name.strip()] = split_lines(add_facet_terms)
        edited = Topic(name=topic_name, facets={k: v for k, v in new_facets.items() if v},
                       exact_phrases=exact, exclude_phrases=exclude)
        if sel_groups:
            st.code(build_query(edited, journal_groups[sel_groups[0]], years_ago(date.today(), years), date.today())
                    if (edited.facets or edited.exact_phrases) else "(키워드 없음)",
                    language="text", wrap_lines=True)
            st.caption(f"위 검색식은 '{sel_groups[0]}' 그룹 예시입니다. 저널 그룹마다 따로 검색해 합칩니다.")
        if st.button("키워드 저장", disabled=edited.to_dict() == topic.to_dict()):
            try:
                STORE.save(edited)
                bump_data_version()
            except ValueError as e:
                st.error(str(e))
            else:
                st.session_state.pop(f"pending_{topic_name}", None)
                st.success("저장했습니다. '업데이트 실행'으로 다시 수집하세요.")
                st.rerun()

        st.divider()
        st.markdown("**Claude 용어 제안**")
        if claude_client() is None:
            st.caption("`.env`에 ANTHROPIC_API_KEY를 넣으면 Claude가 동의어·약어·MeSH 용어·제외 문구를 제안합니다.")
        else:
            desc = st.text_area("주제 설명 (선택, 한국어 가능)", key=f"desc_{topic_name}", height=68,
                                placeholder="예: 몸 안에서 녹아 없어지는 무선 신경 자극기와 센서")
            if st.button("용어 제안 받기", key=f"suggest_{topic_name}"):
                with st.spinner("Claude가 용어를 고르는 중…"):
                    try:
                        st.session_state[f"sug_{topic_name}"] = suggest_terms(claude_client(), desc, edited)
                    except (LLMError, ValueError) as e:
                        st.error(str(e))
            sug = st.session_state.get(f"sug_{topic_name}")
            if sug is not None:
                if sug.notes:
                    st.info(sug.notes)
                st.caption("넣을 용어만 선택된 채로 두고 '선택한 용어 반영'을 누르세요. 반영 후 '키워드 저장'을 눌러야 저장됩니다.")
                for facet, terms in sug.facets.items():
                    tag = "" if facet in new_facets else " (새 facet)"
                    st.pills(f"{facet}{tag}", terms, selection_mode="multi", default=terms,
                             key=f"sugpick_{topic_name}_{facet}")
                if sug.exact_phrases:
                    st.pills("정확한 문구", sug.exact_phrases, selection_mode="multi", default=sug.exact_phrases,
                             key=f"sugpick_{topic_name}__exact")
                if sug.exclude_phrases:
                    st.pills("제외 문구", sug.exclude_phrases, selection_mode="multi", default=sug.exclude_phrases,
                             key=f"sugpick_{topic_name}__exclude")
                b1, b2 = st.columns([1, 1])
                b1.button("선택한 용어 반영", type="primary", on_click=apply_suggestion, args=(topic_name,),
                          key=f"apply_{topic_name}")
                if b2.button("제안 닫기", key=f"dismiss_{topic_name}"):
                    st.session_state.pop(f"sug_{topic_name}", None)
                    st.rerun()


def keyword_summary() -> None:
    """Read-only view of the search keywords for visitors."""
    with st.expander("검색 키워드 보기 (facet 안은 OR, facet끼리는 AND)", icon=":material/tune:"):
        for facet, terms in topic.facets.items():
            st.markdown(f"**{facet}**: " + ", ".join(terms))
        if topic.exact_phrases:
            st.markdown("**정확한 문구**: " + ", ".join(topic.exact_phrases))
        if topic.exclude_phrases:
            st.markdown("**제외 문구**: " + ", ".join(topic.exclude_phrases))


if CAN_MANAGE:
    keyword_editor()
else:
    keyword_summary()


# ------------------------------------------------------------------ data + filters

df_all = cached_frame(topic_name, data_version(), USER_KEY)
if df_all.empty:
    with st.container(border=True):
        st.markdown("#### 아직 수집한 논문이 없습니다")
        st.caption("사이드바의 **업데이트 실행**을 누르면 PubMed·OpenAlex에서 논문을 모으고, "
                   "Claude가 주제와 관련 있는지 판정해 한국어 요약을 붙입니다.")
    st.stop()

n_rejected = int(rejected_mask(df_all).sum())
with st.container(border=True):
    f1, f2, f3 = st.columns([3.2, 2, 1.2])
    text = f1.text_input("검색", placeholder="제목·초록·요약에서 찾기 (예: nerve, lactate)")
    status_pick = (f2.multiselect("읽음 상태", list(STATUS_LABELS.values()), placeholder="전체") if CAN_MARK
                   else [])
    min_rel = f3.number_input("최소 관련도", min_value=0.0, max_value=float(df_all["relevance"].max() or 0),
                              value=0.0, step=1.0)
    g1, g2 = st.columns([1, 3])
    new_only = g1.toggle("신규만", help=f"최근 {NEW_DAYS}일 안에 처음 수집된 논문")
    show_rejected = (g2.toggle(f"Claude가 관련 없다고 판정한 논문도 보기 ({n_rejected}건)", value=False)
                     if n_rejected else False)

filters = Filters(
    groups=sel_groups + ["기타"] if sel_groups else [],
    since=years_ago(date.today(), years),
    min_relevance=min_rel,
    text=text,
    statuses=[LABEL_TO_STATUS[s] for s in status_pick] or None,
    new_only=new_only,
    hide_rejected=not show_rejected,
)
df = apply_filters(df_all, filters)

m = metrics(df)
n_oa = int(df["oa_url"].fillna("").astype(bool).sum())
c1, c2, c3, c4 = st.columns(4)
c1.metric("총 논문 수", f"{m['total']:,}", help=f"전체 저장 {len(df_all)}건 중 필터 통과", border=True)
c2.metric("이번 주 신규", f"{m['new']:,}", help=f"최근 {NEW_DAYS}일 안에 처음 수집", border=True)
c3.metric("상위 저널 비율", f"{m['top_ratio']:.0%}", help="tier1_* 저널 그룹에 속한 논문 비율", border=True)
c4.metric("무료 전문", f"{n_oa / m['total']:.0%}" if m["total"] else "–",
          help="OpenAlex가 찾은 무료 전문(Open Access) 링크가 있는 논문 비율", border=True)

if df.empty:
    st.warning("필터 조건에 맞는 논문이 없습니다.")
    st.stop()

tab_papers, tab_trends = st.tabs([f":material/article: 논문 {len(df)}건", ":material/bar_chart: 동향"])

# ------------------------------------------------------------------ charts

with tab_trends:
    matched, with_doi = citation_coverage(df)
    n_oa_only = int((df["source"] == "openalex").sum())
    st.caption(f"인용 수·OA 링크: OpenAlex (DOI 매칭 {matched}/{with_doi}"
               + (f", {matched / with_doi:.0%}" if with_doi else "") + ")"
               + (f" · PubMed 수록이 부분적인 저널에서 OpenAlex로 보완한 논문 {n_oa_only}건" if n_oa_only else ""))
    r1a, r1b = st.columns(2)
    with r1a.container(border=True):
        st.markdown("**연도별 논문 수**")
        st.altair_chart(bar_chart(per_year(df), "연도", "논문 수"), width="stretch")
    with r1b.container(border=True):
        st.markdown("**저널별 논문 수**")
        st.altair_chart(bar_chart(per_journal(df, top=10), "저널", "논문 수", horizontal=True), width="stretch")
    r2a, r2b = st.columns(2)
    with r2a.container(border=True):
        st.markdown("**자주 매칭된 키워드**")
        st.altair_chart(bar_chart(term_frequency(df, "keywords", top=12), "용어", "논문 수", horizontal=True),
                        width="stretch")
    with r2b.container(border=True):
        st.markdown("**MeSH 주제어**")
        mesh = term_frequency(df, "mesh", top=12)
        if mesh.empty:
            st.caption("MeSH 색인이 아직 없는 논문들입니다 (최근 논문은 색인까지 수개월 걸림).")
        else:
            st.altair_chart(bar_chart(mesh, "용어", "논문 수", horizontal=True), width="stretch")
            st.caption(f"MeSH가 있는 논문: {sum(bool(x) for x in df['mesh'])} / {len(df)}건")

# ------------------------------------------------------------------ papers


def _md(s: str) -> str:
    """Escape characters that markdown would treat as link/emphasis syntax inside a title."""
    for ch in "\\[]*_`$":
        s = s.replace(ch, "\\" + ch)
    return s


def paper_link(r) -> str:
    if r["doi"]:
        return f"https://doi.org/{r['doi']}"
    if r["pmid"]:
        return f"https://pubmed.ncbi.nlm.nih.gov/{r['pmid']}/"
    return f"https://openalex.org/{r['openalex_id']}" if r["openalex_id"] else ""


# Papers marked "읽음" are dimmed and slightly blurred; hovering brings them back to full clarity.
st.html("""<style>
div[class*="st-key-readcard_"] { opacity: .5; filter: blur(.6px); transition: opacity .15s, filter .15s; }
div[class*="st-key-readcard_"]:hover, div[class*="st-key-readcard_"]:focus-within { opacity: 1; filter: none; }
</style>""")


def paper_card(r) -> None:
    pid = int(r["id"])
    key = f"readcard_{pid}" if r["status"] == "read" else f"card_{pid}"
    with st.container(border=True, key=key):
        badges = []
        if r["is_new"]:
            badges.append(":blue-badge[NEW]")
        if r["is_review"]:
            badges.append(":violet-badge[Review]")
        if r["status"] == "starred":
            badges.append(":orange-badge[★ 즐겨찾기]")
        elif r["status"]:
            badges.append(f":gray-badge[{STATUS_LABELS[r['status']]}]")
        if r["oa_url"]:
            badges.append(":green-badge[무료 전문]")
        if r["llm_relevant"] is False:
            badges.append(":red-badge[관련 없음]")
        if badges:
            st.markdown(" ".join(badges))
        link = paper_link(r)
        title = _md(r["title"])
        st.markdown(f"##### [{title}]({link})" if link else f"##### {title}")
        authors = r["authors"]
        meta = [f"**{r['journal']}**", r["pub_date"][:7]]
        if authors:
            meta.append(", ".join(a.split(",")[0] for a in authors[:3]) + (" 외" if len(authors) > 3 else ""))
        if pd.notna(r["cited_by"]):
            meta.append(f"인용 {int(r['cited_by'])}")
        meta.append(f"관련도 {r['relevance']:.0f}")
        st.caption(" · ".join(meta))
        if r["llm_relevant"] is False:
            st.warning(f"Claude 판정: 주제와 관련 없음 — {r['llm_reason']}", icon=":material/block:")
        if r["summary"]:
            st.markdown(r["summary"])
        with st.expander("초록·링크"):
            st.write(r["abstract"] or "_초록 없음 (출판사가 OpenAlex에 초록을 제공하지 않음)_")
            links = []
            if r["doi"]:
                links.append(f"[DOI](https://doi.org/{r['doi']})")
            if r["pmid"]:
                links.append(f"[PubMed](https://pubmed.ncbi.nlm.nih.gov/{r['pmid']}/)")
            if r["openalex_id"]:
                links.append(f"[OpenAlex](https://openalex.org/{r['openalex_id']})")
            if r["oa_url"]:
                links.append(f"[무료 전문]({r['oa_url']})")
            st.markdown(" · ".join(links))
            terms = [t for ts in r["matched_terms"].values() for t in ts]
            if terms:
                st.caption("매칭 용어: " + ", ".join(terms))
            if r["source"] == "openalex":
                st.caption("PubMed 검색에서 빠져 OpenAlex 보완 검색으로 수집 (관련도는 제목·초록 기준)")
            if authors:
                st.caption("저자: " + "; ".join(authors[:10]) + (f" 외 {len(authors) - 10}명" if len(authors) > 10 else ""))
        if not CAN_MARK:
            return
        mc1, mc2 = st.columns([1, 3])
        new_status = mc1.selectbox("상태", list(STATUS_LABELS), index=list(STATUS_LABELS).index(r["status"]),
                                   format_func=STATUS_LABELS.get, key=f"st_{pid}", label_visibility="collapsed")
        new_note = mc2.text_input("메모", value=r["note"], key=f"nt_{pid}", label_visibility="collapsed",
                                  placeholder="메모 (입력 후 Enter)")
        if (new_status, new_note) != (r["status"], r["note"]):
            save_mark(pid, new_status, new_note)
            st.rerun()


with tab_papers:
    v1, v2, v3, v4 = st.columns([2.2, 1.4, 0.9, 0.9], vertical_alignment="bottom")
    view = v1.segmented_control("보기", ["카드", "표"], default="카드", label_visibility="collapsed")
    sort_by = v2.selectbox("정렬", list(SORTS), index=list(SORTS).index("최신순"), label_visibility="collapsed")
    v3.download_button("CSV", to_csv(df), file_name=f"{topic_name}.csv", mime="text/csv", width="stretch",
                       icon=":material/download:")
    v4.download_button("BibTeX", to_bibtex(df), file_name=f"{topic_name}.bib", mime="application/x-bibtex",
                       width="stretch", icon=":material/download:")
    df = sort_frame(df, sort_by)

    if view == "카드":
        pages = max(1, -(-len(df) // PAGE_SIZE))
        page = 1
        if pages > 1:
            p1, p2 = st.columns([4, 1], vertical_alignment="center")
            page = p2.number_input("페이지", 1, pages, 1, key="card_page", label_visibility="collapsed")
            p1.caption(f"{len(df)}건 중 {(page - 1) * PAGE_SIZE + 1}–{min(page * PAGE_SIZE, len(df))} · "
                       f"페이지 {page}/{pages}")
        for _, r in df.iloc[(page - 1) * PAGE_SIZE: page * PAGE_SIZE].iterrows():
            paper_card(r)
    else:
        table = pd.DataFrame({
            "id": df["id"],
            "신규": df["is_new"].map(lambda x: "NEW" if x else ""),
            "제목": [("✕ " if lr is False else "") + display_title(t, rv)
                   for t, rv, lr in zip(df["title"], df["is_review"], df["llm_relevant"])],
            "요약": df["summary"],
            "링크": df.apply(paper_link, axis=1),
            "저널": df["journal"],
            "날짜": df["pub_date"],
            "인용": df["cited_by"],
            "관련도": df["relevance"],
            "전문": df["oa_url"].fillna(""),
            "상태": df["status"].map(STATUS_LABELS),
            "메모": df["note"],
        }).set_index("id")
        if not CAN_MARK:  # anonymous visitors have no reading marks
            table = table.drop(columns=["상태", "메모"])
        if table["인용"].isna().all():  # not enriched yet
            table = table.drop(columns=["인용"])
        if not (table["요약"] != "").any():  # Claude not run yet
            table = table.drop(columns=["요약"])
        edited_table = st.data_editor(
            table,
            hide_index=True,
            width="stretch",
            height=min(680, 38 + 35 * len(table)),
            disabled=["신규", "제목", "요약", "링크", "저널", "날짜", "인용", "관련도", "전문"],
            column_config={
                "신규": st.column_config.TextColumn(width=50),
                "제목": st.column_config.TextColumn(width="large", help="✕ = Claude가 주제와 관련 없다고 판정"),
                "요약": st.column_config.TextColumn(width="large", help="Claude 한 줄 요약"),
                "링크": st.column_config.LinkColumn(display_text="열기", width=60),
                "인용": st.column_config.NumberColumn(help="OpenAlex 인용 수", width=60),
                "전문": st.column_config.LinkColumn(display_text="OA", width=50, help="무료 전문 (OpenAlex)"),
                "관련도": st.column_config.NumberColumn(format="%.0f", width=60),
                "상태": st.column_config.SelectboxColumn(options=list(STATUS_LABELS.values()), required=True,
                                                        width="small"),
                "메모": st.column_config.TextColumn(width="medium"),
            },
            key=f"table_{topic_name}_{data_version()}",
        )
        changed = ((edited_table["상태"] != table["상태"]) | (edited_table["메모"].fillna("") != table["메모"])
                   if CAN_MARK else pd.Series(False, index=table.index))
        if changed.any():
            for pid, row in edited_table[changed].iterrows():
                save_mark(int(pid), LABEL_TO_STATUS.get(row["상태"], ""), row["메모"] or "")
            st.rerun()
        st.caption("초록은 '카드' 보기에서 펼쳐 볼 수 있습니다."
                   + (" 상태·메모는 표에서 바로 고치면 저장됩니다." if CAN_MARK else ""))
