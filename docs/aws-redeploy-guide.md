# AWS Redeploy Guide

## 0. 전제와 최종 구조

이 문서는 새 AWS 계정에 AI 통화비서 서비스를 재배포하기 위한 실무 절차다.

전제:

1. 기존 팀원 개인 AWS 계정은 사용하지 않는다.
2. 기존 운영 데이터는 이전하지 않는다.
3. DB는 `ai-call-assistant/docs/schema_1.sql` 기준으로 새로 만든다.
4. Region은 `ap-northeast-2`를 기준으로 한다.
5. 실제 secret/key/password/token 값은 문서나 로그에 출력하지 않는다.
6. Firebase, Kakao, Naver, Google, OpenAI, CLOVA, Solapi 값은 나중에 새로 발급해 연결한다.

최종 구조:

```text
Android / Web
  -> API Gateway REST API /prod
    -> Lambda auth/call/nlp/calendar
      -> RDS MySQL call_recorder
      -> ElastiCache Redis
      -> S3 audio/photo bucket
      -> Secrets Manager
      -> KMS
      -> External services later: Firebase, OAuth, OpenAI, CLOVA, Solapi

Web static files
  -> S3 web bucket
  -> CloudFront
```

## 1. 새 AWS 계정 기본 설정

콘솔:

1. AWS Console 로그인.
2. 우측 상단 region을 `Asia Pacific (Seoul) ap-northeast-2`로 변경.
3. Billing 알림, MFA, root account 보호를 먼저 설정.
4. IAM Identity Center 또는 관리자 IAM user를 만든다.

CLI:

```bash
aws configure
aws sts get-caller-identity
aws configure set region ap-northeast-2
```

확인할 것:

- `Account`가 새 AWS 계정인지 확인한다.
- CLI profile을 기존 개인 계정과 혼동하지 않도록 profile 이름을 분리한다.

## 2. IAM Role / 권한 설계

콘솔:

1. IAM -> Roles -> Create role.
2. Trusted entity type: AWS service.
3. Use case: Lambda.
4. Role name 예시: `ai-call-lambda-execution-role`.
5. 최소 정책부터 붙이고, 배포 중 필요한 권한을 추가한다.

Lambda execution role 권한 범위:

- CloudWatch Logs 작성.
- S3 audio/photo bucket read/write/delete.
- Secrets Manager read.
- KMS decrypt/encrypt.
- Lambda invoke. `call_handler.py`가 `call-recorder-api-nlp`를 호출한다.
- CloudWatch metric put. polling metric 사용.
- VPC ENI 생성 권한. Lambda를 VPC에 붙이는 경우 필요.

CLI 예시:

```bash
aws iam create-role \
  --role-name ai-call-lambda-execution-role \
  --assume-role-policy-document file://trust-lambda.json
```

GitHub Actions 또는 수동 배포용 IAM은 별도 role/user를 만든다. 아직 workflow가 repo에 확인되지 않았으므로 처음에는 수동 배포 권한으로 시작해도 된다.

## 3. VPC / Subnet / NAT / Endpoint 구성

권장 구조:

- VPC 1개.
- Public subnet 2개.
- Private subnet 2개.
- NAT Gateway 1개 이상. Lambda가 private subnet에서 CLOVA/OpenAI/Solapi 등 외부 API를 호출해야 한다.
- S3 Gateway Endpoint.
- Secrets Manager Interface Endpoint.

콘솔:

1. VPC -> Your VPCs -> Create VPC.
2. `VPC and more`를 선택하면 subnet, route table, NAT까지 한 번에 만들 수 있다.
3. Name 예시: `ai-call-vpc`.
4. AZ는 최소 2개를 선택.
5. NAT Gateway는 비용을 고려해 1개로 시작할 수 있다.

CLI 예시:

```bash
aws ec2 create-vpc --cidr-block 10.20.0.0/16 --region ap-northeast-2
aws ec2 describe-vpcs --region ap-northeast-2
```

