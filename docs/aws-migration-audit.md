# AWS Migration Audit

## 1. 기준 자료

- `ai-call-assistant/docs/핸드오프_가이드.md`
- `ai-call-assistant/docs/핸드오프_가이드_2.md`
- `ai-call-assistant/docs/schema_1.sql`
- `ai-call-assistant/lambda/deploy.md`
- `ai-call-assistant/lambda/README.md`
- `ai-call-assistant/lambda/*.py`
- `ai-call-assistant/lambda/requirements.txt`
- `ai-call-assistant/lambda/migrations/20260526_calendar_connections.sql`
- `ai-call-assistant/lambda/migrations/20260611_custom_keywords.sql`
- `ai-call-assistant-web/README.md`, `next.config.mjs`, `lib/*.js`
- `call-recorder-android/app/build.gradle.kts`, `gradle.properties.example`, `data/api/*.kt`, `data/repository/CallRepository.kt`

주의: 기존 운영 데이터는 이전하지 않는다. 기존 계정의 ARN, endpoint, bucket, distribution, API Gateway ID 등은 새 계정에서 재생성한다. secret/key/password/token 값은 문서에 기록하지 않는다.

## 2. Workspace 구조

- `ai-call-assistant`: Python backend. 루트에는 FastAPI 로컬 개발용 `app/`가 있고, 실제 AWS 운영 배포 단위는 `lambda/`의 분리 핸들러 구조다.
- `ai-call-assistant-web`: Next.js 14 App Router 웹 관리자. `output: 'export'` 정적 export로 `out/`을 생성해 S3/CloudFront에 배포한다.
- `call-recorder-android`: Android Kotlin 앱. Retrofit/OkHttp로 API Gateway를 호출하고, presigned URL로 S3에 직접 업로드한다.

## 3. Backend 구조

실제 운영 기준은 `ai-call-assistant/lambda`다.

| 파일 | 역할 |
| --- | --- |
| `auth_handler.py` | Kakao/Google/Naver OAuth, Firebase Custom Token 발급, 인증 검증 |
| `call_handler.py` | stores/calls/customers/keywords, S3 presigned URL, STT 시작, EventBridge 재시도, 다른 핸들러 위임 |
| `nlp_handler.py` | CLOVA STT 결과 처리, OpenAI GPT 요약, 키워드/문자 추천, 고객 분석 task |
| `calendar_handler.py` | Google/Naver/Kakao 캘린더 연결 및 call 기반 일정 생성 |
| `notes_handler.py` | 통화 메모, 사진 presigned upload, 사진 조회/삭제 |
| `redis_client.py` | Redis cache/rate-limit/lock helper |

`app/`는 FastAPI 로컬/초기 구조로 보이며, AWS 재배포 판단에서는 `lambda/`를 우선한다.

## 4. Lambda 설정

핸드오프 기준 주요 함수:

| Lambda 함수 | Handler | Runtime | Timeout / Memory 기준 |
| --- | --- | --- | --- |
| `call-recorder-api-auth` | `auth_handler.lambda_handler` | Python 3.12 | 30s / 256MB |
| `call-recorder-api-call` | `call_handler.lambda_handler` | Python 3.12 | 300s / 512MB |
| `call-recorder-api-nlp` | `nlp_handler.lambda_handler` | Python 3.12 | 60s / 1024MB |
| `call-recorder-api-calendar` | `calendar_handler.lambda_handler` | Python 3.12 | 3s / 128MB |

`call_handler.py`가 `/auth/*`, `/calendar/*`, `/calls/*/note`, `/calls/*/photos*`를 각 핸들러로 위임할 수 있어, API Gateway를 단일 call Lambda에 묶는 구성과 함수별 통합 구성이 모두 가능하다. handoff는 분리 Lambda 구조를 기준으로 한다.

`lambda/requirements.txt`:

- `boto3`
- `pymysql`
- `requests`
- `firebase-admin`
- `openai`
- `redis==5.0.8`

Layer 필요 여부:

- 필요. handoff 기준 `call-recorder-redis-layer` 또는 dependency layer가 auth/call/nlp에 연결되어 있었다.
- 새 배포에서는 `lambda/requirements.txt` 기반으로 Python 3.12 호환 Lambda Layer를 빌드하거나, 각 함수 zip에 vendor package를 포함한다.
- `pymysql`, `firebase-admin`, `openai`, `redis`, `requests`가 Lambda 런타임 기본 제공이 아니므로 Layer 또는 패키징이 필수다.

