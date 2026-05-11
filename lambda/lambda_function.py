import json
import uuid
import boto3
import pymysql
import requests
import firebase_admin
from firebase_admin import auth, credentials

# ── 환경변수 ──────────────────────────────────────────────
import os
CLOVA_INVOKE_URL  = os.environ.get('CLOVA_INVOKE_URL', '')
CLOVA_SECRET_KEY  = os.environ.get('CLOVA_SECRET_KEY', '')
S3_BUCKET         = os.environ.get('S3_BUCKET', 'call-recoder-audio-1017')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
DB_SECRET_ARN     = 'arn:aws:secretsmanager:ap-northeast-2:135775632268:secret:rds!db-aefb8895-1f09-4168-a4e4-45b4b9a1b076-94BMFa'
DB_HOST           = 'call-recorder-db.czem0u8m8xfi.ap-northeast-2.rds.amazonaws.com'
DB_NAME           = 'call_recorder'

# ── AWS 클라이언트 ────────────────────────────────────────
s3_client      = boto3.client('s3', region_name='ap-northeast-2')
secrets_client = boto3.client('secretsmanager', region_name='ap-northeast-2')

# ── Firebase 초기화 ───────────────────────────────────────
def init_firebase():
    if not firebase_admin._apps:
        secret  = secrets_client.get_secret_value(SecretId='firebase-admin-sdk')
        cred    = credentials.Certificate(json.loads(secret['SecretString']))
        firebase_admin.initialize_app(cred)

# ── DB 연결 ───────────────────────────────────────────────
def get_db():
    secret = secrets_client.get_secret_value(SecretId=DB_SECRET_ARN)
    creds  = json.loads(secret['SecretString'])
    return pymysql.connect(
        host=DB_HOST,
        user=creds['username'],
        password=creds['password'],
        db=DB_NAME,
        port=3306,
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        ssl_disabled=True
    )

# ── Firebase 토큰 검증 ────────────────────────────────────
def verify_token(event):
    headers     = event.get('headers', {})
    auth_header = headers.get('Authorization') or headers.get('authorization', '')
    token       = auth_header.replace('Bearer ', '').strip()
    if not token:
        raise Exception('토큰이 없습니다.')
    decoded = auth.verify_id_token(token)
    return decoded['uid']

# ── 공통 응답 ─────────────────────────────────────────────
CORS_HEADERS = {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type,Authorization',
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS,PATCH,DELETE'
}

def response(status, body):
    return {
        'statusCode': status,
        'headers': CORS_HEADERS,
        'body': json.dumps(body, ensure_ascii=False, default=str)
    }

# ── keywords.json 로드 ────────────────────────────────────
KEYWORDS = None
def get_keywords():
    global KEYWORDS
    if KEYWORDS is None:
        with open('keywords.json', 'r', encoding='utf-8') as f:
            KEYWORDS = json.load(f)
    return KEYWORDS

def match_keywords(text, industry='food'):
    kw_data    = get_keywords()
    categories = kw_data['industries'].get(industry, {}).get('categories', {})
    matched    = []
    matched_category = None
    highest_priority = 999

    for cat_key, cat_val in categories.items():
        for kw in cat_val.get('keywords', []):
            if kw in text:
                matched.append(kw)
                if cat_val['priority'] < highest_priority:
                    highest_priority = cat_val['priority']
                    matched_category = cat_key

    for kw in kw_data['global_keywords']['urgent']['keywords']:
        if kw in text:
            matched.append(kw)
            matched_category = 'urgent'

    return list(set(matched)), matched_category


# ══════════════════════════════════════════════════════════
# STT 정규화 (1차 LLM)
# ══════════════════════════════════════════════════════════
def normalize_stt_with_llm(stt_text, call_created_at=None):
    """
    STT 결과를 정규화:
    - 필러 제거 (음, 어, 그, 저 등)
    - 문맥/문법 보정
    - 시간 표현 → 실제 날짜
    - 전화번호 포맷 통일
    - 화자 라벨 변경 ([화자1] → 발신자:, [화자2] → 수신자:)
    - 비언어는 [한숨], [웃음] 등으로 표시
    """
    if not ANTHROPIC_API_KEY:
        print("[NORMALIZE] API 키 없음 - 정규화 스킵")
        return stt_text

    from datetime import datetime
    if call_created_at:
        if isinstance(call_created_at, str):
            base_time = call_created_at
        else:
            base_time = call_created_at.strftime("%Y-%m-%d %H:%M (%A)")
    else:
        base_time = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")

    prompt = f"""다음은 통화 녹음의 STT 변환 텍스트입니다. 이 텍스트를 정규화해주세요.

[통화 발생 시점]
{base_time}

[원본 STT 텍스트]
{stt_text}

[정규화 규칙]
1. 필러 제거: "음", "어", "아", "그", "저" 같은 의미 없는 추임새 제거
2. 문맥/문법 보정: 한글 문맥상 부자연스러운 표현 자연스럽게 수정 (단, 의미는 보존)
3. 시간 표현 변환: "내일", "오늘", "다음주 월요일", "이번 주말" 같은 상대적 시간 표현을 통화 발생 시점 기준 실제 날짜(YYYY-MM-DD)로 변환
4. 시각 변환: "저녁 7시" → "19:00", "오전 11시" → "11:00" 등 24시간제로
5. 전화번호 포맷: "공일공 일이삼사 오육칠팔" 같은 음성을 "010-1234-5678" 형식으로 통일
6. 화자 라벨 변경: "[화자1]" → "발신자:", "[화자2]" → "수신자:"
7. 비언어 표시: 한숨, 웃음, 침묵 등은 [한숨], [웃음], [침묵] 등으로 표시
8. 비속어: 그대로 유지 (마스킹 X)

[중요]
- 원본 의미를 절대 왜곡하지 마세요
- 명시되지 않은 정보를 추가하지 마세요
- 정규화된 텍스트만 응답하세요 (설명, 주석, 마크다운 없이)

[정규화된 텍스트]"""

    try:
        res = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {ANTHROPIC_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1
            },
            timeout=30
        )
        res.raise_for_status()
        data = res.json()
        normalized = data['choices'][0]['message']['content'].strip()
        
        if '```' in normalized:
            parts = normalized.split('```')
            if len(parts) >= 2:
                normalized = parts[1]
                if normalized.startswith('text\n'):
                    normalized = normalized[5:]
                normalized = normalized.strip()
        
        print(f"[NORMALIZE] 정규화 완료. 길이: {len(stt_text)} → {len(normalized)}")
        return normalized

    except Exception as e:
        print(f"[NORMALIZE ERROR] {str(e)}")
        return stt_text  # 실패 시 원본 반환

# ══════════════════════════════════════════════════════════
# LLM 요약 (구조화 정보 추출)
# ══════════════════════════════════════════════════════════
def summarize_with_llm(stt_text, caller_number=None, duration_sec=None, store_name=None):
    if not ANTHROPIC_API_KEY:
        print("[LLM] API 키 없음 - 스킵")
        return None

    context = ""
    if store_name:
        context += f"가게명: {store_name}\n"
    if duration_sec:
        minutes = duration_sec // 60
        seconds = duration_sec % 60
        context += f"통화 길이: {minutes}분 {seconds}초\n"
    if caller_number:
        context += f"발신 번호: {caller_number}\n"

    prompt = f"""다음은 소상공인 식당에 걸려온 통화 녹음의 STT 변환 텍스트입니다.

{context}
[통화 내용]
{stt_text}

위 통화를 분석해서 아래 JSON 형식으로만 응답해주세요. JSON 외 다른 텍스트는 절대 포함하지 마세요.

{{
  "summary": "통화 내용을 3줄 이내로 요약",
  "category": "예약/주문/취소/환불/불만/문의/칭찬/기타 중 하나",
  "sentiment": "positive/neutral/negative 중 하나",
  "action_required": true 또는 false,
  "keywords": ["핵심 키워드1", "핵심 키워드2", "핵심 키워드3"],
  "extracted_info": {{
    "category_code": "reservation/order/cancel_refund/complaint/hours_location/price/ingredients_allergy/catering_bulk/positive/other 중 하나",
    "customer_name": "고객 이름 (없으면 null)",
    "phone": "고객 전화번호 (없으면 null, 형식: 010-1234-5678)",
    "date": "예약/주문 날짜 (YYYY-MM-DD, 없으면 null. '내일'이면 다음날, '오늘'이면 오늘 날짜로 변환)",
    "time": "예약/주문 시간 (HH:MM 24시간제, 없으면 null)",
    "party_size": "예약 인원 수 (정수, 없으면 null)",
    "menu": ["주문/언급된 메뉴 배열, 없으면 빈 배열 []"],
    "special_notes": "특이사항/요청사항 한 문장 (알레르기, 자리 요청, 컴플레인 내용 등, 없으면 null)"
  }}
}}

규칙:
- 통화 내용에 명시되지 않은 정보는 절대 추측하지 말고 null로 두세요.
- '내일', '오늘', '이번 주말' 같은 표현은 통화 시점 기준으로 실제 날짜로 변환하세요.
- category_code는 위 영문 코드 중 하나를 정확히 사용하세요."""

    try:
        res = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {ANTHROPIC_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        res.raise_for_status()
        data = res.json()
        text = data['choices'][0]['message']['content'].strip()

        if '```' in text:
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]

        result = json.loads(text.strip())
        print(f"[LLM] 요약 완료: {result}")
        return result

    except Exception as e:
        print(f"[LLM ERROR] {str(e)}")
        return None


