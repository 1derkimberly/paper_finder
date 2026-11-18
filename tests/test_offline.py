"""Offline unit tests (no network)."""

from datetime import date

from paper_finder import db
from paper_finder.config import CONFIG_DIR, load_journals, load_topics
from conftest import FIXTURE_TOPICS
from paper_finder.process import dedupe, process
from paper_finder.pubmed import Paper, is_review, parse_pubmed_xml
from paper_finder.query import build_query, years_ago

TOPIC = load_topics(FIXTURE_TOPICS)["transient_bioelectronics"]

SAMPLE_XML = b"""<?xml version="1.0"?>
<PubmedArticleSet>
 <PubmedArticle>
  <MedlineCitation>
   <PMID>41501198</PMID>
   <Article>
    <Journal><JournalIssue><PubDate><Year>2026</Year><Month>Feb</Month></PubDate></JournalIssue>
     <ISOAbbreviation>Nature</ISOAbbreviation></Journal>
    <ArticleTitle>Soft <i>biodegradable</i> implants for long-distance and wide-angle sensing.</ArticleTitle>
    <ELocationID EIdType="doi">10.1038/S41586-025-09874-3</ELocationID>
    <Abstract><AbstractText Label="BACKGROUND">Wireless bioresorbable sensor.</AbstractText>
     <AbstractText>Second part.</AbstractText></Abstract>
    <AuthorList><Author><LastName>Kim</LastName><ForeName>A</ForeName></Author>
     <Author><CollectiveName>Consortium X</CollectiveName></Author></AuthorList>
    <ArticleDate><Year>2026</Year><Month>01</Month><Day>07</Day></ArticleDate>
    <PublicationTypeList><PublicationType UI="D016428">Journal Article</PublicationType>
     <PublicationType UI="D016454">Review</PublicationType></PublicationTypeList>
   </Article>
   <MedlineJournalInfo><MedlineTA>Nature</MedlineTA></MedlineJournalInfo>
   <MeshHeadingList><MeshHeading><DescriptorName>Absorbable Implants</DescriptorName></MeshHeading></MeshHeadingList>
  </MedlineCitation>
 </PubmedArticle>
 <PubmedArticle>
  <MedlineCitation>
   <PMID>2</PMID>
   <Article>
    <Journal><JournalIssue><PubDate><MedlineDate>2025 Nov-Dec</MedlineDate></PubDate></JournalIssue></Journal>
    <ArticleTitle>Other</ArticleTitle>
   </Article>
  </MedlineCitation>
  <PubmedData><ArticleIdList><ArticleId IdType="doi">10.1/abc</ArticleId></ArticleIdList></PubmedData>
 </PubmedArticle>
</PubmedArticleSet>"""


def test_query_tags_every_term_with_tiab():
    q = build_query(TOPIC, ["Nature", "Nat Commun"], date(2021, 11, 18), date(2026, 11, 18))
    assert '"neural interface"[tiab]' in q
    assert '"transient electronics"[tiab]' in q
    assert "bioresorbable[tiab]" in q
    assert '("Nature"[ta] OR "Nat Commun"[ta])' in q
    assert q.endswith("2021/11/18:2026/11/18[dp]")
    # every bare word in the topic clause carries a field tag
    topic_part = q.split(' AND ("Nature"')[0]
    for tok in topic_part.replace("(", " ").replace(")", " ").split():
        if tok in ("AND", "OR") or tok.endswith('"') or tok.startswith('"'):
            continue
        assert tok.endswith("[tiab]"), tok


def test_exclude_phrases_not_in_query():
    q = build_query(TOPIC, ["Sci Adv"], date(2021, 1, 1), date(2026, 1, 1))
    assert "excitation" not in q


def test_journal_groups_within_size_limit():
    for group, journals in load_journals(CONFIG_DIR / "journals.yaml").items():
        assert len(journals) <= 10, group


def test_years_ago_leap_day():
    assert years_ago(date(2024, 2, 29), 5) == date(2019, 2, 28)


def test_parse_pubmed_xml():
    p1, p2 = parse_pubmed_xml(SAMPLE_XML)
    assert p1.pmid == "41501198"
    assert p1.title.startswith("Soft biodegradable implants")
    assert p1.doi == "10.1038/s41586-025-09874-3"
    assert p1.pub_date == "2026-01-07"  # ArticleDate preferred
    assert p1.authors == ["Kim, A", "Consortium X"]
    assert p1.abstract.startswith("BACKGROUND: Wireless")
    assert p1.mesh == ["Absorbable Implants"]
    assert p1.pub_types == ["Journal Article", "Review"]
    assert p2.pub_date == "2025-11"
    assert p2.doi == "10.1/abc"
    assert p2.pub_types == []


def test_is_review():
    assert is_review(["Journal Article", "Review"])
    assert is_review(["Systematic Review"])
    assert is_review(["Meta-Analysis"])
    assert not is_review(["Journal Article", "Research Support, Non-U.S. Gov't"])
    assert not is_review([])


def test_migration_adds_pub_types(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE papers (id INTEGER PRIMARY KEY, pmid TEXT UNIQUE, doi TEXT UNIQUE, "
                "title TEXT NOT NULL, abstract TEXT, journal TEXT, pub_date TEXT, authors TEXT, mesh TEXT, "
                "cited_by INTEGER, oa_url TEXT, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL)")
    old.commit()
    old.close()
    conn = db.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)")}
    assert "pub_types" in cols
    conn.close()


def test_dedupe_doi_pmid_title():
    ps = [
        Paper(pmid="1", title="A study", doi="10.1/x"),
        Paper(pmid="2", title="Different", doi="10.1/X"),        # same DOI
        Paper(pmid="1", title="Whatever"),                        # same PMID
        Paper(pmid="3", title="A  Study!"),                        # same normalized title
        Paper(pmid="4", title="Unique"),
    ]
    assert [p.pmid for p in dedupe(ps)] == ["1", "4"]


def test_false_positive_excluded_and_scoring():
    laser = Paper(pmid="9", doi="10.1126/sciadv.adv4436",
                  title="Ultrahigh-speed laser drilling of transparent materials via transient electronic excitation",
                  abstract="We use a femtosecond laser sensor ...")
    good = Paper(pmid="8", title="Wireless and bioresorbable triboelectric nerve block system",
                 abstract="An implantable stimulator for transient electronics.")
    kept, excluded = process([laser, good], TOPIC)
    assert [p.pmid for p, _ in excluded] == ["9"]
    assert excluded[0][1] == "transient electronic excitation"
    (s,) = kept
    assert set(s.matched_terms) == {"material", "electronics", "application", "exact_phrases"}
    assert s.relevance == 4
    # "electronic" must not match inside "electronics" substring-wise as a separate hit? it's word-bounded
    assert "electronic" not in s.matched_terms["electronics"]


def test_db_upsert_keeps_first_seen(tmp_path):
    good = Paper(pmid="8", title="Bioresorbable sensor", doi="10.1/y", abstract="wireless implant")
    kept, _ = process([good], TOPIC)
    conn = db.connect(tmp_path / "t.db")
    with conn:
        tid = db.upsert_topic(conn, TOPIC)
        pid, new = db.upsert_paper(conn, kept[0], today=date(2026, 1, 1))
        db.link(conn, tid, pid, kept[0])
        assert new
        pid2, new2 = db.upsert_paper(conn, kept[0], today=date(2026, 2, 1))
        db.link(conn, tid, pid2, kept[0])
    assert (pid2, new2) == (pid, False)
    rows = db.topic_papers(conn, TOPIC.name)
    assert len(rows) == 1 and rows[0]["first_seen"] == "2026-01-01"
