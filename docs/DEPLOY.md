# 인터넷 공개 배포 (Streamlit Community Cloud + Supabase)

웹 주소에 들어온 누구나 모든 기능을 써 볼 수 있고, 비용과 자원을 지키기 위해 역할별로 제한합니다.

| 역할 | 할 수 있는 것 | 제한 |
|---|---|---|
| 방문자 (로그인 안 함) | 공개 주제 보기, 내 주제 만들기(Claude 용어 제안), 업데이트, 읽음 표시 | 최근 1년 논문만, 내 주제 1개, 창을 닫으면 사라짐(24시간 뒤 삭제) |
| 로그인 사용자 (구글) | 모든 기능, 읽음 표시·메모 저장 | 내 주제 3개, 검색 기간 최대 10년 |
| 관리자 (`ADMIN_EMAILS`) | 모든 기능 + **⚙ 관리** 화면 | 없음 |

- 관리자가 만든 주제는 **공개 주제**로 모두에게 보이고, 다른 사람이 만든 주제는 만든 사람(과 관리자)에게만 보입니다.
- 방문자·로그인 사용자의 Claude 사용을 합쳐 **하루 $1(추정치)** 를 넘으면 그날은 Claude 기능(용어 제안, 관련성 판정·요약)이 쉽니다. 검색·수집은 계속 됩니다.
- 위 숫자는 모두 관리 화면의 **한도 설정**에서 바꿀 수 있습니다.
- **⚙ 관리** 화면: 사용량·비용(오늘/이번 달, 역할·용도별, 많이 쓴 사용자), 한도 설정, 전체 주제 목록과 삭제, 업데이트 기록(실패 확인), 정리(만료된 방문자 주제·안 쓰는 논문), DB 용량(Supabase 무료 500 MB 대비), API 키 상태, 미판정 논문 일괄 판정.
- 자동 업데이트(월·목)는 공개 주제와 로그인 사용자 주제만 돌리고, 돌기 전에 만료된 방문자 주제를 정리합니다.

구성: 화면은 Streamlit Community Cloud(무료), 데이터는 Supabase Postgres(무료), 주 2회 자동 업데이트는 GitHub Actions(무료).
API 비용(Claude)만 관리자 키로 나갑니다.

## 1. Supabase (데이터베이스)

1. https://supabase.com 가입 → **New project**. Region은 `Northeast Asia (Seoul)`. DB 비밀번호를 정해 따로 적어 둡니다.
2. 프로젝트 화면 위쪽 **Connect** → **Transaction pooler** 의 URI를 복사합니다 (포트 `6543`).
   `[YOUR-PASSWORD]` 자리를 1번의 비밀번호로 바꾼 것이 `DATABASE_URL` 입니다.
3. 표(테이블)는 앱이 처음 연결될 때 자동으로 만들어집니다. 첫 실행 때 `config/topics.yaml` 의 주제가 DB로 복사됩니다.

무료 프로젝트는 7일 동안 아무 접속이 없으면 일시 정지됩니다. 주 2회 자동 업데이트가 접속으로 잡히므로 보통은 멈추지 않습니다.

## 2. 구글 로그인 (OAuth 클라이언트)

1. https://console.cloud.google.com → 프로젝트 만들기.
2. **API 및 서비스 → OAuth 동의 화면**: 사용자 유형 *외부*, 앱 이름·지원 이메일 입력. 범위는 기본값(email, profile, openid)이면 됩니다.
   *게시 상태*를 **프로덕션으로 게시**해야 아무 구글 계정이나 로그인할 수 있습니다 (테스트 상태면 등록한 테스트 사용자만 가능).
3. **사용자 인증 정보 → 사용자 인증 정보 만들기 → OAuth 클라이언트 ID** → 애플리케이션 유형 *웹 애플리케이션*.
   승인된 리디렉션 URI에 `https://<앱주소>.streamlit.app/oauth2callback` 추가 (3단계에서 정할 앱 주소).
   로컬에서도 시험하려면 `http://localhost:8501/oauth2callback` 도 추가.
4. 만들어진 **클라이언트 ID**와 **클라이언트 보안 비밀**을 적어 둡니다.

## 3. Streamlit Community Cloud (웹 주소)

1. https://share.streamlit.io → GitHub 계정으로 로그인 → **Create app → Deploy a public app from GitHub**.
2. Repository `1derkimberly/paper_finder`, Branch `main`, Main file `app.py`, App URL에 원하는 주소(예: `paper-finder-bme`).
3. **Advanced settings**: Python 3.12, **Secrets** 칸에 [`.streamlit/secrets.toml.example`](../.streamlit/secrets.toml.example) 내용을 붙여 넣고 실제 값으로 채웁니다.
   - `ADMIN_EMAILS`: 관리자로 쓸 구글 계정 이메일
   - `cookie_secret`: 아무 긴 무작위 문자열 (예: PowerShell에서 `[guid]::NewGuid().ToString() + [guid]::NewGuid().ToString()`)
   - `redirect_uri`: 2-3에서 넣은 것과 똑같이
4. **Deploy**. 나중에 Secrets를 바꾸려면 앱 메뉴 **Settings → Secrets**.

## 4. GitHub Actions (주 2회 자동 업데이트)

1. GitHub 저장소 → **Settings → Secrets and variables → Actions → New repository secret** 로 다음을 추가:
   `DATABASE_URL`, `ANTHROPIC_API_KEY`, `NCBI_API_KEY`, `NCBI_EMAIL`, `OPENALEX_API_KEY`, `OPENALEX_MAILTO` (값은 Streamlit Secrets와 같게)
2. **Actions → weekly-update → Run workflow** 로 한 번 돌려 처음 데이터를 채웁니다 (몇 분 걸림).
3. 이후 매주 월·목 09:00(KST)에 자동 실행됩니다 ([.github/workflows/weekly-update.yml](../.github/workflows/weekly-update.yml)).
   공개 저장소에 60일 동안 커밋이 없으면 GitHub이 예약 실행을 멈추니, 그때는 Actions 화면에서 다시 켜면 됩니다.

## 5. 비용 안전장치

- https://console.anthropic.com → **Limits** 에서 월 지출 한도(예: $5)를 걸어 두세요.
- 업데이트당 Claude 판정은 최대 300건, 판정 모델은 Haiku (`CLAUDE_JUDGE_MODEL`).

## 로컬에서 미리 시험하기

`.streamlit/secrets.toml.example` 을 `.streamlit/secrets.toml` 로 복사해 값을 채우고(`redirect_uri` 는 `http://localhost:8501/oauth2callback`)
`streamlit run app.py`. secrets.toml 이 없으면 지금처럼 로그인 없이 혼자 쓰는 모드(SQLite)로 돕니다.
