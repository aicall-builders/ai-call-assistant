# FIANO Backend

Deployment URL: https://dsoh4vn0si08a.cloudfront.net/

소상공인을 위한 AI 통화 요약 서비스 FIANO의 백엔드입니다. AWS Lambda 기반으로 인증, 통화 업로드, STT 처리, AI 요약, 고객·일정 데이터를 제공합니다.

## 주요 기능

- 카카오 OAuth 및 Firebase Custom Token 인증
- 매장, 통화, 요약 데이터 API
- S3 presigned URL 발급
- CLOVA Speech 기반 STT 처리
- OpenAI 기반 통화 요약 및 구조화 정보 추출
- Google Calendar OAuth 연동
- Redis 기반 중복 처리 방지 및 캐싱

## 기술 스택

- Python 3.12
- AWS Lambda, API Gateway
- AWS S3, RDS MySQL, Secrets Manager
- Redis / ElastiCache
- Firebase Admin SDK
- CLOVA Speech
- OpenAI API

## 디렉터리 구조

```text
lambda/
  auth_handler.py      # 인증
  call_handler.py      # 매장/통화/요약 API
  calendar_handler.py  # 외부 캘린더 연동
  nlp_handler.py       # STT 콜백 및 AI 요약
  redis_client.py      # Redis 연결
```

## 주요 API

보호 API는 Firebase ID Token이 필요합니다.

```http
Authorization: Bearer <Firebase ID Token>
```

| Method | Path | 설명 |
|---|---|---|
| POST | `/auth/kakao` | 카카오 로그인 |
| GET/POST | `/stores` | 매장 조회/생성 |
| POST | `/calls/upload` | 업로드 URL 발급 |
| POST | `/calls/{id}/process` | STT/분석 시작 |
| POST | `/calls/{id}/cancel` | 분석 취소 |
| GET | `/calls` | 통화 목록 |
| GET/PATCH/DELETE | `/calls/{id}` | 통화 상세/수정/삭제 |
| GET | `/calls/{id}/audio` | 음성 재생 URL |
| GET | `/summaries/{id}` | 요약 조회 |
| GET | `/calendar/connections/{provider}/authorize` | 캘린더 OAuth 시작 |
| POST | `/calendar/connections/oauth-code` | 캘린더 OAuth 완료 |

## 설정

비밀 값은 코드에 저장하지 않습니다. 운영 값은 AWS Secrets Manager와 Lambda 환경변수로 관리합니다.

필수 설정 예:

```text
DB secret
Firebase Admin SDK
Kakao REST/API 설정
CLOVA Speech secret
OpenAI API key
S3 bucket
Redis endpoint
Google OAuth client
```

## 배포

Lambda 함수별로 독립 배포합니다.

```text
auth_handler.py      -> auth Lambda
call_handler.py      -> call Lambda
calendar_handler.py  -> calendar Lambda
nlp_handler.py       -> nlp Lambda
```

배포 전 확인:

```bash
python -m py_compile lambda/*.py
```

## 관련 저장소

- Backend: [aicall-builders/ai-call-assistant](https://github.com/aicall-builders/ai-call-assistant)
- Web: [aicall-builders/ai-call-assistant-web](https://github.com/aicall-builders/ai-call-assistant-web)
- Android: [aicall-builders/call-recorder-android](https://github.com/aicall-builders/call-recorder-android)
