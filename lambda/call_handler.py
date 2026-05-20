"""
call_handler.py — 중복 업로드 방지 with Redis SET NX
변경점:
  - S3 업로드 전 파일 해시로 중복 체크
  - Redis SET NX 원자적 락 (10분 TTL)
  - 중복 요청은 409 Conflict 반환
"""
import os
import json
import hashlib
import logging
import base64
import boto3
from botocore.exceptions import ClientError

from redis_client import set_nx_with_ttl, cache_get, cache_set, TTL_UPLOAD_LOCK

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
BUCKET_NAME = os.environ.get("S3_BUCKET", "call-recoder-audio-1017")


# ── 중복 업로드 체크 ──────────────────────────────────────────────────────────

def _file_hash(file_bytes: bytes) -> str:
    """파일 내용 SHA256 해시 (중복 판별 키)"""
    return hashlib.sha256(file_bytes).hexdigest()


def _upload_lock_key(user_id: str, file_hash: str) -> str:
    """Redis 락 키: user별로 분리해서 다른 유저의 동일 파일은 허용"""
    return f"upload:lock:{user_id}:{file_hash}"


def check_and_lock_upload(user_id: str, file_bytes: bytes) -> tuple[bool, str]:
    """
    업로드 중복 체크 + 락 획득.
    반환: (is_duplicate, file_hash)
      - is_duplicate=True  → 이미 처리 중이거나 최근 업로드된 파일
      - is_duplicate=False → 정상 진행 가능
    """
    fhash = _file_hash(file_bytes)
    lock_key = _upload_lock_key(user_id, fhash)

    acquired = set_nx_with_ttl(lock_key, user_id, TTL_UPLOAD_LOCK)
    if not acquired:
        logger.warning(f"[Call] 중복 업로드 감지 user={user_id} hash={fhash[:16]}...")
        return True, fhash

    logger.info(f"[Call] 업로드 락 획득 user={user_id} hash={fhash[:16]}...")
    return False, fhash


# ── S3 업로드 ─────────────────────────────────────────────────────────────────

def upload_to_s3(user_id: str, file_bytes: bytes, filename: str, file_hash: str) -> str:
    """S3에 오디오 파일 업로드 후 object key 반환"""
    # 파일명에 해시 포함 → 동일 파일 재업로드 방지 + 디버깅 편의
    ext = os.path.splitext(filename)[-1].lower() or ".wav"
    s3_key = f"audio/{user_id}/{file_hash[:16]}{ext}"

    try:
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key=s3_key,
            Body=file_bytes,
            ContentType=_content_type(ext),
            Metadata={
                "user_id":   user_id,
                "file_hash": file_hash,
                "original_filename": filename,
            },
        )
        logger.info(f"[Call] S3 업로드 완료 key={s3_key}")

        # 업로드 결과도 짧게 캐싱 (동일 해시로 재요청 시 S3 키 즉시 반환용)
        cache_set(f"upload:result:{user_id}:{file_hash}", {"s3_key": s3_key}, TTL_UPLOAD_LOCK)
        return s3_key

    except ClientError as e:
        logger.error(f"[Call] S3 업로드 실패: {e}")
        raise


def _content_type(ext: str) -> str:
    return {
        ".wav":  "audio/wav",
        ".mp3":  "audio/mpeg",
        ".m4a":  "audio/mp4",
        ".ogg":  "audio/ogg",
        ".flac": "audio/flac",
        ".webm": "audio/webm",
    }.get(ext, "application/octet-stream")


# ── Lambda 핸들러 ──────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    path   = event.get("path", "")
    method = event.get("httpMethod", "POST")

    if path == "/call/upload" and method == "POST":
        return _handle_upload(event)

    return _response(404, {"error": "Not found"})


def _handle_upload(event: dict) -> dict:
    """
    POST /call/upload
    Body: multipart 또는 base64 JSON { "file": "<base64>", "filename": "...", "user_id": "..." }
    """
    try:
        body = json.loads(event.get("body") or "{}")

        user_id  = body.get("user_id", "").strip()
        filename = body.get("filename", "recording.wav").strip()
        file_b64 = body.get("file", "")

        if not user_id:
            return _response(400, {"error": "user_id 필수"})
        if not file_b64:
            return _response(400, {"error": "file(base64) 필수"})

        # base64 디코딩
        try:
            file_bytes = base64.b64decode(file_b64)
        except Exception:
            return _response(400, {"error": "file base64 디코딩 실패"})

        if len(file_bytes) == 0:
            return _response(400, {"error": "빈 파일"})

        # 최대 파일 크기 체크 (100MB)
        MAX_SIZE = 100 * 1024 * 1024
        if len(file_bytes) > MAX_SIZE:
            return _response(413, {"error": f"파일 크기 초과 (최대 {MAX_SIZE // 1024 // 1024}MB)"})

        # ── 핵심: 중복 업로드 체크 ──────────────────────────────────────────
        is_duplicate, file_hash = check_and_lock_upload(user_id, file_bytes)

        if is_duplicate:
            # 이미 처리된 결과가 캐시에 있으면 그것도 같이 반환
            cached_result = cache_get(f"upload:result:{user_id}:{file_hash}")
            return _response(409, {
                "error": "중복 업로드: 동일한 파일이 이미 업로드되었거나 처리 중입니다",
                "file_hash": file_hash[:16],
                "previous_result": cached_result,  # None이면 처리 중
            })

        # ── S3 업로드 ────────────────────────────────────────────────────────
        s3_key = upload_to_s3(user_id, file_bytes, filename, file_hash)

        return _response(200, {
            "message": "업로드 성공",
            "s3_key":  s3_key,
            "file_hash": file_hash[:16],
            "size_bytes": len(file_bytes),
        })

    except ClientError as e:
        return _response(502, {"error": f"S3 오류: {str(e)}"})
    except Exception as e:
        logger.exception(f"[Call] 처리 오류: {e}")
        return _response(500, {"error": "내부 오류"})


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }
