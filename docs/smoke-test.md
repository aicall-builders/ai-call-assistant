# Smoke Test

이 문서는 새 AWS 계정 배포 후 최소 동작을 검증하는 절차다. 실제 secret/key/password/token 값은 출력하지 않는다.

## 0. 사전 조건

준비할 값:

```bash
export REGION=ap-northeast-2
export API_BASE_URL=https://<api-id>.execute-api.ap-northeast-2.amazonaws.com/prod
export WEB_ORIGIN=https://<cloudfront-domain>
export AUDIO_BUCKET=<audio-bucket-name>
```

로그인 이후 필요한 값:

```bash
export FIREBASE_ID_TOKEN=<do-not-print>
export STORE_ID=<created-store-id>
export CALL_ID=<created-call-id>
```

주의:

- token 값을 화면 공유, 문서, commit에 남기지 않는다.
- S3 presigned URL도 제한 시간 동안 권한이 있으므로 공개하지 않는다.
- 실패 시 CloudWatch Logs에서 request id 기준으로 확인한다.

## 1. API Gateway 기본 응답 확인

목표: API Gateway stage가 살아 있고 Lambda까지 연결되는지 확인한다.

```bash
curl -i "$API_BASE_URL/stores"
```

예상:

- 인증 전이면 `401` 또는 인증 오류 JSON.
- `403 Missing Authentication Token`이면 route/stage/path가 틀렸을 수 있다.
- `502`이면 Lambda error 또는 integration 설정 문제일 수 있다.

## 2. CORS preflight 확인

```bash
curl -i -X OPTIONS "$API_BASE_URL/stores" \
  -H "Origin: $WEB_ORIGIN" \
  -H "Access-Control-Request-Method: GET" \
  -H "Access-Control-Request-Headers: authorization,content-type"
```

확인:

- `HTTP 200` 또는 `204`.
- `Access-Control-Allow-Origin`에 Web origin이 허용되는지.
- `Access-Control-Allow-Methods`에 `GET,POST,PATCH,DELETE,OPTIONS` 계열이 포함되는지.
- `Access-Control-Allow-Headers`에 `authorization`, `content-type`이 포함되는지.

## 3. Auth 테스트

외부 서비스 발급 전에는 전체 OAuth 테스트가 보류될 수 있다.

가능한 확인:

```bash
curl -i -X POST "$API_BASE_URL/auth/verify" \
  -H "Content-Type: application/json"
```

예상:

- token 없이 호출하면 `401`.

OAuth 연결 후:

1. Web `/login`에서 provider login.
2. Firebase sign-in 완료 확인.
3. browser devtools에서 API 요청에 `Authorization: Bearer ...`가 붙는지 확인하되 token 값은 복사하지 않는다.
4. 아래 verify 호출:

```bash
curl -i -X POST "$API_BASE_URL/auth/verify" \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "Content-Type: application/json"
```

예상:

- `200`.
- user id, firebase uid 등 비밀이 아닌 사용자 정보.

## 4. Store 생성/조회 테스트

생성:

```bash
curl -i -X POST "$API_BASE_URL/stores" \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Smoke Test Store","industry":"food"}'
```

응답에서 store id를 `STORE_ID`로 둔다.

조회:

```bash
curl -i "$API_BASE_URL/stores" \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN"
```

예상:

- 생성한 store가 목록에 포함된다.

## 5. Upload URL 발급 테스트

```bash
curl -i -X POST "$API_BASE_URL/calls/upload" \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"store_id\":\"$STORE_ID\",
    \"file_name\":\"smoke-test.m4a\",
    \"file_format\":\"m4a\",
    \"mime_type\":\"audio/mp4\",
    \"caller_number\":\"01000000000\",
    \"duration\":3
  }"
```

확인:

- `upload_url`이 반환된다.
- `upload_headers`에 `Content-Type`이 있다.
- `call_id`가 반환된다.
- `s3_key`가 `recordings/<store-id>/<call-id>/...` 형태인지 확인한다.

