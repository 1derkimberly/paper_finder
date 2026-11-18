# CLAUDE.md

최신 연구논문 대시보드 (Biomedical Engineering, 초기 주제: transient / bioresorbable electronics).
전체 요구사항과 단계별 계획은 **[docs/SPEC.md](docs/SPEC.md)** 가 기준이다. 작업 전에 해당 Phase 절을 확인할 것.

## 현재 상태

- Phase 1 (MVP CLI): 구현됨 — `topics.yaml`·`journals.yaml` → PubMed 검색 → SQLite 저장
  - 검증(2026-11-18): tier1 그룹으로 SPEC §10 기준 논문 4편 모두 검색, 레이저 오탐 미포함. 결과 43건 (SPEC §3의 31건과 차이 — Cowork 테스트의 정확한 저널·용어 조합 불명)
  - `npj Flex Electron`(PubMed 전체 29건), `Nat Electron`(47건)은 PubMed 수록이 부분적 → Phase 3에서 OpenAlex 보완 필요
- Phase 2 (대시보드): 구현됨 — `app.py` (Streamlit). 주제 생성·키워드 편집·업데이트 실행·필터·차트·표/카드·읽음 표시·CSV/BibTeX
  - 키워드 확인 UI는 SPEC의 사이드바 대신 본문 상단 펼침 패널에 둠 (사이드바가 좁아서)
  - UI에서 새 주제를 만들면 키워드는 exact_phrases로 들어감. facet 자동 제안은 Phase 4(Claude API)
- Phase 3 (보강): 구현됨 — OpenAlex로 인용 수·무료 전문 링크(DOI 매칭), PubMed 수록이 부분적인 저널 보완 검색
  - 검증(2026-11-18): DOI 매칭 174/174 (100%, 목표 90%). 보완 검색으로 npj Flex Electron·Nat Electron 논문 11건 추가
  - OpenAlex는 키 없이 쓰면 IP당 하루 $0.10 공유 한도 + 검색 부하 시 429(30초 대기). 보완 검색이 실패해도 PubMed 결과는 저장되고 경고만 표시
- Phase 4 (LLM): 구현됨 — Claude API로 용어 제안(`expand`·대시보드 키워드 패널), 관련성 판정(오탐) + 한국어 한 줄 요약(`judge`, 업데이트 후 자동)
  - `ANTHROPIC_API_KEY` 가 없으면 LLM 기능만 꺼지고 나머지는 동작. 모델은 `CLAUDE_MODEL` (기본 claude-opus-5-5)
  - 검증(2026-11-18, claude-sonnet-5-5): 174편 판정, 17편 관련 없음(토양·가스·전자코·열전 등). SPEC §10 기준 4편 모두 관련 있음
  - 판정 기준은 "분야 전반"이다: 생분해가 아니어도 이식형·무선·연성 바이오전자는 관련 있음, 단어가 다른 뜻으로 우연히 걸린 것만 제외 (모든 facet을 요구하면 §10의 Science in-body networking 논문이 빠졌음)
  - 대시보드에는 별도 판정 버튼이 없다. '업데이트 실행' 안에서 자동으로 돈다
  - 키워드 편집기: 드롭다운 자동완성(수집 논문의 자주 나온 단어·2단어 구), facet 아래 '다음 추천' 칩. facet을 고치면 Claude가 다음 용어를 예상(용어 조합별 세션 캐시), 안 고쳤거나 키가 없으면 `suggest.py`의 문맥 유사도(오프라인, 품질 낮음)
- Phase 5 (운영): 구현됨 — `update` (전체 주제 업데이트 → Claude 판정 → 신규 논문 리포트 → `runs` 기록), Windows 알림, 작업 스케줄러 주간 등록
  - 리포트: `reports/<날짜>_<주제>.html|.md`, 직전 성공 업데이트 날짜 이후 `first_seen` 인 논문 (관련 없음 판정 제외). 대시보드 업데이트도 리포트를 쓴다
  - 검증(2026-11-18): `update transient_bioelectronics --notify` → 리포트 157건(17건 제외), 알림 표시. 스케줄 등록은 사용자 승인 후
  - Streamlit Cloud 배포(선택)는 안 함