# ══════════════════════════════════════════════════════════
# 라우터
# ══════════════════════════════════════════════════════════
def lambda_handler(event, context):
    init_firebase()

    path   = event.get('path') or event.get('rawPath', '')
    method = event.get('httpMethod') or \
             event.get('requestContext', {}).get('http', {}).get('method', '')

    print(f"[ROUTER] {method} {path}")

    if method == 'OPTIONS':
        return response(200, {'message': 'OK'})

    # 🎁 데모용 임시 엔드포인트 (인증 없이, 발표 후 삭제!!)
    if path.endswith('/demo/seed') and method == 'POST':
        try:
            uid = 'kakao:4875885837'
            return demo_seed(uid)
        except Exception as e:
            print(f"[DEMO ERROR] {str(e)}")
            return response(500, {'message': str(e)})

    # 🔧 임시: DB 마이그레이션 (실행 후 삭제!!)
    if path.endswith('/migrate/extracted-info') and method == 'POST':
        try:
            return migrate_add_extracted_info()
        except Exception as e:
            print(f"[MIGRATE ERROR] {str(e)}")
            return response(500, {'message': str(e)})

    # 🧹 임시: 기존 데모 데이터 청소 (실행 후 삭제!!)
    if path.endswith('/demo/clean') and method == 'POST':
        try:
            uid = 'kakao:4875885837'
            return demo_clean(uid)
        except Exception as e:
            print(f"[CLEAN ERROR] {str(e)}")
            return response(500, {'message': str(e)})

    try:
        if path.endswith('/auth/kakao') and method == 'POST':
            return kakao_login(event)

        if '/clova/webhook' in path and method == 'POST':
            return clova_webhook(event)

        uid = verify_token(event)

        if path.endswith('/stores') and method == 'POST':
            return create_store(event, uid)

        if path.endswith('/stores') and method == 'GET':
            return get_stores(uid)

        if path.endswith('/calls/upload') and method == 'POST':
            return calls_upload(event, uid)

        if '/calls/' in path and path.endswith('/process') and method == 'POST':
            call_id = path.split('/calls/')[1].replace('/process', '')
            return calls_process(call_id, uid)

        if path.endswith('/calls') and method == 'GET':
            return get_calls(event, uid)

# /calls/{id}/audio - 음성 재생 URL (다른 GET보다 먼저 매칭!)
        if '/calls/' in path and path.endswith('/audio') and method == 'GET':
            call_id = path.split('/calls/')[1].replace('/audio', '')
            return get_call_audio(call_id, uid)

        if '/calls/' in path and method == 'GET' and not path.endswith('/process'):
            call_id = path.split('/calls/')[1]
            return get_call(call_id, uid)
        if '/calls/' in path and method == 'PATCH':
            call_id = path.split('/calls/')[1]
            return update_call_category(call_id, uid, event)
        if '/calls/' in path and method == 'DELETE':
            call_id = path.split('/calls/')[1]
            return delete_call(call_id, uid)

        if '/summaries/' in path and method == 'GET':
            summary_id = path.split('/summaries/')[1]
            return get_summary(summary_id, uid)

        return response(404, {'message': 'Not Found'})

    except auth.InvalidIdTokenError:
        return response(401, {'message': '인증이 필요합니다.'})
    except Exception as e:
        print(f"[ERROR] {str(e)}")
        return response(500, {'message': str(e)})


# ══════════════════════════════════════════════════════════
# 1. 카카오 로그인
# ══════════════════════════════════════════════════════════
def kakao_login(event):
    body = json.loads(event['body'])
    
    print(f"[KAKAO LOGIN BODY] {json.dumps(body, ensure_ascii=False)}")
    
    kakao_access_token = (
        body.get('kakao_access_token') or
        body.get('access_token') or
        body.get('kakaoAccessToken') or
        body.get('accessToken') or
        body.get('token')
    )
    
    if not kakao_access_token:
        return response(400, {'message': f'kakao_access_token이 없습니다. 받은 키: {list(body.keys())}'})

    kakao_res = requests.get(
        'https://kapi.kakao.com/v2/user/me',
        headers={'Authorization': f'Bearer {kakao_access_token}'}
    )
    kakao_user = kakao_res.json()

    if kakao_res.status_code != 200 or 'id' not in kakao_user:
        return response(401, {'message': 'Invalid Kakao token'})

    kakao_id = str(kakao_user['id'])
    nickname = kakao_user.get('kakao_account', {}).get('profile', {}).get('nickname', 'unknown')
    uid = f'kakao:{kakao_id}'
    custom_token = auth.create_custom_token(uid)

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO users (id, firebase_uid, kakao_id, name, role)
                VALUES (UUID(), %s, %s, %s, 'OWNER')
                ON DUPLICATE KEY UPDATE kakao_id=%s, name=%s
            """, (uid, kakao_id, nickname, kakao_id, nickname))
        conn.commit()
    finally:
        conn.close()

    custom_token_str = custom_token.decode()
    return response(200, {
        'custom_token': custom_token_str,
        'uid': uid,
        'nickname': nickname,
        'access_token': custom_token_str,
        'user': {
            'id': int(kakao_id),
            'nickname': nickname,
            'email': None,
            'profile_image': None
        }
    })


# ══════════════════════════════════════════════════════════
# 2. POST /stores
# ══════════════════════════════════════════════════════════
def create_store(event, uid):
    body = json.loads(event.get('body', '{}'))
    name = body.get('name')

    if not name:
        return response(400, {'message': '가게 이름은 필수입니다.'})

    store_id = str(uuid.uuid4())
    conn     = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE firebase_uid = %s", (uid,))
            user = cursor.fetchone()
            if not user:
                return response(404, {'message': '사용자를 찾을 수 없습니다.'})

            cursor.execute("""
                INSERT INTO stores (id, name, owner_id) VALUES (%s, %s, %s)
            """, (store_id, name, user['id']))
        conn.commit()
    finally:
        conn.close()

    return response(201, {'success': True, 'store_id': store_id, 'name': name})


# ══════════════════════════════════════════════════════════
# 3. GET /stores
# ══════════════════════════════════════════════════════════
def get_stores(uid):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT s.id, s.name, s.created_at
                FROM stores s JOIN users u ON s.owner_id = u.id
                WHERE u.firebase_uid = %s
            """, (uid,))
            stores = cursor.fetchall()
        return response(200, {'stores': stores})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════
