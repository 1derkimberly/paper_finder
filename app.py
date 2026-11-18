"""Streamlit dashboard (SPEC §8). Run: streamlit run app.py"""

from __future__ import annotations

from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

from paper_finder import db
from paper_finder.config import (CONFIG_DIR, DEFAULT_DB, Topic, load_journals, load_openalex_sources,
                                 load_settings, load_topics, save_topic)
from paper_finder.dashboard_data import (SORTS, Filters, apply_filters, citation_coverage, display_title,
                                         load_frame, metrics, per_journal, per_year, sort_frame, term_frequency)
from paper_finder.export import to_bibtex, to_csv
from paper_finder.openalex import OpenAlexClient, OpenAlexError
from paper_finder.pipeline import enrich_db, run
from paper_finder.pubmed import PubMedClient
from paper_finder.query import build_query, years_ago

TOPICS_PATH = CONFIG_DIR / "topics.yaml"
JOURNALS_PATH = CONFIG_DIR / "journals.yaml"
DB_PATH = DEFAULT_DB
NEW_TOPIC = "＋ 새 주제 만들기"
STATUS_LABELS = {"": "—", "to-read": "읽을 예정", "read": "읽음", "starred": "★ 즐겨찾기"}
LABEL_TO_STATUS = {v: k for k, v in STATUS_LABELS.items()}
PAGE_SIZE = 20

st.set_page_config(page_title="Paper Finder", page_icon="📄", layout="wide")


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
            y=alt.Y(f"{x}:N", sort="-x", title=None),
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
def cached_frame(topic_name: str, version: int) -> pd.DataFrame:
    return load_frame(DB_PATH, topic_name, load_journals(JOURNALS_PATH))


def data_version() -> int:
    return st.session_state.setdefault("data_version", 0)


def bump_data_version() -> None:
    st.session_state["data_version"] = data_version() + 1


def save_mark(paper_id: int, status: str, note: str) -> None:
    conn = db.connect(DB_PATH)
    try:
        with conn:
            db.set_mark(conn, paper_id, status, note)
    finally:
        conn.close()
    bump_data_version()


def openalex_client() -> OpenAlexClient:
    s = load_settings()
    return OpenAlexClient(api_key=s.openalex_api_key, mailto=s.openalex_mailto)


def run_update(topic: Topic, groups: dict[str, list[str]], years: int) -> None:
    end = date.today()
    start = years_ago(end, years)
    settings = load_settings()
    client = PubMedClient(api_key=settings.ncbi_api_key, email=settings.ncbi_email)
    with st.status("PubMed·OpenAlex 검색 중… (OpenAlex가 붐비면 최대 2분)", expanded=True) as status:
        try:
            res = run(client, topic, groups, start, end, db_path=DB_PATH,
                      openalex=openalex_client(), openalex_sources=load_openalex_sources())
        except Exception as e:  # network / PubMed errors shown to the user
            status.update(label=f"업데이트 실패: {e}", state="error")
            return
        for g in res.groups:
            st.write(f"**{g.group}**: {g.count}건" + (f" — 경고: {g.warnings}" if g.warnings else ""))
        if res.openalex_found:
            st.write(f"OpenAlex 보완 검색: {res.openalex_found}건 → PubMed 결과에 없던 {res.openalex_added}건 추가")
        st.write(f"저장 {len(res.kept)}건, 제외 {len(res.excluded)}건, 신규 {res.new_count}건")
        if res.enrich:
            st.write(f"OpenAlex DOI 매칭: {res.enrich.matched}/{res.enrich.with_doi} ({res.enrich.rate:.0%})")
        for w in res.warnings:
            st.warning(w)
        for p, reason in res.excluded:
            st.caption(f"제외 ({reason}): {p.title}")
        status.update(label=f"업데이트 완료: {len(res.kept)}건 (신규 {res.new_count}건)",
                      state="complete" if not res.warnings else "error", expanded=bool(res.warnings))
    bump_data_version()


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

topics = load_topics(TOPICS_PATH)
journal_groups = load_journals(JOURNALS_PATH)

