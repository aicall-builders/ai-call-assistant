# Environment Variables

이 문서는 새 AWS 계정 재배포 시 필요한 환경변수와 설정 이름만 정리한다. 실제 secret/key/password/token 값은 넣지 않는다.

## Backend Lambda env

| Name | Required | Secret 여부 | 용도 | 예시 형식 | 적용 Lambda |
| --- | --- | --- | --- | --- | --- |
| `AWS_REGION` | Yes | No | AWS SDK region | `ap-northeast-2` | all |
| `DB_HOST` | Yes | No | RDS endpoint | `<rds-endpoint>.ap-northeast-2.rds.amazonaws.com` | auth, call, nlp, calendar |
| `DB_NAME` | Yes | No | DB 이름 | `call_recorder` | auth, call, nlp, calendar |
| `DB_USER` | Yes | No | DB user | `admin` | auth, call, nlp, calendar |
| `DB_SECRET_NAME` | Yes | No | DB password secret 이름 | `ai-call/prod/db` | auth, call, nlp |
| `DB_SECRET_ARN` | Optional | No | DB password secret ARN | `arn:aws:secretsmanager:ap-northeast-2:<account-id>:secret:<name>` | auth, call, nlp |
| `DB_PASSWORD` | No | Yes | DB password fallback. 새 배포에서는 비권장 | `<do-not-print>` | auth, call, nlp, calendar |
| `REDIS_HOST` | Yes | No | Redis endpoint | `<redis-endpoint>` | auth, call, nlp |
| `REDIS_PORT` | Yes | No | Redis port | `6379` | auth, call, nlp |
| `CORS_ALLOWED_ORIGINS` | Yes | No | 허용 Web origin | `https://<cloudfront-domain>,http://localhost:3000` | auth, call, calendar |
| `CORS_ALLOW_ORIGIN` | Optional | No | 단일 CORS origin fallback | `https://<cloudfront-domain>` | auth, call, calendar |
| `S3_BUCKET` | Yes | No | 음성/사진 S3 bucket | `<audio-bucket-name>` | call, nlp, notes |
| `KEYWORDS_S3_KEY` | Optional | No | keyword config object key | `config/keywords.json` | nlp |
| `CLOVA_SPEECH_INVOKE_URL` | Later | No | CLOVA Speech invoke URL | `https://<clova-endpoint>` | call |
| `CLOVA_INVOKE_URL` | Later | No | CLOVA invoke URL fallback | `https://<clova-endpoint>` | call |
| `CLOVA_API_URL` | Later | No | CLOVA invoke URL fallback | `https://<clova-endpoint>` | call |
| `CLOVA_SPEECH_SECRET_KEY` | Later | Yes | CLOVA Speech secret | `<stored-in-secret-manager-or-env>` | call |
| `CLOVA_SECRET_KEY` | Later | Yes | CLOVA secret fallback | `<stored-in-secret-manager-or-env>` | call |
| `STT_MAX_RETRY` | Optional | No | STT retry 횟수 | `3` | call |
| `STT_STALE_MINUTES` | Optional | No | stale STT 재시도 기준 | `5` | call |
| `OPENAI_API_KEY` | Later | Yes | GPT 요약/분석 key | `<stored-in-secret-manager-or-env>` | nlp |
| `TTL_CUSTOM_KEYWORDS` | Optional | No | custom keyword cache TTL | `300` | nlp |
| `ADMIN_KEY` | Optional | Yes | `/admin/reload-keywords` 보호 key | `<stored-in-secret-manager-or-env>` | nlp |
| `SOLAPI_API_KEY` | Later | Yes | SMS API key | `<stored-in-secret-manager-or-env>` | call, nlp |
| `SOLAPI_API_SECRET` | Later | Yes | SMS API secret | `<stored-in-secret-manager-or-env>` | call, nlp |
| `SOLAPI_SENDER` | Later | No | SMS sender 번호/식별자 | `<sender-id-or-phone>` | call, nlp |
| `FIREBASE_SERVICE_ACCOUNT_SECRET_NAME` | Later | No | Firebase Admin SDK secret 이름 | `ai-call/prod/firebase-admin-sdk` | auth, call |
| `FIREBASE_SERVICE_ACCOUNT_SECRET_ARN` | Optional | No | Firebase Admin SDK secret ARN | `arn:aws:secretsmanager:...` | auth, call |
| `FIREBASE_SERVICE_ACCOUNT` | No | Yes | Firebase Admin SDK JSON fallback. 비권장 | `<do-not-print>` | auth |
| `FIREBASE_SERVICE_ACCOUNT_BASE64` | No | Yes | Firebase Admin SDK base64 fallback. 비권장 | `<do-not-print>` | auth |
| `KAKAO_REST_API_KEY` | Later | Yes | Kakao OAuth client id 계열 | `<issued-later>` | auth, calendar |
| `KAKAO_CLIENT_ID` | Later | No | Kakao client id fallback | `<issued-later>` | auth, calendar |
| `KAKAO_CLIENT_SECRET` | Later | Yes | Kakao client secret | `<stored-in-secret-manager-or-env>` | auth, calendar |
| `KAKAO_OAUTH_CLIENT_SECRET` | Later | Yes | Kakao OAuth secret fallback | `<stored-in-secret-manager-or-env>` | auth, calendar |
| `KAKAO_LOGIN_SCOPE` | Optional | No | Kakao login scope | `profile_nickname` | auth |
| `GOOGLE_OAUTH_CLIENT_ID` | Later | No | Google OAuth client id | `<issued-later>` | auth |
| `GOOGLE_CLIENT_ID` | Later | No | Google client id fallback | `<issued-later>` | auth, calendar |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Later | Yes | Google OAuth secret | `<stored-in-secret-manager-or-env>` | auth |
| `GOOGLE_CLIENT_SECRET` | Later | Yes | Google secret fallback | `<stored-in-secret-manager-or-env>` | auth, calendar |
| `GOOGLE_LOGIN_SCOPE` | Optional | No | Google login scope | `openid email profile` | auth |
| `NAVER_OAUTH_CLIENT_ID` | Later | No | Naver OAuth client id | `<issued-later>` | auth |
| `NAVER_CLIENT_ID` | Later | No | Naver client id fallback | `<issued-later>` | auth, calendar |
| `NAVER_OAUTH_CLIENT_SECRET` | Later | Yes | Naver OAuth secret | `<stored-in-secret-manager-or-env>` | auth |
| `NAVER_CLIENT_SECRET` | Later | Yes | Naver secret fallback | `<stored-in-secret-manager-or-env>` | auth, calendar |
| `CALENDAR_TIMEZONE` | Yes | No | Calendar timezone | `Asia/Seoul` | calendar |
| `CALENDAR_DEFAULT_DURATION_MINUTES` | Optional | No | 기본 일정 길이 | `60` | calendar |
| `CALENDAR_AUTO_MIGRATE` | Optional | No | calendar table 자동 DDL 여부. 새 배포는 `false` 권장 | `false` | calendar |
| `CALENDAR_TOKEN_KMS_KEY_ID` | Later | No | Calendar token KMS key id/alias | `alias/ai-call-calendar-token` | calendar |
| `TOKEN_KMS_KEY_ID` | Optional | No | KMS key fallback | `alias/ai-call-calendar-token` | calendar |
| `GOOGLE_CALENDAR_CLIENT_ID` | Later | No | Google Calendar OAuth client id | `<issued-later>` | calendar |
| `GOOGLE_CALENDAR_CLIENT_SECRET` | Later | Yes | Google Calendar OAuth secret | `<stored-in-secret-manager-or-env>` | calendar |
| `GOOGLE_CALENDAR_SCOPE` | Optional | No | Google Calendar scope | `https://www.googleapis.com/auth/calendar.events` | calendar |
| `NAVER_CALENDAR_CLIENT_ID` | Later | No | Naver Calendar OAuth client id | `<issued-later>` | calendar |
| `NAVER_CALENDAR_CLIENT_SECRET` | Later | Yes | Naver Calendar OAuth secret | `<stored-in-secret-manager-or-env>` | calendar |
| `NAVER_CALENDAR_SCOPE` | Optional | No | Naver Calendar scope | `calendar` | calendar |
| `KAKAO_CALENDAR_CLIENT_ID` | Later | No | Kakao Calendar client id | `<issued-later>` | calendar |
| `KAKAO_CALENDAR_CLIENT_SECRET` | Later | Yes | Kakao Calendar secret | `<stored-in-secret-manager-or-env>` | calendar |
| `KAKAO_CALENDAR_SCOPE` | Optional | No | Kakao Calendar scope | `talk_calendar` | calendar |