## 5. API Gateway Routes

API Gateway는 REST API + Lambda Proxy 통합 기준이다. stage는 `prod`를 기준으로 한다.

| Method | Path | Handler | 근거 |
| --- | --- | --- | --- |
| `OPTIONS` | `/*` | 각 handler | CORS preflight |
| `GET` | `/auth/{provider}/authorize` | auth | code OAuth 시작 |
| `POST` | `/auth/kakao` | auth | Kakao 로그인 |
| `POST` | `/auth/google` | auth | Google 로그인 |
| `POST` | `/auth/naver` | auth | Naver 로그인 |
| `POST` | `/auth/verify` | auth | Firebase token 검증 |
| `POST` | `/auth/logout` | auth | logout/cache invalidation |
| `GET` | `/stores` | call | store 목록 |
| `POST` | `/stores` | call | store 생성 |
| `GET` | `/stores/{storeId}/keywords` | call | custom keyword 목록 |
| `POST` | `/stores/{storeId}/keywords` | call | custom keyword 생성 |
| `PATCH` | `/stores/{storeId}/keywords/{keywordId}` | call | custom keyword 활성/수정 |
| `DELETE` | `/stores/{storeId}/keywords/{keywordId}` | call | custom keyword 삭제 |
| `GET` | `/me` | call | 사용자 프로필/업종 |
| `PATCH` | `/me` | call | 사용자 업종 수정 |
| `GET` | `/calls` | call | 통화 목록 |
| `POST` | `/calls/upload` | call | S3 upload presigned URL 발급 |
| `POST` | `/calls/{id}/process` | call | STT/요약 처리 시작 |
| `GET` | `/calls/{id}` | call | 통화 상세 + summary join |
| `PATCH` | `/calls/{id}` | call | caller/category 수정 |
| `DELETE` | `/calls/{id}` | call | 통화 삭제 |
| `GET` | `/calls/{id}/audio` | call | audio 재생용 presigned URL |
| `GET` | `/customers/{phone}` | call | 고객 프로필 + AI 분석 조회 |
| `PATCH` | `/customers/{phone}` | call | 고객 프로필 편집 |
| `GET` | `/calendar/events` | calendar | 캘린더 이벤트 조회 |
| `GET` | `/calendar/connections` | calendar | 캘린더 연결 목록 |
| `GET` | `/calendar/connections/{provider}/authorize` | calendar | 캘린더 OAuth URL 발급 |
| `POST` | `/calendar/connections/oauth-code` | calendar | 캘린더 OAuth code 교환 |
| `PATCH` | `/calendar/connections/default` | calendar | 기본 캘린더 설정 |
| `DELETE` | `/calendar/connections/{provider}` | calendar | 캘린더 연결 해제 |
| `POST` | `/calls/{id}/calendar-events` | calendar | 통화 기반 일정 생성 |
| `GET` | `/calls/{id}/note` | notes | 메모/사진 목록 |
| `PATCH` | `/calls/{id}/note` | notes | 메모 수정 |
| `POST` | `/calls/{id}/photos/upload-url` | notes | 사진 upload presigned URL |
| `POST` | `/calls/{id}/photos` | notes | 사진 저장 완료 등록 |
| `DELETE` | `/calls/{id}/photos/{photoId}` | notes | 사진 삭제 |
| `POST` | `/clova/webhook` | nlp/handoff | handoff 문서상 CLOVA callback |
| `POST` | `/admin/reload-keywords` | nlp | S3 keyword config reload |

불일치: `lambda/README.md`, web, Android에는 `GET /summaries/{id}`가 있으나 현재 `call_handler.py` 라우팅에는 해당 path 분기가 없다. 현재 코드에서는 `GET /calls/{id}`가 summaries를 join해 반환한다. API Gateway에 `/summaries/{id}`를 유지할 경우 구현 또는 라우팅 보강이 필요하다.

## 6. Environment Variables

값은 새 계정/새 외부 서비스 설정에서 재발급한다. 아래는 이름과 용도만 기록한다.

공통/DB:

| Name | 용도 |
| --- | --- |
| `AWS_REGION` | AWS SDK region. 기본 `ap-northeast-2` |
| `DB_HOST` | RDS MySQL endpoint |
| `DB_NAME` | DB name. 코드/문서 기준 `call_recorder` |
| `DB_USER` | DB user |
| `DB_PASSWORD` | DB password fallback. 새 배포에서는 평문 env 대신 Secrets Manager 권장 |
| `DB_SECRET_ARN` / `DB_SECRET_NAME` | Secrets Manager에서 DB password 조회 |
| `REDIS_HOST` | ElastiCache Redis endpoint |
| `REDIS_PORT` | Redis port |
| `CORS_ALLOWED_ORIGINS` / `CORS_ALLOW_ORIGIN` | 허용 origin |

Auth/Firebase/OAuth:

| Name | 용도 |
| --- | --- |
| `FIREBASE_SERVICE_ACCOUNT_SECRET_ARN` / `FIREBASE_SERVICE_ACCOUNT_SECRET_NAME` | Firebase Admin SDK JSON secret |
| `FIREBASE_SERVICE_ACCOUNT` / `FIREBASE_SERVICE_ACCOUNT_BASE64` | Firebase Admin SDK fallback. secret manager 우선 권장 |
| `KAKAO_REST_API_KEY` / `KAKAO_CLIENT_ID` | Kakao OAuth client id 계열 |
| `KAKAO_CLIENT_SECRET` / `KAKAO_OAUTH_CLIENT_SECRET` | Kakao OAuth secret |
| `KAKAO_LOGIN_SCOPE` | Kakao login scope |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_CLIENT_ID` | Google OAuth client id |
| `GOOGLE_OAUTH_CLIENT_SECRET` / `GOOGLE_CLIENT_SECRET` | Google OAuth secret |
| `GOOGLE_LOGIN_SCOPE` | Google login scope |
| `NAVER_OAUTH_CLIENT_ID` / `NAVER_CLIENT_ID` | Naver OAuth client id |
| `NAVER_OAUTH_CLIENT_SECRET` / `NAVER_CLIENT_SECRET` | Naver OAuth secret |

Call/STT/S3/SMS:

| Name | 용도 |
| --- | --- |
| `S3_BUCKET` | 음성/사진 저장 S3 bucket |
| `CLOVA_SPEECH_INVOKE_URL` / `CLOVA_INVOKE_URL` / `CLOVA_API_URL` | NCP CLOVA Speech invoke URL |
| `CLOVA_SPEECH_SECRET_KEY` / `CLOVA_SECRET_KEY` | NCP CLOVA Speech secret |
| `STT_MAX_RETRY` | STT polling/retry 최대 횟수 |
| `STT_STALE_MINUTES` | stale STT 재조회 기준 |
| `SOLAPI_API_KEY` | SMS API key |
| `SOLAPI_API_SECRET` | SMS API secret |
| `SOLAPI_SENDER` | SMS sender |

NLP:

| Name | 용도 |
| --- | --- |
| `OPENAI_API_KEY` | GPT 요약/분석 API key |
| `KEYWORDS_S3_KEY` | keyword config S3 object key. 기본 `config/keywords.json` |
| `TTL_CUSTOM_KEYWORDS` | custom keyword cache TTL |
| `ADMIN_KEY` | `/admin/reload-keywords` 보호용 key |

Calendar:

| Name | 용도 |
| --- | --- |
| `CALENDAR_TIMEZONE` | 일정 timezone. 기본 `Asia/Seoul` |
| `CALENDAR_DEFAULT_DURATION_MINUTES` | 기본 일정 길이 |
| `CALENDAR_AUTO_MIGRATE` | calendar table 자동 생성 여부 |
| `CALENDAR_TOKEN_KMS_KEY_ID` / `TOKEN_KMS_KEY_ID` | calendar token KMS encrypt/decrypt |
| `GOOGLE_CALENDAR_CLIENT_ID` / `GOOGLE_CALENDAR_CLIENT_SECRET` / `GOOGLE_CALENDAR_SCOPE` | Google Calendar OAuth |
| `NAVER_CALENDAR_CLIENT_ID` / `NAVER_CALENDAR_CLIENT_SECRET` / `NAVER_CALENDAR_SCOPE` | Naver Calendar OAuth |
| `KAKAO_CALENDAR_CLIENT_ID` / `KAKAO_CALENDAR_CLIENT_SECRET` / `KAKAO_CALENDAR_SCOPE` | Kakao Calendar OAuth |

## 7. Database Schema

RDS MySQL:

- Engine: MySQL 8.x. handoff 2 기준 MySQL 8.4.8, schema 파일 주석도 MySQL 8.4.x.
- DB name: `call_recorder`
- Charset: `utf8mb4`

`schema_1.sql` 기준 테이블:

| Table | 주요 용도 | 주요 index/unique |
| --- | --- | --- |
| `users` | 사용자/Firebase/OAuth 기본 정보 | `firebase_uid` unique, `kakao_id` unique |
| `user_social_accounts` | provider별 social account | `uniq_provider_user`, `uniq_user_provider`, `idx_social_user` |
| `stores` | 매장 | `idx_stores_user` |
| `calls` | 통화 메타, S3 key, STT status | `idx_calls_user`, `idx_calls_store`, `idx_calls_caller`, `idx_calls_status` |
| `summaries` | 통화 요약/키워드/추출 정보 | PK `call_id`, `idx_summaries_call` |
| `caller_stats` | 발신번호별 누적 통계 | `uq_user_store_caller`, `idx_user_id`, `idx_caller_number` |
| `custom_keywords` | 매장별 사용자 키워드 | `uq_store_keyword`, `idx_store_enabled`, `idx_user_store` |
| `customer_profiles` | 고객 편집 프로필 | `uq_user_phone`, `idx_user` |
| `customer_analysis` | 고객 AI 분석 결과 | `uq_user_phone_an`, `idx_user_an` |
| `calendar_connections` | 외부 캘린더 연결/token | `uk_calendar_connections_user_provider`, `idx_calendar_connections_user_default` |
| `calendar_event_logs` | 외부 일정 생성 로그 | `uk_calendar_event_logs_call_provider`, `idx_calendar_event_logs_user_call` |
| `call_notes` | 통화별 메모 | `idx_call_notes_user` |
| `call_photos` | 통화별 사진 S3 key | `idx_call_photos_call`, `idx_call_photos_user` |

초기 적용 순서:

1. 새 RDS MySQL 생성 및 DB `call_recorder` 생성.
2. DB user/password 생성. password는 Secrets Manager에 저장한다.
3. `SET NAMES utf8mb4`.
4. `schema_1.sql` 전체 적용.
5. Lambda가 사용하는 DB secret/env 연결.
6. 필요 시 운영에서 `SHOW TABLES`, `SHOW INDEX FROM <table>`로 schema 반영 확인.

주의: `stores`, `calls`, `summaries`는 schema 파일 주석상 코드 쿼리에서 복원한 구조라 기존 운영 DB와 100% 일치 보장이 없다. 기존 데이터 이전은 하지 않지만, 새 서비스가 바로 쓰는 코드 기준 컬럼과 맞는지 smoke test로 검증해야 한다.

## 8. Migration SQL 적용 여부

- `20260526_calendar_connections.sql`: `calendar_connections`, `calendar_event_logs` 생성.
- `20260611_custom_keywords.sql`: `custom_keywords` 생성.

판단:

- 새 DB를 `docs/schema_1.sql`로 생성한다면 두 migration SQL은 이미 schema에 반영되어 있으므로 별도 적용하지 않는다.
- `schema_1.sql`이 아닌 과거 schema를 사용하거나 일부 테이블만 수동 생성한 경우에는 두 migration을 별도 적용해야 한다.
- `calendar_handler.py`에는 `CALENDAR_AUTO_MIGRATE=true`일 때 calendar 관련 DDL을 자동 실행하는 코드가 있으나, handler 내부 DDL은 `calendar_events` 계열과 migration의 `calendar_event_logs` 정의가 완전히 같지 않을 수 있다. 새 배포는 자동 마이그레이션에 의존하지 말고 `schema_1.sql`을 우선 적용한다.

## 9. S3 / Presigned URL

음성 업로드 흐름:

1. Android/Web이 `POST /calls/upload`에 store_id, file metadata, caller metadata를 보낸다.
2. `call_handler.py`가 `recordings/{store_id}/{call_id}/{file_name}` 형식의 S3 key를 만들고 DB `calls` row를 `uploaded` 상태로 생성한다.
3. Lambda가 `put_object` presigned URL을 발급한다. 응답에는 `upload_url`, `upload_headers`, `call_id`, `s3_key`가 포함된다.
4. Client가 S3에 직접 `PUT`한다. Android는 S3 요청에는 Authorization header를 붙이지 않도록 처리한다.
5. Client가 `POST /calls/{id}/process`를 호출한다.
6. `call_handler.py`가 CLOVA Speech에 STT를 요청한다. STT 요청에는 S3 object에 대한 GET presigned URL이 사용된다.
7. `GET /calls/{id}/audio`는 재생용 GET presigned URL을 반환한다.

사진 업로드 흐름:

1. `POST /calls/{id}/photos/upload-url`
2. S3 direct PUT
3. `POST /calls/{id}/photos`로 DB 저장 완료 등록
4. 조회 시 GET presigned URL 반환

S3 설정 필요:

- 새 audio/photo bucket 생성.
- Lambda role에 해당 bucket `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` 권한 부여.
- Android/Web direct PUT을 위한 CORS 설정 필요. 허용 origin은 새 CloudFront domain, 로컬 개발 origin, 필요 시 Android user agent 흐름을 고려한다.
- `KEYWORDS_S3_KEY`를 사용한다면 같은 bucket 또는 설정 bucket에 keyword config object를 배치하고 nlp Lambda에 읽기 권한을 준다.

## 10. Web Deployment Connection

Web은 Next.js static export 방식이다.

- `next.config.mjs`: `output: 'export'`, `images.unoptimized: true`, `trailingSlash: true`
- build: `npm ci`, `npm run build`
- artifact: `out/`
- 배포: S3 static hosting/origin bucket sync + CloudFront invalidation
- README에는 GitHub Actions 자동 배포가 언급되지만 현재 checkout에서 `.github/workflows` 파일은 확인되지 않았다.

필요한 `NEXT_PUBLIC_` 환경변수:

| Name | 용도 |
| --- | --- |
| `NEXT_PUBLIC_API_BASE_URL` | 새 API Gateway stage URL. 예: `https://<api-id>.execute-api.ap-northeast-2.amazonaws.com/prod` |
| `NEXT_PUBLIC_KAKAO_JS_KEY` | Kakao JS SDK 공개 키 |
| `NEXT_PUBLIC_FIREBASE_API_KEY` | Firebase Web API Key |
| `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN` | Firebase Auth domain |
| `NEXT_PUBLIC_FIREBASE_PROJECT_ID` | Firebase project id |
| `NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET` | Firebase storage bucket |
| `NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID` | Firebase messaging sender id |
| `NEXT_PUBLIC_FIREBASE_APP_ID` | Firebase app id |