with st.sidebar:
    st.header("Paper Finder")
    options = list(topics) + [NEW_TOPIC]
    default_topic = st.session_state.get("selected_topic")
    topic_name = st.selectbox("주제", options,
                              index=options.index(default_topic) if default_topic in options else 0)

    if topic_name == NEW_TOPIC:
        with st.form("new_topic"):
            name = st.text_input("주제 이름 (영문·숫자·_)", placeholder="e.g. soft_neural_probes")
            kw = st.text_area("키워드 (한 줄에 하나, 정확한 문구로 검색)",
                              placeholder="transient electronics\nbioresorbable sensor")
            st.caption("만든 뒤 본문의 '검색 키워드'에서 facet(개념 그룹)을 추가해 재현율을 높일 수 있습니다.")
            if st.form_submit_button("주제 만들기", type="primary"):
                try:
                    save_topic(Topic(name=name.strip(), facets={}, exact_phrases=split_lines(kw)), TOPICS_PATH)
                except ValueError as e:
                    st.error(str(e))
                else:
                    st.session_state["selected_topic"] = name.strip()
                    st.rerun()

if topic_name == NEW_TOPIC:
    st.title("새 주제")
    st.info("사이드바에 주제 이름과 키워드를 입력하고 **주제 만들기**를 누르세요. "
            "그다음 **업데이트 실행**으로 논문을 수집합니다.")
    st.stop()

with st.sidebar:

    topic = topics[topic_name]
    st.session_state["selected_topic"] = topic_name

    sel_groups = st.multiselect("저널 그룹", list(journal_groups), default=list(journal_groups))
    years = st.slider("기간 (최근 N년)", 1, 10, 5)
    if st.button("업데이트 실행", type="primary", use_container_width=True, disabled=not sel_groups):
        run_update(topic, {g: journal_groups[g] for g in sel_groups}, years)
    if st.button("인용 수 갱신", use_container_width=True, help="저장된 모든 논문의 인용 수·OA 링크를 OpenAlex에서 다시 받음"):
        refresh_citations()
    if not load_settings().openalex_api_key:
        st.caption("OpenAlex API 키 없음: 검색이 몰리면 보완 검색이 실패할 수 있습니다 (`.env`의 OPENALEX_API_KEY).")
    st.caption(f"DB: `{DB_PATH.relative_to(DB_PATH.parent.parent)}`")


# ------------------------------------------------------------------ keywords

st.title(topic_name)

with st.expander("검색 키워드 (facet 안은 OR, facet끼리는 AND)", expanded=False):
    st.caption("체크를 빼거나 새 용어를 입력해 추가한 뒤 저장하세요. 저장 후 '업데이트 실행'을 누르면 새 검색식으로 다시 수집합니다.")
    new_facets: dict[str, list[str]] = {}
    for facet, terms in topic.facets.items():
        new_facets[facet] = st.multiselect(facet, terms, default=terms, accept_new_options=True,
                                           key=f"facet_{topic_name}_{facet}")
    c1, c2 = st.columns(2)
    add_facet_name = c1.text_input("새 facet 이름", key=f"newfacet_name_{topic_name}")
    add_facet_terms = c2.text_input("새 facet 용어 (쉼표로 구분)", key=f"newfacet_terms_{topic_name}")
    exact = st.multiselect("정확한 문구 (단독으로도 매칭)", topic.exact_phrases, default=topic.exact_phrases,
                           accept_new_options=True, key=f"exact_{topic_name}")
    exclude = st.multiselect("제외 문구 (수집 후 제목·초록에서 걸러냄)", topic.exclude_phrases,
                             default=topic.exclude_phrases, accept_new_options=True, key=f"exclude_{topic_name}")

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
            save_topic(edited, TOPICS_PATH)
        except ValueError as e:
            st.error(str(e))
        else:
            st.success("저장했습니다. '업데이트 실행'으로 다시 수집하세요.")
            st.rerun()


# ------------------------------------------------------------------ data + filters

