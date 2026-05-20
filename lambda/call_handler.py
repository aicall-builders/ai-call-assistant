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

    try:
        uid = verify_token(event)

        # ── /stores ──
        if path.endswith('/stores') and method == 'GET':
            return get_stores(uid)

        if path.endswith('/stores') and method == 'POST':
            return create_store(event, uid)

        # ── /calls ──
        if path.endswith('/calls/upload') and method == 'POST':
            return calls_upload(event, uid)

        if path.endswith('/calls') and method == 'GET':
            return get_calls(event, uid)

        if '/calls/' in path and path.endswith('/audio') and method == 'GET':
            call_id = path.split('/calls/')[1].replace('/audio', '')
            return get_call_audio(call_id, uid)

        if '/calls/' in path and path.endswith('/process') and method == 'POST':
            call_id = path.split('/calls/')[1].replace('/process', '')
            return calls_process(call_id, uid)

        if '/calls/' in path and method == 'GET':
            call_id = path.split('/calls/')[1]
            return get_call(call_id, uid)

        if '/calls/' in path and method == 'PATCH':
            call_id = path.split('/calls/')[1]
            return update_call_category(call_id, uid, event)

        if '/calls/' in path and method == 'DELETE':
            call_id = path.split('/calls/')[1]
            return delete_call(call_id, uid)

        # ── /summaries ──
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
# GET /stores
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
# POST /stores
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
# POST /calls/upload
# ══════════════════════════════════════════════════════════
def calls_upload(event, uid):
    body         = json.loads(event.get('body', '{}'))
    store_id     = body.get('store_id')
    file_name    = body.get('file_name')
    mime_type    = body.get('mime_type', 'audio/mp4')
    caller_number   = body.get('counterpart_number') or body.get('caller_number')
    caller_category = body.get('caller_category', 'UNCLASSIFIED')
    duration     = body.get('duration_seconds') or body.get('duration')
    recorded_at  = body.get('recorded_at') or body.get('callStartedAt') or body.get('call_started_at')

    if not all([store_id, file_name]):
        return response(400, {'message': 'store_id, file_name은 필수입니다.'})

    if duration and duration < 5:
        return response(400, {'message': '통화 길이가 너무 짧습니다. (5초 이하)'})

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

            call_id   = str(uuid.uuid4())
            s3_key    = f"recordings/{store_id}/{call_id}/{file_name}"

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
# POST /calls/{id}/process
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

            webhook_url = "https://avrq2kzfp9.execute-api.ap-northeast-2.amazonaws.com/prod/clova/webhook"

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
# GET /calls
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

            sql = """
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
# GET /calls/{id}/audio
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
                ExpiresIn=600
            )

        return response(200, {
            'url': audio_url,
            'audio_url': audio_url,
            'expires_in': 600
        })
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════
# GET /calls/{id}
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
# PATCH /calls/{id}
# ══════════════════════════════════════════════════════════
def update_call_category(call_id, uid, event):
    body = json.loads(event.get('body', '{}'))
    new_category = body.get('caller_category')
    if new_category not in ['BUSINESS', 'PERSONAL', 'UNCLASSIFIED']:
        return response(400, {'message': '유효하지 않은 분류값입니다.'})
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
# DELETE /calls/{id}
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
            except Exception as e:
                print(f"[DELETE] S3 삭제 실패: {e}")
        conn.commit()
        return response(200, {'success': True, 'call_id': call_id})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════
# GET /summaries/{id}
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