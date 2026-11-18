# 최신 연구논문 대시보드: 개발 스펙 (Claude Code 전달용)

- 작성일: 2026-11-18
- 분야: Biomedical Engineering (초기 주제: Transient / Biodegradable / Bioresorbable electronics)
- 개발 환경: **Claude Code** (본인 PC, 로컬). 탐색과 테스트는 Cowork에서 완료.
- 사용법: 이 문서를 리포지토리의 `docs/SPEC.md`로 두고, `CLAUDE.md`에서 참조하게 한 뒤 "SPEC.md의 Phase 1부터 구현해줘"라고 요청한다.

---

## 1. 목표

사용자가 키워드를 1개 이상 입력하면 다음을 수행한다.

1. 키워드와 유사 키워드로 확장한다.
2. 지정한 **상위 저널**에서 **최근 5년 이내** 출판된 논문을 수집한다.
3. 중복을 제거하고 관련도 순으로 정렬한다.
4. 대시보드에서 탐색·필터링·저장·내보내기를 할 수 있게 한다.

## 2. 기술 스택

| 영역 | 선택 |
|---|---|
| 언어 | Python 3.11+ |
| 대시보드 | Streamlit |
| 저장소 | SQLite (단일 파일) |
| 주 데이터 소스 | PubMed E-utilities (esearch, efetch) |
| 보완 데이터 소스 | OpenAlex (인용 수, PubMed 미수록 저널) |
| 선택 | Semantic Scholar (TLDR, 임베딩), Claude API (키워드 확장, 관련성 판정, 요약) |
| 설정 | `config/journals.yaml`, `config/topics.yaml`, `.env` (API 키) |

## 3. Cowork 테스트에서 확인된 사항 (설계에 반드시 반영)

테스트 조건: 2021-11-18 ~ 2026-11-18, PubMed 기준.

| 검색 방식 | 결과 | 결론 |
|---|---|---|
| 태그 없이 검색 (`transient electronics`) | 17,743건 | ❌ 자동 용어 변환 때문에 노이즈가 대부분 ("transient" → "transients and migrants") |
| 정확한 문구 + 최상위 저널 | 10건 (그중 4건은 물리학 오탐) | ⚠️ 재현율이 낮고, "transient electronic excitation" 같은 표현이 오탐을 만든다 |
| **개념 그룹 AND 조합 + `[tiab]` + 최상위 저널** | **31건, 상위 20건 중 19건 관련** | ✅ 채택 |

추가로 확인된 사항:

- PubMed 검색식은 **불리언 연산자 개수 제한**이 있다 (사용한 MCP 도구 기준 20개 이하). 원본 E-utilities API는 제한이 더 느슨하지만, 검색식이 길면 URL 길이 문제가 생긴다. 따라서 **저널 그룹별로 나눠 검색하고 PMID 기준으로 합친다.**
- 모든 검색어에는 `[tiab]` 태그를 붙인다 (자동 용어 변환 방지).
- OpenAlex(api.openalex.org)는 Cowork 클라우드 환경에서는 차단되어 테스트하지 못했다. 로컬에서 접속을 확인해야 한다.

## 4. 키워드 모델: 개념 그룹 구조

키워드는 단순 동의어 목록이 아니라 **개념 그룹(facet)** 으로 관리한다. 같은 그룹 안은 OR, 그룹끼리는 AND로 연결한다.

```yaml
# config/topics.yaml
transient_bioelectronics:
  facets:
    material:     [bioresorbable, biodegradable, transient, bioabsorbable, dissolvable]
    electronics:  [electronics, electronic, wireless, bioelectronic, bioelectronics, piezoelectric, triboelectric]
    application:  [implant, implantable, sensor, stimulator, "neural interface", pacemaker]
  exact_phrases:  # 단독으로도 매칭
    - "transient electronics"
    - "bioresorbable electronics"
    - "biodegradable electronics"
  exclude_phrases:  # 오탐 방지
    - "transient electronic excitation"
    - "transient absorption"
```

생성되는 검색식 형태:

```
((material OR ...)[tiab] AND (electronics OR ...)[tiab] AND (application OR ...)[tiab])
  OR ("exact phrase"[tiab] OR ...)
AND (저널 그룹 N)
AND 2021/11/18:2026/11/18[dp]
```

키워드 확장 흐름:

1. 사용자가 키워드를 입력한다.
2. Claude API가 facet별 후보 용어를 제안한다 (MeSH 용어, 약어, 신조어 포함).
3. 사용자가 UI에서 체크박스로 확정한다.
4. 확정한 내용을 `topics.yaml`에 저장해서 재사용한다.

## 5. 상위 저널 목록 (초안, 연구실 검토 필요)