# 4. POST /calls/upload
# ══════════════════════════════════════════════════════════
def calls_upload(event, uid):
    body          = json.loads(event.get('body', '{}'))
    store_id      = body.get('store_id')
    file_name     = body.get('file_name')
    mime_type     = body.get('mime_type', 'audio/mp4')
    caller_number    = body.get('counterpart_number') or body.get('caller_number')
    caller_category  = body.get('caller_category', 'UNCLASSIFIED')
    duration      = body.get('duration_seconds') or body.get('duration')
    recorded_at   = body.get('recorded_at') or body.get('callStartedAt') or body.get('call_started_at')

    if not all([store_id, file_name]):
        return response(400, {'message': 'store_id, file_name은 필수입니다.'})

    if duration and duration < 5:
        return response(400, {'message': '통화 길이가 너무 짧습니다. (5초 이하)'})

    # 🕐 1시간 이내 통화만 업로드 허용
    if recorded_at:
        from datetime import datetime, timezone, timedelta
        try:
            # ISO 8601 형식 파싱 (예: "2026-05-11T14:30:00+09:00")
            if isinstance(recorded_at, str):
                recorded_dt = datetime.fromisoformat(recorded_at.replace('Z', '+00:00'))
            else:
                # Unix timestamp (밀리초 또는 초)
                ts = float(recorded_at)
                if ts > 1e12:  # 밀리초
                    ts = ts / 1000
                recorded_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            
            now = datetime.now(timezone.utc)
            age = now - recorded_dt
            
            if age > timedelta(hours=1):
                age_hours = age.total_seconds() / 3600
                print(f"[UPLOAD REJECT] 통화가 너무 오래됨: {age_hours:.1f}시간 전")
                return response(400, {
                    'message': f'1시간 이내의 통화만 업로드 가능합니다. (이 통화: {age_hours:.1f}시간 전)',
                    'recorded_at': recorded_at,
                    'age_hours': round(age_hours, 1)
                })
        except Exception as e:
            print(f"[UPLOAD WARN] recorded_at 파싱 실패: {e}, 검증 스킵")
    else:
        # recorded_at 없으면 경고만 (당분간 호환성 유지)
        print(f"[UPLOAD WARN] recorded_at 없음. 1시간 검증 스킵")

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE firebase_uid = %s", (uid,))
            user = cursor.fetchone()
            if not user:
                return response(404, {'message': '사용자를 찾을 수 없습니다.'})

            cursor.execute("""
                SELECT id FROM stores WHERE id = %s AND owner_id = %s
            """, (store_id, user['id']))
            if not cursor.fetchone():
                return response(403, {'message': '해당 가게에 대한 권한이 없습니다.'})

            call_id    = str(uuid.uuid4())
            s3_key     = f"recordings/{store_id}/{call_id}/{file_name}"
            
            print(f"[UPLOAD] mime_type={mime_type}, s3_key={s3_key}")
            
            upload_url = s3_client.generate_presigned_url(
                'put_object',
                Params={
                    'Bucket': S3_BUCKET,
                    'Key': s3_key,
                    'ContentType': mime_type
                },
                ExpiresIn=600
            )

            cursor.execute("""
                INSERT INTO calls
                    (id, store_id, user_id, caller_number, caller_category, s3_key, duration, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'uploaded')
            """, (call_id, store_id, user["id"], caller_number, caller_category, s3_key, duration))
        conn.commit()
    finally:
        conn.close()

    return response(200, {
        'success': True,
        'call_id': call_id,
        'upload_url': upload_url,
        's3_key': s3_key,
        'message': '10분 이내에 업로드해주세요.'
    })


