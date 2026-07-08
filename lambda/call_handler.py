# deploy trigger v2

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

from redis_client import set_nx_with_ttl, cache_get, cache_set, cache_delete, TTL_UPLOAD_LOCK, check_rate_limit

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
lambda_client = boto3.client("lambda")
S3_BUCKET_NAME = os.environ.get("S3_BUCKET", "call-recoder-audio-1017")

CLOVA_SPEECH_INVOKE_URL = os.environ.get("CLOVA_SPEECH_INVOKE_URL") or os.environ.get("CLOVA_INVOKE_URL") or os.environ.get("CLOVA_API_URL", "")
CLOVA_SPEECH_SECRET_KEY = os.environ.get("CLOVA_SPEECH_SECRET_KEY") or os.environ.get("CLOVA_SECRET_KEY", "")

STT_MAX_RETRY_COUNT = int(os.environ.get("STT_MAX_RETRY", 3))
STT_STALE_MINUTES = int(os.environ.get("STT_STALE_MINUTES", 5))

CUSTOM_KEYWORDS_CACHE_PREFIX = "nlp:custom_keywords:store"
CUSTOM_KEYWORD_MAX_LENGTH = 50
CUSTOM_KEYWORD_BLOCKLIST = {
    "네", "예", "아니요", "아니", "저기", "그럼", "음", "어", "고객", "전화", "통화",
    "문의", "내용", "확인", "요청", "사장님", "손님", "안녕하세요",
}


def _get_db_password() -> str:
    secret_id = os.environ.get("DB_SECRET_ARN") or os.environ.get("DB_SECRET_NAME") or ""
    if secret_id:
        try:
            sm = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "ap-northeast-2"))
            secret = sm.get_secret_value(SecretId=secret_id)
            data = json.loads(secret.get("SecretString") or "{}")
            return data.get("password") or data.get("db_password") or data.get("PASSWORD") or ""
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


# ── 중복 업로드 체크 ──────────────────────────────────────────────────────────

def _mask_phone(phone) -> str:
    """전화번호 마스킹 (로그 PII 보호). 01012345678 -> 010****5678"""
    p = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(p) < 8:
        return "***"
    return p[:3] + "****" + p[-4:]


def _normalize_phone_value(phone) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


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


# ── S3 업로드 ─────────────────────────────────────────────────────────────────

def upload_to_s3(user_id: str, file_bytes: bytes, filename: str, file_hash: str) -> str:
    ext    = os.path.splitext(filename)[-1].lower() or ".wav"
    s3_key = f"audio/{user_id}/{file_hash[:16]}{ext}"
    s3.put_object(
        Bucket=S3_BUCKET_NAME, Key=s3_key, Body=file_bytes,
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


# ── calls 테이블 INSERT ───────────────────────────────────────────────────────

def insert_call(user_id: str, store_id: str, s3_key: str,
                caller_number: str = "", duration: int = 0,
                direction: str = "unknown") -> str:
    call_id = str(uuid.uuid4())
    sql = """
        INSERT INTO calls
            (id, store_id, user_id, caller_number, s3_key, status, direction)
        VALUES
            (%s, %s, %s, %s, %s, 'uploaded', %s)
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (call_id, store_id, user_id, caller_number, s3_key, direction or 'unknown'))
        conn.commit()

    if caller_number:
        try:
            import customer_handler
            customer_handler.ensure_schema()
            customer_handler._upsert_profile(user_id, caller_number, consent_status="pending")
        except Exception as e:
            logger.warning(f"[Customer] profile upsert skipped call_id={call_id} error={e}")

    logger.info(f"[Call] calls INSERT call_id={call_id}")
    return call_id


# ── CLOVA STT 동기 요청 ───────────────────────────────────────────────────────

def _extract_clova_job_id(data: dict) -> str:
    if not isinstance(data, dict):
        return ""

    for key in ("token", "job_id", "jobId", "id", "requestId", "taskId"):
        value = data.get(key)
        if value:
            return str(value)

    for parent_key in ("result", "data", "job", "task"):
        parent = data.get(parent_key)
        if isinstance(parent, dict):
            for key in ("token", "job_id", "jobId", "id", "requestId", "taskId"):
                value = parent.get(key)
                if value:
                    return str(value)

    return ""


def _s3_object_size(s3_key: str) -> int:
    try:
        head = s3.head_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        return int(head.get("ContentLength") or 0)
    except Exception as e:
        logger.warning(f"[CLOVA] S3 size check skipped key={s3_key}: {e}")
        return 0


def _request_clova_sync(call_id: str, presigned_url: str) -> str | None:
    headers = {
        "Accept": "application/json",
        "X-CLOVASPEECH-API-KEY": CLOVA_SPEECH_SECRET_KEY,
        "Content-Type": "application/json",
    }

    body = {
        "url": presigned_url,
        "language": "ko-KR",
        "completion": "sync",
        "diarization": {"enable": True},
    }

    resp = requests.post(
        f"{CLOVA_SPEECH_INVOKE_URL}/recognizer/url",
        headers=headers,
        json=body,
        timeout=120,
    )

    if not resp.ok:
        logger.warning(f"[CLOVA] sync 실패 {resp.status_code}: {resp.text[:1000]}")
        return None

    data = resp.json()
    transcript = (_extract_transcript(data) or "").strip()

    if not transcript:
        logger.warning(
            f"[CLOVA] sync 결과 없음 call_id={call_id} "
            f"body={json.dumps(data, ensure_ascii=False)[:1000]}"
        )
        return None

    _update_call_status(call_id, status="transcribed", stt_result=transcript)
    logger.info(f"[CLOVA] STT 완료(sync) call_id={call_id} len={len(transcript)}")
    _invoke_nlp(call_id, transcript)
    return call_id


def _request_clova_async(call_id: str, presigned_url: str) -> str | None:
    headers = {
        "Accept": "application/json",
        "X-CLOVASPEECH-API-KEY": CLOVA_SPEECH_SECRET_KEY,
        "Content-Type": "application/json",
    }

    body = {
        "url": presigned_url,
        "language": "ko-KR",
        "completion": "async",
        "diarization": {"enable": True},
    }

    resp = requests.post(
        f"{CLOVA_SPEECH_INVOKE_URL}/recognizer/url",
        headers=headers,
        json=body,
        timeout=30,
    )

    if not resp.ok:
        logger.error(f"[CLOVA] async 요청 실패 {resp.status_code}: {resp.text[:1000]}")
        raise ValueError(f"CLOVA async {resp.status_code}: {resp.text[:500]}")

    data = resp.json()

    transcript = (_extract_transcript(data) or "").strip()
    if transcript:
        _update_call_status(call_id, status="transcribed", stt_result=transcript)
        logger.info(f"[CLOVA] STT 완료(immediate) call_id={call_id} len={len(transcript)}")
        _invoke_nlp(call_id, transcript)
        return call_id

    job_id = _extract_clova_job_id(data)
    logger.info(
        f"[CLOVA] async 응답 call_id={call_id} "
        f"job_id={job_id or '-'} body={json.dumps(data, ensure_ascii=False)[:1500]}"
    )

    if not job_id:
        _update_call_status(call_id, status="error", error_message="CLOVA async job_id 없음")
        return None

    _update_call_status(call_id, status="processing", clova_job_id=job_id)
    logger.info(f"[CLOVA] async 접수 call_id={call_id} job_id={job_id}")
    return job_id


def request_clova_stt(call_id: str, s3_key: str) -> str | None:
    """
    작은 파일은 sync, 큰 파일은 async.
    STT_SYNC_MAX_BYTES 이하만 sync 시도. 기본 1.2MB.
    """
    presigned_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET_NAME, "Key": s3_key},
        ExpiresIn=3600,
    )

    try:
        size = _s3_object_size(s3_key)
        sync_max = int(os.environ.get("STT_SYNC_MAX_BYTES", "1200000"))

        if size and size <= sync_max:
            logger.info(f"[CLOVA] sync 시도 call_id={call_id} size={size}")
            sync_result = _request_clova_sync(call_id, presigned_url)
            if sync_result:
                return sync_result
            logger.warning(f"[CLOVA] sync 실패/빈결과 → async fallback call_id={call_id}")

        logger.info(f"[CLOVA] async 시도 call_id={call_id} size={size}")
        return _request_clova_async(call_id, presigned_url)

    except Exception as e:
        logger.error(f"[CLOVA] STT 요청 오류 call_id={call_id}: {e}", exc_info=True)
        _update_call_status(call_id, status="error", error_message=str(e))
        return None



# ════════════════════════════════════════════════════════════
# 폴링 메커니즘 — EventBridge 5분 주기로 호출
# ════════════════════════════════════════════════════════════

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
            if retry_count < STT_MAX_RETRY_COUNT and clova_job:
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
                {"MetricName": "PollingTotal",   "Value": result["total"],    "Unit": "Count"},
                {"MetricName": "PollingClovaOk", "Value": result["clova_ok"], "Unit": "Count"},
                {"MetricName": "PollingFailed",  "Value": result["failed"],   "Unit": "Count"},
            ],
        )
        logger.info("[Metrics] CloudWatch 메트릭 전송 완료")
    except Exception as e:
        logger.error(f"[Metrics] 전송 실패: {e}")


def _query_pending_calls() -> list[dict]:
    sql = """
        SELECT id, clova_job_id, s3_key, retry_count, updated_at
        FROM calls
        WHERE
            status = 'processing'
            AND updated_at < NOW() - INTERVAL %s MINUTE
        ORDER BY updated_at ASC
        LIMIT 50
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (STT_STALE_MINUTES,))
            return cur.fetchall()