Web API 연결:

- `lib/api.js`가 `NEXT_PUBLIC_API_BASE_URL`로 axios instance를 생성한다.
- Firebase ID token을 `Authorization: Bearer <token>`으로 자동 부착한다.
- `callApi.uploadToS3`와 `notesApi.uploadPhotoToS3`는 presigned URL에 직접 `PUT`한다.
- `lib/socialOAuth.js`는 `NEXT_PUBLIC_API_BASE_URL` 기준 `/auth/{provider}/authorize`를 호출한다.

## 11. Android Connection

Android API base URL:

- `app/build.gradle.kts`가 root `gradle.properties`의 `API_BASE_URL`을 읽어 `BuildConfig.API_BASE_URL`로 주입한다.
- `gradle.properties.example`에는 `API_BASE_URL=https://YOUR_API_GATEWAY_URL/` 형식이 명시되어 있다.
- `ApiClient.kt`가 `Retrofit.Builder().baseUrl(BuildConfig.API_BASE_URL)`를 사용한다. Retrofit base URL은 trailing slash가 필요하다.

주요 API 호출 위치:

- `ApiService.kt`
  - `POST auth/kakao`, `POST auth/naver`, `POST auth/google`
  - `GET/PATCH me`
  - `POST/GET stores`
  - `POST calls/upload`
  - `PUT {presigned_url}`
  - `POST calls/{id}/process`
  - `GET calls`, `GET calls/{id}`, `PATCH calls/{id}`, `DELETE calls/{id}`
  - `GET calls/{id}/audio`
  - `GET summaries/{id}` (backend route 확인 필요)
  - calendar, notes/photos, keywords, customers endpoints