# ══════════════════════════════════════════════════════════
# 5. POST /calls/{id}/process
# ══════════════════════════════════════════════════════════
def calls_process(call_id, uid):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT c.id, c.s3_key, c.status, u.firebase_uid
                FROM calls c JOIN users u ON c.user_id = u.id
                WHERE c.id = %s
            """, (call_id,))
            call = cursor.fetchone()

            if not call:
                return response(404, {'message': '통화를 찾을 수 없습니다.'})
            if call['firebase_uid'] != uid:
                return response(403, {'message': '권한이 없습니다.'})
            if call['status'] != 'uploaded':
                return response(400, {'message': f"이미 처리된 통화입니다. (status: {call['status']})"})

            presigned_url = s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': S3_BUCKET, 'Key': call['s3_key']},
                ExpiresIn=3600
            )

            webhook_url = "https://sxj5qje9bd.execute-api.ap-northeast-2.amazonaws.com/clova/webhook"

            clova_res = requests.post(
                f"{CLOVA_INVOKE_URL}/recognizer/url",
                headers={
                    'Accept': 'application/json',
                    'X-CLOVASPEECH-API-KEY': CLOVA_SECRET_KEY,
                    'Content-Type': 'application/json'
                },
                json={
                    'url': presigned_url,
                    'language': 'ko-KR',
                    'completion': 'async',
                    'callback': webhook_url,
                    'wordAlignment': True,
                    'fullText': True,
                    'diarization': {
                        'enable': True,
                        'speakerCountMin': 2,
                        'speakerCountMax': 2
                    }
                },
                timeout=30
            )
            
            print(f"[CLOVA RESPONSE] status={clova_res.status_code}")
            print(f"[CLOVA RESPONSE BODY] {clova_res.text}")
            
            clova_res.raise_for_status()
            clova_job_id = clova_res.json().get('token')

            cursor.execute("""
                UPDATE calls SET status = 'processing', clova_job_id = %s
                WHERE id = %s
            """, (clova_job_id, call_id))
        conn.commit()
    finally:
        conn.close()

    return response(200, {
        'success': True,
        'call_id': call_id,
        'clova_job_id': clova_job_id,
        'message': 'STT 처리가 시작되었습니다.'
    })


# ══════════════════════════════════════════════════════════
# 6. CLOVA Webhook → LLM 요약 연동
# ══════════════════════════════════════════════════════════
def clova_webhook(event):
    body   = json.loads(event.get('body', '{}'))
    token  = body.get('token')
    result = body.get('result')

    print(f"[WEBHOOK FULL BODY] {json.dumps(body, ensure_ascii=False)[:3000]}")
    print(f"[WEBHOOK] token={token} result={result}")

    if not token:
        return response(400, {'message': 'token이 없습니다.'})

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT c.id, c.store_id, c.caller_number, c.caller_category, c.duration,
                       s.name as store_name
                FROM calls c
                JOIN stores s ON c.store_id = s.id
                WHERE c.clova_job_id = %s
            """, (token,))
            call = cursor.fetchone()

            if not call:
                return response(404, {'message': 'call을 찾을 수 없습니다.'})

            call_id    = call['id']
            store_name = call['store_name']

            if result == 'SUCCEEDED':
                segments  = body.get('segments', [])
                raw_text = '\n'.join([
                    f"[화자{seg.get('speaker', {}).get('label', '?')}]: {seg.get('text', '').strip()}"
                    for seg in segments if seg.get('text', '').strip()
                ]) or body.get('text', '')

                # 🔧 1차 LLM: STT 정규화
                cursor.execute("SELECT created_at FROM calls WHERE id = %s", (call_id,))
                call_info = cursor.fetchone()
                call_created_at = call_info['created_at'] if call_info else None
                
                full_text = normalize_stt_with_llm(raw_text, call_created_at)
                
                print(f"[WEBHOOK] 정규화 전: {raw_text[:200]}")
                print(f"[WEBHOOK] 정규화 후: {full_text[:200]}")

                matched_kws, matched_cat = match_keywords(full_text)

                cursor.execute("""
                    UPDATE calls SET status = 'transcribed', stt_result = %s
                    WHERE id = %s
                """, (full_text, call_id))

                llm_result = summarize_with_llm(
                    stt_text=full_text,
                    caller_number=call['caller_number'],
                    duration_sec=call['duration'],
                    store_name=store_name
                )

                summary_id      = str(uuid.uuid4())
                summary_text    = llm_result.get('summary') if llm_result else None
                category        = llm_result.get('category', matched_cat) if llm_result else matched_cat
                sentiment       = llm_result.get('sentiment') if llm_result else None
                action_required = llm_result.get('action_required', False) if llm_result else False
                llm_keywords    = llm_result.get('keywords', []) if llm_result else []
                all_keywords    = list(set(matched_kws + llm_keywords))
                extracted_info  = llm_result.get('extracted_info') if llm_result else None  # ← NEW

                cursor.execute("""
                    INSERT INTO summaries
                        (id, call_id, summary, category, sentiment, action_required, keywords, extracted_info)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        summary = VALUES(summary), category = VALUES(category),
                        sentiment = VALUES(sentiment), action_required = VALUES(action_required),
                        keywords = VALUES(keywords), extracted_info = VALUES(extracted_info)
                """, (
                    summary_id, call_id, summary_text, category,
                    sentiment, 1 if action_required else 0,
                    json.dumps(all_keywords, ensure_ascii=False),
                    json.dumps(extracted_info, ensure_ascii=False) if extracted_info else None
                ))

                cursor.execute("UPDATE calls SET status = 'summarized' WHERE id = %s", (call_id,))
                print(f"[SUCCESS] call_id={call_id} 키워드={all_keywords} extracted={extracted_info}")

            else:
                error_msg = body.get('message', f'CLOVA STT 실패 (result={result})')
                cursor.execute("""
                    UPDATE calls SET status = 'error', error_message = %s WHERE id = %s
                """, (error_msg, call_id))

        conn.commit()
    finally:
        conn.close()

    return response(200, {'success': True, 'call_id': call_id})


# ══════════════════════════════════════════════════════════
# 7. GET /calls
# ══════════════════════════════════════════════════════════
def get_calls(event, uid):
    params   = event.get('queryStringParameters') or {}
    store_id = params.get('store_id')
    status   = params.get('status')
    limit    = int(params.get('limit', 20))
    offset   = int(params.get('offset', 0))

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE firebase_uid = %s", (uid,))
            user = cursor.fetchone()
            if not user:
                return response(404, {'message': '사용자를 찾을 수 없습니다.'})

            sql         = """
                SELECT c.id, c.store_id, c.caller_number, c.caller_category, c.duration,
                       c.status, c.created_at,
                       s.summary, s.category, s.sentiment, s.action_required, s.is_read,
                       s.extracted_info
                FROM calls c
                JOIN stores st ON c.store_id = st.id
                LEFT JOIN (
                    SELECT s1.*
                    FROM summaries s1
                    INNER JOIN (
                        SELECT call_id, MAX(id) AS max_id
                        FROM summaries
                        GROUP BY call_id
                    ) s2 ON s1.call_id = s2.call_id AND s1.id = s2.max_id
                ) s ON s.call_id = c.id
                WHERE st.owner_id = %s
            """
            params_list = [user['id']]

            if store_id:
                sql += " AND c.store_id = %s"
                params_list.append(store_id)
            if status:
                sql += " AND c.status = %s"
                params_list.append(status)

            sql += " ORDER BY c.created_at DESC LIMIT %s OFFSET %s"
            params_list.extend([limit, offset])

            cursor.execute(sql, params_list)
            calls = cursor.fetchall()

        return response(200, {'calls': calls, 'limit': limit, 'offset': offset})
    finally:
        conn.close()