# ── CLOVA 재조회 ──────────────────────────────────────────────────────────────

def _poll_clova(call_id: str, job_id: str, retry_count: int) -> bool:
    logger.info(f"[CLOVA] 재조회 call_id={call_id} job_id={job_id} retry={retry_count}")

    headers = {
        "Accept": "application/json",
        "X-CLOVASPEECH-API-KEY": CLOVA_SPEECH_SECRET_KEY,
    }

    poll_urls = [
        f"{CLOVA_SPEECH_INVOKE_URL}/recognizer/upload/{job_id}",
        f"{CLOVA_SPEECH_INVOKE_URL}/recognizer/url/{job_id}",
        f"{CLOVA_SPEECH_INVOKE_URL}/recognizer/{job_id}",
    ]

    last_error = ""

    for url in poll_urls:
        try:
            resp = requests.get(url, headers=headers, timeout=20)

            if resp.status_code in (404, 405):
                last_error = f"{resp.status_code} {url}"
                continue

            if not resp.ok:
                last_error = f"{resp.status_code}: {resp.text[:500]}"
                logger.warning(f"[CLOVA] polling 실패 후보 url={url} body={resp.text[:500]}")
                continue

            data = resp.json()
            transcript = (_extract_transcript(data) or "").strip()

            if transcript:
                _update_call_status(call_id, status="transcribed", stt_result=transcript)
                logger.info(f"[CLOVA] 완료 call_id={call_id} len={len(transcript)}")
                _invoke_nlp(call_id, transcript)
                return True

            status = str(
                data.get("status")
                or data.get("result")
                or data.get("state")
                or ""
            ).lower()

            if status in ("failed", "error", "fail"):
                logger.warning(
                    f"[CLOVA] 실패 call_id={call_id} "
                    f"body={json.dumps(data, ensure_ascii=False)[:1000]}"
                )
                _update_call_status(call_id, status="error", error_message=f"CLOVA failed: {status}")
                _increment_retry(call_id, force_max=True)
                return False

            logger.info(
                f"[CLOVA] 진행중 call_id={call_id} status={status or 'unknown'} "
                f"body={json.dumps(data, ensure_ascii=False)[:800]}"
            )
            _increment_retry(call_id, retry_count=retry_count)
            return False

        except Exception as e:
            last_error = str(e)
            logger.warning(f"[CLOVA] polling 후보 실패 url={url} error={e}")

    logger.warning(f"[CLOVA] 재조회 endpoint 미확정 call_id={call_id} last={last_error}")
    _increment_retry(call_id, retry_count=retry_count)
    return False



def _extract_transcript(data: dict) -> str:
    segments = data.get("segments", [])
    if not segments:
        return data.get("text", "")

    lines = []
    current_speaker = None
    current_texts = []

    for seg in segments:
        speaker = seg.get("speaker", {})
        label = speaker.get("label") if isinstance(speaker, dict) else None
        text = seg.get("text", "").strip()
        if not text:
            continue
        if label:
            if label != current_speaker:
                if current_texts:
                    lines.append(f"[화자{current_speaker}]: {' '.join(current_texts)}")
                    current_texts = []
                current_speaker = label
            current_texts.append(text)
        else:
            lines.append(text)

    if current_texts and current_speaker:
        lines.append(f"[화자{current_speaker}]: {' '.join(current_texts)}")

    return '\n'.join(lines) if lines else data.get("text", "")


def _invoke_nlp(call_id: str, transcript: str) -> None:
    try:
        caller_number = ""
        store_id = ""
        user_id = ""
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT caller_number, store_id, user_id FROM calls WHERE id = %s",
                    (call_id,),
                )
                row = cur.fetchone()
                if row:
                    caller_number = row.get("caller_number", "") or ""
                    store_id = row.get("store_id", "") or ""
                    user_id = row.get("user_id", "") or ""

        response = lambda_client.invoke(
            FunctionName="call-recorder-api-nlp",
            InvocationType="RequestResponse",
            Payload=json.dumps({
                "call_id":       call_id,
                "transcript":    transcript,
                "caller_number": caller_number,
                "store_id":      store_id,
                "user_id":       user_id,
            }).encode(),
        )
        payload = json.loads(response["Payload"].read())
        body = json.loads(payload.get("body", "{}"))
        if body:
            _insert_summary(call_id, body)
            phone = (body.get("customer", {}).get("phone") or "").strip()
            if phone:
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE calls SET caller_number = %s WHERE id = %s", (phone, call_id))
                    conn.commit()
                try:
                    import customer_handler
                    if user_id:
                        customer_handler.ensure_schema()
                        customer_handler._upsert_profile(user_id, phone, consent_status="pending")
                except Exception as e:
                    logger.warning(f"[Customer] NLP profile upsert skipped call_id={call_id} error={e}")
            _upsert_caller_stats(call_id) 
            try:
                import customer_handler
                if user_id and phone:
                    customer_handler._refresh_customer_analysis(user_id, phone, reason="call_summary")
            except Exception as e:
                logger.warning(f"[CustomerAnalysis] refresh skipped call_id={call_id} error={e}")
            _update_call_status(call_id, status="completed")
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

    # summary 안전 처리 — 항상 문자열로
    raw_summary = internal.get("summary") or result.get("summary") or ""
    if isinstance(raw_summary, dict):
        summary_str = raw_summary.get("text") or raw_summary.get("label") or raw_summary.get("content") or str(raw_summary)
    elif isinstance(raw_summary, list):
        summary_str = " ".join(str(s) for s in raw_summary)
    else:
        summary_str = str(raw_summary) if raw_summary else ""

    # extracted_info에서 내부 디버그 필드 제거 (_로 시작하는 키)
    raw_extracted = result.get("extracted_info", {})
    if isinstance(raw_extracted, dict):
        clean_extracted = {k: v for k, v in raw_extracted.items() if not k.startswith("_")}
    else:
        clean_extracted = {}

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                str(uuid.uuid4()), call_id,                
                summary_str,
                result.get("category", "기타"),
                result.get("domain", "기타"),
                result.get("sentiment", "neutral"),
                1 if result.get("action_required") else 0,
                json.dumps(result.get("keywords", []), ensure_ascii=False),
                json.dumps(internal.get("keywords", {}), ensure_ascii=False),
                json.dumps(clean_extracted, ensure_ascii=False),
                1 if sms.get("recommended") else 0,
                sms.get("message", ""),
            ))
        conn.commit()
    logger.info(f"[Call] summaries INSERT 완료 call_id={call_id} domain={result.get('domain', '기타')}")

    try:
        import customer_handler
        linked = customer_handler.link_call_to_customer_from_analysis(call_id, clean_extracted, result)
        if linked:
            _upsert_caller_stats(call_id)
    except Exception as e:
        logger.warning(f"[CustomerLink] skipped call_id={call_id}: {e}")