Endpoint:

```bash
aws ec2 create-vpc-endpoint \
  --vpc-id <vpc-id> \
  --service-name com.amazonaws.ap-northeast-2.s3 \
  --route-table-ids <private-route-table-id> \
  --vpc-endpoint-type Gateway \
  --region ap-northeast-2
```

Secrets Manager endpoint는 Interface type으로 만든다.

## 4. Security Group 구성

필요한 SG:

| SG | Inbound | Outbound |
| --- | --- | --- |
| `ai-call-lambda-sg` | 없음 | HTTPS 443, MySQL 3306, Redis 6379 |
| `ai-call-rds-sg` | Lambda SG에서 TCP 3306 | 기본 허용 |
| `ai-call-redis-sg` | Lambda SG에서 TCP 6379 | 기본 허용 |
| `ai-call-endpoint-sg` | Lambda SG에서 TCP 443 | 기본 허용 |

콘솔:

1. EC2 -> Security Groups -> Create security group.
2. VPC를 새 VPC로 선택.
3. RDS SG inbound에 source를 CIDR이 아니라 Lambda SG로 지정.

CLI 확인:

```bash
aws ec2 describe-security-groups \
  --filters Name=vpc-id,Values=<vpc-id> \
  --region ap-northeast-2
```

## 5. RDS MySQL 생성

콘솔:

1. RDS -> Databases -> Create database.
2. Engine: MySQL.
3. Version: MySQL 8.x.
4. Template: Dev/Test 또는 Production은 예산과 운영 수준에 맞게 선택.
5. DB instance identifier 예시: `ai-call-recorder-db`.
6. Master username 예시: `admin`.
7. Password는 콘솔에서 생성하고 즉시 Secrets Manager에 저장한다. 문서에 적지 않는다.
8. Initial database name: `call_recorder`.
9. VPC: 새 VPC.
10. Public access: No 권장.
11. VPC security group: `ai-call-rds-sg`.

CLI 예시:

```bash
aws rds create-db-instance \
  --db-instance-identifier ai-call-recorder-db \
  --db-instance-class db.t4g.micro \
  --engine mysql \
  --engine-version <mysql-8-version> \
  --allocated-storage 20 \
  --master-username admin \
  --manage-master-user-password \
  --db-name call_recorder \
  --vpc-security-group-ids <rds-sg-id> \
  --db-subnet-group-name <db-subnet-group-name> \
  --no-publicly-accessible \
  --region ap-northeast-2
```

## 6. schema_1.sql 적용

적용 파일:

```text
ai-call-assistant/docs/schema_1.sql
```

방법 A: bastion/CloudShell에서 RDS 접근이 가능한 경우.

```bash
mysql -h <rds-endpoint> -u admin -p call_recorder < ai-call-assistant/docs/schema_1.sql
```

방법 B: RDS가 private이고 로컬에서 접근할 수 없는 경우.

1. 같은 VPC 안의 임시 EC2 또는 VPN/SSM tunnel을 준비한다.
2. `schema_1.sql`을 임시 위치에 전달한다.
3. `mysql` client로 적용한다.
4. 적용 후 임시 접근 경로는 운영 정책에 따라 정리한다.

검증:

```sql
SHOW TABLES;
SHOW INDEX FROM users;
SHOW INDEX FROM calls;
SHOW INDEX FROM calendar_connections;
SHOW INDEX FROM custom_keywords;
```

주의:

- `20260526_calendar_connections.sql`, `20260611_custom_keywords.sql` 내용은 `schema_1.sql`에 이미 포함되어 있다.
- 새 DB를 `schema_1.sql`로 만들면 migration SQL 2개는 별도 적용하지 않는다.

## 7. ElastiCache Redis 생성

Redis는 생성 대상으로 둔다. 다만 코드상 Redis 장애 시 fallback 범위는 추가 확인이 필요하므로 필수/선택 여부는 보류다.