df_all = cached_frame(topic_name, data_version())
if df_all.empty:
    st.info("저장된 논문이 없습니다. 사이드바에서 **업데이트 실행**을 눌러 PubMed에서 수집하세요.")
    st.stop()

f1, f2, f3, f4 = st.columns([3, 1.4, 2, 1])
text = f1.text_input("제목·초록 검색", placeholder="예: nerve, lactate")
min_rel = f2.number_input("최소 관련도", min_value=0.0, max_value=float(df_all["relevance"].max() or 0),
                          value=0.0, step=1.0)
status_pick = f3.multiselect("읽음 상태", list(STATUS_LABELS.values()), placeholder="전체")
new_only = f4.toggle("신규만", help="최근 7일 안에 처음 수집된 논문")

filters = Filters(
    groups=sel_groups + ["기타"] if sel_groups else [],
    since=years_ago(date.today(), years),
    min_relevance=min_rel,
    text=text,
    statuses=[LABEL_TO_STATUS[s] for s in status_pick] or None,
    new_only=new_only,
)
df = apply_filters(df_all, filters)

m = metrics(df)
c1, c2, c3 = st.columns(3)
c1.metric("총 논문 수", f"{m['total']:,}", help=f"전체 저장 {len(df_all)}건 중 필터 통과")
c2.metric("이번 주 신규", f"{m['new']:,}")
c3.metric("상위 저널 비율", f"{m['top_ratio']:.0%}", help="tier1_* 저널 그룹에 속한 논문 비율")
matched, with_doi = citation_coverage(df)
n_oa_only = int((df["source"] == "openalex").sum())
st.caption(f"인용 수·OA 링크: OpenAlex (DOI 매칭 {matched}/{with_doi}"
           + (f", {matched / with_doi:.0%}" if with_doi else "") + ")"
           + (f" · PubMed 수록이 부분적인 저널에서 OpenAlex로 보완한 논문 {n_oa_only}건" if n_oa_only else ""))

if df.empty:
    st.warning("필터 조건에 맞는 논문이 없습니다.")
    st.stop()

# ------------------------------------------------------------------ charts

t1, t2, t3, t4 = st.tabs(["연도별", "저널별", "키워드", "MeSH"])
with t1:
    st.altair_chart(bar_chart(per_year(df), "연도", "논문 수"), use_container_width=True)
with t2:
    st.altair_chart(bar_chart(per_journal(df), "저널", "논문 수", horizontal=True), use_container_width=True)
with t3:
    kw = term_frequency(df, "keywords")
    st.altair_chart(bar_chart(kw, "용어", "논문 수", horizontal=True), use_container_width=True)
with t4:
    mesh = term_frequency(df, "mesh")
    if mesh.empty:
        st.caption("MeSH 색인이 아직 없는 논문들입니다 (최근 논문은 색인까지 수개월 걸림).")
    else:
        st.altair_chart(bar_chart(mesh, "용어", "논문 수", horizontal=True), use_container_width=True)
        st.caption(f"MeSH가 있는 논문: {sum(bool(x) for x in df['mesh'])} / {len(df)}건")

# ------------------------------------------------------------------ papers

hdr, exp1, exp2 = st.columns([4, 1, 1])
hdr.subheader(f"논문 {len(df)}건")
exp1.download_button("CSV", to_csv(df), file_name=f"{topic_name}.csv", mime="text/csv",
                     use_container_width=True)
exp2.download_button("BibTeX", to_bibtex(df), file_name=f"{topic_name}.bib", mime="application/x-bibtex",
                     use_container_width=True)

v1, v2 = st.columns([3, 1])
view = v1.segmented_control("보기", ["표", "카드"], default="표", label_visibility="collapsed")
sort_by = v2.selectbox("정렬", list(SORTS), label_visibility="collapsed")
df = sort_frame(df, sort_by)

