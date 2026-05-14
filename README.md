# 🐍 ai-call-assistant (Backend)

> 소상공인 AI 통화 요약 서비스의 백엔드 — AWS Lambda 기반 서버리스 API

[🌐 웹 데모](https://dk1k75g0ji3vw.cloudfront.net) 

[📱 APK 다운로드](https://drive.google.com/file/d/1jJNRF2CCVcCKSpdIPUODjWL6F5exxJ-T/view?usp=sharing) 

[📖 메인 README](https://github.com/seongminj0613-tech/business-ai-assistant)

---

## 🎯 역할

본 레포는 [소상공인 AI 통화 요약 서비스](https://github.com/seongminj0613-tech/business-ai-assistant)의 **백엔드 API**를 담당합니다.

- 카카오 OAuth → Firebase Custom Token 교환
- 매장·통화·요약 CRUD
- S3 presigned URL 발급
- CLOVA Speech 비동기 호출 + Webhook 수신
- 룰베이스 NLP + GPT-4o-mini 하이브리드 요약 처리

> 안드로이드·웹 클라이언트는 별도 레포에서 관리됩니다. [관련 저장소](#-관련-저장소) 참조.

---

## 🏗️ 아키텍처 (백엔드 관점)

```
        Android / Web Client
                │
                │ HTTPS + Firebase ID Token
                ▼
   ┌─────────────────────────────────┐
   │  AWS API Gateway (HTTP API)     │
   └────────────────┬────────────────┘
                    ▼
   ┌─────────────────────────────────┐
   │  AWS Lambda (Python 3.12)       │
   │  call-recorder-api              │
   │  VPC 프라이빗 서브넷            │
   │                                 │
   │  ┌───────────────────────────┐  │
   │  │ Router (단일 핸들러)       │  │
   │  ├───────────────────────────┤  │
   │  │ Auth (Firebase Admin SDK) │  │
   │  ├───────────────────────────┤  │
   │  │ Business Logic            │  │
   │  ├───────────────────────────┤  │
   │  │ Rule-based NLP + LLM      │  │
   │  └───────────────────────────┘  │
   └──┬──────────┬──────────┬────────┘
      │ Interface│ Gateway  │ VPC 내부
      ▼ Endpoint ▼ Endpoint ▼
   ┌────────┐ ┌────────┐ ┌────────┐
   │Secrets │ │   S3   │ │  RDS   │
   │Manager │ │ (음성) │ │MySQL 8 │
   └────────┘ └────────┘ └────────┘
                    │
                    ▼   외부 API
            ┌─────────────────┐
            │ CLOVA Speech    │
            │ OpenAI          │
            │ Kakao           │
            └─────────────────┘
```

---

## 🛠️ 기술 스택

### Runtime & Infrastructure
- **AWS Lambda** (Python 3.12)
- **AWS API Gateway** (HTTP API, 단일 진입점)
- **AWS RDS MySQL 8.0** (db.t4g.micro, 프라이빗 서브넷)
- **AWS S3** (Seoul 리전, SSE-S3 암호화)
- **AWS Secrets Manager** (DB 자격증명, Firebase Admin SDK)
- **AWS VPC** (3계층 보안그룹 + VPC Endpoint)
- **Lambda Layer** ([별도 레포](https://github.com/seongminj0613-tech/lambda-layer))

### Authentication
- **Firebase Authentication** (Custom Token 모델)
- **Kakao OAuth** → Firebase Custom Token 교환

### External AI
- **NCP CLOVA Speech** — 한국어 STT (장문 인식, async + Webhook)
- **OpenAI GPT-4o-mini** — 통화 요약 + 구조화 추출 (JSON 스키마)

---

## 🔌 API 엔드포인트

진입점: AWS API Gateway HTTP API · 인증: `Authorization: Bearer <Firebase ID Token>`

| Method | Path | 설명 | 인증 |
|--------|------|------|------|
| POST | `/auth/kakao` | 카카오 토큰 → Firebase Custom Token 교환 | ❌ |
| POST | `/clova/webhook` | CLOVA STT 완료 콜백 수신 | ❌ (CLOVA만) |
| POST | `/stores` | 가게 등록 | ✅ |
| GET | `/stores` | 내 가게 목록 | ✅ |
| POST | `/calls/upload` | S3 presigned URL 발급 + calls INSERT | ✅ |
| POST | `/calls/{id}/process` | CLOVA STT 처리 시작 | ✅ |
| GET | `/calls` | 통화 목록 (필터: store_id, status) | ✅ |
| GET | `/calls/{id}` | 통화 상세 | ✅ |
| GET | `/calls/{id}/audio` | 음성 재생용 presigned URL (10분) | ✅ |
| PATCH | `/calls/{id}` | 분류 변경 (BUSINESS/PERSONAL) | ✅ |
| DELETE | `/calls/{id}` | 통화 삭제 | ✅ |
| GET | `/summaries/{id}` | 요약 상세 | ✅ |

---

## 🧠 처리 파이프라인

### STT 처리 (비동기)

Lambda 15분 타임아웃 제약을 우회하기 위해 **CLOVA async 모드 + Webhook 콜백** 구조를 사용합니다.

```
1. 클라이언트 → POST /calls/upload
   └─ presigned URL 발급, calls INSERT (status='uploaded')

2. 클라이언트 → S3 직접 PUT (Lambda 경유 안 함)

3. 클라이언트 → POST /calls/{id}/process
   └─ CLOVA Speech async 호출 (callback URL 지정)

4. CLOVA → POST /clova/webhook (STT 완료 시)
   └─ stt_result 저장 (status='transcribed')
   └─ NLP 파이프라인 트리거
```

### NLP 처리 (하이브리드)

LLM 의존도를 낮추기 위해 룰베이스 NLP를 1차로 적용합니다. 룰베이스가 채우지 못한 슬롯이 임계 이상일 때만 GPT-4o-mini를 fallback으로 호출하여 **LLM 호출 비율을 25% 이하로 제어**합니다.

| 단계 | 처리 방식 | LLM 사용 |
|------|---------|--------|
| 1. 의도 분류 | 키워드·패턴 룰 (8종 분류) | ❌ |
| 2. 엔터티 추출 | 정규식·사전 (날짜·시간·인원·메뉴·금액·전화번호) | ❌ |
| 3. 구조화 카드 채우기 | 룰베이스 템플릿 슬롯 매핑 | ❌ |
| 4. 룰 실패 케이스 보강 | 임계 초과 시에만 LLM 호출 | ⚠️ 일부 |
| 5. 통화 요약 생성 | 룰 우선, 자연스러움 부족 시 LLM | ⚠️ 일부 |

---

## 🔐 보안 설계

### 1. 네트워크 격리 (3계층 보안그룹)
| 보안그룹 | 인바운드 규칙 | 효과 |
|---------|------------|------|
| `lambda-sg` | 없음 (아웃바운드 only) | Lambda 외부 직접 접근 불가 |
| `rds-sg` | TCP 3306 ← lambda-sg | RDS는 Lambda에서만 접근 |
| `endpoint-sg` | TCP 443 ← lambda-sg | Secrets Manager는 Lambda만 접근 |

### 2. 자격증명 관리
- 외부 API 키와 DB 비밀번호는 **AWS Secrets Manager에서 런타임 조회**
- 코드·환경변수에 비밀 직접 저장 금지
- RDS 마스터 비밀번호 자동 회전 30일

### 3. 인증·인가
- 모든 보호 엔드포인트에서 `firebase_admin.auth.verify_id_token()` 검증
- 리소스 소유권(`owner_id = 요청자 UID`) 확인
- 401 응답 시 클라이언트 측 토큰 자동 정리

### 4. S3 업로드 보안
| 항목 | 값 |
|------|------|
| URL 유효 기간 | 10분 (600초) |
| 허용 메서드 | PUT only |
| 객체 키 패턴 | `recordings/{store_id}/{call_id}/{file_name}` |
| 버킷 ACL | Block all public access |
| 통화 길이 제약 | 5초 미만 거부 |
| 통화 신선도 | 녹음 발생 1시간 이내만 허용 |

---

## 🗃️ 데이터 모델 (요약)

```sql
users       -- 사용자 (Firebase UID + 카카오 ID)
stores      -- 매장 (owner_id → users)
calls       -- 통화 메타데이터 (s3_key, stt_result, status)
summaries   -- AI 요약 결과 (category, keywords, extracted_info JSON)
```

모든 ID는 UUID4 문자열(`VARCHAR(64)`). MySQL JSON 컬럼으로 `stt_result`, `keywords`, `extracted_info` 저장.

---

## 🚀 배포

### 환경변수
Lambda 환경변수에는 **식별자 성격의 값**만 두고, 비밀은 Secrets Manager의 ARN만 환경변수로 등록합니다.

`.env.example` 참조 (로컬 개발용 placeholder).

### 배포 절차 (Phase 1)
1. 코드 수정 (`lambda/lambda_function.py` 또는 `lambda/keywords.json`)
2. AWS Console → Lambda → `call-recorder-api` → Deploy
3. Smoke test (인증·매장·통화 조회)

자동화는 Phase 2 로드맵.

---

## 📊 비기능 요구사항 (SLA)

| 지표 | 목표 |
|------|------|
| 통화 종료 → 카드 알림 도달 (평균) | 60초 이내 |
| API 응답 (p95, /calls 목록) | 1초 이내 |
| 통화 → 카드 처리 성공률 | ≥ 95% |
| LLM Fallback 호출 비율 | ≤ 25% (원가 절감 KPI) |
| API 서버 가용성 (월) | ≥ 99.5% |

---

## 🔗 관련 저장소

| 저장소 | 설명 |
|--------|------|
| [business-ai-assistant](https://github.com/seongminj0613-tech/business-ai-assistant) | 📖 메인 통합 문서 |
| **이 저장소** (`ai-call-assistant`) | 🐍 Backend (이 레포) |
| [ai-call-assistant-web](https://github.com/seongminj0613-tech/ai-call-assistant-web) | 🌐 Web (Next.js) |
| [call-recorder-android](https://github.com/seongminj0613-tech/call-recorder-android) | 📱 Android (Kotlin) |
| [lambda-layer](https://github.com/seongminj0613-tech/lambda-layer) | ☁️ Lambda Layer |

---

## 📈 향후 로드맵 (Phase 2)

- Lambda 코드 모듈 분리
- GitHub Actions 기반 자동 배포 파이프라인
- 단위 테스트 추가
- 키워드 룰셋 핫리로드 (Redis)
- LLM 호출 PII 마스킹 (전화번호·이름 토큰화)

---



## 📄 라이선스

부트캠프 학습 프로젝트입니다. 코드 참고·학습 목적의 열람은 자유이나, 본 서비스의 아키텍처·디자인·문서를 무단으로 상업적 목적에 재이용하지 않기를 부탁드립니다.

---

*문서 기준: Tech Spec v2.5 (2026.05.11), API명세서 v1.0.0*
