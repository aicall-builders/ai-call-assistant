# 🚀 Lambda 배포 가이드

현재는 **AWS Lambda 콘솔에서 직접 수정**하는 방식으로 배포 중입니다. (MVP 단계)

향후 GitHub Actions 또는 SAM/CDK로 자동화 예정.

---

## 📋 배포 환경

- **함수명**: `call-recorder-api`
- **런타임**: Python 3.x
- **리전**: `ap-northeast-2` (서울)
- **API Gateway**: `sxj5qje9bd.execute-api.ap-northeast-2.amazonaws.com`

---

## 🔧 코드 수정 방법

### 방법 1: AWS Lambda 콘솔 (현재 방식)

1. AWS Console → Lambda → `call-recorder-api` 함수 진입
2. **Code** 탭 클릭
3. 좌측 파일 트리에서 `lambda_function.py` 또는 `keywords.json` 클릭
4. 코드 수정
5. 우측 상단 **Deploy** 버튼 클릭 (`Ctrl+Shift+U`)

⚠️ **Deploy 버튼을 누르지 않으면 변경사항이 반영되지 않습니다.**

### 방법 2: 로컬 → Lambda 업로드 (추후 권장)

```bash
# 의존성 패키징
pip install -r requirements.txt -t package/
cp lambda_function.py keywords.json package/
cd package
zip -r ../lambda_deploy.zip .
cd ..

# AWS CLI로 업로드
aws lambda update-function-code \
  --function-name call-recorder-api \
  --zip-file fileb://lambda_deploy.zip \
  --region ap-northeast-2
```

---

## 🛣️ API Gateway 라우트 추가

새 엔드포인트를 추가할 때:

1. **Lambda 함수 코드**의 `lambda_handler` 라우터에 분기 추가
2. **API Gateway 콘솔** → HTTP API → Routes
3. **Create** 버튼 → Method/Path 입력 (예: `GET /calls/{id}/audio`)
4. 생성된 라우트의 **Integration** → 기존 Lambda 통합 (`h7efjdl`) 선택
5. 별도 배포 단계 없음 (HTTP API는 자동 배포)

---

## 🔐 환경 변수 / 시크릿

### Lambda 환경 변수
Lambda 콘솔 → Configuration → Environment variables
CLOVA_INVOKE_URL=https://clovaspeech-gw.ncloud.com/external/v1/...
CLOVA_SECRET_KEY=...
S3_BUCKET=call-recoder-audio-1017
ANTHROPIC_API_KEY=sk-...  # (실제로는 OpenAI 키 - 변수명 정리 TODO)

### Secrets Manager
DB 비밀번호와 Firebase Admin SDK는 Secrets Manager에서 가져옵니다.

| Secret Name | 용도 |
|-------------|------|
| `rds!db-...` (코드 내 ARN 참조) | RDS MySQL 비밀번호 |
| `firebase-admin-sdk` | Firebase Admin SDK 인증 정보 |

---

## 🔌 외부 서비스 의존성

### NCP CLOVA Speech
- 콘솔: https://www.ncloud.com/product/aiService/clovaSpeech
- **invoke URL** + **secret key** 발급
- Webhook URL을 콜백으로 등록:
https://sxj5qje9bd.execute-api.ap-northeast-2.amazonaws.com/clova/webhook

### OpenAI API
- 콘솔: https://platform.openai.com/
- API key 발급 → 환경 변수에 설정
- 사용 모델: `gpt-4o-mini`

### Kakao Developers
- 콘솔: https://developers.kakao.com/
- 안드로이드/웹 플랫폼 등록
- key hash 등록 (debug/release 각각)

### Firebase
- Firebase Console → 프로젝트 설정 → 서비스 계정
- Admin SDK private key 다운로드 → Secrets Manager에 저장

---

## 🗄️ DB 마이그레이션

스키마 변경이 필요할 때는 임시 엔드포인트를 만들어 호출하는 방식 사용 중:

```python
# 예: extracted_info 컬럼 추가
def migrate_add_extracted_info():
    cursor.execute("ALTER TABLE summaries ADD COLUMN extracted_info JSON NULL ...")
```

호출:
```bash
curl -X POST https://sxj5qje9bd.execute-api.ap-northeast-2.amazonaws.com/migrate/extracted-info
```

⚠️ 실행 후 보안상 라우터에서 즉시 제거하는 게 좋음. (현재는 임시로 열려있음)

---

## 🧹 발표 후 정리할 것

- [ ] `/demo/seed`, `/demo/clean`, `/migrate/extracted-info` 임시 엔드포인트 제거
- [ ] `demo_seed`의 하드코딩된 `kakao:4875885837` UID 제거
- [ ] 환경 변수명 `ANTHROPIC_API_KEY` → `OPENAI_API_KEY` 정리
- [ ] DB Secret ARN 하드코딩 → 환경 변수로 분리
- [ ] 자동 배포 파이프라인 구축 (GitHub Actions or SAM)

---