```yaml
# config/journals.yaml  (PubMed [ta] 약어 기준)
tier1_general:
  - Nature
  - Science
  - Proc Natl Acad Sci U S A
  - Nat Commun
  - Sci Adv
tier1_bme:
  - Nat Biomed Eng
  - Sci Transl Med
  - Nat Biotechnol
  - Nat Electron
  - Nat Mater
materials_devices:
  - Adv Mater
  - Adv Funct Mater
  - Adv Sci (Weinh)
  - ACS Nano
  - Adv Healthc Mater
  - Biomaterials
  - Bioact Mater
  - Biosens Bioelectron
  - npj Flex Electron   # PubMed 수록 여부 확인 필요. 미수록이면 OpenAlex로 수집
```

- 그룹당 저널은 8~10개 이하로 유지한다 (연산자 제한 대응).
- 선택 기능: SCImago "Biomedical Engineering" 카테고리 Q1 CSV를 불러와 저널 목록을 자동으로 확장한다.

## 6. 처리 파이프라인

```
1. expand    : 키워드 → facet 용어 (Claude API + 사용자 확인)
2. search    : 저널 그룹별 PubMed esearch → PMID 목록
3. fetch     : efetch로 메타데이터 (제목, 초록, 저자, 저널, 날짜, DOI, MeSH)
4. enrich    : OpenAlex에서 DOI로 인용 수, OA 링크 / PubMed 미수록 저널 보완 검색
5. dedupe    : DOI → PMID → 정규화한 제목 순으로 중복 제거
6. filter    : exclude_phrases 적용, 선택적으로 Claude API로 관련성 판정 (yes/no + 이유)
7. score     : 관련도 = facet 매칭 수 + (선택) 임베딩 유사도, 인용 수는 별도 컬럼
8. store     : SQLite upsert, first_seen 날짜 기록 → "신규" 표시
```

API 사용 규칙:

- NCBI: `api_key`와 `email`을 설정하고 초당 10회 이하로 호출한다.
- OpenAlex: `mailto` 파라미터를 붙인다 (polite pool).

## 7. 데이터 모델 (SQLite)

- `papers`: pmid, doi, title, abstract, journal, pub_date, authors, mesh, cited_by, oa_url, first_seen
- `topics`: id, name, facets_json, created_at
- `topic_papers`: topic_id, paper_id, relevance, matched_terms, llm_verdict
- `user_marks`: paper_id, status (to-read / read / starred), note

## 8. 대시보드 화면

1. **사이드바**: 주제 선택·생성, 키워드 확장 확인, 저널 그룹 선택, 기간 선택 (기본 5년), "업데이트 실행" 버튼
2. **요약 지표**: 총 논문 수, 이번 주 신규 논문 수, 상위 저널 비율
3. **차트**: 연도별 논문 수 추이, 저널별 분포, 주요 MeSH 용어·키워드 빈도
4. **논문 테이블**: 제목(DOI 링크), 저널, 날짜, 인용 수, 관련도, 신규 배지, 초록 펼치기, 읽음·즐겨찾기 표시
5. **내보내기**: CSV, BibTeX

## 9. 단계별 개발 계획

| Phase | 내용 | 완료 기준 |
|---|---|---|
| 1. MVP CLI | `topics.yaml`·`journals.yaml` → PubMed 검색 → SQLite 저장 | §3의 ③번 검색 결과(31건)를 재현 |
| 2. 대시보드 | Streamlit 화면, 필터, 차트, 테이블, 내보내기 | 키워드 입력부터 결과 탐색까지 UI에서 가능 |
| 3. 보강 | OpenAlex 인용 수 추가, PubMed 미수록 저널 보완 | DOI 매칭률 90% 이상 |
| 4. LLM 기능 | Claude API로 키워드 확장, 관련성 필터, 한 줄 요약 | 오탐(물리학 논문 등) 자동 제거 |
| 5. 운영 | 주간 자동 업데이트(cron), 신규 논문 알림, (선택) Streamlit Cloud 배포 | 매주 신규 논문 리포트 생성 |

## 10. 검증용 기준 데이터 (Phase 1 테스트용)

§3의 ③번 검색식으로 아래 논문이 반드시 검색되어야 한다.

- Soft biodegradable implants for long-distance and wide-angle sensing — Nature, 2026 — doi:10.1038/s41586-025-09874-3
- An in-body networking system for communication between wearable and implantable therapeutics — Science, 2026 — doi:10.1126/science.adz5300
- Wireless and bioresorbable triboelectric nerve block system for postoperative pain control — Nat Biomed Eng, 2026 — doi:10.1038/s41551-025-01579-2
- Bioresorbable electrochemical sensors for continuous deep-tissue lactate monitoring in critical care — Nat Commun, 2026 — doi:10.1038/s41467-026-74248-w

아래 논문은 걸러져야 하는 오탐 예시다.

- Ultrahigh-speed laser drilling of transparent materials via transient electronic excitation — Sci Adv, 2025 — doi:10.1126/sciadv.adv4436

## 11. 미결 사항

- [ ] 연구실 기준으로 저널 목록 확정
- [ ] PubMed에 수록되지 않은 저널 파악 (npj Flex Electron, IEEE 계열 등)
- [ ] Claude API 키 사용 여부와 월 예산
- [ ] 연구실 공유 방식 (로컬 실행 vs Streamlit Cloud)