`Required` 값 의미:

- `Yes`: AWS 리소스 배포 직후 필요.
- `Later`: 외부 서비스 새 발급 후 연결.
- `Optional`: 기본값 또는 특정 기능에서만 필요.

## Web env

| Name | Required | 용도 | 예시 형식 |
| --- | --- | --- | --- |
| `NEXT_PUBLIC_API_BASE_URL` | Yes | API Gateway prod URL | `https://<api-id>.execute-api.ap-northeast-2.amazonaws.com/prod` |
| `NEXT_PUBLIC_KAKAO_JS_KEY` | Later | Kakao JS SDK public key | `<issued-later-public-key>` |
| `NEXT_PUBLIC_FIREBASE_API_KEY` | Later | Firebase Web API public key | `<issued-later-public-key>` |
| `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN` | Later | Firebase Auth domain | `<project-id>.firebaseapp.com` |
| `NEXT_PUBLIC_FIREBASE_PROJECT_ID` | Later | Firebase project id | `<firebase-project-id>` |
| `NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET` | Later | Firebase storage bucket | `<project-id>.appspot.com` |
| `NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID` | Later | FCM sender id | `<numeric-sender-id>` |
| `NEXT_PUBLIC_FIREBASE_APP_ID` | Later | Firebase web app id | `<firebase-app-id>` |

