"""
notes_handler.py — 통화(히스토리 항목)별 메모 + 사진 관리

지원 엔드포인트
- GET   /calls/{callId}/note                  메모 + 사진 목록 조회
- PATCH /calls/{callId}/note                  메모/내용 수정
- POST  /calls/{callId}/photos/upload-url     사진 업로드용 presigned URL 발급
- POST  /calls/{callId}/photos                업로드 완료된 사진 URL 저장
- DELETE /calls/{callId}/photos/{photoId}     사진 삭제

저장 구조
- call_notes 테이블: 통화당 1행 (memo + updated_at)
- call_photos 테이블: 통화당 N행 (s3_key + 생성시각)

call_handler.py 의 get_db / _get_current_user_id / _response 패턴을 그대로 재사용.
"""
import os
import json
import uuid
import logging

import boto3

from call_handler import get_db, _get_current_user_id, _response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
S3_BUCKET_NAME = os.environ.get("S3_BUCKET", "call-recoder-audio-1017")

# 사진 확장자 → content-type
_PHOTO_CONTENT_TYPE = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png",  ".webp": "image/webp",
    ".heic": "image/heic", ".gif": "image/gif",
}


# ─────────────────────────────────────────────────────
# 스키마 자동 생성 (데모 안전용 — 최초 호출 시 테이블 보장)
# ─────────────────────────────────────────────────────
def _ensure_schema():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS call_notes (
                    call_id    VARCHAR(64) NOT NULL PRIMARY KEY,
                    user_id    VARCHAR(64) NOT NULL,
                    memo       TEXT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_call_notes_user (user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS call_photos (
                    id         VARCHAR(64) NOT NULL PRIMARY KEY,
                    call_id    VARCHAR(64) NOT NULL,
                    user_id    VARCHAR(64) NOT NULL,
                    s3_key     VARCHAR(512) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_call_photos_call (call_id),
                    INDEX idx_call_photos_user (user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        conn.commit()


def _verify_call_owner(call_id, user_id):
    """해당 통화가 이 사용자 소유인지 확인."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM calls WHERE id=%s AND user_id=%s LIMIT 1", (call_id, user_id))
            return cur.fetchone() is not None


def _photo_url(s3_key, expires=3600):
    """사진 조회용 presigned GET URL."""
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET_NAME, "Key": s3_key},
        ExpiresIn=expires,
    )


# ─────────────────────────────────────────────────────
# GET /calls/{id}/note — 메모 + 사진 조회
# ─────────────────────────────────────────────────────
def _get_note(event, user_id, call_id):
    if not _verify_call_owner(call_id, user_id):
        return _response(404, {"error": "통화를 찾을 수 없습니다"}, event)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT memo, updated_at FROM call_notes WHERE call_id=%s", (call_id,))
                note_row = cur.fetchone()
                cur.execute(
                    "SELECT id, s3_key, created_at FROM call_photos WHERE call_id=%s ORDER BY created_at ASC",
                    (call_id,),
                )
                photo_rows = cur.fetchall() or []

        photos = [{
            "id": row["id"],
            "url": _photo_url(row["s3_key"]),
            "created_at": str(row.get("created_at") or ""),
        } for row in photo_rows]

        return _response(200, {
            "call_id": call_id,
            "memo": (note_row or {}).get("memo") or "",
            "updated_at": str((note_row or {}).get("updated_at") or ""),
            "photos": photos,
            "photo_count": len(photos),
        }, event)
    except Exception as e:
        logger.exception("[Notes] get note 실패 call_id=%s", call_id)
        return _response(500, {"error": str(e)}, event)


# ─────────────────────────────────────────────────────
# PATCH /calls/{id}/note — 메모 수정
# ─────────────────────────────────────────────────────
def _update_note(event, user_id, call_id):
    if not _verify_call_owner(call_id, user_id):
        return _response(404, {"error": "통화를 찾을 수 없습니다"}, event)
    try:
        body = json.loads(event.get("body") or "{}")
        memo = (body.get("memo") or "").strip()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO call_notes (call_id, user_id, memo)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE memo=VALUES(memo), updated_at=CURRENT_TIMESTAMP
                """, (call_id, user_id, memo))
            conn.commit()
        return _response(200, {"message": "메모 저장 완료", "memo": memo}, event)
    except Exception as e:
        logger.exception("[Notes] update note 실패 call_id=%s", call_id)
        return _response(500, {"error": str(e)}, event)


# ─────────────────────────────────────────────────────
# POST /calls/{id}/photos/upload-url — presigned PUT URL 발급
# ─────────────────────────────────────────────────────
def _photo_upload_url(event, user_id, call_id):
    if not _verify_call_owner(call_id, user_id):
        return _response(404, {"error": "통화를 찾을 수 없습니다"}, event)
    try:
        body = json.loads(event.get("body") or "{}")
        file_name = (body.get("file_name") or "photo.jpg").strip()
        ext = os.path.splitext(file_name)[-1].lower() or ".jpg"
        content_type = _PHOTO_CONTENT_TYPE.get(ext, "image/jpeg")

        photo_id = str(uuid.uuid4())
        s3_key = f"call-photos/{user_id}/{call_id}/{photo_id}{ext}"

        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": S3_BUCKET_NAME, "Key": s3_key, "ContentType": content_type},
            ExpiresIn=600,
        )

        return _response(200, {
            "photo_id": photo_id,
            "upload_url": upload_url,
            "s3_key": s3_key,
            "upload_headers": {"Content-Type": content_type},
        }, event)
    except Exception as e:
        logger.exception("[Notes] photo upload-url 실패 call_id=%s", call_id)
        return _response(500, {"error": str(e)}, event)


# ─────────────────────────────────────────────────────
# POST /calls/{id}/photos — 업로드 완료된 사진 DB 저장
# ─────────────────────────────────────────────────────
def _save_photo(event, user_id, call_id):
    if not _verify_call_owner(call_id, user_id):
        return _response(404, {"error": "통화를 찾을 수 없습니다"}, event)
    try:
        body = json.loads(event.get("body") or "{}")
        photo_id = (body.get("photo_id") or "").strip() or str(uuid.uuid4())
        s3_key = (body.get("s3_key") or "").strip()
        if not s3_key:
            return _response(400, {"error": "s3_key 필수"}, event)

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO call_photos (id, call_id, user_id, s3_key)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE s3_key=VALUES(s3_key)
                """, (photo_id, call_id, user_id, s3_key))
            conn.commit()

        return _response(201, {
            "message": "사진 저장 완료",
            "photo": {"id": photo_id, "url": _photo_url(s3_key)},
        }, event)
    except Exception as e:
        logger.exception("[Notes] save photo 실패 call_id=%s", call_id)
        return _response(500, {"error": str(e)}, event)


# ─────────────────────────────────────────────────────
# DELETE /calls/{id}/photos/{photoId} — 사진 삭제
# ─────────────────────────────────────────────────────
def _delete_photo(event, user_id, call_id, photo_id):
    if not _verify_call_owner(call_id, user_id):
        return _response(404, {"error": "통화를 찾을 수 없습니다"}, event)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT s3_key FROM call_photos WHERE id=%s AND call_id=%s AND user_id=%s",
                    (photo_id, call_id, user_id),
                )
                row = cur.fetchone()
                if not row:
                    return _response(404, {"error": "사진을 찾을 수 없습니다"}, event)
                cur.execute("DELETE FROM call_photos WHERE id=%s", (photo_id,))
            conn.commit()

        # S3 객체도 삭제 (실패해도 DB는 이미 정리됨)
        try:
            s3.delete_object(Bucket=S3_BUCKET_NAME, Key=row["s3_key"])
        except Exception as e:
            logger.warning("[Notes] S3 삭제 실패(무시) key=%s error=%s", row["s3_key"], e)

        return _response(200, {"message": "사진 삭제 완료"}, event)
    except Exception as e:
        logger.exception("[Notes] delete photo 실패 call_id=%s photo_id=%s", call_id, photo_id)
        return _response(500, {"error": str(e)}, event)


# ─────────────────────────────────────────────────────
# 라우팅 진입점 — call_handler.py 에서 위임
# ─────────────────────────────────────────────────────
def lambda_handler(event, context):
    method = (
        event.get("httpMethod")
        or (event.get("requestContext") or {}).get("http", {}).get("method")
        or "GET"
    ).upper()

    if method == "OPTIONS":
        return _response(200, {"message": "OK"}, event)

    user_id = _get_current_user_id(event)
    if not user_id:
        return _response(401, {"error": "인증 필요"}, event)

    try:
        _ensure_schema()
    except Exception as e:
        logger.warning("[Notes] schema ensure skipped: %s", e)

    path = event.get("rawPath") or event.get("path") or "/"
    parts = [p for p in path.split("/") if p]
    # parts 예: ["calls", "{id}", "note"] 또는 ["calls", "{id}", "photos", "{photoId}"]

    try:
        # /calls/{id}/note
        if len(parts) == 3 and parts[0] == "calls" and parts[2] == "note":
            call_id = parts[1]
            if method == "GET":
                return _get_note(event, user_id, call_id)
            if method == "PATCH":
                return _update_note(event, user_id, call_id)

        # /calls/{id}/photos/upload-url
        if len(parts) == 4 and parts[0] == "calls" and parts[2] == "photos" and parts[3] == "upload-url":
            if method == "POST":
                return _photo_upload_url(event, user_id, parts[1])

        # /calls/{id}/photos
        if len(parts) == 3 and parts[0] == "calls" and parts[2] == "photos":
            if method == "POST":
                return _save_photo(event, user_id, parts[1])

        # /calls/{id}/photos/{photoId}
        if len(parts) == 4 and parts[0] == "calls" and parts[2] == "photos":
            if method == "DELETE":
                return _delete_photo(event, user_id, parts[1], parts[3])

        return _response(404, {"error": "Not found", "path": path}, event)
    except Exception as e:
        logger.exception("[Notes] 라우팅 처리 실패")
        return _response(500, {"error": str(e)}, event)