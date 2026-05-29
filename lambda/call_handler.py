"""
call_handler.py — S3 업로드 + CLOVA STT 요청 + 폴링 메커니즘
"""
import os
import json
import uuid
import hashlib
import logging
import base64
import boto3
import pymysql
import pymysql.cursors
import requests
from botocore.exceptions import ClientError

from redis_client import set_nx_with_ttl, cache_get, cache_set, TTL_UPLOAD_LOCK, check_rate_limit

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
lambda_client = boto3.client("lambda")
BUCKET_NAME    = os.environ.get("S3_BUCKET", "call-recoder-audio-1017")

CLOVA_API_URL  = os.environ.get("CLOVA_API_URL", "")
CLOVA_SECRET   = os.environ.get("CLOVA_SECRET_KEY", "")

MAX_RETRY      = int(os.environ.get("STT_MAX_RETRY", 3))
STALE_MINUTES  = int(os.environ.get("STT_STALE_MINUTES", 5))

ALLOWED_ORIGINS = [
    "https://dk1k75g0ji3vw.cloudfront.net",
    "http://localhost:3000",
]

def _response(status: int, body: dict, event: dict = {}) -> dict:
    request_origin = (event.get("headers") or {}).get("origin", "")
    cors_origin = request_origin if request_origin in ALLOWED_ORIGINS else ALLOWED_ORIGINS[0]
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": cors_origin,
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
        },
        "body": json.dumps(body, ensure_ascii=False),
    }

def _get_db_password() -> str:
    secret_name = os.environ.get("DB_SECRET_NAME", "")
    if secret_name:
        try:
            sm = boto3.client("secretsmanager", region_name="ap-northeast-2")
            secret = sm.get_secret_value(SecretId=secret_name)
            data = json.loads(secret["SecretString"])
            return data.get("password") or data.get("db_password", "")
        except Exception as e:
            logger.error(f"[DB] Secrets Manager 조회 실패: {e}")
    return os.environ.get("DB_PASSWORD", "")

def get_db():
    config = {
        "host":        os.environ.get("DB_HOST", "call-recorder-db.czem0u8m8xfi.ap-northeast-2.rds.amazonaws.com"),
        "user":        os.environ.get("DB_USER", "admin"),
        "password":    _get_db_password(),
        "db":          os.environ.get("DB_NAME", "call_recorder"),
        "charset":     "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "connect_timeout": 5,
    }
    return pymysql.connect(**config)

