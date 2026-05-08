# 📞 AI 통화 비서 (AI Call Assistant)

소상공인을 위한 AI 통화 분석 서비스. 통화 녹음을 자동으로 STT 변환 + GPT 요약하여 예약/주문/문의/불만 등을 한눈에 관리할 수 있습니다.

> 🎬 [시연 영상 다운로드](https://drive.google.com/file/d/1jJNRF2CCVcCKSpdIPUODjWL6F5exxJ-T/view?usp=sharing) · 🌐 [웹 데모](https://dk1k75g0ji3vw.cloudfront.net)

---

## 🎯 핵심 기능

- 🎙️ **자동 통화 녹음 분석** — 삼성 통화 녹음 앱이 저장한 음성 파일 자동 감지
- 🗣️ **STT (CLOVA Speech)** — 한국어 화자 분리 STT
- 🤖 **AI 요약 (GPT-4o-mini)** — 통화 내용 3줄 요약 + 카테고리 분류
- 🔍 **구조화 정보 추출** — 고객명, 연락처, 예약 날짜/시간/인원, 메뉴, 특이사항 자동 추출
- 📊 **카테고리 분류** — 예약/주문/취소/환불/불만/문의/칭찬 자동 라벨링
- 🔊 **음성 재생** — 통화 상세에서 원본 음성 재생
- 📅 **캘린더 연동** — 예약 정보 자동 등록
- 👥 **업무/개인 분류** — 통화별 분류 가능

---

## 🏗️ 시스템 아키텍처
┌─────────────────┐         ┌─────────────────┐
│  Android App    │         │   Web (Next.js) │
│  (Kotlin/Compose)│         │  Login + Dashboard│
└────────┬────────┘         └────────┬────────┘
│                            │
│      Firebase Auth         │
└─────────────┬──────────────┘
│
▼
┌─────────────────────────────┐
│  AWS API Gateway (HTTP API) │
└─────────────┬───────────────┘
│
▼
┌─────────────────────────────┐
│  AWS Lambda                 │
│  call-recorder-api          │
└──┬───────┬──────────┬───────┘
│       │          │
▼       ▼          ▼
┌────┐  ┌─────┐  ┌────────┐
│ S3 │  │ RDS │  │Secrets │
│음성│  │MySQL│  │Manager │
└────┘  └─────┘  └────────┘
│
▼
┌──────────────────┐
│  CLOVA Speech    │ ← STT 변환
│  OpenAI GPT-4o   │ ← 요약 + 추출
└──────────────────┘

---

## 📁 리포 구조
ai-call-assistant/
├── app/                    # 🚧 [LEGACY] FastAPI + Whisper 프로토타입 (사용 안 함)
├── lambda/                 # ✅ 운영 중인 Lambda 백엔드
│   ├── lambda_function.py  # Lambda 핸들러 (라우터 + 비즈니스 로직)
│   ├── keywords.json       # 키워드 사전
│   ├── requirements.txt
│   ├── README.md           # Lambda 백엔드 상세 설명
│   └── deploy.md           # 배포 가이드
├── scripts/                # 유틸리티 스크립트
├── tests/                  # 테스트 (FastAPI 시절)
├── .env.example
└── README.md               # 이 파일

> 📌 `app/` 폴더는 초기에 만든 FastAPI + Whisper 기반 프로토타입입니다. 발표 일정상 AWS Lambda로 빠르게 전환하여 운영 중이며, **`app/` 폴더는 더 이상 사용되지 않습니다.** 향후 정리 예정.

---

## 🔗 관련 리포지토리

| 리포 | 설명 |
|------|------|
| **이 리포** (`ai-call-assistant`) | Lambda 백엔드 |
| `AndroidProjects/CallRecorder` | 안드로이드 앱 (Kotlin + Jetpack Compose) |
| `ai-call-assistant-web` | 웹 (Next.js, S3 + CloudFront) |

---

## 🛠️ 기술 스택

### Backend
- **AWS Lambda** (Python 3.x)
- **AWS API Gateway** (HTTP API)
- **AWS RDS MySQL** (통화/요약 데이터)
- **AWS S3** (음성 파일 저장)
- **AWS Secrets Manager** (DB 비밀번호, Firebase Admin SDK)
- **Firebase Auth** (Custom Token 기반 인증)

### External
- **NCP CLOVA Speech** — 한국어 STT (화자 분리)
- **OpenAI GPT-4o-mini** — 통화 요약 + 구조화 추출
- **Kakao OAuth** — 카카오 로그인

### Frontend
- **Android**: Kotlin, Jetpack Compose, Hilt, Retrofit, ExoPlayer
- **Web**: Next.js 14 (App Router), Tailwind CSS

### CI/CD
- **Web**: GitHub Actions → S3 + CloudFront 자동 배포
- **Lambda**: AWS 콘솔 직접 수정 (MVP 단계)

---

## 🚀 빠른 시작

### 1. 안드로이드 앱 설치
[APK 다운로드](https://drive.google.com/file/d/1jJNRF2CCVcCKSpdIPUODjWL6F5exxJ-T/view?usp=sharing) → 폰에 설치 → 카카오 로그인

### 2. 웹에서 통화 내역 확인
[https://dk1k75g0ji3vw.cloudfront.net](https://dk1k75g0ji3vw.cloudfront.net) → 카카오 로그인 → 대시보드

### 3. 백엔드 개발/배포
[`lambda/README.md`](./lambda/README.md) 및 [`lambda/deploy.md`](./lambda/deploy.md) 참고.

---

## 📊 개발 현황

- ✅ 카카오 로그인 + Firebase 인증
- ✅ 가게 등록/관리
- ✅ 통화 녹음 자동 감지 + 백엔드 업로드 (안드로이드)
- ✅ CLOVA STT 비동기 처리
- ✅ GPT 기반 요약 + 구조화 정보 추출
- ✅ 통화 목록 + 상세 화면
- ✅ 음성 재생 (안드로이드 + 웹)
- ✅ 카테고리 자동 분류 + 수동 변경
- ✅ 캘린더 화면

---

## 🚧 향후 계획

- [ ] Lambda 코드 모듈 분리 (현재 단일 파일 ~700줄)
- [ ] 자동 배포 파이프라인 (GitHub Actions or AWS SAM)
- [ ] 임시 엔드포인트 제거 (`/demo/*`, `/migrate/*`)
- [ ] 환경 변수명 정리 (`ANTHROPIC_API_KEY` → `OPENAI_API_KEY`)
- [ ] 단위 테스트 추가
- [ ] Play Store 정식 배포 (현재는 APK 직접 배포)
- [ ] 다른 업종 키워드 사전 추가 (현재는 식당만)

---

## 👥 Contributors

- **정성민** ([@seongminj0613-tech](https://github.com/seongminj0613-tech)) — 풀스택 (Android, Backend, Web)

---

## 📄 License

Private. 발표/포트폴리오 용도.