def _normalize_phone(raw: str | None) -> str:
    if not raw:
        return ""
    return "".join(ch for ch in str(raw) if ch.isdigit())




# ── DB 업데이트 헬퍼 ──────────────────────────────────────────────────────────

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
        if stt_result == "":
            values.append(None)
        elif isinstance(stt_result, (dict, list)):
            values.append(json.dumps(stt_result, ensure_ascii=False))
        else:
            text = str(stt_result)
            if not text:
                values.append(None)
            else:
                try:
                    parsed = json.loads(text)
                    values.append(json.dumps(parsed, ensure_ascii=False))
                except Exception:
                    values.append(json.dumps({"text": text}, ensure_ascii=False))
    if error_message is not None:
        fields.append("error_message = %s")
        values.append(error_message)
    elif status != "error":
        fields.append("error_message = NULL")

    values.append(call_id)
    sql = f"UPDATE calls SET {', '.join(fields)} WHERE id = %s"

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)
        conn.commit()


def _increment_retry(call_id: str, retry_count: int = 0, force_max: bool = False) -> None:
    new_count = STT_MAX_RETRY_COUNT if force_max else retry_count + 1
    sql = "UPDATE calls SET retry_count = %s, updated_at = NOW() WHERE id = %s"
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (new_count, call_id))
        conn.commit()


# ── 라우팅 헬퍼 ───────────────────────────────────────────────────────────────

def _normalize_path(event: dict) -> str:
    path = event.get("rawPath") or event.get("path") or "/"
    stage = (event.get("requestContext") or {}).get("stage")
    if stage and path.startswith(f"/{stage}/"):
        path = path[len(stage) + 1:]
    elif stage and path == f"/{stage}":
        path = "/"
    # 앞에 / 없으면 추가
    if not path.startswith("/"):
        path = "/" + path
    logger.info(f"[Route] path={path} stage={stage}")
    return path or "/"


def _method(event: dict) -> str:
    return (
        event.get("httpMethod")
        or (event.get("requestContext") or {}).get("http", {}).get("method")
        or "GET"
    ).upper()


def _event_with_path(event: dict, path: str, method: str) -> dict:
    copied = dict(event)
    copied["path"] = path
    copied["rawPath"] = path
    copied["httpMethod"] = method
    return copied



def _s3_object_exists(s3_key: str) -> bool:
    try:
        s3.head_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        return True
    except Exception:
        return False


def _process_consented_calls(event: dict) -> dict:
    """
    동의 완료 고객의 기존 uploaded 통화 자동 처리.
    - 동의 상태 확인
    - S3 파일 존재 확인
    - status=uploaded 인 항목만 원자적으로 processing 전환
    - 중복 실행 방지
    """
    uid = event.get("user_id") or ""
    phone = _normalize_phone_value(event.get("phone") or "")
    try:
        limit = max(1, min(int(event.get("limit") or 3), 5))
    except Exception:
        limit = 3

    result = {"phone": phone, "checked": 0, "processed": 0, "skipped": 0, "failed": 0}

    if not uid or not phone:
        return {"statusCode": 400, "body": json.dumps({"error": "user_id/phone 필수"}, ensure_ascii=False)}

    try:
        import customer_handler
        if not customer_handler._is_consented(uid, phone):
            return {"statusCode": 200, "body": json.dumps({**result, "message": "동의 상태 아님"}, ensure_ascii=False)}
    except Exception as e:
        logger.warning(f"[ConsentProcess] consent check failed uid={uid} phone={_mask_phone(phone)} error={e}")
        return {"statusCode": 200, "body": json.dumps({**result, "message": "동의 확인 실패"}, ensure_ascii=False)}

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, s3_key, caller_number, status
                FROM calls
                WHERE user_id=%s
                  AND status IN ('uploaded', 'consent_required')
                  AND caller_number IS NOT NULL
                  AND caller_number <> ''
                ORDER BY created_at ASC
                LIMIT 100
            """, (uid,))
            rows = cur.fetchall() or []

    for row in rows:
        if result["processed"] >= limit:
            break

        result["checked"] += 1
        call_id = row.get("id")
        s3_key = row.get("s3_key") or ""
        row_phone = _normalize_phone_value(row.get("caller_number") or "")

        if row_phone != phone:
            result["skipped"] += 1
            continue

        if not s3_key or not _s3_object_exists(s3_key):
            result["skipped"] += 1
            continue

        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE calls
                        SET status='processing', updated_at=NOW()
                        WHERE id=%s
                          AND user_id=%s
                          AND status IN ('uploaded', 'consent_required')
                    """, (call_id, uid))
                    changed = cur.rowcount
                conn.commit()

            if not changed:
                result["skipped"] += 1
                continue

            request_clova_stt(call_id, s3_key)
            result["processed"] += 1
        except Exception as e:
            logger.warning(f"[ConsentProcess] call process failed call_id={call_id} error={e}")
            result["failed"] += 1

    return {"statusCode": 200, "body": json.dumps(result, ensure_ascii=False)}


def _column_exists(cur, table_name: str, column_name: str) -> bool:
    cur.execute("""
        SELECT COUNT(*) AS cnt
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = %s
          AND column_name = %s
    """, (table_name, column_name))
    return int((cur.fetchone() or {}).get("cnt", 0)) > 0


def _migrate_missing_upload_columns() -> dict:
    try:
        changed = []

        with get_db() as conn:
            with conn.cursor() as cur:
                if not _column_exists(cur, "summaries", "id"):
                    cur.execute("ALTER TABLE summaries ADD COLUMN id VARCHAR(36) NULL FIRST")
                    cur.execute("UPDATE summaries SET id = UUID() WHERE id IS NULL OR id = ''")
                    changed.append("summaries.id")

                if not _column_exists(cur, "calls", "caller_name"):
                    cur.execute("ALTER TABLE calls ADD COLUMN caller_name VARCHAR(255) NULL")
                    changed.append("calls.caller_name")

                if not _column_exists(cur, "calls", "caller_category"):
                    cur.execute("ALTER TABLE calls ADD COLUMN caller_category VARCHAR(32) NULL DEFAULT 'UNCLASSIFIED'")
                    changed.append("calls.caller_category")

            conn.commit()

        return _response(200, {
            "message": "missing upload columns migrated",
            "changed": changed,
        })

    except Exception as e:
        logger.exception(f"[Migrate] missing upload columns 오류: {e}")
        return _response(500, {"error": str(e)})


