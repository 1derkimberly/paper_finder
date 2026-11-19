# CLAUDE.md

최신 연구논문 대시보드 (Biomedical Engineering, 초기 주제: transient / bioresorbable electronics).
전체 요구사항과 단계별 계획은 **[docs/SPEC.md](docs/SPEC.md)** 가 기준이다. 작업 전에 해당 Phase 절을 확인할 것.

## 현재 상태

- Phase 1 (MVP CLI): 구현됨 — `topics.yaml`·`journals.yaml` → PubMed 검색 → SQLite 저장
  - 검증(2026-11-18): tier1 그룹으로 SPEC §10 기준 논문 4편 모두 검색, 레이저 오탐 미포함. 결과 43건 (SPEC §3의 31건과 차이 — Cowork 테스트의 정확한 저널·용어 조합 불명)
  - `npj Flex Electron`(PubMed 전체 29건), `Nat Electron`(47건)은 PubMed 수록이 부분적 → Phase 3에서 OpenAlex 보완 필요
- Phase 2 (대시보드): 구현됨 — `app.py` (Streamlit). 주제 생성·키워드 편집·업데이트 실행·필터·차트·표/카드·읽음 표시·CSV/BibTeX
  - 키워드 확인 UI는 SPEC의 사이드바 대신 본문 상단 펼침 패널에 둠 (사이드바가 좁아서)
  - 새 주제는 본문 '새 주제' 화면에서 만든다: 주제 설명(한국어 가능) → Claude 용어 제안(facet·정확한 문구·제외 문구·주제 이름) → 칩으로 고르기 → 생성. 이것이 핵심 기능이라 키워드 직접 입력 화면은 두지 않는다 (만든 뒤 '검색 키워드' 패널에서 수정). 키가 없으면 새 주제를 만들 수 없다는 안내만 보임
- Phase 3 (보강): 구현됨 — OpenAlex로 인용 수·무료 전문 링크(DOI 매칭), PubMed 수록이 부분적인 저널 보완 검색
  - 검증(2026-11-18): DOI 매칭 174/174 (100%, 목표 90%). 보완 검색으로 npj Flex Electron·Nat Electron 논문 11건 추가
  - OpenAlex는 키 없이 쓰면 IP당 하루 $0.10 공유 한도 + 검색 부하 시 429(30초 대기). 보완 검색이 실패해도 PubMed 결과는 저장되고 경고만 표시