주의:

- `upload_url`은 공개하지 않는다.

## 6. S3 PUT 테스트

테스트용 작은 오디오 파일을 준비한다.

```bash
curl -i -X PUT "<upload_url>" \
  -H "Content-Type: audio/mp4" \
  --data-binary @smoke-test.m4a
```

예상:

- `200 OK`.
- `SignatureDoesNotMatch`이면 Content-Type, header, URL 복사 과정 문제를 확인한다.
- CORS 문제는 browser에서만 드러날 수 있으므로 Web 업로드도 별도로 확인한다.

S3 object 확인:

```bash
aws s3api head-object \
  --bucket "$AUDIO_BUCKET" \
  --key "<s3_key>" \
  --region "$REGION"
```

## 7. Process Call 테스트

```bash
curl -i -X POST "$API_BASE_URL/calls/$CALL_ID/process" \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

예상:

- `200`.
- `message`가 STT 처리 시작 계열.
- CLOVA/OpenAI를 아직 연결하지 않았다면 이 단계는 외부 서비스 설정 전까지 실패할 수 있다.

확인 위치:

- CloudWatch Logs `/aws/lambda/call-recorder-api-call`.
- CLOVA URL/secret 설정 여부.
- Lambda가 NAT를 통해 외부로 나갈 수 있는지.

## 8. Calls 목록/상세 테스트

목록:

```bash
curl -i "$API_BASE_URL/calls?store_id=$STORE_ID&limit=20" \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN"
```

상세:

```bash
curl -i "$API_BASE_URL/calls/$CALL_ID" \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN"
```

예상:

- 목록에 call이 보인다.
- 상세에 `status`, `s3_key`, summary join field가 포함될 수 있다.

보류:

- `GET /summaries/{id}`는 현재 backend routing 불일치가 있어 smoke test 대상에서 제외한다. 필요 시 먼저 route/handler를 확정한다.

## 9. Audio URL 테스트

```bash
curl -i "$API_BASE_URL/calls/$CALL_ID/audio" \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN"
```

예상:

- presigned GET URL 반환.
- 반환 URL을 browser 또는 curl로 열면 제한 시간 동안 오디오 접근 가능.

```bash
curl -I "<audio_presigned_url>"
```

## 10. Web 화면 테스트

확인 항목:

1. CloudFront URL 접속.
2. `/login` 표시.
3. OAuth provider 버튼 동작.
4. 로그인 후 `/dashboard` 이동.
5. store 목록 조회.
6. 수동 파일 업로드:
   - `POST /calls/upload`.
   - S3 `PUT`.
   - `POST /calls/{id}/process`.
7. 통화 상세 화면에서 오디오 재생.

Browser DevTools 확인:

- API 요청 base URL이 새 API Gateway인지.
- CORS error가 없는지.
- 401 발생 시 Firebase token이 붙었는지.
- presigned S3 PUT에는 Authorization header가 붙지 않는지.

## 11. Android 앱 테스트

확인 전:

- `gradle.properties`의 `API_BASE_URL`이 새 API URL이고 trailing slash가 있는지 확인.

```properties
API_BASE_URL=https://<api-id>.execute-api.ap-northeast-2.amazonaws.com/prod/
```

빌드:

```powershell
cd call-recorder-android
.\gradlew.bat assembleDebug
```

테스트:

1. 앱 실행.
2. 로그인.
3. store 선택 또는 생성.
4. 짧은 테스트 오디오 업로드.
5. 업로드 상태가 `uploaded` -> `processing`으로 가는지 확인.
6. 목록/상세 재조회.
7. 오디오 재생 URL 조회.

확인 위치:

- Android Logcat.
- CloudWatch Logs.
- S3 object.

## 12. Calendar 테스트

외부 OAuth 발급 후 진행한다.

연결 목록:

```bash
curl -i "$API_BASE_URL/calendar/connections" \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN"
```

authorize URL:

```bash
curl -i "$API_BASE_URL/calendar/connections/google/authorize?redirect_uri=https%3A%2F%2F<cloudfront-domain>%2Foauth%2Fgoogle%2F&state=smoke" \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN"
```

확인:

- OAuth URL이 반환된다.
- token 저장 후 KMS encrypt/decrypt 오류가 없는지 CloudWatch에서 확인.

일정 생성:

```bash
curl -i -X POST "$API_BASE_URL/calls/$CALL_ID/calendar-events" \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"provider":"google"}'
```

## 13. Notes / Photos 테스트

메모 저장:

```bash
curl -i -X PATCH "$API_BASE_URL/calls/$CALL_ID/note" \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"memo":"smoke test memo"}'
```

메모 조회:

```bash
curl -i "$API_BASE_URL/calls/$CALL_ID/note" \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN"
```

사진 upload URL:

```bash
curl -i -X POST "$API_BASE_URL/calls/$CALL_ID/photos/upload-url" \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"file_name":"smoke-photo.jpg"}'
```

S3 PUT 후 저장 완료:

```bash
curl -i -X POST "$API_BASE_URL/calls/$CALL_ID/photos" \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"photo_id":"<photo-id>","s3_key":"<photo-s3-key>"}'
```

## 14. CloudWatch 로그 확인

Log groups:

```bash
aws logs describe-log-groups \
  --log-group-name-prefix /aws/lambda/call-recorder-api \
  --region ap-northeast-2