# ── Lambda 핸들러 ─────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    if event.get("action") == "migrate_missing_upload_columns":
        return _migrate_missing_upload_columns()
    if event.get("action") == "clean_extracted_info":
        return _clean_extracted_info()
    if event.get("action") == "migrate_caller_stats": 
        return _migrate_caller_stats()
    if event.get("action") == "migrate_custom_keywords":
        return _migrate_custom_keywords()
    if event.get("action") == "migrate_user_domain":
        return _handle_migrate_user_domain(event)
    if event.get("action") == "migrate_customer_profiles":
        return _migrate_customer_profiles()
    if event.get("action") == "generate_customer_analysis":
        return _run_customer_analysis_batch()
    if event.get("action") == "process_consented_calls":
        return _process_consented_calls(event)

    
    path   = _normalize_path(event)
    method = _method(event)

    if event.get("source") == "aws.events":
        # EventBridge 규칙별 분기: detail.job == "customer_analysis" 면 분석 배치,
        # 아니면 기존 STT 폴링.
        detail = event.get("detail") or {}
        if detail.get("job") == "customer_analysis":
            return _run_customer_analysis_batch()
        return check_pending_stt(event, context)

    if method == "OPTIONS":
        return _response(200, {"message": "OK"}, event)

    routed_event = _event_with_path(event, path, method)

    # customer consent/history routes
    if path.startswith("/consent/") or path.startswith("/customers/"):
        import customer_handler
        return customer_handler.lambda_handler(routed_event, context)

    # auth 라우트
    if path.startswith("/auth/"):
        import auth_handler
        return auth_handler.lambda_handler(routed_event, context)

    # calendar 라우트
    if path.startswith("/calendar/") or (path.startswith("/calls/") and path.endswith("/calendar-events")):
        import calendar_handler
        return calendar_handler.lambda_handler(routed_event, context)
    
    if path.startswith("/calls/") and (path.endswith("/note") or "/photos" in path):
        import notes_handler
        return notes_handler.lambda_handler(routed_event, context)

    # stores
    if path == "/stores" and method == "GET":
        return _handle_stores_list(routed_event)
    if path == "/stores" and method == "POST":
        return _handle_stores_create(routed_event)

    if path.startswith("/stores/") and "/keywords" in path:
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "stores" and parts[2] == "keywords":
            store_id = parts[1]
            if method == "GET":
                return _handle_custom_keywords_list(routed_event, store_id)
            if method == "POST":
                return _handle_custom_keywords_create(routed_event, store_id)
        if len(parts) == 4 and parts[0] == "stores" and parts[2] == "keywords":
            store_id = parts[1]
            keyword_id = parts[3]
            if method == "PATCH":
                return _handle_custom_keywords_update(routed_event, store_id, keyword_id)
            if method == "DELETE":
                return _handle_custom_keywords_delete(routed_event, store_id, keyword_id)
            
    # me (유저 본인 / 도메인)
    if path == "/me" and method == "GET":
        return _handle_me_get(routed_event)
    if path == "/me" and method == "PATCH":
        return _handle_me_patch(routed_event)

    # calls
    if path == "/calls" and method == "GET":
        return _handle_calls_list(routed_event)
    if path.startswith("/calls/") and path.endswith("/audio") and method == "GET":
        call_id = path.split("/")[2]
        return _handle_call_audio(routed_event, call_id)
    if path.startswith("/calls/") and path.endswith("/process") and method == "POST":
        call_id = path.split("/")[2]
        return _handle_call_process(routed_event, call_id)
    if path.startswith("/calls/") and method == "GET":
        call_id = path.split("/")[2]
        return _handle_call_get(routed_event, call_id)
    if path.startswith("/calls/") and method == "PATCH":
        call_id = path.split("/")[2]
        return _handle_call_patch(routed_event, call_id)
    if path.startswith("/calls/") and method == "DELETE":
        call_id = path.split("/")[2]
        return _handle_call_delete(routed_event, call_id)
    if path == "/calls/upload" and method == "POST":
        return _handle_upload(routed_event)

    # customers
    # customer_profiles 기준 고객 목록/상세/동의/메모/히스토리 공통 처리
    if path == "/customers" or path.startswith("/customers/") or path.startswith("/consent/"):
        import customer_handler
        return customer_handler.lambda_handler(routed_event, context)

    # ── 임시 마이그레이션 라우트 제거됨 (보안) ──
    # /migrate/caller-name, /migrate/user-domain 공개 라우트는 인증 없이
    # ALTER TABLE을 실행할 수 있어 제거함. 스키마 변경이 다시 필요하면
    # 콘솔에서 Lambda를 직접 invoke(payload {"action": ...})하거나 마이그레이션
    # 도구를 통해 수행할 것.

    return _response(404, {"error": "Not found", "path": path}, event)


def _get_current_user_id(event: dict) -> str | None:
    headers = event.get("headers", {}) or {}
    auth_header = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth_header.startswith("Bearer "):
        return None
    try:
        from auth_handler import verify_firebase_token
        decoded = verify_firebase_token(auth_header[7:])
        if not decoded:
            return None
        firebase_uid = decoded.get("uid") or decoded.get("user_id") or decoded.get("sub")
        if not firebase_uid:
            return None
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE firebase_uid = %s LIMIT 1", (firebase_uid,))
                user = cur.fetchone()
        return user["id"] if user else None
    except Exception as e:
        logger.error(f"[Call] _get_current_user_id 오류: {e}")
        return None


def _assert_owns_store(uid: str, store_id: str) -> bool:
    """
    store_id가 uid 소유(stores.owner_id)인지 확인.
    BOLA(객체 단위 권한 우회) 방지를 위해, store_id를 외부에서 받는
    모든 경로에서 INSERT/조회 전에 반드시 호출한다.
    """
    if not uid or not store_id:
        return False
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM stores WHERE id = %s AND user_id = %s LIMIT 1",
                    (store_id, uid),
                )
                return cur.fetchone() is not None
    except Exception as e:
        logger.error(f"[Auth] store 소유권 확인 오류 store={store_id} uid={uid}: {e}")
        return False


def _handle_stores_list(event: dict) -> dict:
    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"})
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, user_id, created_at FROM stores WHERE user_id = %s",
                    (uid,)
                )
                stores = cur.fetchall()
        result = [{k: str(v) if hasattr(v, "isoformat") else v for k, v in s.items()} for s in stores]
        return _response(200, {"stores": result})
    except Exception as e:
        logger.exception(f"[Store] list 오류: {e}")
        return _response(500, {"error": "내부 오류"})


def _handle_stores_create(event: dict) -> dict:
    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"})
    try:
        body = json.loads(event.get("body") or "{}")
        name = body.get("name", "").strip()
        if not name:
            return _response(400, {"error": "name 필수"})
        store_id = str(uuid.uuid4())
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO stores (id, name, user_id) VALUES (%s, %s, %s)",
                    (store_id, name, uid)
                )
            conn.commit()
        return _response(201, {"id": store_id, "name": name, "owner_id": uid})
    except Exception as e:
        logger.exception(f"[Store] create 오류: {e}")
        return _response(500, {"error": "내부 오류"})


