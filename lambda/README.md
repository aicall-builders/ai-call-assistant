# 🔧 Lambda Backend

소상공인 AI 통화 비서의 백엔드. **AWS Lambda 핸들러 분리 구조**로 운영 중입니다.

---

## 📐 아키텍처

```
┌─────────────────┐     ┌─────────────────┐
│  안드로이드 앱   │     │    웹 (Next.js)  │
└────────┬────────┘     └────────┬────────┘
         │ HTTPS                  │ HTTPS
         └──────────┬─────────────┘
                    ▼
┌─────────────────────────────────────────────┐
│  AWS API Gateway (REST API)                 │
│  avrq2kzfp9.execute-api.ap-northeast-2      │
└────┬──────────────┬──────────────┬──────────┘
     │              │              │
     ▼              ▼              ▼
┌─────────┐  ┌─────────┐  ┌─────────────┐
│  auth   │  │  call   │  │     nlp     │
│ handler │  │ handler │  │   handler   │
│         │  │         │  │             │
│ 카카오  │  │ stores  │  │ CLOVA       │
│ 로그인  │  │ calls   │  │ Webhook     │
│Firebase │  │ CRUD    │  │ LLM 요약    │
└────┬────┘  └────┬────┘  └──────┬──────┘
     │             │              │
     ▼             ▼              ▼
  Firebase       RDS/S3       CLOVA/GPT
```

---

## 🔄 아키텍처 변경 이유

### ❌ 이전: 모놀리식 구조
- 단일 Lambda(`call-recorder-api`)가 인증·통화·NLP 전부 담당
- 에러 발생 시 원인 파악이 어려움
- 수정 시 전체 함수 재배포 필요
- CloudWatch 로그가 뒤섞여 모니터링 불편

### ✅ 현재: 핸들러 분리 구조
- 기능별 독립 배포 가능
- CloudWatch 로그가 핸들러별로 분리되어 에러 추적 용이
- 인증 / 통화 / NLP 오류를 개별 모니터링 가능

---

## 📁 파일 구조

```
lambda/
├── auth_handler.py   # 카카오 로그인, Firebase Custom Token 발급
├── call_handler.py   # stores/calls CRUD, S3 업로드, STT 처리 시작
├── nlp_handler.py    # CLOVA Webhook 수신, LLM 요약 처리
├── keywords.json     # 룰베이스 NLP 키워드 사전
├── requirements.txt  # Python 의존성 (참고용)
├── README.md         # 이 파일
└── deploy.md         # 배포 방법
```

| 파일 | Lambda 함수 | 담당 역할 |
|------|------------|---------|
| `auth_handler.py` | `call-recorder-api-auth` | 카카오 로그인, Firebase 토큰 발급 |
| `call_handler.py` | `call-recorder-api-call` | stores/calls CRUD, S3, STT |
| `nlp_handler.py` | `call-recorder-api-nlp` | CLOVA Webhook, LLM 요약 |

---

## 🔌 외부 API

- **CLOVA Speech** (Naver Cloud) — 통화 녹음 → 한국어 STT
- **OpenAI GPT-4o-mini** — STT 텍스트 → 구조화된 요약
- **Kakao OAuth** — 카카오 로그인
- **Firebase Auth** — 카카오 토큰 → Firebase Custom Token

---

## 🛣️ API 엔드포인트

진입점: `https://avrq2kzfp9.execute-api.ap-northeast-2.amazonaws.com/prod`

| Method | Path | 설명 | 인증 | 핸들러 |
|--------|------|------|------|--------|
| POST | `/auth/kakao` | 카카오 로그인 → Firebase 토큰 발급 | ❌ | auth |
| POST | `/clova/webhook` | CLOVA STT 완료 콜백 | ❌ | nlp |
| POST | `/stores` | 가게 등록 | ✅ | call |
| GET | `/stores` | 내 가게 목록 | ✅ | call |
| POST | `/calls/upload` | S3 업로드용 presigned URL 발급 | ✅ | call |
| POST | `/calls/{id}/process` | STT 처리 시작 | ✅ | call |
| GET | `/calls` | 통화 목록 (최신순) | ✅ | call |
| GET | `/calls/{id}` | 통화 상세 | ✅ | call |
| GET | `/calls/{id}/audio` | 음성 재생용 presigned URL (10분) | ✅ | call |
| PATCH | `/calls/{id}` | 분류 변경 (BUSINESS/PERSONAL/UNCLASSIFIED) | ✅ | call |
| DELETE | `/calls/{id}` | 통화 삭제 | ✅ | call |
| GET | `/summaries/{id}` | 요약 상세 | ✅ | call |