콘솔:

1. ElastiCache -> Redis caches -> Create Redis cache.
2. Deployment option은 처음에는 단일 노드로 시작 가능.
3. VPC: 새 VPC.
4. Subnet group: private subnet.
5. Security group: `ai-call-redis-sg`.

CLI 예시:

```bash
aws elasticache create-cache-subnet-group \
  --cache-subnet-group-name ai-call-redis-subnet-group \
  --cache-subnet-group-description "AI Call Redis subnet group" \
  --subnet-ids <private-subnet-a> <private-subnet-b> \
  --region ap-northeast-2
```

```bash
aws elasticache create-cache-cluster \
  --cache-cluster-id ai-call-redis \
  --engine redis \
  --cache-node-type cache.t4g.micro \
  --num-cache-nodes 1 \
  --cache-subnet-group-name ai-call-redis-subnet-group \
  --security-group-ids <redis-sg-id> \
  --region ap-northeast-2
```

Lambda env에는 `REDIS_HOST`, `REDIS_PORT`를 넣는다.

## 8. S3 오디오/사진 버킷 생성

버킷 용도:

- 음성 파일 업로드.
- 통화 사진 업로드.
- 필요 시 keyword config object.

버킷 이름 예시:

```text
ai-call-audio-<account-or-env>
```

콘솔:

1. S3 -> Buckets -> Create bucket.
2. Region: `ap-northeast-2`.
3. Block all public access: On.
4. Versioning은 운영 정책에 따라 선택.
5. CORS는 API/Web/Android 테스트 단계에서 설정한다.

CLI:

```bash
aws s3api create-bucket \
  --bucket <audio-bucket-name> \
  --region ap-northeast-2 \
  --create-bucket-configuration LocationConstraint=ap-northeast-2
```

CORS 예시:

```json
[
  {
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["PUT", "GET", "HEAD"],
    "AllowedOrigins": [
      "https://<cloudfront-domain>",
      "http://localhost:3000"
    ],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }
]
```

## 9. S3 웹 버킷 생성

버킷 용도:

- `ai-call-assistant-web`의 `out/` 정적 파일 저장.
- CloudFront origin.

콘솔:

1. S3 -> Buckets -> Create bucket.
2. 이름 예시: `ai-call-web-<account-or-env>`.
3. Public access block은 On 유지.
4. CloudFront OAC를 사용할 예정이면 bucket public hosting은 켜지 않는다.

CLI:

```bash
aws s3api create-bucket \
  --bucket <web-bucket-name> \
  --region ap-northeast-2 \
  --create-bucket-configuration LocationConstraint=ap-northeast-2
```

## 10. KMS Key 생성

용도:

- Calendar OAuth token encrypt/decrypt.
- Secrets Manager는 자체 KMS를 써도 되지만, 운영상 분리 key를 둘 수 있다.

콘솔:

1. KMS -> Customer managed keys -> Create key.
2. Key type: Symmetric.
3. Usage: Encrypt and decrypt.
4. Alias 예시: `alias/ai-call-calendar-token`.
5. Lambda execution role에 encrypt/decrypt 권한 부여.

CLI:

```bash
aws kms create-key \
  --description "AI Call calendar token encryption" \
  --region ap-northeast-2

aws kms create-alias \
  --alias-name alias/ai-call-calendar-token \
  --target-key-id <kms-key-id> \
  --region ap-northeast-2
```

Lambda env에는 `CALENDAR_TOKEN_KMS_KEY_ID` 또는 `TOKEN_KMS_KEY_ID`로 alias/key id를 넣는다.

## 11. Secrets Manager 등록

등록할 secret:

- DB password.
- Firebase Admin SDK JSON.
- OAuth client secrets.
- OpenAI API key.
- CLOVA secret.
- Solapi secret.

권장:

- DB password는 Lambda env 평문 대신 Secrets Manager 우선.
- secret 값은 CLI output에 노출하지 않는다.
- secret 이름만 문서화한다.

콘솔:

1. Secrets Manager -> Store a new secret.
2. Secret type: Other type of secret.
3. Key/value 또는 Plaintext JSON 선택.
4. Secret name 예시:
   - `ai-call/prod/db`
   - `ai-call/prod/firebase-admin-sdk`
   - `ai-call/prod/openai`
   - `ai-call/prod/clova`
   - `ai-call/prod/solapi`

CLI 예시:

```bash
aws secretsmanager create-secret \
  --name ai-call/prod/db \
  --description "AI Call DB credentials" \
  --secret-string file://db-secret-placeholder.json \
  --region ap-northeast-2
```

`db-secret-placeholder.json`에는 실제 작업자가 로컬에서만 값을 넣고, repo에 commit하지 않는다.

## 12. Lambda Layer 생성

작업 위치:

```text
ai-call-assistant/lambda
```

권장 빌드 방식:

```bash
cd ai-call-assistant/lambda
mkdir -p python
pip install --platform manylinux2014_x86_64 \
  --only-binary=:all: \
  -r requirements.txt \
  -t python/
zip -r layer.zip python
```

배포:

```bash
aws lambda publish-layer-version \
  --layer-name ai-call-python-dependencies \
  --zip-file fileb://layer.zip \
  --compatible-runtimes python3.12 \
  --region ap-northeast-2
```

콘솔:

1. Lambda -> Layers -> Create layer.
2. Upload `layer.zip`.
3. Compatible runtime: Python 3.12.

## 13. Lambda 함수 생성

함수 목록:

| Function name | Handler |
| --- | --- |
| `call-recorder-api-auth` | `auth_handler.lambda_handler` |
| `call-recorder-api-call` | `call_handler.lambda_handler` |
| `call-recorder-api-nlp` | `nlp_handler.lambda_handler` |
| `call-recorder-api-calendar` | `calendar_handler.lambda_handler` |

콘솔:

1. Lambda -> Functions -> Create function.
2. Author from scratch.
3. Runtime: Python 3.12.
4. Execution role: `ai-call-lambda-execution-role`.
5. Advanced settings에서 VPC를 새 VPC/private subnet/Lambda SG로 지정.
6. 생성 후 Runtime settings에서 handler를 파일별로 설정.
7. Layer를 연결.

CLI 예시:

```bash
aws lambda create-function \
  --function-name call-recorder-api-call \
  --runtime python3.12 \
  --role <lambda-execution-role-arn> \
  --handler call_handler.lambda_handler \
  --zip-file fileb://call-handler.zip \
  --timeout 300 \
  --memory-size 512 \
  --vpc-config SubnetIds=<private-subnet-a>,SecurityGroupIds=<lambda-sg-id> \
  --region ap-northeast-2
```

패키징 예시:

```bash
cd ai-call-assistant/lambda
zip call-handler.zip call_handler.py auth_handler.py calendar_handler.py notes_handler.py nlp_handler.py redis_client.py
```

`call_handler.py`가 다른 핸들러를 import/위임하므로 call 함수 zip에는 관련 핸들러를 함께 넣는 편이 안전하다.

## 14. Lambda 환경변수 설정

원칙:

- secret 값은 직접 문서화하지 않는다.
- DB password는 `DB_SECRET_NAME` 또는 `DB_SECRET_ARN`을 우선 사용한다.
- 외부 서비스 값은 나중에 연결할 값으로 둔다.

콘솔:

1. Lambda -> Function -> Configuration -> Environment variables.
2. Edit.
3. `env.example.md`의 이름을 기준으로 추가.

CLI 예시:

```bash
aws lambda update-function-configuration \
  --function-name call-recorder-api-call \
  --environment "Variables={AWS_REGION=ap-northeast-2,DB_HOST=<rds-endpoint>,DB_NAME=call_recorder,DB_USER=admin,DB_SECRET_NAME=ai-call/prod/db,S3_BUCKET=<audio-bucket-name>,REDIS_HOST=<redis-endpoint>,REDIS_PORT=6379,CORS_ALLOWED_ORIGINS=https://<cloudfront-domain>}" \
  --region ap-northeast-2
```

CLI 명령에는 실제 secret 값을 넣지 않는다.

## 15. API Gateway REST API 생성

콘솔:

1. API Gateway -> REST API -> Build.
2. New API.
3. API name 예시: `ai-call-api`.
4. Endpoint type: Regional.
5. Create API.
6. Resources/Methods를 수동 생성한다.
7. Stage name: `prod`.

CLI:

```bash
aws apigateway create-rest-api \
  --name ai-call-api \
  --endpoint-configuration types=REGIONAL \
  --region ap-northeast-2
```

결과의 `id`가 새 API ID다.

## 16. API Gateway Route / Method 수동 생성

API Gateway route export가 없으므로 `aws-migration-audit.md`의 route 목록과 코드 기준으로 수동 생성한다.

우선 생성할 route:

```text
GET    /auth/{provider}/authorize
POST   /auth/kakao
POST   /auth/google
POST   /auth/naver
POST   /auth/verify
POST   /auth/logout
GET    /stores
POST   /stores
GET    /stores/{storeId}/keywords
POST   /stores/{storeId}/keywords
PATCH  /stores/{storeId}/keywords/{keywordId}
DELETE /stores/{storeId}/keywords/{keywordId}
GET    /me
PATCH  /me
GET    /calls
POST   /calls/upload
POST   /calls/{id}/process
GET    /calls/{id}
PATCH  /calls/{id}
DELETE /calls/{id}
GET    /calls/{id}/audio
GET    /customers/{phone}
PATCH  /customers/{phone}
GET    /calendar/events
GET    /calendar/connections
GET    /calendar/connections/{provider}/authorize
POST   /calendar/connections/oauth-code
PATCH  /calendar/connections/default
DELETE /calendar/connections/{provider}
POST   /calls/{id}/calendar-events
GET    /calls/{id}/note
PATCH  /calls/{id}/note
POST   /calls/{id}/photos/upload-url
POST   /calls/{id}/photos
DELETE /calls/{id}/photos/{photoId}
POST   /admin/reload-keywords
```

보류 route:

- `GET /summaries/{id}`: backend routing 불일치로 보류/확인 필요.
- `POST /clova/webhook`: 문서상 필요하지만 현재 구현 방식 확인 필요.

통합 방식:

- Lambda Proxy integration.
- auth path는 auth Lambda.
- call/store/customer/notes path는 call Lambda 또는 notes 위임 포함 call Lambda.
- nlp/admin path는 nlp Lambda.
- calendar path는 calendar Lambda.

콘솔:

1. API Gateway -> Resources.
2. Create resource로 path segment 생성.
3. Create method.
4. Integration type: Lambda Function.
5. Use Lambda Proxy integration 체크.
6. Lambda function 선택.
7. Save 후 Lambda permission 추가 확인.

배포:

```bash
aws apigateway create-deployment \
  --rest-api-id <api-id> \
  --stage-name prod \
  --region ap-northeast-2
```

## 17. CORS 설정

필요 origin:

- Web CloudFront domain.
- 로컬 개발용 `http://localhost:3000`.
- 필요 시 배포 전 임시 도메인.

콘솔:

1. API Gateway -> Resources.
2. 각 resource에서 Enable CORS.
3. Allowed Origin을 운영 domain으로 제한.
4. OPTIONS method 생성 확인.
5. Deploy API.

Lambda도 CORS header를 반환하므로 API Gateway와 Lambda header가 충돌하지 않는지 smoke test에서 확인한다.

CLI preflight 테스트:

```bash
curl -i -X OPTIONS \
  "https://<api-id>.execute-api.ap-northeast-2.amazonaws.com/prod/stores" \
  -H "Origin: https://<cloudfront-domain>" \
  -H "Access-Control-Request-Method: GET"
```

## 18. EventBridge Rule 생성

용도:

- pending STT retry.
- customer analysis batch.

콘솔:

1. EventBridge -> Rules -> Create rule.
2. Rule type: Schedule.
3. 예: 5분마다 STT retry.
4. Target: `call-recorder-api-call`.
5. Input은 constant JSON으로 구분한다.

예시 input:

```json
{"detail":{"job":"pending_stt"}}
```

고객 분석 batch 예시:

```json
{"detail":{"job":"customer_analysis"}}
```

CLI:

```bash
aws events put-rule \
  --name ai-call-pending-stt-retry \
  --schedule-expression "rate(5 minutes)" \
  --region ap-northeast-2
```

```bash
aws events put-targets \
  --rule ai-call-pending-stt-retry \
  --targets "Id"="1","Arn"="<call-lambda-arn>","Input"="{\"detail\":{\"job\":\"pending_stt\"}}" \
  --region ap-northeast-2
```

## 19. CloudWatch Logs / Metrics 확인

콘솔:

1. CloudWatch -> Log groups.
2. `/aws/lambda/call-recorder-api-auth`
3. `/aws/lambda/call-recorder-api-call`
4. `/aws/lambda/call-recorder-api-nlp`
5. `/aws/lambda/call-recorder-api-calendar`

CLI:

```bash
aws logs describe-log-groups \
  --log-group-name-prefix /aws/lambda/call-recorder-api \
  --region ap-northeast-2
```

Metric:

- `call_handler.py`는 `CallRecorder/Polling` namespace에 polling metric을 put할 수 있다.
- Lambda role에 `cloudwatch:PutMetricData`가 필요하다.

## 20. Web 빌드 및 S3 업로드

작업 위치:

```text
ai-call-assistant-web
```

빌드 전 env:

```bash
NEXT_PUBLIC_API_BASE_URL=https://<api-id>.execute-api.ap-northeast-2.amazonaws.com/prod
NEXT_PUBLIC_KAKAO_JS_KEY=<later-issued-public-key>
NEXT_PUBLIC_FIREBASE_API_KEY=<later-issued-public-key>
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=<firebase-auth-domain>
NEXT_PUBLIC_FIREBASE_PROJECT_ID=<firebase-project-id>
NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET=<firebase-storage-bucket>
NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID=<firebase-sender-id>
NEXT_PUBLIC_FIREBASE_APP_ID=<firebase-app-id>
```

secret이 아닌 public client config만 `NEXT_PUBLIC_`에 둔다.

빌드:

```bash
cd ai-call-assistant-web
npm ci
npm run build
```

업로드:

```bash
aws s3 sync out/ s3://<web-bucket-name>/ --delete --region ap-northeast-2
```

위 명령은 웹 버킷 내용을 동기화한다. 운영에서 실행 전 대상 bucket이 맞는지 반드시 확인한다.

## 21. CloudFront 생성

콘솔:

1. CloudFront -> Distributions -> Create distribution.
2. Origin domain: web S3 bucket.
3. Origin access: Origin access control settings 권장.
4. Viewer protocol policy: Redirect HTTP to HTTPS.
5. Default root object: `index.html`.
6. Error pages:
   - 403 -> `/index.html`, HTTP 200.
   - 404 -> `/index.html`, HTTP 200.

CLI invalidation:

```bash
aws cloudfront create-invalidation \
  --distribution-id <distribution-id> \
  --paths "/*"
```

CloudFront domain이 나오면:

- API Gateway CORS origin에 추가.
- Firebase/Kakao/Naver/Google redirect URI에 추가.
- Web env의 callback domain을 사용하는 코드가 있으면 갱신.

## 22. Android API_BASE_URL 교체