def _handle_calls_list(event: dict) -> dict:
    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"}, event)

    # Rate Limiting — 조회는 1분에 60회 제한
    allowed, _ = check_rate_limit(uid, "api")
    if not allowed:
        return _response(429, {"error": "요청 한도 초과. 잠시 후 다시 시도해주세요."}, event)

    try:
        params   = event.get("queryStringParameters") or {}
        store_id = params.get("store_id")
        status   = params.get("status")
        limit    = int(params.get("limit", 20))
        offset   = int(params.get("offset", 0))

        sql = """
            SELECT c.*,
                   s.summary, s.category, s.domain, s.sentiment, 
                   s.action_required, s.keywords, s.internal_keywords, s.extracted_info
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
        return _response(200, {"calls": result})
    except Exception as e:
        logger.exception(f"[Call] list 오류: {e}")
        return _response(500, {"error": "내부 오류"})


def _handle_call_get(event: dict, call_id: str) -> dict:
    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"})
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT c.*,
                           s.summary, s.category, s.domain, s.sentiment,
                           s.action_required, s.keywords, s.internal_keywords,
                           s.extracted_info, s.sms_recommended, s.sms_message
                    FROM calls c
                    LEFT JOIN summaries s ON s.call_id = c.id
                    WHERE c.id = %s AND c.user_id = %s
                """, (call_id, uid))
                call = cur.fetchone()
        if not call:
            return _response(404, {"error": "통화를 찾을 수 없습니다"})
        result = {k: str(v) if hasattr(v, "isoformat") else v for k, v in call.items()}
        return _response(200, {"call": result})
    except Exception as e:
        logger.exception(f"[Call] get 오류: {e}")
        return _response(500, {"error": "내부 오류"})


def _handle_call_patch(event: dict, call_id: str) -> dict:
    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"})
    try:
        body = json.loads(event.get("body") or "{}")
        fields: list[str] = []
        params: list = []

        # 부분 업데이트: 들어온 필드만 수정
        if "caller_category" in body:
            caller_category = (body.get("caller_category") or "").strip()
            if caller_category not in ("BUSINESS", "PERSONAL", "UNCLASSIFIED"):
                return _response(400, {"error": "유효하지 않은 category"})
            fields.append("caller_category = %s")
            params.append(caller_category)
        if "caller_number" in body:
            fields.append("caller_number = %s")
            params.append((body.get("caller_number") or "").strip())
        if "caller_name" in body:
            fields.append("caller_name = %s")
            params.append((body.get("caller_name") or "").strip())

        if not fields:
            return _response(400, {"error": "수정할 항목이 없습니다"})

        params.extend([call_id, uid])
        sql = f"UPDATE calls SET {', '.join(fields)} WHERE id = %s AND user_id = %s"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
            conn.commit()
        return _response(200, {"message": "업데이트 완료"})
    except Exception as e:
        logger.exception(f"[Call] patch 오류: {e}")
        return _response(500, {"error": "내부 오류"})

# ── 유저 도메인(업종) ─────────────────────────────────────────
VALID_DOMAINS = {"real_estate", "education", "insurance", "construction", "retail"}

def _handle_me_get(event: dict) -> dict:
    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"}, event)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, email, role, domain FROM users WHERE id = %s", (uid,))
                user = cur.fetchone()
        if not user:
            return _response(404, {"error": "유저를 찾을 수 없습니다"}, event)
        result = {k: str(v) if hasattr(v, "isoformat") else v for k, v in user.items()}
        return _response(200, {"user": result}, event)
    except Exception as e:
        logger.exception(f"[Me] get 오류: {e}")
        return _response(500, {"error": "내부 오류"}, event)


def _handle_me_patch(event: dict) -> dict:
    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"}, event)
    try:
        body = json.loads(event.get("body") or "{}")
        domain = (body.get("domain") or "").strip()
        if domain not in VALID_DOMAINS:
            return _response(400, {"error": "유효하지 않은 domain"}, event)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET domain = %s WHERE id = %s", (domain, uid))
            conn.commit()
        return _response(200, {"message": "업데이트 완료", "domain": domain}, event)
    except Exception as e:
        logger.exception(f"[Me] patch 오류: {e}")
        return _response(500, {"error": "내부 오류"}, event)


def _handle_migrate_user_domain(event: dict) -> dict:
    """
    users.domain 컬럼 추가 (멱등). 관리자 전용.
    API Gateway 공개 라우트는 제거했으며, 콘솔에서 Lambda를 직접
    invoke(payload {"action": "migrate_user_domain"})할 때만 호출된다.
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) AS cnt FROM information_schema.columns
                    WHERE table_schema = DATABASE()
                      AND table_name = 'users' AND column_name = 'domain'
                """)
                already = int((cur.fetchone() or {}).get("cnt", 0))
                if not already:
                    cur.execute("ALTER TABLE users ADD COLUMN domain VARCHAR(40) NULL")
            conn.commit()
        return _response(200, {"message": "domain 컬럼 마이그레이션 완료", "already_existed": bool(already)}, event)
    except Exception as e:
        logger.exception(f"[Migrate] user domain 오류: {e}")
        return _response(500, {"error": str(e)}, event)



def _handle_call_delete(event: dict, call_id: str) -> dict:
    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"})
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM calls WHERE id = %s AND user_id = %s", (call_id, uid))
            conn.commit()
        return _response(200, {"message": "삭제 완료"})
    except Exception as e:
        logger.exception(f"[Call] delete 오류: {e}")
        return _response(500, {"error": "내부 오류"})


def _handle_call_audio(event: dict, call_id: str) -> dict:
    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"})
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT s3_key FROM calls WHERE id = %s AND user_id = %s", (call_id, uid))
                call = cur.fetchone()
        if not call:
            return _response(404, {"error": "통화를 찾을 수 없습니다"})
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET_NAME, "Key": call["s3_key"]},
            ExpiresIn=600,
        )
        return _response(200, {"url": url})
    except Exception as e:
        logger.exception(f"[Call] audio 오류: {e}")
        return _response(500, {"error": "내부 오류"})


def _handle_call_process(event: dict, call_id: str) -> dict:
    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"})
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s3_key, status, clova_job_id
                    FROM calls
                    WHERE id = %s AND user_id = %s
                """, (call_id, uid))
                call = cur.fetchone()

        if not call:
            return _response(404, {"error": "통화를 찾을 수 없습니다"})

        if call.get("status") in ("completed", "transcribed"):
            return _response(200, {
                "message": "이미 처리 완료",
                "call_id": call_id,
                "status": call.get("status"),
            })

        if call.get("status") == "processing" and call.get("clova_job_id"):
            return _response(200, {
                "message": "이미 STT 처리 중",
                "call_id": call_id,
                "clova_job_id": call.get("clova_job_id"),
            })

        job_id = request_clova_stt(call_id, call["s3_key"])
        if not job_id:
            return _response(422, {
                "error": "CLOVA STT 요청 실패",
                "call_id": call_id,
            })

        return _response(200, {
            "message": "STT 처리 예약",
            "call_id": call_id,
            "clova_job_id": job_id,
        })

    except Exception as e:
        logger.exception(f"[Call] process 오류: {e}")
        return _response(500, {"error": "내부 오류"})



def _handle_upload(event: dict) -> dict:
    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"}, event)

    # Rate Limiting — 업로드는 1분에 10회 제한
    allowed, remaining = check_rate_limit(uid, "upload")
    if not allowed:
        return _response(429, {"error": "요청 한도 초과. 잠시 후 다시 시도해주세요."}, event)

    try:
        body      = json.loads(event.get("body") or "{}")
        store_id  = body.get("store_id", "").strip()
        file_name = body.get("file_name", "recording.m4a").strip()
        mime_type = body.get("mime_type", "audio/mp4").strip()
        if file_name.lower().endswith((".m4a", ".mp4")) or mime_type in ("audio/m4a", "audio/x-m4a"):
            mime_type = "audio/mp4"

        if store_id:
            # store_id가 명시되면 반드시 소유권 검증 (BOLA 방지)
            if not _assert_owns_store(uid, store_id):
                logger.warning(f"[Call] upload 권한 위반 시도 uid={uid} store={store_id}")
                return _response(403, {"error": "권한이 없는 매장입니다"}, event)
        else:
            # store_id 없으면 user_id로 대체 (개인 기본 버킷)
            store_id = uid

        call_id          = str(uuid.uuid4())
        s3_key           = f"recordings/{store_id}/{call_id}/{file_name}"
        counterpart_number = (
            body.get("caller_number")
            or body.get("counterpart_number")
            or body.get("phone")
            or ""
        ).strip()

        # 통화 길이(초) — 앱이 duration_seconds로 보냄. 없으면 0.
        try:
            duration_sec = int(body.get("duration_seconds") or 0)
        except (TypeError, ValueError):
            duration_sec = 0
        if duration_sec < 0:
            duration_sec = 0

        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket":      S3_BUCKET_NAME,
                "Key":         s3_key,
                "ContentType": mime_type,
            },
            ExpiresIn=600,
        )

        sql = """
            INSERT INTO calls (id, store_id, user_id, s3_key, status, caller_number, duration)
            VALUES (%s, %s, %s, %s, 'uploaded', %s, %s)
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (call_id, store_id, uid, s3_key, counterpart_number, duration_sec))
            conn.commit()

        if counterpart_number:
            try:
                import customer_handler
                customer_handler.ensure_schema()
                customer_handler._upsert_profile(uid, counterpart_number, consent_status="pending")
            except Exception as e:
                logger.warning(f"[Customer] upload profile upsert skipped call_id={call_id} error={e}")

        return _response(200, {
            "call_id":        call_id,
            "upload_url":     upload_url,
            "s3_key":         s3_key,
            "upload_headers": {"Content-Type": mime_type},
        })

    except Exception as e:
        logger.exception(f"[Call] upload 오류: {e}")
        return _response(500, {"error": "내부 오류"})


def _cors_origin(event=None):
    allowed_raw = os.environ.get("CORS_ALLOWED_ORIGINS") or os.environ.get("CORS_ALLOW_ORIGIN") or "*"
    allowed = [x.strip().rstrip("/") for x in allowed_raw.split(",") if x.strip()]
    if not allowed or "*" in allowed:
        return "*"
    headers = (event or {}).get("headers") or {}
    origin  = (headers.get("origin") or headers.get("Origin") or "").rstrip("/")
    if origin in allowed:
        return origin
    if origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1"):
        return origin
    return allowed[0]