✅ = `Authorization: Bearer <Firebase ID Token>` 헤더 필요

---

## 🔄 통화 처리 흐름

```
1. 안드로이드: 통화 녹음 감지
2. 안드로이드 → 백엔드: POST /calls/upload (메타데이터)
3. 백엔드 → 안드로이드: presigned URL 반환
4. 안드로이드 → S3: 음성 파일 업로드 (직접)
5. 안드로이드 → 백엔드: POST /calls/{id}/process
6. 백엔드 → CLOVA: STT 요청 (async, callback URL 포함)
7. CLOVA → 백엔드: POST /clova/webhook (STT 완료)
8. 백엔드: STT 텍스트 → 키워드 매칭 + GPT 요약
9. 백엔드 → DB: summaries 테이블 저장
10. 안드로이드/웹: GET /calls/{id} 폴링 → 결과 표시
```

---

## 🗄️ DB 스키마 (요약)

### `users`
- `id` (UUID), `firebase_uid`, `kakao_id`, `name`, `role`

### `stores`
- `id` (UUID), `name`, `owner_id` → users

### `calls`
- `id` (UUID), `store_id`, `user_id`, `caller_number`, `caller_category`, `s3_key`, `duration`, `status`, `stt_result`, `clova_job_id`, `error_message`, `created_at`
- `status`: `uploaded` → `processing` → `transcribed` → `summarized` (또는 `error`)

### `summaries`
- `id` (UUID), `call_id`, `summary`, `category`, `sentiment`, `action_required`, `keywords` (JSON), `extracted_info` (JSON), `is_read`

`extracted_info` 예시:
```json
{
  "category_code": "reservation",
  "customer_name": "김민수",
  "phone": "010-1234-5678",
  "date": "2026-05-08",
  "time": "19:00",
  "party_size": 4,
  "menu": [],
  "special_notes": null
}
```

---

## 🔐 환경 변수

Lambda 콘솔의 **Configuration → Environment variables**에서 설정:

| 변수 | 설명 | 사용 Lambda |
|------|------|------------|
| `CLOVA_INVOKE_URL` | NCP CLOVA Speech invoke URL | call |
| `CLOVA_SECRET_KEY` | NCP CLOVA Speech secret key | call |
| `S3_BUCKET` | 음성 저장 버킷 (기본: `call-recoder-audio-1017`) | call |
| `ANTHROPIC_API_KEY` | OpenAI API 키 (변수명 정리 필요 - TODO) | nlp |
| `FIREBASE_SERVICE_ACCOUNT_BASE64` | Firebase 서비스 계정 JSON (Base64) | auth |

⚠️ DB 비밀번호와 Firebase Admin SDK는 **AWS Secrets Manager**에서 가져옵니다.

---

## 🚀 배포

자세한 배포 방법은 [`deploy.md`](./deploy.md) 참고.

| 수정 대상 | 배포 함수 |
|---------|---------|
| 로그인 로직 | `call-recorder-api-auth` |
| 통화/가게 CRUD | `call-recorder-api-call` |
| STT/LLM 처리 | `call-recorder-api-nlp` |

---

## ⚠️ 알려진 이슈 / TODO

- [ ] `ANTHROPIC_API_KEY` 환경 변수 이름이 실제로는 OpenAI 키. 변수명 정리 필요
- [ ] DB Secret ARN 하드코딩 → 환경 변수로 분리
- [ ] Redis 캐시 연결 (키워드 룰셋 핫리로드)
- [ ] CLOVA Webhook 재시도 메커니즘 추가
- [ ] GitHub Actions 자동 배포 파이프라인
- [ ] 단위 테스트 추가