# ══════════════════════════════════════════════════════════
# 8.7. GET /calls/{id}/audio - 음성 재생용 presigned URL
# ══════════════════════════════════════════════════════════
def get_call_audio(call_id, uid):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT c.s3_key
                FROM calls c
                JOIN users u ON c.user_id = u.id
                WHERE c.id = %s AND u.firebase_uid = %s
            """, (call_id, uid))
            call = cursor.fetchone()

            if not call:
                return response(404, {'message': '통화를 찾을 수 없거나 권한이 없습니다.'})
            if not call['s3_key']:
                return response(404, {'message': '음성 파일이 없습니다.'})

            audio_url = s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': S3_BUCKET, 'Key': call['s3_key']},
                ExpiresIn=600  # 10분
            )

        return response(200, {
            'url': audio_url,
            'audio_url': audio_url,  # 안드로이드 DTO 호환용
            'expires_in': 600
        })
    finally:
        conn.close()

# ══════════════════════════════════════════════════════════
# 8. GET /calls/{id}
# ══════════════════════════════════════════════════════════
def get_call(call_id, uid):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT c.*, s.summary, s.category, s.sentiment,
                       s.action_required, s.is_read, s.keywords, s.extracted_info
                FROM calls c
                JOIN users u ON c.user_id = u.id
                LEFT JOIN summaries s ON s.call_id = c.id
                WHERE c.id = %s AND u.firebase_uid = %s
            """, (call_id, uid))
            call = cursor.fetchone()

        if not call:
            return response(404, {'message': '통화를 찾을 수 없습니다.'})
        return response(200, {'call': call})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════
# 8.5. PATCH /calls/{id} - 분류 변경
# ══════════════════════════════════════════════════════════
def update_call_category(call_id, uid, event):
    body = json.loads(event.get('body', '{}'))
    new_category = body.get('caller_category')
    if new_category not in ['BUSINESS', 'PERSONAL', 'UNCLASSIFIED']:
        return response(400, {'message': '유효하지 않은 분류값입니다. (BUSINESS/PERSONAL/UNCLASSIFIED)'})
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE calls c
                JOIN users u ON c.user_id = u.id
                SET c.caller_category = %s
                WHERE c.id = %s AND u.firebase_uid = %s
            """, (new_category, call_id, uid))
            if cursor.rowcount == 0:
                return response(404, {'message': '통화를 찾을 수 없거나 권한이 없습니다.'})
        conn.commit()
        return response(200, {'success': True, 'call_id': call_id, 'caller_category': new_category})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════
# 8.6. DELETE /calls/{id} - 통화 삭제
# ══════════════════════════════════════════════════════════
def delete_call(call_id, uid):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT c.s3_key FROM calls c
                JOIN users u ON c.user_id = u.id
                WHERE c.id = %s AND u.firebase_uid = %s
            """, (call_id, uid))
            call = cursor.fetchone()
            if not call:
                return response(404, {'message': '통화를 찾을 수 없거나 권한이 없습니다.'})
            cursor.execute("DELETE FROM summaries WHERE call_id = %s", (call_id,))
            cursor.execute("DELETE FROM calls WHERE id = %s", (call_id,))
            try:
                s3_client.delete_object(Bucket=S3_BUCKET, Key=call['s3_key'])
                print(f"[DELETE] S3 객체 삭제 완료: {call['s3_key']}")
            except Exception as e:
                print(f"[DELETE] S3 삭제 실패 (DB는 삭제됨): {e}")
        conn.commit()
        return response(200, {'success': True, 'call_id': call_id})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════
