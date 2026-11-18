# CLAUDE.md

최신 연구논문 대시보드 (Biomedical Engineering, 초기 주제: transient / bioresorbable electronics).
전체 요구사항과 단계별 계획은 **[docs/SPEC.md](docs/SPEC.md)** 가 기준이다. 작업 전에 해당 Phase 절을 확인할 것.

## 현재 상태

- Phase 1 (MVP CLI): 구현됨 — `topics.yaml`·`journals.yaml` → PubMed 검색 → SQLite 저장
  - 검증(2026-11-18): tier1 그룹으로 SPEC §10 기준 논문 4편 모두 검색, 레이저 오탐 미포함. 결과 43건 (SPEC §3의 31건과 차이 — Cowork 테스트의 정확한 저널·용어 조합 불명)
  - `npj Flex Electron`(PubMed 전체 29건), `Nat Electron`(47건)은 PubMed 수록이 부분적 → Phase 3에서 OpenAlex 보완 필요
- Phase 2~5: 미구현 (Streamlit 대시보드, OpenAlex 보강, Claude API, 자동 업데이트)

## 실행

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env   # NCBI_API_KEY, NCBI_EMAIL 채우기 (없어도 동작, 초당 3회로 제한)
.\.venv\Scripts\python -m paper_finder search transient_bioelectronics           # 검색 + 저장
.\.venv\Scripts\python -m paper_finder search transient_bioelectronics --groups tier1_general tier1_bme --dry-run
.\.venv\Scripts\python -m paper_finder list transient_bioelectronics             # 저장된 결과 보기
.\.venv\Scripts\python -m pytest                                                  # 오프라인 단위 테스트
.\.venv\Scripts\python -m pytest -m network                                       # PubMed 실제 호출 검증 (SPEC §10)
```

## 구조

```
config/topics.yaml     주제별 facet / exact_phrases / exclude_phrases
config/journals.yaml   저널 그룹 (PubMed [ta] 약어)
paper_finder/
  config.py    YAML·.env 로딩
  query.py     facet → PubMed 검색식 생성 (모든 용어에 [tiab])
  pubmed.py    E-utilities esearch/efetch 클라이언트 (rate limit, XML 파싱)
  process.py   dedupe → exclude 필터 → facet 매칭 점수
  db.py        SQLite 스키마 (SPEC §7) 와 upsert
  pipeline.py  search → fetch → dedupe → filter → score → store
  cli.py       argparse CLI (`python -m paper_finder`)
data/papers.db         SQLite (gitignore)
tests/                 pytest (network 마커는 실제 API 호출)
```

## 설계 규칙 (SPEC §3에서 검증된 것 — 바꾸지 말 것)

- 모든 검색어에 `[tiab]` 를 붙인다. 태그 없는 검색은 자동 용어 변환으로 노이즈가 된다.
- 검색식 = `(facet1 OR …) AND (facet2 OR …) AND …` **OR** `(exact phrases)`, 그 뒤에 저널 그룹과 `[dp]` 기간을 AND.
- 저널 그룹별로 따로 검색하고 PMID로 합친다 (연산자 개수·URL 길이 제한). 그룹당 저널 8~10개 이하.
- `exclude_phrases` 는 검색식이 아니라 수집 후 필터 단계에서 제목+초록에 적용한다.
- 중복 제거 순서: DOI → PMID → 정규화한 제목.
- 관련도(Phase 1) = 매칭된 facet 수 + exact phrase 매칭 보너스. 인용 수는 별도 컬럼(Phase 3).
- NCBI: `api_key`·`email`·`tool` 파라미터를 붙이고 초당 10회(키 없으면 3회) 이하. OpenAlex는 `mailto` 사용.
- `first_seen` 은 처음 저장될 때만 기록하고 upsert 때 덮어쓰지 않는다 ("신규" 표시용).

## 작업 규칙

- Python 3.11+ 문법, 타입 힌트 사용. 의존성은 `requirements.txt` 에 추가.
- 네트워크가 필요한 테스트는 `@pytest.mark.network` 로 표시하고 기본 실행에서 제외.
- API 키는 `.env` 에만 둔다. 코드·설정·커밋에 넣지 않는다.
- 사용자와의 대화·문서는 한국어.