```

최근 로그 stream:

```bash
aws logs describe-log-streams \
  --log-group-name /aws/lambda/call-recorder-api-call \
  --order-by LastEventTime \
  --descending \
  --max-items 5 \
  --region ap-northeast-2
```

확인:

- DB connection error.
- Secrets Manager permission error.
- KMS access denied.
- S3 access denied.
- timeout.
- ImportError. Layer 누락 가능.
- 외부 API timeout. NAT/SG/route 확인 필요.

로그에 secret 값이 찍히면 즉시 마스킹/로그 정책을 수정하고 값을 rotate한다.

## 15. 자주 나는 오류와 확인 위치

| 증상 | 가능 원인 | 확인 위치 |
| --- | --- | --- |
| `403 Missing Authentication Token` | API Gateway path/stage/method 누락 | API Gateway Resources, deployment stage |
| `502 Bad Gateway` | Lambda exception, handler 이름 오류, response format 오류 | CloudWatch Logs |
| `Task timed out` | VPC NAT 없음, 외부 API 지연, RDS 연결 지연 | Lambda VPC, route table, NAT, logs |
| `AccessDeniedException` Secrets Manager | Lambda role 권한 부족 | IAM policy, secret resource policy |
| `AccessDeniedException` KMS | KMS key policy 또는 Lambda role 권한 부족 | KMS key policy |
| `No module named ...` | Layer 누락 또는 zip 패키징 누락 | Lambda Layers, deployment zip |
| `pymysql` connection error | RDS SG, subnet, DB endpoint/env 오류 | RDS SG, Lambda SG, env |
| Redis connection error | Redis SG/subnet/env 오류 | ElastiCache endpoint, SG |
| S3 `SignatureDoesNotMatch` | PUT header가 presign 때와 다름 | upload headers, Content-Type |
| Browser CORS error | API Gateway/Lambda/S3 CORS origin 누락 | API Gateway CORS, S3 CORS |
| Web API가 예전 계정 호출 | `NEXT_PUBLIC_API_BASE_URL` build 값이 이전 값 | Web build env, deployed JS |
| Android API가 예전 계정 호출 | `API_BASE_URL` 미교체 또는 trailing slash 누락 | `BuildConfig.API_BASE_URL`, Logcat |
| `/summaries/{id}` 404 | 현재 backend routing 불일치 | 보류 항목. `/calls/{id}` 사용 여부 결정 |
| `/clova/webhook` 404 | 현재 구현 방식 미확정 | CLOVA integration 설계 확인 |