# 9. GET /summaries/{id}
# ══════════════════════════════════════════════════════════
def get_summary(summary_id, uid):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT s.*, c.caller_number, c.duration, c.created_at as call_date
                FROM summaries s
                JOIN calls c ON s.call_id = c.id
                JOIN users u ON c.user_id = u.id
                WHERE s.id = %s AND u.firebase_uid = %s
            """, (summary_id, uid))
            summary = cursor.fetchone()

        if not summary:
            return response(404, {'message': '요약을 찾을 수 없습니다.'})

        conn2 = get_db()
        try:
            with conn2.cursor() as cursor2:
                cursor2.execute("UPDATE summaries SET is_read = 1 WHERE id = %s", (summary_id,))
            conn2.commit()
        finally:
            conn2.close()

        return response(200, {'summary': summary})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════
# 🧹 임시: 기존 데모 데이터 청소 (실행 후 삭제!!)
# ══════════════════════════════════════════════════════════
def demo_clean(uid):
    """uid 사용자의 모든 통화/요약/S3 파일을 삭제."""
    conn = get_db()
    deleted = {'calls': 0, 'summaries': 0, 's3_files': 0, 's3_failed': 0}
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT c.id, c.s3_key FROM calls c
                JOIN users u ON c.user_id = u.id
                WHERE u.firebase_uid = %s
            """, (uid,))
            rows = cursor.fetchall()
            call_ids = [r['id'] for r in rows]
            s3_keys = [r['s3_key'] for r in rows if r['s3_key']]

            print(f"[CLEAN] uid={uid}, 삭제 대상: {len(call_ids)}개 통화, {len(s3_keys)}개 S3 파일")

            if call_ids:
                placeholders = ','.join(['%s'] * len(call_ids))
                cursor.execute(f"DELETE FROM summaries WHERE call_id IN ({placeholders})", call_ids)
                deleted['summaries'] = cursor.rowcount
                cursor.execute(f"DELETE FROM calls WHERE id IN ({placeholders})", call_ids)
                deleted['calls'] = cursor.rowcount

            for key in s3_keys:
                try:
                    s3_client.delete_object(Bucket=S3_BUCKET, Key=key)
                    deleted['s3_files'] += 1
                except Exception as e:
                    print(f"[S3 DELETE FAIL] {key}: {e}")
                    deleted['s3_failed'] += 1

        conn.commit()
    finally:
        conn.close()

    return response(200, {
        'success': True,
        'message': '내 데이터 전체 청소 완료',
        'deleted': deleted
    })