def _migrate_caller_stats() -> dict:
    sql = """
        CREATE TABLE IF NOT EXISTS caller_stats (
            id              VARCHAR(36)  NOT NULL PRIMARY KEY,
            user_id         VARCHAR(36)  NOT NULL,
            store_id        VARCHAR(36)  NOT NULL,
            caller_number   VARCHAR(20)  NOT NULL,
            call_count      INT          NOT NULL DEFAULT 1,
            last_called_at  DATETIME     NOT NULL DEFAULT NOW(),
            first_called_at DATETIME     NOT NULL DEFAULT NOW(),
            updated_at      DATETIME     NOT NULL DEFAULT NOW()
                            ON UPDATE NOW(),
            UNIQUE KEY uq_user_store_caller (user_id, store_id, caller_number),
            INDEX idx_user_id (user_id),
            INDEX idx_caller_number (caller_number)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    logger.info("[Migrate] caller_stats 테이블 생성 완료")
    return {
        "statusCode": 200,
        "body": json.dumps({"message": "caller_stats 테이블 생성 완료"})
    }



def _migrate_custom_keywords() -> dict:
    sql = """
        CREATE TABLE IF NOT EXISTS custom_keywords (
            id                 VARCHAR(36)  NOT NULL PRIMARY KEY,
            user_id            VARCHAR(36)  NOT NULL,
            store_id           VARCHAR(36)  NOT NULL,
            keyword            VARCHAR(100) NOT NULL,
            normalized_keyword VARCHAR(100) NOT NULL,
            label              VARCHAR(100) NULL,
            action_required    TINYINT(1)   NOT NULL DEFAULT 1,
            is_enabled         TINYINT(1)   NOT NULL DEFAULT 1,
            created_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                       ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_store_keyword (store_id, normalized_keyword),
            INDEX idx_store_enabled (store_id, is_enabled),
            INDEX idx_user_store (user_id, store_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    logger.info("[Migrate] custom_keywords 테이블 생성 완료")
    return {
        "statusCode": 200,
        "body": json.dumps({"message": "custom_keywords 테이블 생성 완료"}, ensure_ascii=False),
    }


def _upsert_caller_stats(call_id: str) -> None:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id, store_id, caller_number FROM calls WHERE id = %s",
                    (call_id,)
                )
                row = cur.fetchone()

        if not row or not row.get("caller_number"):
            logger.warning(f"[Stats] caller_number 없음 call_id={call_id}")
            return

        user_id       = row["user_id"]
        store_id      = row["store_id"]
        caller_number = row["caller_number"]

        sql = """
            INSERT INTO caller_stats
                (id, user_id, store_id, caller_number,
                 call_count, last_called_at, first_called_at)
            VALUES
                (%s, %s, %s, %s, 1, NOW(), NOW())
            ON DUPLICATE KEY UPDATE
                call_count     = call_count + 1,
                last_called_at = NOW()
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    str(uuid.uuid4()), user_id, store_id, caller_number
                ))
            conn.commit()
        logger.info(f"[Stats] 누적 완료 caller={_mask_phone(caller_number)} user={user_id}")

    except Exception as e:
        logger.error(f"[Stats] caller_stats 업데이트 실패: {e}")


# ════════════════════════════════════════════════════════════
# 고객 프로필 + AI 분석
# ════════════════════════════════════════════════════════════

CUSTOMER_PROFILE_FIELDS = ("email", "tendency", "medical", "special_notes")


def _migrate_customer_profiles() -> dict:
    """
    customer_profiles (편집 필드) + customer_analysis (AI 분석문) 테이블 생성. 멱등.
    고객 식별키 = (user_id, phone).
    """
    sql_profiles = """
        CREATE TABLE IF NOT EXISTS customer_profiles (
            id            VARCHAR(36)  NOT NULL PRIMARY KEY,
            user_id       VARCHAR(36)  NOT NULL,
            phone         VARCHAR(20)  NOT NULL,
            email         VARCHAR(200) NULL,
            tendency      VARCHAR(500) NULL,
            medical       VARCHAR(500) NULL,
            special_notes VARCHAR(1000) NULL,
            custom_fields JSON         NULL,
            created_at    DATETIME     NOT NULL DEFAULT NOW(),
            updated_at    DATETIME     NOT NULL DEFAULT NOW() ON UPDATE NOW(),
            UNIQUE KEY uq_user_phone (user_id, phone),
            INDEX idx_user (user_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    sql_analysis = """
        CREATE TABLE IF NOT EXISTS customer_analysis (
            id          VARCHAR(36) NOT NULL PRIMARY KEY,
            user_id     VARCHAR(36) NOT NULL,
            phone       VARCHAR(20) NOT NULL,
            analysis    TEXT        NULL,
            call_count  INT         NOT NULL DEFAULT 0,
            generated_at DATETIME   NOT NULL DEFAULT NOW(),
            UNIQUE KEY uq_user_phone_an (user_id, phone),
            INDEX idx_user_an (user_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_profiles)
            cur.execute(sql_analysis)
        conn.commit()
    logger.info("[Migrate] customer_profiles + customer_analysis 생성 완료")
    return {"statusCode": 200, "body": json.dumps({"message": "customer 테이블 생성 완료"}, ensure_ascii=False)}


def _user_owns_phone(uid: str, phone: str) -> bool:
    """해당 phone이 user의 통화(calls)에 존재하는지 확인 (BOLA 방지)."""
    if not uid or not phone:
        return False
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM calls WHERE user_id = %s AND caller_number = %s LIMIT 1",
                    (uid, phone),
                )
                return cur.fetchone() is not None
    except Exception as e:
        logger.error(f"[Customer] phone 소유 확인 오류 uid={uid}: {e}")
        return False


def _handle_customer_get(event: dict, phone: str) -> dict:
    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"}, event)
    if not _user_owns_phone(uid, phone):
        return _response(404, {"error": "고객을 찾을 수 없습니다"}, event)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT email, tendency, medical, special_notes, custom_fields, updated_at
                       FROM customer_profiles WHERE user_id = %s AND phone = %s""",
                    (uid, phone),
                )
                profile = cur.fetchone()
                cur.execute(
                    """SELECT analysis, call_count, generated_at
                       FROM customer_analysis WHERE user_id = %s AND phone = %s""",
                    (uid, phone),
                )
                analysis = cur.fetchone()

        prof = {k: (str(v) if hasattr(v, "isoformat") else v) for k, v in (profile or {}).items()}
        anal = {k: (str(v) if hasattr(v, "isoformat") else v) for k, v in (analysis or {}).items()}
        return _response(200, {"profile": prof, "analysis": anal}, event)
    except Exception as e:
        logger.exception(f"[Customer] get 오류: {e}")
        return _response(500, {"error": "내부 오류"}, event)


def _handle_customer_patch(event: dict, phone: str) -> dict:
    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"}, event)
    if not _user_owns_phone(uid, phone):
        return _response(404, {"error": "고객을 찾을 수 없습니다"}, event)
    try:
        body = json.loads(event.get("body") or "{}")

        email = (body.get("email") or "").strip()
        tendency = (body.get("tendency") or "").strip()
        medical = (body.get("medical") or "").strip()
        special_notes = (body.get("special_notes") or "").strip()
        custom_fields = body.get("custom_fields")
        custom_json = json.dumps(custom_fields, ensure_ascii=False) if custom_fields is not None else None

        # UPSERT (user_id+phone 유니크)
        sql = """
            INSERT INTO customer_profiles
                (id, user_id, phone, email, tendency, medical, special_notes, custom_fields)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                email = VALUES(email),
                tendency = VALUES(tendency),
                medical = VALUES(medical),
                special_notes = VALUES(special_notes),
                custom_fields = VALUES(custom_fields),
                updated_at = NOW()
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    str(uuid.uuid4()), uid, phone,
                    email or None, tendency or None, medical or None,
                    special_notes or None, custom_json,
                ))
            conn.commit()
        return _response(200, {"message": "저장 완료"}, event)
    except Exception as e:
        logger.exception(f"[Customer] patch 오류: {e}")
        return _response(500, {"error": "내부 오류"}, event)


def _run_customer_analysis_batch() -> dict:
    """
    EventBridge(하루 2회) 진입점. 통화가 있는 (user_id, phone)별로
    통화 요약들을 모아 nlp Lambda의 고객 분석을 호출 → customer_analysis 저장.
    GPT 비용/시간 절약을 위해: 통화 수가 변한 고객만, 1회 최대 MAX_PER_RUN명만 갱신.
    """
    MAX_PER_RUN = 15  # 한 번 실행에서 GPT 호출할 최대 고객 수 (타임아웃 방지)
    logger.info("[CustomerAI] 배치 시작")
    result = {"targets": 0, "updated": 0, "skipped": 0, "failed": 0, "remaining": 0}
    try:
        # 1) 고객별 통화 수 (가벼운 집계만)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT user_id, caller_number AS phone, COUNT(*) AS call_count
                    FROM calls
                    WHERE caller_number IS NOT NULL AND caller_number <> ''
                    GROUP BY user_id, caller_number
                """)
                call_rows = cur.fetchall()

                # 2) 이미 분석된 고객의 call_count 맵 (한 번에 조회)
                cur.execute("SELECT user_id, phone, call_count FROM customer_analysis")
                analyzed_rows = cur.fetchall()

        analyzed_map = {(r["user_id"], r["phone"]): int(r["call_count"]) for r in analyzed_rows}
        result["targets"] = len(call_rows)
        logger.info(f"[CustomerAI] 집계 완료 targets={len(call_rows)} analyzed={len(analyzed_rows)}")

        # 변동된 고객만 추림
        todo = []
        for row in call_rows:
            key = (row["user_id"], row["phone"])
            cc = int(row["call_count"])
            if analyzed_map.get(key, -1) == cc:
                result["skipped"] += 1
            else:
                todo.append((row["user_id"], row["phone"], cc))

        # 1회 처리량 제한
        result["remaining"] = max(0, len(todo) - MAX_PER_RUN)
        todo = todo[:MAX_PER_RUN]

        logger.info(f"[CustomerAI] 분석 대상 {len(todo)}명 (남은 {result['remaining']}명)")
        for uid, phone, call_count in todo:
            try:
                summaries = _fetch_customer_summaries(uid, phone)
                logger.info(f"[CustomerAI] nlp 호출 phone={_mask_phone(phone)} 요약 {len(summaries)}건")

                # 요약이 아예 없는 고객 → 분석할 내용 없음.
                # 빈 분석을 call_count와 함께 저장해 다음 실행에서 skip 되게 함(무한 반복 방지).
                if not summaries:
                    _save_customer_analysis(uid, phone, "", call_count)
                    result["skipped"] += 1
                    continue

                analysis_text = _invoke_customer_analysis(phone, summaries)
                if analysis_text:
                    _save_customer_analysis(uid, phone, analysis_text, call_count)
                    result["updated"] += 1
                else:
                    # GPT 호출 자체가 실패한 경우만 failed로 두고 다음 실행에서 재시도
                    result["failed"] += 1
            except Exception as e:
                logger.error(f"[CustomerAI] 분석 실패 phone={_mask_phone(phone)}: {e}")
                result["failed"] += 1

        logger.info(f"[CustomerAI] 배치 완료: {result}")
    except Exception as e:
        logger.exception(f"[CustomerAI] 배치 오류: {e}")
    return {"statusCode": 200, "body": json.dumps(result, ensure_ascii=False)}


def _fetch_customer_summaries(uid: str, phone: str) -> list[str]:
    try:
        import customer_handler
        return customer_handler.fetch_customer_analysis_items(uid, phone)
    except Exception as e:
        logger.warning(f"[CustomerAI] 확장 히스토리 조회 실패, 기존 요약 조회로 fallback: {e}")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.summary, s.category
                FROM calls c
                LEFT JOIN summaries s ON s.call_id = c.id
                WHERE c.user_id = %s AND c.caller_number = %s
                ORDER BY c.created_at DESC
                LIMIT 20
            """, (uid, phone))
            rows = cur.fetchall()
    out = []
    for r in rows:
        summ = (r.get("summary") or "").strip()
        cat = (r.get("category") or "").strip()
        if summ:
            out.append(f"[{cat}] {summ}" if cat else summ)
    return out


def _invoke_customer_analysis(phone: str, summaries: list[str]) -> str | None:
    """nlp Lambda를 호출해 고객 종합 분석문 생성."""
    if not summaries:
        return None
    try:
        response = lambda_client.invoke(
            FunctionName="call-recorder-api-nlp",
            InvocationType="RequestResponse",
            Payload=json.dumps({
                "task": "customer_analysis",
                "phone": phone,
                "summaries": summaries,
            }).encode(),
        )
        payload = json.loads(response["Payload"].read())
        body = json.loads(payload.get("body", "{}"))
        return (body.get("analysis") or "").strip() or None
    except Exception as e:
        logger.error(f"[CustomerAI] nlp invoke 실패 phone={_mask_phone(phone)}: {e}")
        return None


def _save_customer_analysis(uid: str, phone: str, analysis: str, call_count: int) -> None:
    sql = """
        INSERT INTO customer_analysis (id, user_id, phone, analysis, call_count, generated_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON DUPLICATE KEY UPDATE
            analysis = VALUES(analysis),
            call_count = VALUES(call_count),
            generated_at = NOW()
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (str(uuid.uuid4()), uid, phone, analysis, call_count))
        conn.commit()
        
    


def _response(status: int, body: dict, event: dict = None) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type":                 "application/json; charset=utf-8",
            "Access-Control-Allow-Origin":  _cors_origin(event),
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
        },
        "body": json.dumps(body, ensure_ascii=False, default=str),
    }
