# 🐍 ai-call-assistant (Backend)

> 소상공인 AI 통화 요약 서비스의 백엔드 — AWS Lambda 기반 서버리스 API

[🌐 웹 데모](https://dk1k75g0ji3vw.cloudfront.net) 

[📱 APK 다운로드](https://drive.google.com/file/d/1jJNRF2CCVcCKSpdIPUODjWL6F5exxJ-T/view?usp=sharing) 

[📊 모니터링 대시보드](http://15.165.17.218:3000/public-dashboards/97b5462a12b54bf9b827b07eeee699f4)

[📖 메인 README](https://github.com/seongminj0613-tech/business-ai-assistant)

---

## 🎯 역할

본 레포는 [소상공인 AI 통화 요약 서비스](https://github.com/seongminj0613-tech/business-ai-assistant)의 **백엔드 API**를 담당합니다.

- 카카오 OAuth → Firebase Custom Token 교환
- 매장·통화·요약 CRUD
- S3 presigned URL 발급
- CLOVA Speech 비동기 호출 + Webhook 수신
- 룰베이스 NLP + GPT-4o-mini 하이브리드 요약 처리
- ElastiCache Redis 캐싱 (키워드 핫리로드 / 토큰 캐싱 / 중복 방지)

> 안드로이드·웹 클라이언트는 별도 레포에서 관리됩니다. [관련 저장소](#-관련-저장소) 참조.

---

## 🏗️ 아키텍처 변경 이력

### ❌ Phase 0: 모놀리식 구조 (구버전)

초기 MVP는 단일 Lambda 함수(`call-recorder-api`)가 인증·통화·NLP 처리를 모두 담당하는 **모놀리식 구조**였습니다.

```
Android / Web
      │
      ▼
API Gateway
      │
      ▼
call-recorder-api (단일 Lambda)
  ├── 카카오 로그인
  ├── stores CRUD
  ├── calls CRUD
  ├── CLOVA STT
  └── LLM 요약
      │
      ▼
RDS / S3 / 외부 API
```

**문제점:**
- 한 함수에 모든 로직이 집중되어 에러 발생 시 원인 파악이 어려움
- 특정 기능만 수정해도 전체 함수를 재배포해야 함
- CloudWatch 로그가 뒤섞여 모니터링이 불편함
- 기능별 독립 스케일링 불가

---

### ✅ Phase 1: 핸들러 분리 + Redis 캐싱 (현재)

오류 분석 용이성과 독립 배포를 위해 **기능별 Lambda 분리**를 진행하고, 공통 ElastiCache Redis 클러스터를 도입했습니다.

```
        Android / Web Client
                │
                │ HTTPS + Firebase ID Token
                ▼
   ┌─────────────────────────────────┐
   │  AWS API Gateway (REST API)     │
   │  avrq2kzfp9 · /prod (ap-ne-2)   │
   └────┬──────────┬──────────┬──────┘
        │          │          │
        ▼          ▼          ▼
┌──────────┐ ┌──────────┐ ┌──────────┐
│  auth    │ │  call    │ │   nlp    │
│ handler  │ │ handler  │ │ handler  │
│          │ │          │ │          │
│ 카카오   │ │ stores   │ │ CLOVA    │
│ 로그인   │ │ calls    │ │ Webhook  │
│ Firebase │ │ CRUD     │ │ LLM 요약 │
└────┬─────┘ └────┬─────┘ └────┬─────┘
     │            │            │
     │   ┌────────┴────────┐   │   (3개 핸들러 공유)
     ├──▶│ ElastiCache Redis│◀──┤
     │   │ 키워드/토큰/락    │   │
     │   └─────────────────┘   │
     ▼            ▼            ▼
  Firebase       RDS/S3       CLOVA/GPT
```

**개선 효과:**
- 기능별 독립 배포 → 수정 범위 최소화
- CloudWatch 로그가 핸들러별로 분리되어 에러 추적 용이
- 인증 오류 / 통화 오류 / NLP 오류를 개별 모니터링 가능
- Redis 캐싱으로 키워드 사전 무중단 갱신(102ms → 5ms) + Firebase 토큰 검증 비용 절감
- 향후 기능별 독립 스케일링 기반 마련

**Phase 1에서 함께 도입한 것:**
- Redis 캐싱 — keywords 핫리로드 102ms → 5ms (20배 개선)
- Firebase 토큰 캐싱 + 중복 업로드 방지 (SET NX 락)
- CloudWatch + Slack 알림 연동
- CLOVA Webhook 폴링 메커니즘 (5분 주기 자동 복구)

---

## 📁 Lambda 구조

```
lambda/
  ├── auth_handler.py   # 카카오 로그인, Firebase Custom Token 발급
  ├── call_handler.py   # stores/calls CRUD, S3 업로드, STT 처리 시작
  ├── nlp_handler.py    # CLOVA Webhook 수신, LLM 요약 처리
  ├── redis_client.py   # Redis 공통 연결 모듈 (RedisCluster, TLS)
  ├── keywords.json     # 룰베이스 NLP 키워드 사전 (Redis 핫리로드 원본)
  ├── requirements.txt  # 의존성
  └── deploy.md         # 배포 절차
```

| 파일 | Lambda 함수 | 담당 엔드포인트 |
|------|------------|--------------|
| `auth_handler.py` | `call-recorder-api-auth` | `POST /auth/kakao` |
| `call_handler.py` | `call-recorder-api-call` | `/stores`, `/calls/*`, `/summaries/*` |
| `nlp_handler.py` | `call-recorder-api-nlp` | `POST /clova/webhook` |

---

## 🛠️ 기술 스택

### Runtime & Infrastructure
- **AWS Lambda** (Python 3.12)
- **AWS API Gateway** (REST API, `avrq2kzfp9`, `/prod` 스테이지)
- **AWS ElastiCache Redis 7.1** (키워드 핫리로드 / Firebase 토큰 캐싱 / 중복 방지 락)
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

진입점: `https://avrq2kzfp9.execute-api.ap-northeast-2.amazonaws.com/prod`  
인증: `Authorization: Bearer <Firebase ID Token>`

| Method | Path | 설명 | 인증 | 핸들러 |
|--------|------|------|------|--------|
| POST | `/auth/kakao` | 카카오 토큰 → Firebase Custom Token 교환 | ❌ | auth |
| POST | `/clova/webhook` | CLOVA STT 완료 콜백 수신 | ❌ | nlp |
| POST | `/stores` | 가게 등록 | ✅ | call |
| GET | `/stores` | 내 가게 목록 | ✅ | call |
| POST | `/calls/upload` | S3 presigned URL 발급 + calls INSERT | ✅ | call |
| POST | `/calls/{id}/process` | CLOVA STT 처리 시작 | ✅ | call |
| GET | `/calls` | 통화 목록 (필터: store_id, status) | ✅ | call |
| GET | `/calls/{id}` | 통화 상세 | ✅ | call |
| GET | `/calls/{id}/audio` | 음성 재생용 presigned URL (10분) | ✅ | call |
| PATCH | `/calls/{id}` | 분류 변경 (BUSINESS/PERSONAL) | ✅ | call |
| DELETE | `/calls/{id}` | 통화 삭제 | ✅ | call |
| GET | `/summaries/{id}` | 요약 상세 | ✅ | call |

---

## 🧠 처리 파이프라인

### STT 처리 (비동기)

Lambda 15분 타임아웃 제약을 우회하기 위해 **CLOVA async 모드 + Webhook 콜백** 구조를 사용합니다.

```
1. 클라이언트 → POST /calls/upload
   └─ presigned URL 발급, calls INSERT (status='uploaded')
   └─ Redis SET NX 락으로 동일 파일 중복 업로드 차단

2. 클라이언트 → S3 직접 PUT (Lambda 경유 안 함)

3. 클라이언트 → POST /calls/{id}/process
   └─ CLOVA Speech async 호출 (callback URL 지정)

4. CLOVA → POST /clova/webhook (STT 완료 시)
   └─ stt_result 저장 (status='transcribed')
   └─ NLP 파이프라인 트리거
```

### NLP 처리 (하이브리드)

키워드 사전은 Redis에서 핫리로드되며, 재배포 없이 사전 갱신이 반영됩니다.

| 단계 | 처리 방식 | LLM 사용 |
|------|---------|--------|
| 1. 의도 분류 | 키워드·패턴 룰 (8종 분류, Redis 캐싱) | ❌ |
| 2. 엔터티 추출 | 정규식·사전 (날짜·시간·인원·메뉴·전화번호) | ❌ |
| 3. 구조화 카드 채우기 | 룰베이스 템플릿 슬롯 매핑 | ❌ |
| 4. 룰 실패 케이스 보강 | 임계 초과 시에만 LLM 호출 | ⚠️ 일부 |
| 5. 통화 요약 생성 | 룰 우선, 자연스러움 부족 시 LLM | ⚠️ 일부 |

---

## ⚡ Redis 캐싱 설계

3개 Lambda 핸들러가 동일 ElastiCache Redis 클러스터(cluster mode, TLS)를 공유하며, `redis_client.py` 공통 모듈로 연결합니다.

| 용도 | 동작 | 효과 |
|------|------|------|
| **키워드 핫리로드** | `keywords.json`을 Redis에 캐싱, 변경 시 무중단 갱신 | 조회 102ms → 5ms (20배) |
| **Firebase 토큰 캐싱** | 검증된 ID Token을 TTL 캐싱 | 매 요청 Firebase 검증 호출 절감 |
| **중복 업로드 방지** | 업로드 중 call_id를 `SET NX` 락 | 동일 파일 중복 INSERT 차단 |
| **Rate Limiting (예정)** | INCR + EXPIRE 기반 (인프라 준비 완료) | Phase 2 적용 |

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

---

## 🚀 배포

### 환경변수
Lambda 환경변수에는 **식별자 성격의 값**만 두고, 비밀은 Secrets Manager의 ARN만 환경변수로 등록합니다.

`.env.example` 참조 (로컬 개발용 placeholder).

### 배포 절차 (Phase 1)
1. 코드 수정 (`lambda/auth_handler.py`, `call_handler.py`, `nlp_handler.py`)
2. `main` 브랜치 push → GitHub Actions가 해당 함수 자동 배포
3. Smoke test (인증·매장·통화 조회)

| 수정 대상 | 배포 함수 |
|---------|---------|
| 로그인 로직 | `call-recorder-api-auth` |
| 통화/가게 CRUD | `call-recorder-api-call` |
| STT/LLM 처리 | `call-recorder-api-nlp` |

---

## 📊 비기능 요구사항 (SLA)

| 지표 | 목표 |
|------|------|
| 통화 종료 → 카드 알림 도달 (평균) | 60초 이내 |
| API 응답 (p95, /calls 목록) | 1초 이내 |
| 통화 → 카드 처리 성공률 | ≥ 95% |
| LLM Fallback 호출 비율 | ≤ 25% |
| API 서버 가용성 (월) | ≥ 99.5% |

---

## 📈 향후 로드맵 (Phase 2)
- Rate Limiting 적용 (Redis INCR + EXPIRE, 인프라 준비 완료)
- 단위 테스트 추가
- Grafana 대시보드 고도화
- LLM 호출 PII 마스킹
- 삼성 권한 다중화

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

## 📄 라이선스

부트캠프 학습 프로젝트입니다. 코드 참고·학습 목적의 열람은 자유이나, 본 서비스의 아키텍처·디자인·문서를 무단으로 상업적 목적에 재이용하지 않기를 부탁드립니다.

---

*문서 기준: Tech Spec v3.0 (2026.05.20) — Phase 1 Lambda 분리 + Redis 캐싱 완료*
