# 🔧 Lambda Backend

소상공인 AI 통화 비서의 백엔드. **AWS Lambda 단일 함수**로 운영 중입니다.

---

## 📐 아키텍처
┌─────────────────┐
│  안드로이드 앱   │
│   (CallRecorder)│
└────────┬────────┘
│ HTTPS
▼
┌─────────────────────────────────────┐
│  AWS API Gateway (HTTP API)         │
│  sxj5qje9bd.execute-api.            │
│  ap-northeast-2.amazonaws.com       │
└────────┬────────────────────────────┘
│
▼
┌─────────────────────────────────────┐
│  AWS Lambda                         │
│  call-recorder-api                  │
│  - lambda_function.py (이 파일)      │
│  - keywords.json                    │
└────┬────────┬───────────┬───────────┘
│        │           │
▼        ▼           ▼
┌────┐  ┌─────────┐ ┌──────────┐
│ S3 │  │ RDS     │ │ Secrets  │
│음성│  │ MySQL   │ │ Manager  │
└────┘  └─────────┘ └──────────┘
│              │
▼              ▼
통화/요약 저장   DB 비밀번호
Firebase 키

## 🔌 외부 API

- **CLOVA Speech** (Naver Cloud) — 통화 녹음 → 한국어 STT
- **OpenAI GPT-4o-mini** — STT 텍스트 → 구조화된 요약
- **Kakao OAuth** — 카카오 로그인
- **Firebase Auth** — 카카오 토큰 → Firebase Custom Token

---

## 📁 파일 구조
lambda/
├── lambda_function.py   # Lambda 핸들러 (라우터 + 모든 비즈니스 로직)
├── keywords.json        # 업종별 카테고리 키워드 사전 (룰베이스 매칭)
├── requirements.txt     # Python 의존성 (참고용)
├── README.md            # 이 파일
└── deploy.md            # 배포 방법

---

## 🛣️ API 엔드포인트

| Method | Path | 설명 | 인증 |
|--------|------|------|------|
| POST | `/auth/kakao` | 카카오 로그인 → Firebase 토큰 발급 | ❌ |
| POST | `/clova/webhook` | CLOVA STT 완료 콜백 | ❌ |
| POST | `/stores` | 가게 등록 | ✅ |
| GET | `/stores` | 내 가게 목록 | ✅ |
| POST | `/calls/upload` | S3 업로드용 presigned URL 발급 | ✅ |
| POST | `/calls/{id}/process` | STT 처리 시작 | ✅ |
| GET | `/calls` | 통화 목록 (최신순) | ✅ |
| GET | `/calls/{id}` | 통화 상세 | ✅ |
| GET | `/calls/{id}/audio` | 음성 재생용 presigned URL | ✅ |
| PATCH | `/calls/{id}` | 분류 변경 (BUSINESS/PERSONAL/UNCLASSIFIED) | ✅ |
| DELETE | `/calls/{id}` | 통화 삭제 | ✅ |
| GET | `/summaries/{id}` | 요약 상세 | ✅ |

✅ = `Authorization: Bearer <Firebase ID Token>` 헤더 필요

---

## 🔄 통화 처리 흐름

안드로이드: 통화 녹음 감지
안드로이드 → 백엔드: POST /calls/upload (메타데이터)
백엔드 → 안드로이드: presigned URL 반환
안드로이드 → S3: 음성 파일 업로드 (직접)
안드로이드 → 백엔드: POST /calls/{id}/process
백엔드 → CLOVA: STT 요청 (async, callback URL 포함)
CLOVA → 백엔드: POST /clova/webhook (STT 완료)
백엔드: STT 텍스트 → 키워드 매칭 + GPT 요약
백엔드 → DB: summaries 테이블 저장
안드로이드: GET /calls/{id} 폴링 → 결과 표시


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

| 변수 | 설명 |
|------|------|
| `CLOVA_INVOKE_URL` | NCP CLOVA Speech invoke URL |
| `CLOVA_SECRET_KEY` | NCP CLOVA Speech secret key |
| `S3_BUCKET` | 음성 저장 버킷 (기본: `call-recoder-audio-1017`) |
| `ANTHROPIC_API_KEY` | (이름은 anthropic이지만 실제로는 OpenAI API 키 - TODO 정리) |

⚠️ DB 비밀번호와 Firebase Admin SDK는 **AWS Secrets Manager**에서 가져옵니다 (코드 내 ARN 하드코딩).

---

## 🚀 배포

자세한 배포 방법은 [`deploy.md`](./deploy.md) 참고.

현재는 AWS Lambda 콘솔에서 직접 코드를 수정/배포하고 있습니다.

---

## ⚠️ 알려진 이슈 / TODO

- [ ] `ANTHROPIC_API_KEY` 환경 변수 이름이 실제로는 OpenAI 키. 변수명 정리 필요
- [ ] 데모용 임시 엔드포인트 (`/demo/seed`, `/demo/clean`, `/migrate/extracted-info`) 발표 후 삭제
- [ ] DB Secret ARN 하드코딩 → 환경 변수로 분리
- [ ] 단일 파일 → 모듈 분리 (필요 시)
- [ ] 단위 테스트 추가