def _file_hash(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()

def _upload_lock_key(user_id: str, file_hash: str) -> str:
    return f"upload:lock:{user_id}:{file_hash}"

def check_and_lock_upload(user_id: str, file_bytes: bytes) -> tuple[bool, str]:
    fhash    = _file_hash(file_bytes)
    lock_key = _upload_lock_key(user_id, fhash)
    acquired = set_nx_with_ttl(lock_key, user_id, TTL_UPLOAD_LOCK)
    if not acquired:
        logger.warning(f"[Call] 중복 업로드 감지 user={user_id} hash={fhash[:16]}")
        return True, fhash
    return False, fhash

def upload_to_s3(user_id: str, file_bytes: bytes, filename: str, file_hash: str) -> str:
    ext    = os.path.splitext(filename)[-1].lower() or ".wav"
    s3_key = f"audio/{user_id}/{file_hash[:16]}{ext}"
    s3.put_object(
        Bucket=BUCKET_NAME, Key=s3_key, Body=file_bytes,
        ContentType=_content_type(ext),
        Metadata={"user_id": user_id, "file_hash": file_hash, "original_filename": filename},
    )
    logger.info(f"[Call] S3 업로드 완료 key={s3_key}")
    cache_set(f"upload:result:{user_id}:{file_hash}", {"s3_key": s3_key}, TTL_UPLOAD_LOCK)
    return s3_key

def _content_type(ext: str) -> str:
    return {
        ".wav": "audio/wav", ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4", ".ogg": "audio/ogg",
        ".flac": "audio/flac", ".webm": "audio/webm",
    }.get(ext, "application/octet-stream")

def insert_call(user_id: str, store_id: str, s3_key: str,
                caller_number: str = "", duration: int = 0) -> str:
    call_id = str(uuid.uuid4())
    sql = """
        INSERT INTO calls
            (id, store_id, user_id, caller_number, s3_key, status)
        VALUES
            (%s, %s, %s, %s, %s, 'uploaded')
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (call_id, store_id, user_id, caller_number, s3_key))
        conn.commit()
    logger.info(f"[Call] calls INSERT call_id={call_id}")
    return call_id

def request_clova_stt(call_id: str, s3_key: str) -> str | None:
    presigned_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET_NAME, "Key": s3_key},
        ExpiresIn=3600,
    )
    headers = {
        "Accept":               "application/json",
        "X-CLOVASPEECH-API-KEY": CLOVA_SECRET,
        "Content-Type":         "application/json",
    }
    body = {
        "url":      presigned_url,
        "language": "ko-KR",
        "completion": "sync",      # async → sync: 콜백/OBS 없이 결과 바로 받음
    }
    try:
        resp = requests.post(
            f"{CLOVA_API_URL}/recognizer/url",
            headers=headers, json=body, timeout=120,   # sync는 인식 끝날 때까지 대기
        )
        if not resp.ok:
            logger.error(f"[CLOVA] {resp.status_code} 본문: {resp.text}")
            raise ValueError(f"CLOVA {resp.status_code}: {resp.text}")
        data = resp.json()
        transcript = _extract_transcript(data)
        _update_call_status(call_id, status="transcribed", stt_result=transcript)
        logger.info(f"[CLOVA] STT 완료(sync) call_id={call_id} len={len(transcript)}")
        _invoke_nlp(call_id, transcript)          # 바로 NLP 호출 → 요약 저장
        return call_id
    except Exception as e:
        logger.error(f"[CLOVA] STT 요청 실패 call_id={call_id}: {e}")
        _update_call_status(call_id, status="error", error_message=str(e))
        return None

def check_pending_stt(event=None, context=None):
    logger.info("[Polling] check_pending_stt 시작")
    pending = _query_pending_calls()
    logger.info(f"[Polling] 대상 {len(pending)}건")
    result = {"total": len(pending), "clova_ok": 0, "failed": 0}
    for call in pending:
        call_id     = call["id"]
        retry_count = call["retry_count"] or 0
        clova_job   = call.get("clova_job_id")
        try:
            if retry_count < MAX_RETRY and clova_job:
                ok = _poll_clova(call_id, clova_job, retry_count)
                if ok:
                    result["clova_ok"] += 1
                else:
                    result["failed"] += 1
            else:
                _update_call_status(call_id, status="error",
                                    error_message="CLOVA STT 최대 재시도 초과")
                result["failed"] += 1
        except Exception as e:
            logger.error(f"[Polling] call_id={call_id} 오류: {e}", exc_info=True)
            _update_call_status(call_id, status="error", error_message=str(e))
            result["failed"] += 1
    logger.info(f"[Polling] 완료: {result}")
    _put_metrics(result)
    return {"statusCode": 200, "body": json.dumps(result, ensure_ascii=False)}

def _put_metrics(result: dict) -> None:
    try:
        cloudwatch = boto3.client("cloudwatch")
        cloudwatch.put_metric_data(
            Namespace="CallRecorder/Polling",
            MetricData=[
                {"MetricName": "PollingTotal", "Value": result["total"], "Unit": "Count"},
                {"MetricName": "PollingClovaOk", "Value": result["clova_ok"], "Unit": "Count"},
                {"MetricName": "PollingFailed", "Value": result["failed"], "Unit": "Count"},
            ],
        )
        logger.info("[Metrics] CloudWatch 메트릭 전송 완료")
    except Exception as e:
        logger.error(f"[Metrics] 전송 실패: {e}")

def _query_pending_calls() -> list[dict]:
    sql = """
        SELECT id, clova_job_id, s3_key, retry_count, updated_at
        FROM calls
        WHERE status = 'processing'
        AND updated_at < NOW() - INTERVAL %s MINUTE
        ORDER BY updated_at ASC
        LIMIT 50
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (STALE_MINUTES,))
            return cur.fetchall()