파일:

```text
call-recorder-android/gradle.properties
```

설정:

```properties
API_BASE_URL=https://<api-id>.execute-api.ap-northeast-2.amazonaws.com/prod/
```

주의:

- trailing slash가 필요하다.
- `gradle.properties`는 실제 key를 포함할 수 있으므로 commit 전 정책 확인.
- `gradle.properties.example`만 repo에 두고 실제 값 파일은 개인/CI 환경에서 관리하는 방식 권장.

빌드:

```bash
cd call-recorder-android
./gradlew assembleDebug
```

Windows:

```powershell
cd call-recorder-android
.\gradlew.bat assembleDebug
```

## 23. 외부 서비스 새 발급 후 연결

나중에 연결할 값:

| 서비스 | AWS/Lambda 연결 위치 |
| --- | --- |
| Firebase Admin SDK | Secrets Manager `ai-call/prod/firebase-admin-sdk` |
| Firebase Web config | Web `NEXT_PUBLIC_FIREBASE_*` |
| Firebase Android | Android app config 파일 및 Firebase project |
| Kakao OAuth | Lambda env/secret, Web `NEXT_PUBLIC_KAKAO_JS_KEY`, Android native key |
| Google OAuth/Calendar | Lambda env/secret, redirect URI |
| Naver OAuth/Calendar | Lambda env/secret, redirect URI |
| OpenAI | Secrets Manager 또는 Lambda env name `OPENAI_API_KEY` |
| NCP CLOVA Speech | Lambda env/secret |
| Solapi | Lambda env/secret |

Redirect URI 후보:

```text
https://<cloudfront-domain>/oauth/kakao/
https://<cloudfront-domain>/oauth/google/
https://<cloudfront-domain>/oauth/naver/
https://<api-id>.execute-api.ap-northeast-2.amazonaws.com/prod/auth/{provider}/callback
```

실제 redirect URI는 현재 구현과 provider console 설정을 대조해 확정한다.

## 24. 배포 후 점검

최소 점검:

1. Lambda가 RDS에 연결되는지 확인.
2. Lambda가 Secrets Manager 값을 읽는지 확인.
3. Lambda가 S3 presigned URL을 발급하는지 확인.
4. S3 direct PUT이 CORS와 signature 문제 없이 동작하는지 확인.
5. API Gateway CORS preflight가 Web origin에서 통과하는지 확인.
6. Web CloudFront 화면이 로드되는지 확인.
7. Android가 새 `API_BASE_URL`로 호출하는지 확인.
8. CloudWatch Logs에 secret 값이 찍히지 않는지 확인.

상세 점검은 `smoke-test.md`를 따른다.

## 25. 현재 보류/확인 필요 항목

1. `/summaries/{id}`는 client/README에는 있으나 backend routing이 확인되지 않아 보류.
2. `/clova/webhook`은 문서상 필요하지만 현재 구현 방식이 sync/polling인지 webhook인지 확인 필요.
3. Redis는 생성 대상으로 두되, 장애 시 fallback 범위와 필수/선택 여부 확인 필요.
4. API Gateway route export가 없어 코드/audit 기준으로 수동 생성해야 한다.
5. Lambda integration을 함수별로 분리할지, `call_handler.py` dispatcher 중심으로 둘지 최종 결정 필요.
6. Firebase/Google/Kakao/Naver redirect URI는 새 CloudFront/API Gateway URL이 나온 뒤 확정.
7. GitHub Actions workflow 파일이 현재 checkout에서 확인되지 않았다. 자동 배포는 별도 생성 필요.
8. RDS SSL 적용 여부 확인 필요. 현재 코드에는 SSL disabled 계열 흔적이 있다.
9. 기존 데이터 이전은 없지만 `schema_1.sql`과 실제 코드 쿼리의 `stores`, `calls`, `summaries` 컬럼 호환성은 smoke test로 검증해야 한다.