## 실행

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env   # NCBI_API_KEY, NCBI_EMAIL 채우기 (없어도 동작, 초당 3회로 제한)
.\.venv\Scripts\python -m paper_finder search transient_bioelectronics           # 검색 + 저장
.\.venv\Scripts\python -m paper_finder search transient_bioelectronics --groups tier1_general tier1_bme --dry-run
.\.venv\Scripts\python -m paper_finder list transient_bioelectronics             # 저장된 결과 보기
.\.venv\Scripts\python -m paper_finder enrich                                      # 저장된 모든 논문의 인용 수 갱신
.\.venv\Scripts\python -m paper_finder judge transient_bioelectronics [--force]   # Claude 관련성 판정 + 요약 (미판정분만)
.\.venv\Scripts\python -m paper_finder expand --topic transient_bioelectronics "설명"  # Claude 용어 제안 (YAML 출력)
.\.venv\Scripts\python -m paper_finder update [주제...] [--notify] [--no-llm]          # 전체 업데이트 + 리포트 (주간 자동 실행과 같음)
.\.venv\Scripts\python -m paper_finder report transient_bioelectronics --since 2026-11-01  # 저장된 데이터로 리포트만
.\.venv\Scripts\python -m paper_finder schedule install|remove|status [--day MON --at 09:00]  # 작업 스케줄러 (scripts/weekly_update.ps1, 로그는 logs/)
# search 옵션: --no-openalex (PubMed만), --no-llm (Claude 판정 생략), --quiet (목록 출력 생략)
.\.venv\Scripts\python -m streamlit run app.py                                   # 대시보드 (http://localhost:8501)
.\.venv\Scripts\python -m pytest                                                  # 오프라인 단위 테스트 + 앱 스모크 테스트
.\.venv\Scripts\python -m pytest -m network                                       # PubMed 실제 호출 검증 (SPEC §10)
```

## 구조

```
config/topics.yaml     주제별 facet / exact_phrases / exclude_phrases
config/journals.yaml   저널 그룹 (PubMed [ta] 약어)
config/openalex_sources.yaml  PubMed 수록이 부분적인 저널 → OpenAlex 보완 검색 대상 (그룹·ISSN·OpenAlex ID)
paper_finder/
  config.py    YAML·.env 로딩
  query.py     facet → PubMed 검색식 생성 (모든 용어에 [tiab])
  pubmed.py    E-utilities esearch/efetch 클라이언트 (rate limit, XML 파싱)
  process.py   dedupe → exclude 필터 → facet 매칭 점수
  db.py        SQLite 스키마 (SPEC §7) 와 upsert
  openalex.py  OpenAlex 클라이언트 (DOI 일괄 조회, 부울 검색, Retry-After 재시도)
  llm.py       Claude API (구조화 출력 JSON): suggest_terms, next_terms, judge_papers (10편씩 배치)
  suggest.py   오프라인 키워드 추천 (문맥 유사도) + 자동완성 어휘
  update.py    Phase 5 업데이트 한 번 = run → judge(최대 300건) → 리포트 → runs 기록 (CLI·대시보드·스케줄 공용)
  report.py    신규 논문 리포트 HTML/Markdown
  notify.py    Windows 토스트 알림 (Windows PowerShell 5.1 WinRT, 클릭하면 리포트 열림)
  schedule.py  작업 스케줄러 등록 (-StartWhenAvailable: 꺼져 있던 주는 켜지면 실행)
  pipeline.py  search → fetch → OpenAlex 보완 → dedupe → filter → score → enrich → store; judge_topic
  cli.py       argparse CLI (`python -m paper_finder`)
  dashboard_data.py  DB → DataFrame, 필터, 지표, 차트용 집계 (Streamlit 비의존, 테스트 대상)
  export.py    CSV (utf-8-sig, Excel 호환) / BibTeX
app.py                 Streamlit UI (얇게 유지, 로직은 dashboard_data.py로)
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
- 주제의 키워드가 바뀌면 그 주제의 `topic_papers` 링크를 지우고 다시 수집한다 (옛 검색식 결과가 섞이지 않게). `user_marks` 는 논문 단위라 유지.
- 저자는 `"성, 이름"` 형식으로 저장한다 (BibTeX 호환).
- OpenAlex 레코드는 PubMed 레코드를 덮어쓰지 않는다 (PMID·MeSH·논문 종류 보존). 같은 DOI면 인용 수·OA 링크만 갱신.
- OpenAlex 검색은 어간 처리를 하므로 결과를 `process.matches_topic` 으로 다시 확인한다. 초록이 없는 레코드(Springer Nature 등은 OpenAlex에 초록 미제공)는 제목에 주제 용어가 하나라도 있으면 통과.
- 리뷰 판별은 PubMed PublicationType 기준 (`Review`, `* Review`, `Meta-Analysis`) → 제목 앞 `(review)`. 새 컬럼은 `db._ADDED_COLUMNS` 에 추가하면 기존 DB에 자동 반영.
- 대시보드에서 키워드를 저장하면 `topics.yaml` 이 다시 쓰여 머리말 외 주석은 사라진다.
- Claude 판정은 삭제가 아니라 표시다: `topic_papers.llm_verdict` (JSON: relevant·reason·model·date), 요약은 `papers.summary`. 대시보드는 관련 없음 판정을 기본으로 숨기고 토글로 보여준다.
- 판정은 `llm_verdict IS NULL` 인 링크만 한다 (재검색해도 `link()` 가 판정을 보존). 키워드가 바뀌면 링크가 지워지므로 다시 판정된다.
- Claude 판정은 업데이트당 최대 `MAX_JUDGE_PER_UPDATE`(300)건, 나머지는 다음 업데이트에서. 결과 1000건 초과면 '키워드가 너무 넓다' 경고 (대시보드 새 주제에서 단어 하나를 정확한 문구로 넣으면 수천 건이 걸림)
- `.env` 는 `load_settings()` 호출마다 다시 읽고 `os.environ` 에 복사하지 않는다 (대시보드를 켠 채 키를 넣어도 반영). 실제 환경 변수가 있으면(빈 값 포함) 그쪽이 우선
- Claude 용어 제안은 바로 저장하지 않고 키워드 편집기에 넣기만 한다 (사용자가 '키워드 저장'으로 확정).
- Claude 호출은 `client.beta.messages.create` + `output_config.format` (JSON 스키마) + 서버 측 fallback(`fallbacks="default"`). 오류는 `LLMError` 로 바꾸고, 판정 중 오류가 나면 거기서 멈추되 저장된 배치는 유지.

## 작업 규칙

- Python 3.11+ 문법, 타입 힌트 사용. 의존성은 `requirements.txt` 에 추가.
- 네트워크가 필요한 테스트는 `@pytest.mark.network` 로 표시하고 기본 실행에서 제외.
- API 키는 `.env` 에만 둔다. 코드·설정·커밋에 넣지 않는다.
- 사용자와의 대화·문서는 한국어.