def _poll_clova(call_id: str, job_id: str, retry_count: int) -> bool:
    logger.info(f"[CLOVA] 재조회 call_id={call_id} job_id={job_id} retry={retry_count}")
    try:
        headers = {
            "Accept":                "application/json",
            "X-CLOVASPEECH-API-KEY": CLOVA_SECRET,
        }
        resp = requests.get(
            f"{CLOVA_API_URL}/recognizer/upload/{job_id}",
            headers=headers, timeout=10,
        )
        resp.raise_for_status()
        data   = resp.json()
        status = data.get("status", "").lower()
        if status == "completed":
            transcript = _extract_transcript(data)
            _update_call_status(call_id, status="transcribed", stt_result=transcript)
            logger.info(f"[CLOVA] 완료 call_id={call_id}")
            _invoke_nlp(call_id, transcript)
            return True
        elif status in ("failed", "error"):
            logger.warning(f"[CLOVA] 실패 → 최대 재시도로 전환 call_id={call_id}")
            _increment_retry(call_id, force_max=True)
            return False
        else:
            _increment_retry(call_id, retry_count=retry_count)
            logger.info(f"[CLOVA] 진행중 call_id={call_id} status={status}")
            return False
    except Exception as e:
        logger.error(f"[CLOVA] 재조회 오류 call_id={call_id}: {e}")
        _increment_retry(call_id, retry_count=retry_count)
        return False

def _extract_transcript(data: dict) -> str:
    segments = data.get("segments", [])
    if segments:
        return " ".join(seg.get("text", "") for seg in segments).strip()
    return data.get("text", "")

def _invoke_nlp(call_id: str, transcript: str) -> None:
    try:
        # 통화 정보에서 caller_number 조회
        caller_number = ""
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT caller_number FROM calls WHERE id = %s", (call_id,))
                row = cur.fetchone()
                if row:
                    caller_number = row.get("caller_number", "")

        response = lambda_client.invoke(
            FunctionName="call-recorder-api-nlp",
            InvocationType="RequestResponse",
            Payload=json.dumps({
                "call_id": call_id,
                "transcript": transcript,
                "caller_number": caller_number,
            }).encode(),
        )
        payload = json.loads(response["Payload"].read())
        body = json.loads(payload.get("body", "{}"))
        if body:
            _insert_summary(call_id, body)
        logger.info(f"[NLP] 분석 및 저장 완료 call_id={call_id}")
    except Exception as e:
        logger.error(f"[NLP] invoke 실패 call_id={call_id}: {e}")