주의:

- `NEXT_PUBLIC_` 값은 client bundle에 포함된다.
- Firebase/Kakao public config만 넣는다.
- OpenAI, CLOVA, Solapi, Firebase Admin SDK, OAuth client secret은 절대 Web env에 넣지 않는다.

## Android config

| Name | 위치 | Required | 용도 | 예시 형식 |
| --- | --- | --- | --- | --- |
| `API_BASE_URL` | `call-recorder-android/gradle.properties` | Yes | API Gateway prod URL | `https://<api-id>.execute-api.ap-northeast-2.amazonaws.com/prod/` |
| `KAKAO_NATIVE_APP_KEY` | `call-recorder-android/gradle.properties` | Later | Kakao Android SDK native app key | `<issued-later>` |
| `NAVER_CLIENT_ID` | `call-recorder-android/gradle.properties` | Later | Naver OAuth client id | `<issued-later>` |
| `NAVER_CLIENT_SECRET` | `call-recorder-android/gradle.properties` | Later | Naver OAuth client secret | `<stored-outside-repo>` |
| Firebase Android config | `call-recorder-android/app/google-services.json` | Later | Firebase Android app 연결 | `<download-from-firebase-console>` |

주의:

- Retrofit base URL은 trailing slash가 필요하다.
- 실제 `gradle.properties`와 `google-services.json`은 secret 또는 private config로 취급할 수 있다. repo 정책에 맞게 관리한다.

## GitHub Actions secrets / variables

현재 checkout에는 workflow 파일이 확인되지 않았다. 자동 배포를 만들 경우 아래 이름을 기준으로 설계한다.

| Name | Secret/Variable | 용도 | 비고 |
| --- | --- | --- | --- |
| `AWS_ROLE_ARN` | Secret | GitHub OIDC로 assume할 deploy role | 권장 방식 |
| `AWS_ACCESS_KEY_ID` | Secret | access key 방식 배포 시 사용 | OIDC 사용 시 불필요 |
| `AWS_SECRET_ACCESS_KEY` | Secret | access key 방식 배포 시 사용 | OIDC 사용 시 불필요 |
| `AWS_REGION` | Variable | 배포 region | `ap-northeast-2` |
| `LAMBDA_AUTH_FUNCTION` | Variable | auth Lambda 이름 | `call-recorder-api-auth` |
| `LAMBDA_CALL_FUNCTION` | Variable | call Lambda 이름 | `call-recorder-api-call` |
| `LAMBDA_NLP_FUNCTION` | Variable | nlp Lambda 이름 | `call-recorder-api-nlp` |
| `LAMBDA_CALENDAR_FUNCTION` | Variable | calendar Lambda 이름 | `call-recorder-api-calendar` |
| `LAMBDA_LAYER_NAME` | Variable | dependency layer 이름 | `ai-call-python-dependencies` |
| `WEB_S3_BUCKET` | Variable | Web static bucket | `<web-bucket-name>` |
| `CLOUDFRONT_DISTRIBUTION_ID` | Variable | Web CloudFront distribution | `<distribution-id>` |
| `NEXT_PUBLIC_API_BASE_URL` | Variable | Web build-time API URL | public 값 |
| `NEXT_PUBLIC_KAKAO_JS_KEY` | Variable | Web build-time Kakao public key | public 값 |
| `NEXT_PUBLIC_FIREBASE_API_KEY` | Variable | Web build-time Firebase public key | public 값 |
| `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN` | Variable | Web build-time Firebase auth domain | public 값 |
| `NEXT_PUBLIC_FIREBASE_PROJECT_ID` | Variable | Web build-time Firebase project id | public 값 |
| `NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET` | Variable | Web build-time Firebase storage bucket | public 값 |
| `NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID` | Variable | Web build-time Firebase sender id | public 값 |
| `NEXT_PUBLIC_FIREBASE_APP_ID` | Variable | Web build-time Firebase app id | public 값 |

GitHub Actions에 넣지 않는 값:

- DB password.
- OpenAI API key.
- CLOVA secret.
- Firebase Admin SDK JSON.
- OAuth client secret.
- Solapi secret.

위 값들은 Secrets Manager에 저장하고 Lambda execution role이 읽도록 구성한다.
