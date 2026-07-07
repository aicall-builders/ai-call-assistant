# 3라운드 고객관리 기능 개발 변경내역

## 1. 고객 등급 기준 변경

기준 변경:
- VIP: 통화 20회 이상
- 단골: 통화 10~19회
- 일반: 통화 2~9회
- 신규: 통화 1회 이하

반영 위치:
- Android 고객관리 화면
- Web 고객관리 화면

## 2. 고객 책갈피 / 주요관리 고객 연동

추가 필드:
- customer_profiles.is_pinned

동작:
- 고객상세에서 ☆/★ 토글
- is_pinned=true 저장
- 홈 화면 주요관리 고객에 우선 표시
- 해제 시 주요관리 고객에서 제외

우선순위:
1. is_pinned=true 고객
2. 통화 수 높은 고객
3. 최근 통화 고객

## 3. AI 고객분석 최신화

갱신 트리거:
- 고객정보 저장
- 수동 메모 생성
- 메모 이미지 저장
- 통화 요약 완료 후

반영 데이터:
- 고객정보
- 통화 요약
- 통화 카테고리
- 통화별 메모
- 수동 메모
- 이미지 caption
- 누적 통화 수

비용/안정성 처리:
- source_hash 기반 중복 AI 호출 방지
- 동일 데이터면 재분석 skip
- 저장 완료 후 1회 재분석
- 입력 중 매 글자마다 AI 호출하지 않음
- AI 실패 시 fallback 분석 생성

## 4. Backend 변경

customer_handler.py:
- customer_profiles.is_pinned 추가
- customer_analysis.source_hash/status/raw_json 추가
- PATCH /customers/{phone} 부분수정 지원
- is_pinned PATCH 지원
- profile/memo/photo 저장 후 _refresh_customer_analysis 실행
- GET /customers, GET /customers/{phone} 응답에 is_pinned 및 analysis 상태 포함

call_handler.py:
- 통화 요약 완료 후 고객 AI 분석 갱신 트리거
- direction 인자 보정

## 5. Android 변경

반영 branch:
- feature/fiano-ui

변경:
- CustomerGrade 기준 변경
- CustomerProfile / CustomerListItem / UpdateCustomerRequest에 isPinned 추가
- 고객상세 책갈피 토글 추가
- 홈 주요관리 고객 pinnedCustomers 우선 표시
- 고객정보 저장 후 AI 분석 재조회
- 고객상세 메모/이미지 히스토리 표시 유지

## 6. Web 변경

반영 branch:
- main

변경:
- 고객 등급 기준 변경
- 고객 목록 customerApi.list 기반 profile 병합
- 고객상세 customerApi.get/history 연동
- 책갈피 토글 추가
- AI 종합요약 서버 analysis 우선 표시
- 메모/이미지 히스토리 표시

## 7. 아직 필요한 테스트

API:
- PATCH /customers/{phone} { is_pinned: true/false }
- GET /customers/{phone} profile.is_pinned 확인
- PATCH 고객정보 후 analysis.status/source_hash 확인
- POST /customers/{phone}/memos 후 history/analysis 갱신 확인

Web:
- /customers 등급 기준 표시
- ☆/★ 토글 후 새로고침 유지
- AI 종합요약 표시
- 메모/이미지 히스토리 표시

Android:
- 고객관리 등급 기준 표시
- 고객상세 ☆/★ 토글
- 홈 주요관리 고객 반영
- 고객정보 저장 후 AI 분석 갱신

실기기:
- 갤럭시 실제 통화녹음 업로드
- STT 완료
- 고객상세 히스토리/AI 분석 반영

## 8. 주의사항

- API 직접 테스트 시 Authorization 헤더 필요
- headers 없으면 {"error":"인증 필요"} 정상
- 개인정보 동의 미완료 고객은 AI 분석 locked 처리될 수 있음
- OpenAI API 키가 없으면 fallback 분석으로 저장됨