def _insert_summary(call_id: str, result: dict) -> None:
    sql = """
        INSERT INTO summaries
            (id, call_id, summary, category, domain, sentiment,
             action_required, keywords, internal_keywords,
             extracted_info, sms_recommended, sms_message)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    internal = result.get("internal", {})
    sms      = result.get("sms", {})

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                str(uuid.uuid4()), call_id,
                internal.get("summary", result.get("summary", "")),
                result.get("category", "기타"),
                result.get("domain", "기타"),
                result.get("sentiment", "neutral"),
                1 if result.get("action_required") else 0,
                json.dumps(result.get("keywords", []), ensure_ascii=False),
                json.dumps(internal.get("keywords", {}), ensure_ascii=False),
                json.dumps(result.get("extracted_info", {}), ensure_ascii=False),
                1 if sms.get("recommended") else 0,
                sms.get("message", ""),
            ))
        conn.commit()
    logger.info(f"[Call] summaries INSERT 완료 call_id={call_id} domain={result.get('domain', '기타')}")

def _update_call_status(call_id: str, *, status: str,
                         clova_job_id: str = None,
                         stt_result: str = None,
                         error_message: str = None) -> None:
    fields = ["status = %s", "updated_at = NOW()"]
    values = [status]
    if clova_job_id is not None:
        fields.append("clova_job_id = %s")
        values.append(clova_job_id)
    if stt_result is not None:
        fields.append("stt_result = %s")
        values.append(stt_result)
    if error_message is not None:
        fields.append("error_message = %s")
        values.append(error_message)
    values.append(call_id)
    sql = f"UPDATE calls SET {', '.join(fields)} WHERE id = %s"
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)
        conn.commit()

def _increment_retry(call_id: str, retry_count: int = 0, force_max: bool = False) -> None:
    new_count = MAX_RETRY if force_max else retry_count + 1
    sql = "UPDATE calls SET retry_count = %s, updated_at = NOW() WHERE id = %s"
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (new_count, call_id))
        conn.commit()

def lambda_handler(event: dict, context) -> dict:
    path   = event.get("path", "")
    method = event.get("httpMethod", "GET")

    if event.get("source") == "aws.events":
        return check_pending_stt(event, context)

    if method == "OPTIONS":
        return _response(200, {}, event)

    if path == "/stores" and method == "GET":
        return _handle_stores_list(event)
    if path == "/stores" and method == "POST":
        return _handle_stores_create(event)
    if path == "/calls" and method == "GET":
        return _handle_calls_list(event)
    if path and path.startswith("/calls/") and path.endswith("/audio") and method == "GET":
        call_id = path.split("/")[2]
        return _handle_call_audio(event, call_id)
    if path and path.startswith("/calls/") and path.endswith("/process") and method == "POST":
        call_id = path.split("/")[2]
        return _handle_call_process(event, call_id)
    if path and path.startswith("/calls/") and method == "GET":
        call_id = path.split("/")[2]
        return _handle_call_get(event, call_id)
    if path and path.startswith("/calls/") and method == "PATCH":
        call_id = path.split("/")[2]
        return _handle_call_patch(event, call_id)
    if path and path.startswith("/calls/") and method == "DELETE":
        call_id = path.split("/")[2]
        return _handle_call_delete(event, call_id)
    if path == "/calls/upload" and method == "POST":
        return _handle_upload(event)
    if path and path.startswith("/calls/") and path.endswith("/sms") and method == "POST":
        call_id = path.split("/")[2]
        return _handle_sms_send(event, call_id)

    return _response(404, {"error": "Not found"}, event)

def _get_uid(event: dict) -> str | None:
    headers = event.get("headers", {}) or {}
    auth = headers.get("Authorization", headers.get("authorization", ""))
    if not auth.startswith("Bearer "):
        return None
    try:
        import base64 as b64
        token = auth[7:]
        parts = token.split(".")
        padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(b64.urlsafe_b64decode(padded))
        firebase_uid = payload.get("user_id") or payload.get("sub")
        if not firebase_uid:
            return None
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM users WHERE firebase_uid = %s LIMIT 1",
                    (firebase_uid,)
                )
                user = cur.fetchone()
        return user["id"] if user else None
    except Exception as e:
        logger.error(f"[Call] _get_uid 오류: {e}")
        return None

def _handle_stores_list(event: dict) -> dict:
    uid = _get_uid(event)
    if not uid:
        return _response(401, {"error": "인증 필요"}, event)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, owner_id, created_at FROM stores WHERE owner_id = %s",
                    (uid,)
                )
                stores = cur.fetchall()
        result = [{k: str(v) if hasattr(v, "isoformat") else v for k, v in s.items()} for s in stores]
        return _response(200, {"stores": result}, event)
    except Exception as e:
        logger.exception(f"[Store] list 오류: {e}")
        return _response(500, {"error": "내부 오류"}, event)

def _handle_stores_create(event: dict) -> dict:
    uid = _get_uid(event)
    if not uid:
        return _response(401, {"error": "인증 필요"}, event)
    try:
        body = json.loads(event.get("body") or "{}")
        name = body.get("name", "").strip()
        if not name:
            return _response(400, {"error": "name 필수"}, event)
        store_id = str(uuid.uuid4())
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO stores (id, name, owner_id) VALUES (%s, %s, %s)",
                    (store_id, name, uid)
                )
            conn.commit()
        return _response(201, {"id": store_id, "name": name, "owner_id": uid}, event)
    except Exception as e:
        logger.exception(f"[Store] create 오류: {e}")
        return _response(500, {"error": "내부 오류"}, event)

def _handle_calls_list(event: dict) -> dict:
    uid = _get_uid(event)
    if not uid:
        return _response(401, {"error": "인증 필요"}, event)

    # Rate Limiting — 조회는 1분에 60회 제한
    allowed, _ = check_rate_limit(uid, "api")
    if not allowed:
        return _response(429, {"error": "요청 한도 초과. 잠시 후 다시 시도해주세요."}, event)

    try:
        params = event.get("queryStringParameters") or {}
        store_id = params.get("store_id")
        status   = params.get("status")
        limit    = int(params.get("limit", 20))
        offset   = int(params.get("offset", 0))
        sql = """
            SELECT c.*,
                   s.summary, s.category, s.sentiment,
                   s.action_required, s.keywords, s.extracted_info
            FROM calls c
            LEFT JOIN summaries s ON s.call_id = c.id
            WHERE c.user_id = %s
        """
        values = [uid]
        if store_id:
            sql += " AND c.store_id = %s"
            values.append(store_id)
        if status:
            sql += " AND c.status = %s"
            values.append(status)
        sql += " ORDER BY c.created_at DESC LIMIT %s OFFSET %s"
        values += [limit, offset]
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, values)
                calls = cur.fetchall()
        result = [{k: str(v) if hasattr(v, "isoformat") else v for k, v in c.items()} for c in calls]
        return _response(200, {"calls": result}, event)
    except Exception as e:
        logger.exception(f"[Call] list 오류: {e}")
        return _response(500, {"error": "내부 오류"}, event)

def _handle_call_get(event: dict, call_id: str) -> dict:
    uid = _get_uid(event)
    if not uid:
        return _response(401, {"error": "인증 필요"}, event)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM calls WHERE id = %s AND user_id = %s", (call_id, uid))
                call = cur.fetchone()
        if not call:
            return _response(404, {"error": "통화를 찾을 수 없습니다"}, event)
        result = {k: str(v) if hasattr(v, "isoformat") else v for k, v in call.items()}
        return _response(200, {"call": result}, event)
    except Exception as e:
        logger.exception(f"[Call] get 오류: {e}")
        return _response(500, {"error": "내부 오류"}, event)

def _handle_call_patch(event: dict, call_id: str) -> dict:
    uid = _get_uid(event)
    if not uid:
        return _response(401, {"error": "인증 필요"}, event)
    try:
        body = json.loads(event.get("body") or "{}")
        caller_category = body.get("caller_category", "").strip()
        if caller_category not in ("BUSINESS", "PERSONAL", "UNCLASSIFIED"):
            return _response(400, {"error": "유효하지 않은 category"}, event)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE calls SET caller_category = %s WHERE id = %s AND user_id = %s",
                    (caller_category, call_id, uid)
                )
            conn.commit()
        return _response(200, {"message": "업데이트 완료"}, event)
    except Exception as e:
        logger.exception(f"[Call] patch 오류: {e}")
        return _response(500, {"error": "내부 오류"}, event)

def _handle_call_delete(event: dict, call_id: str) -> dict:
    uid = _get_uid(event)
    if not uid:
        return _response(401, {"error": "인증 필요"}, event)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM calls WHERE id = %s AND user_id = %s", (call_id, uid))
            conn.commit()
        return _response(200, {"message": "삭제 완료"}, event)
    except Exception as e:
        logger.exception(f"[Call] delete 오류: {e}")
        return _response(500, {"error": "내부 오류"}, event)

def _handle_call_audio(event: dict, call_id: str) -> dict:
    uid = _get_uid(event)
    if not uid:
        return _response(401, {"error": "인증 필요"}, event)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT s3_key FROM calls WHERE id = %s AND user_id = %s", (call_id, uid))
                call = cur.fetchone()
        if not call:
            return _response(404, {"error": "통화를 찾을 수 없습니다"}, event)
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET_NAME, "Key": call["s3_key"]},
            ExpiresIn=600,
        )
        return _response(200, {"url": url}, event)
    except Exception as e:
        logger.exception(f"[Call] audio 오류: {e}")
        return _response(500, {"error": "내부 오류"}, event)

def _handle_call_process(event: dict, call_id: str) -> dict:
    uid = _get_uid(event)
    if not uid:
        return _response(401, {"error": "인증 필요"}, event)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT s3_key FROM calls WHERE id = %s AND user_id = %s", (call_id, uid))
                call = cur.fetchone()
        if not call:
            return _response(404, {"error": "통화를 찾을 수 없습니다"}, event)
        job_id = request_clova_stt(call_id, call["s3_key"])
        return _response(200, {"message": "STT 처리 시작", "clova_job_id": job_id}, event)
    except Exception as e:
        logger.exception(f"[Call] process 오류: {e}")
        return _response(500, {"error": "내부 오류"}, event)

def _handle_sms_send(event: dict, call_id: str) -> dict:
    """
    POST /calls/{callId}/sms
    소상공인이 직접 문자 발송 선택
    body: { "message": "수정된 문자 내용" } (선택사항 — 없으면 AI 생성 메시지 사용)
    """
    uid = _get_uid(event)
    if not uid:
        return _response(401, {"error": "인증 필요"}, event)

    try:
        # 통화 정보 조회
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT c.caller_number, s.sms_message
                    FROM calls c
                    LEFT JOIN summaries s ON s.call_id = c.id
                    WHERE c.id = %s AND c.user_id = %s
                """, (call_id, uid))
                row = cur.fetchone()

        if not row:
            return _response(404, {"error": "통화를 찾을 수 없습니다"}, event)

        caller_number = row.get("caller_number", "")
        if not caller_number:
            return _response(400, {"error": "발신번호가 없습니다"}, event)

        # 사용자가 수정한 메시지 or AI 생성 메시지 사용
        body    = json.loads(event.get("body") or "{}")
        message = body.get("message", "").strip() or row.get("sms_message", "")

        if not message:
            return _response(400, {"error": "문자 내용이 없습니다"}, event)

        # 솔라피 발송
        from sms_handler import send_sms
        success = send_sms(caller_number, message)

        if success:
            logger.info(f"[SMS] 발송 완료 call_id={call_id} to={caller_number}")
            return _response(200, {"message": "문자 발송 완료"}, event)
        else:
            return _response(500, {"error": "문자 발송 실패"}, event)

    except Exception as e:
        logger.exception(f"[SMS] 발송 오류 call_id={call_id}: {e}")
        return _response(500, {"error": "내부 오류"}, event)