- `CallRepository.kt`
  - `uploadAndProcess()`에서 `requestUploadUrl` -> S3 `PUT` -> `processCall` 순서로 처리한다.
- `ApiClient.kt`
  - S3 host 또는 presigned URL 요청에는 Authorization header를 붙이지 않는다.
  - 일반 API에는 Firebase ID token을 `Authorization` header로 붙인다.

Android 재배포 시 필요한 값:

- `API_BASE_URL`: 새 API Gateway prod URL, trailing slash 포함.
- `KAKAO_NATIVE_APP_KEY`, `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`: 새 OAuth 앱 설정에서 발급/등록.
- Firebase Android 앱 설정 및 `google-services.json` 확인 필요. 실제 파일 내용은 이 감사에서 읽지 않았다.

## 12. AWS Resources Needed

새 AWS 계정에서 생성할 리소스 순서:

1. IAM 배포 계정/role
   - GitHub Actions 또는 수동 배포용 최소 권한.
   - Lambda execution role.
2. VPC 네트워크
   - 최소 2 AZ subnet 권장.
   - Lambda가 RDS/Redis에 접근할 private subnet.
   - 외부 CLOVA/OpenAI/Solapi 호출을 위한 NAT Gateway 또는 egress 경로.
   - S3 Gateway Endpoint, Secrets Manager Interface Endpoint 권장.