# redeploy notes 2

def _handle_custom_keywords_list(event: dict, store_id: str) -> dict:
    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"})
    if not _assert_owns_store(uid, store_id):
        return _response(403, {"error": "권한이 없는 매장입니다"})
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, keyword, label, action_required, is_enabled, created_at
                       FROM custom_keywords
                       WHERE store_id = %s AND user_id = %s AND is_enabled = 1
                       ORDER BY created_at DESC""",
                    (store_id, uid),
                )
                rows = cur.fetchall()
        result = [{k: str(v) if hasattr(v, "isoformat") else v for k, v in r.items()} for r in rows]
        return _response(200, {"keywords": result})
    except Exception as e:
        logger.exception(f"[Keyword] list 오류: {e}")
        return _response(500, {"error": "내부 오류"})


def _handle_custom_keywords_create(event: dict, store_id: str) -> dict:
    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"})
    if not _assert_owns_store(uid, store_id):
        # store 소유권 미검증 시 타 매장 NLP 파이프라인에 키워드 주입 가능 (cross-tenant)
        logger.warning(f"[Keyword] create 권한 위반 시도 uid={uid} store={store_id}")
        return _response(403, {"error": "권한이 없는 매장입니다"})
    try:
        body = json.loads(event.get("body") or "{}")
        keyword = (body.get("keyword") or "").strip()
        label = (body.get("label") or keyword).strip()
        action_required = body.get("action_required", True)

        if not keyword:
            return _response(400, {"error": "keyword 필수"})
        if len(keyword) > CUSTOM_KEYWORD_MAX_LENGTH:
            return _response(400, {"error": f"키워드는 {CUSTOM_KEYWORD_MAX_LENGTH}자 이하"})
        normalized = "".join(keyword.lower().split())
        if normalized in CUSTOM_KEYWORD_BLOCKLIST:
            return _response(400, {"error": "사용할 수 없는 키워드예요"})

        keyword_id = str(uuid.uuid4())
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO custom_keywords
                       (id, user_id, store_id, keyword, normalized_keyword, label, action_required)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (keyword_id, uid, store_id, keyword, normalized, label, 1 if action_required else 0),
                )
            conn.commit()

        # Redis 캐시 무효화
        cache_key = f"{CUSTOM_KEYWORDS_CACHE_PREFIX}:{store_id}"
        cache_delete(cache_key)

        return _response(201, {
            "id": keyword_id,
            "keyword": keyword,
            "label": label,
            "action_required": bool(action_required),
            "is_enabled": True,
        })
    except Exception as e:
        if "Duplicate entry" in str(e):
            return _response(409, {"error": "이미 등록된 키워드예요"})
        logger.exception(f"[Keyword] create 오류: {e}")
        return _response(500, {"error": "내부 오류"})


def _handle_custom_keywords_update(event: dict, store_id: str, keyword_id: str) -> dict:
    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"})
    if not _assert_owns_store(uid, store_id):
        return _response(403, {"error": "권한이 없는 매장입니다"})
    try:
        body = json.loads(event.get("body") or "{}")
        is_enabled = body.get("is_enabled", True)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE custom_keywords SET is_enabled = %s
                       WHERE id = %s AND store_id = %s AND user_id = %s""",
                    (1 if is_enabled else 0, keyword_id, store_id, uid),
                )
            conn.commit()

        cache_key = f"{CUSTOM_KEYWORDS_CACHE_PREFIX}:{store_id}"
        cache_delete(cache_key)

        return _response(200, {"message": "업데이트 완료"})
    except Exception as e:
        logger.exception(f"[Keyword] update 오류: {e}")
        return _response(500, {"error": "내부 오류"})


def _handle_custom_keywords_delete(event: dict, store_id: str, keyword_id: str) -> dict:
    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"})
    if not _assert_owns_store(uid, store_id):
        return _response(403, {"error": "권한이 없는 매장입니다"})
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM custom_keywords WHERE id = %s AND store_id = %s AND user_id = %s",
                    (keyword_id, store_id, uid),
                )
            conn.commit()

        cache_key = f"{CUSTOM_KEYWORDS_CACHE_PREFIX}:{store_id}"
        cache_delete(cache_key)

        return _response(200, {"message": "삭제 완료"})
    except Exception as e:
        logger.exception(f"[Keyword] delete 오류: {e}")
        return _response(500, {"error": "내부 오류"})
    
    

def _clean_extracted_info() -> dict:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, extracted_info, internal_keywords FROM summaries WHERE extracted_info IS NOT NULL OR internal_keywords IS NOT NULL")
                rows = cur.fetchall()
            updated = 0
            for row in rows:
                try:
                    ei = row.get('extracted_info')
                    if isinstance(ei, str):
                        try: ei = json.loads(ei)
                        except: ei = {}
                    clean_ei = {k: v for k, v in (ei or {}).items() if not k.startswith('_')}

                    ik = row.get('internal_keywords')
                    if isinstance(ik, str):
                        try: ik = json.loads(ik)
                        except: ik = {}
                    clean_ik = {k: v for k, v in (ik or {}).items() if not k.startswith('_')}

                    if len(clean_ei) != len(ei or {}) or len(clean_ik) != len(ik or {}):
                        with conn.cursor() as cur2:
                            cur2.execute(
                                "UPDATE summaries SET extracted_info = %s, internal_keywords = %s WHERE id = %s",
                                (json.dumps(clean_ei, ensure_ascii=False),
                                 json.dumps(clean_ik, ensure_ascii=False),
                                 row['id'])
                            )
                        updated += 1
                except Exception as e:
                    logger.error(f"clean 오류: {e}")
            conn.commit()
        return {"statusCode": 200, "body": json.dumps({"updated": updated, "total": len(rows)})}
    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}