def _handle_upload(event: dict) -> dict:
    uid = _get_uid(event)
    if not uid:
        return _response(401, {"error": "인증 필요"}, event)

    # Rate Limiting — 업로드는 1분에 10회 제한
    allowed, remaining = check_rate_limit(uid, "upload")
    if not allowed:
        return _response(429, {"error": "요청 한도 초과. 잠시 후 다시 시도해주세요."}, event)

    try:
        body        = json.loads(event.get("body") or "{}")
        store_id    = body.get("store_id", "").strip()
        file_name   = body.get("file_name", "recording.m4a").strip()
        mime_type   = body.get("mime_type", "audio/mp4").strip()
        if not store_id:
            return _response(400, {"error": "store_id 필수"}, event)
        call_id = str(uuid.uuid4())
        s3_key  = f"recordings/{store_id}/{call_id}/{file_name}"
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": BUCKET_NAME,
                "Key": s3_key,
                "ContentType": mime_type,
            },
            ExpiresIn=600,
        )
        sql = """
            INSERT INTO calls (id, store_id, user_id, s3_key, status)
            VALUES (%s, %s, %s, %s, 'uploaded')
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (call_id, store_id, uid, s3_key))
            conn.commit()
        return _response(200, {
            "call_id": call_id,
            "upload_url": upload_url,
            "s3_key": s3_key,
        }, event)
    except Exception as e:
        logger.exception(f"[Call] upload 오류: {e}")
        return _response(500, {"error": "내부 오류"}, event)