3. Security Groups
   - Lambda SG.
   - RDS SG: Lambda SG에서 3306 허용.
   - Redis SG: Lambda SG에서 6379 허용.
   - Endpoint SG: Lambda SG에서 443 허용.
4. RDS MySQL
   - DB `call_recorder`.
   - 새 password 발급, Secrets Manager 저장.
   - 가능하면 public access disabled.
5. ElastiCache Redis
   - auth/call/nlp cache/rate-limit/lock 용도.
6. S3 buckets
   - audio/photo upload bucket.
   - web static hosting/origin bucket.
   - 필요 시 keyword config object 위치.
7. KMS key
   - Calendar OAuth token encryption.
8. Secrets Manager
   - DB password.
   - Firebase Admin SDK.
   - 외부 API/OAuth secret.
9. Lambda Layer
   - Python 3.12 dependency layer.
10. Lambda functions
   - auth, call, nlp, calendar.
   - 필요 시 notes는 call Lambda 위임 또는 별도 함수로 구성.
11. API Gateway REST API
   - Lambda Proxy integration.
   - CORS 설정.
   - `prod` stage 배포.
12. EventBridge rules
   - pending STT retry.
   - customer analysis batch.
13. CloudWatch Logs/Metrics
   - Lambda log groups.
   - `CallRecorder/Polling` custom metrics 권한.
14. CloudFront distribution
   - Web S3 origin.
   - SPA/static export routing에 맞는 error response 설정.
15. External services
   - NCP CLOVA Speech.
   - OpenAI.
   - Firebase project.
   - Kakao/Google/Naver OAuth apps.
   - Solapi.

## 13. Deployment Order

1. 새 외부 서비스 계정/API 앱을 준비하고 callback/redirect URL 초안을 정한다.
2. AWS VPC, subnet, NAT, endpoints, SG를 만든다.
3. RDS MySQL, Redis를 만든다.
4. Secrets Manager/KMS를 만든다. 실제 secret 값은 문서화하지 않는다.
5. S3 audio/photo bucket과 web bucket을 만든다.
6. `schema_1.sql`을 새 DB에 적용한다.
7. Lambda Layer를 빌드/배포한다.
8. Lambda execution role에 RDS/Secrets/KMS/S3/Lambda invoke/CloudWatch 권한을 부여한다.
9. `auth_handler.py`, `call_handler.py`, `nlp_handler.py`, `calendar_handler.py`를 배포한다.
10. Lambda 환경변수를 이름 기준으로 설정한다.
11. API Gateway REST API route/method를 만들고 Lambda Proxy로 연결한다.
12. API Gateway CORS와 stage `prod`를 배포한다.
13. Web `NEXT_PUBLIC_API_BASE_URL`을 새 API URL로 설정하고 `npm run build`한다.
14. Web `out/`을 S3에 sync하고 CloudFront invalidation을 수행한다.
15. Android `API_BASE_URL`을 새 API URL로 바꾸고 debug/release build를 검증한다.
16. Smoke test를 수행하고 CORS/OAuth redirect/API route 누락을 보정한다.