if view == "카드":
    pages = max(1, -(-len(df) // PAGE_SIZE))
    page = st.number_input("페이지", 1, pages, 1, key="card_page") if pages > 1 else 1
    for _, r in df.iloc[(page - 1) * PAGE_SIZE: page * PAGE_SIZE].iterrows():
        badge = "NEW · " if r["is_new"] else ""
        star = "★ " if r["status"] == "starred" else ""
        title = display_title(r["title"], r["is_review"])
        with st.expander(f"{badge}{star}{title}  ·  {r['journal']}, {r['pub_date'][:7]}"):
            authors = r["authors"]
            st.caption("; ".join(authors[:6]) +(f" 외 {len(authors) - 6}명" if len(authors) > 6 else ""))
            links = []
            if r["doi"]:
                links.append(f"[DOI](https://doi.org/{r['doi']})")
            if r["pmid"]:
                links.append(f"[PubMed](https://pubmed.ncbi.nlm.nih.gov/{r['pmid']}/)")
            if r["openalex_id"]:
                links.append(f"[OpenAlex](https://openalex.org/{r['openalex_id']})")
            if r["oa_url"]:
                links.append(f"[무료 전문]({r['oa_url']})")
            cites = f" · 인용 **{r['cited_by']}**" if pd.notna(r["cited_by"]) else ""
            st.markdown(" · ".join(links) + f" · 관련도 **{r['relevance']:.0f}**{cites}")
            if r["source"] == "openalex":
                st.caption("PubMed 검색에서 빠져 OpenAlex 보완 검색으로 수집 (관련도는 제목·초록 기준)")
            st.write(r["abstract"] or "_초록 없음 (출판사가 OpenAlex에 초록을 제공하지 않음)_")
            terms = [t for ts in r["matched_terms"].values() for t in ts]
            if terms:
                st.caption("매칭 용어: " + ", ".join(terms))
            mc1, mc2 = st.columns([1, 3])
            pid = int(r["id"])
            new_status = mc1.selectbox("상태", list(STATUS_LABELS), index=list(STATUS_LABELS).index(r["status"]),
                                       format_func=STATUS_LABELS.get, key=f"st_{pid}")
            new_note = mc2.text_input("메모", value=r["note"], key=f"nt_{pid}")
            if (new_status, new_note) != (r["status"], r["note"]):
                save_mark(pid, new_status, new_note)
                st.rerun()
else:
    table = pd.DataFrame({
        "id": df["id"],
        "신규": df["is_new"].map(lambda x: "NEW" if x else ""),
        "제목": [display_title(t, rv) for t, rv in zip(df["title"], df["is_review"])],
        "링크": df.apply(lambda r: f"https://doi.org/{r['doi']}" if r["doi"]
                       else f"https://pubmed.ncbi.nlm.nih.gov/{r['pmid']}/" if r["pmid"]
                       else f"https://openalex.org/{r['openalex_id']}", axis=1),
        "저널": df["journal"],
        "날짜": df["pub_date"],
        "인용": df["cited_by"],
        "관련도": df["relevance"],
        "전문": df["oa_url"].fillna(""),
        "상태": df["status"].map(STATUS_LABELS),
        "메모": df["note"],
    }).set_index("id")
    if table["인용"].isna().all():  # not enriched yet
        table = table.drop(columns=["인용"])
    edited_table = st.data_editor(
        table,
        hide_index=True,
        use_container_width=True,
        height=min(640, 38 + 35 * len(table)),
        disabled=["신규", "제목", "링크", "저널", "날짜", "인용", "관련도", "전문"],
        column_config={
            "신규": st.column_config.TextColumn(width=50),
            "제목": st.column_config.TextColumn(width="large"),
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
    changed = (edited_table["상태"] != table["상태"]) | (edited_table["메모"].fillna("") != table["메모"])
    if changed.any():
        for pid, row in edited_table[changed].iterrows():
            save_mark(int(pid), LABEL_TO_STATUS.get(row["상태"], ""), row["메모"] or "")
        st.rerun()
    st.caption("초록은 '카드' 보기에서 펼쳐 볼 수 있습니다. 상태·메모는 표에서 바로 고치면 저장됩니다.")
