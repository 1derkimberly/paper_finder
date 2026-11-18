"""Privacy policy (served at /privacy; linked from the Google sign-in consent screen)."""

import os

import streamlit as st

st.set_page_config(page_title="개인정보처리방침 · Paper Finder", page_icon="📄")

try:  # Streamlit secrets -> environment (same as app.py)
    for _k, _v in dict(st.secrets).items():
        if isinstance(_v, str):
            os.environ.setdefault(_k, _v)
except Exception:
    pass
contact = os.environ.get("CONTACT_EMAIL") or "구글 로그인 동의 화면에 표시된 지원 이메일"

st.title("개인정보처리방침")
st.caption("Paper Finder (nst-paperdigging) · 시행일 2026-11-18")

st.markdown(f"""
Paper Finder는 상위 저널의 최신 논문을 모아 보여 주는 연구용 대시보드입니다. 이 문서는 서비스가 어떤 정보를
왜 저장하는지 설명합니다.

### 1. 수집하는 정보
- **구글 로그인 시:** 구글 계정의 **이메일 주소**만 사용합니다 (이름·사진 등 다른 정보는 저장하지 않습니다).
- **로그인한 사용자가 남긴 것:** 직접 만든 주제(검색 키워드), 논문별 읽음 상태와 메모.
- **로그인하지 않은 방문자:** 브라우저 세션마다 무작위 식별자를 만들어 그 세션에서 만든 주제·읽음 표시를 구분합니다.
  이 정보는 **24시간 뒤 자동 삭제**됩니다.
- **사용량 기록:** 비용 한도를 지키기 위해 Claude(AI) 요청마다 시각, 요청 종류, 사용 토큰 수, 요청한 사용자
  식별자(이메일 또는 방문자 세션 식별자)를 기록합니다.

### 2. 이용 목적
위 정보는 로그인 유지, 내 주제·읽음 표시 제공, 사용량 제한과 서비스 운영에만 사용합니다.
광고나 마케팅에 쓰지 않으며 판매하지 않습니다.

### 3. 외부 서비스
- **저장:** 데이터는 Supabase(PostgreSQL)에 저장됩니다.
- **AI 처리:** 주제 설명과 논문 제목·초록을 Anthropic의 Claude API로 보내 검색어 제안, 관련성 판정, 요약을 받습니다.
  이때 **이메일 등 개인 정보는 보내지 않습니다.**
- **논문 검색:** PubMed(NCBI)와 OpenAlex에 검색어를 보냅니다 (개인 정보 없음).
- **로그인:** Google 계정 인증을 사용하며, 로그인 상태 유지를 위해 브라우저 쿠키를 씁니다.

### 4. 보관과 삭제
- 로그인 사용자의 정보는 계정이 서비스를 쓰는 동안 보관합니다.
- 내 주제는 화면에서 언제든 직접 삭제할 수 있습니다.
- 계정과 관련된 모든 정보(주제, 읽음 표시, 메모, 사용량 기록)의 삭제를 원하면 아래 연락처로 요청해 주세요.
  확인 후 지체 없이 삭제합니다.

### 5. 문의
{contact}
""")