## 14. Blockers / Missing Information

- 현재 checkout에 backend/web `.github/workflows`가 없어, handoff의 GitHub Actions 자동 배포 절차를 파일로 검증하지 못했다.
- API Gateway 실제 route export가 없다. 코드 기반 route 목록과 handoff route 목록을 새 API Gateway에 수동 반영해야 한다.
- `GET /summaries/{id}`는 client/README에는 있으나 현재 backend routing에서 확인되지 않았다. 유지할지 제거할지 결정 필요.
- `POST /clova/webhook`는 handoff/README에 있으나 현재 `nlp_handler.py`의 공개 route 분기는 제한적으로만 확인됐다. CLOVA 연동 방식이 sync/polling인지 webhook인지 최종 결정 필요.
- 기존 운영 DB schema와 `schema_1.sql`의 `stores`, `calls`, `summaries`가 100% 일치하는지 불명확하다. 데이터 이전은 없지만 코드 동작 검증 필요.
- Lambda 함수별 API Gateway integration mapping이 실제로 분리 함수인지, `call_handler.py` 단일 dispatcher인지 최종 선택 필요.
- 새 계정의 domain/CloudFront URL이 정해지면 OAuth redirect URI, CORS origin, Android redirect 설정을 모두 갱신해야 한다.
- Firebase Android/Web app 설정 파일 및 실제 앱 등록 상태는 secret/private 파일을 읽지 않는 조건 때문에 확인하지 않았다.
- `DB_PASSWORD` 평문 env fallback이 코드에 존재한다. 새 배포에서는 Secrets Manager를 우선 사용하도록 운영 정책 확정 필요.
- RDS SSL이 코드에서 disabled로 보인다. 새 운영에서는 SSL 적용 여부 확인 필요.
- Redis가 필수인지 degraded mode 허용인지 운영 정책 확인 필요. `redis_client.py` 구현에 따라 캐시 실패 시 fallback 가능 범위를 확인해야 한다.

## 15. Smoke Test Plan

1. API Gateway
   - `OPTIONS` preflight 응답 확인.
   - 인증 없는 endpoint가 의도대로 401/404를 반환하는지 확인.
2. Auth
   - Kakao/Google/Naver 중 최소 1개 provider login.
   - Firebase Custom Token 발급 후 client sign-in.
   - `POST /auth/verify`.
3. Stores
   - `POST /stores`.
   - `GET /stores`.
4. Audio upload pipeline
   - `POST /calls/upload`.
   - returned presigned URL로 S3 `PUT`.
   - `POST /calls/{id}/process`.
   - `GET /calls/{id}`에서 status/summary 변화 확인.
   - `GET /calls/{id}/audio`로 재생 URL 확인.
5. NLP
   - OpenAI key 설정 후 summary 생성.
   - custom keyword가 있으면 `/stores/{storeId}/keywords` CRUD와 summary 반영 확인.
6. Calendar
   - `GET /calendar/connections`.
   - OAuth authorize URL 발급.
   - token 저장 시 KMS encrypt/decrypt 정상 확인.
   - `POST /calls/{id}/calendar-events`.
7. Notes/photos
   - `PATCH /calls/{id}/note`.
   - `POST /calls/{id}/photos/upload-url` -> S3 PUT -> `POST /calls/{id}/photos`.
8. Web
   - CloudFront URL 접속.
   - login redirect, dashboard load, manual upload, audio playback.
9. Android
   - `API_BASE_URL` trailing slash 포함 확인.
   - login, recording upload, process trigger, list/detail reload.
10. Operations
   - CloudWatch logs for all Lambdas.
   - EventBridge retry invocation.
   - RDS/Redis SG access.
   - S3 CORS and CloudFront invalidation.