# ══════════════════════════════════════════════════════════
# 🎁 데모용: 가짜 통화 + 요약 데이터 INSERT (발표 후 삭제)
# ══════════════════════════════════════════════════════════
def demo_seed(uid):
    """발표 데모용 가짜 데이터를 DB에 넣음. 한 번 실행하면 됨."""

    DEMO_DATA = [
        {
            'caller_number': '010-1234-5678',
            'duration': 45,
            'stt_result': '[화자1]: 여보세요, 명동 칼국수죠?\n[화자2]: 네 맞습니다.\n[화자1]: 내일 저녁 7시에 4명 예약 가능할까요?\n[화자2]: 네, 가능합니다. 성함이 어떻게 되시나요?\n[화자1]: 김민수입니다. 010-1234-5678이에요.\n[화자2]: 네 김민수님, 내일 저녁 7시 4명 예약 잡아드렸습니다.',
            'summary': '김민수 고객이 내일 저녁 7시에 4명 예약 요청. 예약 확정 완료. 연락처: 010-1234-5678.',
            'category': '예약',
            'sentiment': 'positive',
            'action_required': 0,
            'caller_category': 'BUSINESS',
            'keywords': ['예약', '4명', '저녁 7시', '김민수'],
            'extracted_info': {
                'category_code': 'reservation',
                'customer_name': '김민수',
                'phone': '010-1234-5678',
                'date': '2026-05-08',
                'time': '19:00',
                'party_size': 4,
                'menu': [],
                'special_notes': None
            }
        },
        {
            'caller_number': '010-9876-5432',
            'duration': 60,
            'stt_result': '[화자1]: 사장님, 어제 시킨 짜장면 너무 짜요. 환불 가능할까요?\n[화자2]: 정말 죄송합니다 손님. 어떻게 도와드릴까요?\n[화자1]: 환불보다는 다음에 무료로 한 그릇 드시고 싶어요.\n[화자2]: 네 알겠습니다. 손님 성함과 연락처 알려주시면 다음 방문 때 무료로 처리해드리겠습니다.\n[화자1]: 박지영이고요, 010-9876-5432입니다.',
            'summary': '박지영 고객, 어제 짜장면 짜다고 불만 제기. 환불 대신 다음 방문 시 무료 식사 약속. 메모 필요.',
            'category': '불만',
            'sentiment': 'negative',
            'action_required': 1,
            'caller_category': 'BUSINESS',
            'keywords': ['불만', '환불', '짜다', '무료 식사', '박지영'],
            'extracted_info': {
                'category_code': 'complaint',
                'customer_name': '박지영',
                'phone': '010-9876-5432',
                'date': None,
                'time': None,
                'party_size': None,
                'menu': ['짜장면'],
                'special_notes': '짜장면이 너무 짜다는 컴플레인. 다음 방문 시 무료 식사 제공 약속.'
            }
        },
        {
            'caller_number': '010-2222-3333',
            'duration': 25,
            'stt_result': '[화자1]: 안녕하세요, 거기 영업시간이 어떻게 되나요?\n[화자2]: 저희는 오전 11시부터 밤 10시까지 영업합니다.\n[화자1]: 일요일도 영업하나요?\n[화자2]: 일요일은 쉽니다.\n[화자1]: 네 알겠습니다. 감사합니다.',
            'summary': '영업시간 문의. 평일 11시~22시 운영, 일요일 휴무 안내.',
            'category': '문의',
            'sentiment': 'neutral',
            'action_required': 0,
            'caller_category': 'BUSINESS',
            'keywords': ['영업시간', '일요일 휴무', '문의'],
            'extracted_info': {
                'category_code': 'hours_location',
                'customer_name': None,
                'phone': None,
                'date': None,
                'time': None,
                'party_size': None,
                'menu': [],
                'special_notes': '영업시간 문의 (평일 11~22시, 일요일 휴무)'
            }
        },
        {
            'caller_number': '010-5555-7777',
            'duration': 35,
            'stt_result': '[화자1]: 사장님 칼국수 정말 맛있어요! 친구들한테도 다 추천했어요.\n[화자2]: 와 정말 감사합니다 손님!\n[화자1]: 다음에 또 갈게요. 단골 인증 같은 거 있나요?\n[화자2]: 다음 방문 때 말씀해주시면 서비스 드릴게요.',
            'summary': '단골 고객 칭찬 전화. 칼국수 만족, 지인 추천. 다음 방문 시 서비스 약속.',
            'category': '칭찬',
            'sentiment': 'positive',
            'action_required': 0,
            'caller_category': 'BUSINESS',
            'keywords': ['칭찬', '추천', '단골', '서비스'],
            'extracted_info': {
                'category_code': 'positive',
                'customer_name': None,
                'phone': None,
                'date': None,
                'time': None,
                'party_size': None,
                'menu': ['칼국수'],
                'special_notes': '단골 고객 칭찬 전화. 다음 방문 시 서비스 약속.'
            }
        },
        {
            'caller_number': '010-8888-9999',
            'duration': 50,
            'stt_result': '[화자1]: 여보세요, 어제 예약했던 김철수인데요.\n[화자2]: 네 김철수님 안녕하세요.\n[화자1]: 죄송한데 오늘 예약 취소해야 할 것 같아요. 갑자기 일이 생겼어요.\n[화자2]: 네 알겠습니다. 다음에 또 방문해주세요.\n[화자1]: 네 정말 죄송합니다.',
            'summary': '김철수 고객 예약 취소 요청. 갑작스러운 일정. 다음 방문 안내.',
            'category': '취소',
            'sentiment': 'neutral',
            'action_required': 0,
            'caller_category': 'BUSINESS',
            'keywords': ['예약 취소', '김철수', '일정'],
            'extracted_info': {
                'category_code': 'cancel_refund',
                'customer_name': '김철수',
                'phone': None,
                'date': '2026-05-07',
                'time': None,
                'party_size': None,
                'menu': [],
                'special_notes': '오늘 예약 취소 (갑작스러운 일정 변경)'
            }
        }
    ]

    conn = get_db()
    inserted = []
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT s.id as store_id, u.id as user_id
                FROM stores s JOIN users u ON s.owner_id = u.id
                WHERE u.firebase_uid = %s
                LIMIT 1
            """, (uid,))
            row = cursor.fetchone()
            if not row:
                return response(404, {'message': '먼저 가게를 등록해주세요'})

            store_id = row['store_id']
            user_id  = row['user_id']

            for i, demo in enumerate(DEMO_DATA):
                call_id    = str(uuid.uuid4())
                summary_id = str(uuid.uuid4())
                s3_key     = f"recordings/{store_id}/{call_id}/demo_{i+1}.m4a"

                cursor.execute("""
                    INSERT INTO calls
                        (id, store_id, user_id, caller_number, caller_category, s3_key, duration,
                         status, stt_result, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'summarized', %s,
                            DATE_SUB(NOW(), INTERVAL %s HOUR))
                """, (call_id, store_id, user_id, demo['caller_number'], demo['caller_category'],
                      s3_key, demo['duration'], demo['stt_result'], i * 3))

                cursor.execute("""
                    INSERT INTO summaries
                        (id, call_id, summary, category, sentiment,
                         action_required, keywords, extracted_info, is_read)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0)
                """, (summary_id, call_id, demo['summary'], demo['category'],
                      demo['sentiment'], demo['action_required'],
                      json.dumps(demo['keywords'], ensure_ascii=False),
                      json.dumps(demo['extracted_info'], ensure_ascii=False)))

                inserted.append({'call_id': call_id, 'category': demo['category']})

        conn.commit()
    finally:
        conn.close()

    return response(200, {
        'success': True,
        'message': f'{len(inserted)}개 데모 데이터 추가 완료',
        'inserted': inserted
    })


# ══════════════════════════════════════════════════════════
# 🔧 임시: DB 마이그레이션 (extracted_info 컬럼 추가) - 실행 후 삭제
# ══════════════════════════════════════════════════════════
def migrate_add_extracted_info():
    conn = get_db()
    result = {}
    try:
        with conn.cursor() as cursor:
            cursor.execute("DESCRIBE summaries")
            before = cursor.fetchall()
            existing = [r['Field'] for r in before]
            result['before'] = existing
            
            if 'extracted_info' in existing:
                result['status'] = 'ALREADY_EXISTS'
                result['message'] = 'extracted_info 컬럼이 이미 존재합니다.'
                return response(200, result)
            
            cursor.execute("""
                ALTER TABLE summaries 
                ADD COLUMN extracted_info JSON NULL 
                AFTER keywords
            """)
        conn.commit()
        
        with conn.cursor() as cursor:
            cursor.execute("DESCRIBE summaries")
            after = cursor.fetchall()
            result['after'] = [{'Field': r['Field'], 'Type': r['Type']} for r in after]
        
        result['status'] = 'SUCCESS'
        result['message'] = 'extracted_info JSON 컬럼 추가 완료'
        return response(200, result)
    finally:
        conn.close()