- Phase 4 (LLM): 구현됨 — Claude API로 용어 제안(`expand`·대시보드 키워드 패널), 관련성 판정(오탐) + 한국어 한 줄 요약(`judge`, 업데이트 후 자동)
  - `ANTHROPIC_API_KEY` 가 없으면 LLM 기능만 꺼지고 나머지는 동작
  - 비용 분리: 용어 제안 = `CLAUDE_MODEL` (기본 claude-sonnet-5-5), 판정·요약 = `CLAUDE_JUDGE_MODEL` (기본 claude-haiku-5-5). Haiku는 서버 측 fallback이 없어 `fallbacks` 를 안 붙인다
  - 검증(2026-11-18, claude-sonnet-5-5): 174편 판정, 17편 관련 없음(토양·가스·전자코·열전 등). SPEC §10 기준 4편 모두 관련 있음
  - 판정 기준은 "모든 facet 개념이 실제로 해당될 것"(엄격). 2026-11-18 "분야 전반"으로 넓혔다가 사용자 요청으로 되돌림. 이 기준에서는 §10의 Science in-body networking 논문(생분해 아님)이 관련 없음으로 숨겨짐 (174편 중 49편 숨김, sonnet-5-5 기준)
  - 대시보드에는 별도 판정 버튼이 없다. '업데이트' 버튼 안에서 자동으로 돈다
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
- 디자인: `.streamlit/config.toml` 테마(인디고 강조색 #5b5bd6 / 다크 #7072f2 = 차트 색, 완전 흰색·검은색 대신 오프화이트·잉크, Pretendard, light/dark 따로). 사이드바 맨 위 로고 `assets/nst_logo.png`(흰색+투명)는 CSS mask + `currentColor` 라 테마 글자색을 따른다. 본문 = 헤더 → 필터 상자 → 지표 카드 4개 → [논문 | 동향] 탭, 논문은 카드 보기·최신순이 기본. '읽음' 카드는 흐리게(컨테이너 key `readcard_*` + CSS, 마우스를 올리면 선명). `use_container_width` 대신 `width="stretch"`
- `st.cache_data` 캐시 키(`data_version()`)에 DB 파일 수정 시각을 넣는다. 그래야 CLI·주간 자동 업데이트로 바뀐 DB가 켜져 있는 대시보드에 바로 보인다
- 미리보기용 별도 데이터: `PAPER_FINDER_CONFIG`·`PAPER_FINDER_DB`·`PAPER_FINDER_REPORTS` 환경 변수로 다른 폴더를 가리킬 수 있다 (`.claude/launch.json` 의 dashboard-demo, 포트 8502)
- 공개 배포([docs/DEPLOY.md](docs/DEPLOY.md)): Streamlit Community Cloud + Supabase Postgres + GitHub Actions(월·목 업데이트)
  - 속도: Postgres 연결은 `psycopg_pool` 로 재사용(autocommit, `with conn:` 은 명시적 트랜잭션), `data_stamp`·설정은 한 번의 쿼리. 앱은 주제 목록·한도·예산 확인을 `st.cache_data` 로 캐시(DB 스탬프 30초 TTL + 세션 쓰기 시 bump)
  - `DATABASE_URL`(postgres://)이 있으면 Postgres, 없으면 SQLite. `db.Conn` 이 `?`→`%s` 변환, INSERT는 `RETURNING id`. Supabase는 transaction pooler(6543) + `prepare_threshold=None`
  - 배포 모드에선 주제 정의를 DB `topic_defs` 에 저장 (`topics_store.TopicStore`). 서비스는 주제 없이 시작한다 (topics.yaml을 복사하지 않음, 사용자가 직접 만듦)
  - secrets에 `[auth]` 가 있으면 구글 로그인(st.login). 역할(`quota.Role`): 방문자(anon:<세션>, 최근 1년·주제 1개·세션 한정), 로그인(이메일, 주제 3개), 관리자(`ADMIN_EMAILS`, 제한 없음 + ⚙ 관리 화면). 로컬(secrets 없음)은 관리자 1명
  - 주제 소유: `topic_defs.owner` (''=관리자, 이메일, anon:*). 비관리자 주제 ID는 `이름-<owner 해시 6자>`, 표시 이름은 `title`. 사이드바에는 내 주제만 보인다(관리자도 마찬가지, 남의 주제는 ⚙ 관리 화면 목록·삭제로만)
  - Claude 사용은 `api_usage` 에 기록(`quota.claude_for`), 방문자+로그인 합계가 하루 한도(`app_settings.daily_budget_usd`, 기본 $1)를 넘으면 그날 Claude 기능 중단. 관리자·자동 업데이트(role system)는 막지 않음
  - 한도 값은 `app_settings` (관리 화면에서 수정). 만료 방문자 주제 정리는 `maintenance.cleanup` (`update` 전에 자동, `cleanup` 명령, 관리 화면 버튼)
  - 리포트는 파일 대신 DB에서 다시 만든다 (`report.last_update_report`). Streamlit secrets 최상위 문자열은 `_secrets_to_env()` 로 환경 변수에 복사
  - 테스트: `PAPER_FINDER_FAKE_LOGIN`(""=방문자, 이메일=로그인)으로 역할 시뮬레이션. Postgres 테스트는 `TEST_DATABASE_URL` 이 있을 때만
- 테스트는 `tests/fixtures/topics.yaml` 을 쓴다 (conftest가 매 테스트 임시 config 폴더로 `CONFIG_DIR` 교체). 사용자의 `config/topics.yaml` 은 대시보드에서 바뀌므로 테스트가 기대면 안 된다
- 주제 선택은 selectbox가 아니라 사이드바 popover 안의 버튼 줄(주제 | 휴지통 아이콘, 키 `pick_<ID>`·`delicon_<ID>`). 테스트는 `tests/app_helpers.py` 의 `menu`·`go` 사용
- 오른쪽 위 계정 아이콘(popover): My page·로그아웃(바로 `st.logout`)·방문자는 로그인. My page(MY_PAGE) = 탭 [신규 논문 리포트 | 계정·개인정보]. 사이드바에는 리포트·로그아웃 없음 (방문자 로그인 버튼만)
- 마지막 업데이트 정보는 주제 제목 줄 오른쪽. 저널 그룹은 `select_slider` 범위(종합 → 전문, journals.yaml 순서 = 일반→전문, 표시 이름 `GROUP_LABELS`)
- 미리보기 dashboard-demo는 `DATABASE_URL=""` 로 SQLite 데모 DB만 쓴다 (`.env` 의 Supabase에 붙지 않게)
- 주제 삭제(휴지통 아이콘 또는 관리 화면 → `st.dialog` 확인창의 '삭제'. 대기 중인 삭제는 `session_state["ask_delete"]`, 취소·삭제·닫기에서 지움): `topics.yaml` 항목·`topics`·`topic_papers`·`runs` 삭제. 어느 주제에도 안 속하게 된 논문은 지우되 `user_marks` 가 있는 논문은 남긴다. 리포트 파일은 둔다
- Claude 용어 제안은 바로 저장하지 않고 키워드 편집기에 넣기만 한다 (사용자가 '키워드 저장'으로 확정).
- Claude 호출은 `client.beta.messages.create` + `output_config.format` (JSON 스키마) + 서버 측 fallback(`fallbacks="default"`). 오류는 `LLMError` 로 바꾸고, 판정 중 오류가 나면 거기서 멈추되 저장된 배치는 유지.

## 작업 규칙

- Python 3.11+ 문법, 타입 힌트 사용. 의존성은 `requirements.txt` 에 추가.
- 네트워크가 필요한 테스트는 `@pytest.mark.network` 로 표시하고 기본 실행에서 제외.
- API 키는 `.env` 에만 둔다. 코드·설정·커밋에 넣지 않는다.
- 사용자와의 대화·문서는 한국어.
