import os
import json
import uuid
import hashlib
import secrets
import logging
from datetime import datetime, timedelta
from urllib.parse import unquote

import boto3

from call_handler import get_db, _get_current_user_id, _response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
S3_BUCKET_NAME = os.environ.get("S3_BUCKET", "call-recoder-audio-1017")

CONSENT_VERSION = os.environ.get("CONSENT_VERSION", "v1")
CONSENT_TOKEN_TTL_DAYS = int(os.environ.get("CONSENT_TOKEN_TTL_DAYS", "30"))
CONSENT_ENFORCEMENT = os.environ.get("CONSENT_ENFORCEMENT", "false").lower() == "true"

_PHOTO_CONTENT_TYPE = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".gif": "image/gif",
}


def _normalize_phone(phone: str) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _json_body(event: dict) -> dict:
    try:
        return json.loads(event.get("body") or "{}")
    except Exception:
        return {}


def _method(event: dict) -> str:
    return (
        event.get("httpMethod")
        or (event.get("requestContext") or {}).get("http", {}).get("method")
        or "GET"
    ).upper()


def _normalize_path(event: dict) -> str:
    path = event.get("rawPath") or event.get("path") or "/"
    stage = (event.get("requestContext") or {}).get("stage")
    if stage and path.startswith(f"/{stage}/"):
        path = path[len(stage) + 1:]
    elif stage and path == f"/{stage}":
        path = "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


def _header(event: dict, name: str) -> str:
    headers = event.get("headers") or {}
    return headers.get(name) or headers.get(name.lower()) or headers.get(name.title()) or ""


def _client_ip(event: dict) -> str:
    forwarded = _header(event, "x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return ((event.get("requestContext") or {}).get("identity") or {}).get("sourceIp", "")


def _consent_base_url(event: dict) -> str:
    base = (
        os.environ.get("CONSENT_WEB_BASE_URL")
        or os.environ.get("WEB_BASE_URL")
        or os.environ.get("PUBLIC_WEB_BASE_URL")
        or ""
    ).rstrip("/")
    if base:
        return base
    origin = _header(event, "origin").rstrip("/")
    return origin


def _consent_url(event: dict, token: str) -> str:
    base = _consent_base_url(event)
    path = f"/consent/{token}"
    return f"{base}{path}" if base else path


def _ensure_column(cur, table: str, column: str, ddl: str) -> None:
    cur.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = %s
          AND column_name = %s
        """,
        (table, column),
    )
    row = cur.fetchone() or {}
    if int(row.get("cnt", 0)) == 0:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def ensure_schema() -> None:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS customer_profiles (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    user_id VARCHAR(36) NOT NULL,
                    phone VARCHAR(20) NOT NULL,
                    name VARCHAR(100) NULL,
                    email VARCHAR(200) NULL,
                    tendency VARCHAR(500) NULL,
                    medical VARCHAR(500) NULL,
                    special_notes VARCHAR(1000) NULL,
                    custom_fields JSON NULL,
                    consent_status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    consented_at DATETIME NULL,
                    consent_revoked_at DATETIME NULL,
                    consent_version VARCHAR(20) NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_user_phone (user_id, phone),
                    INDEX idx_user (user_id),
                    INDEX idx_user_consent (user_id, consent_status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            _ensure_column(cur, "customer_profiles", "name", "name VARCHAR(100) NULL")
            _ensure_column(cur, "customer_profiles", "consent_status", "consent_status VARCHAR(20) NOT NULL DEFAULT 'pending'")
            _ensure_column(cur, "customer_profiles", "consented_at", "consented_at DATETIME NULL")
            _ensure_column(cur, "customer_profiles", "consent_revoked_at", "consent_revoked_at DATETIME NULL")
            _ensure_column(cur, "customer_profiles", "consent_version", "consent_version VARCHAR(20) NULL")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS consent_links (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    user_id VARCHAR(36) NOT NULL,
                    store_id VARCHAR(36) NULL,
                    phone VARCHAR(20) NOT NULL,
                    customer_name VARCHAR(100) NULL,
                    token_hash CHAR(64) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    expires_at DATETIME NOT NULL,
                    used_at DATETIME NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_token_hash (token_hash),
                    INDEX idx_user_phone (user_id, phone),
                    INDEX idx_status_expires (status, expires_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS consent_records (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    user_id VARCHAR(36) NOT NULL,
                    phone VARCHAR(20) NOT NULL,
                    customer_name VARCHAR(100) NULL,
                    agreed TINYINT(1) NOT NULL,
                    consent_version VARCHAR(20) NOT NULL DEFAULT 'v1',
                    consent_scope JSON NULL,
                    ip_address VARCHAR(64) NULL,
                    user_agent VARCHAR(500) NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_user_phone (user_id, phone),
                    INDEX idx_created_at (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS customer_memos (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    user_id VARCHAR(36) NOT NULL,
                    phone VARCHAR(20) NOT NULL,
                    memo TEXT NOT NULL,
                    source VARCHAR(20) NOT NULL DEFAULT 'manual',
                    is_anonymized TINYINT(1) NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_user_phone_created (user_id, phone, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS customer_memo_photos (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    memo_id VARCHAR(36) NOT NULL,
                    user_id VARCHAR(36) NOT NULL,
                    phone VARCHAR(20) NOT NULL,
                    s3_key VARCHAR(512) NOT NULL,
                    caption VARCHAR(500) NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_memo_id (memo_id),
                    INDEX idx_user_phone (user_id, phone)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        conn.commit()


def _upsert_profile(uid: str, phone: str, *, name=None, consent_status=None) -> None:
    phone = _normalize_phone(phone)
    if not uid or not phone:
        return
    status = consent_status or "pending"
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO customer_profiles
                    (id, user_id, phone, name, consent_status)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    name = COALESCE(VALUES(name), name),
                    consent_status = IF(consent_status = 'consented', consent_status, VALUES(consent_status)),
                    updated_at = CURRENT_TIMESTAMP
            """, (str(uuid.uuid4()), uid, phone, name, status))
        conn.commit()


def _get_profile(uid: str, phone: str) -> dict:
    phone = _normalize_phone(phone)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, user_id, phone, name, email, tendency, medical, special_notes,
                       custom_fields, consent_status, consented_at, consent_revoked_at,
                       consent_version, created_at, updated_at
                FROM customer_profiles
                WHERE user_id = %s AND phone = %s
                LIMIT 1
            """, (uid, phone))
            row = cur.fetchone()
    if not row:
        return {
            "phone": phone,
            "name": "",
            "consent_status": "pending",
        }
    if isinstance(row.get("custom_fields"), str):
        try:
            row["custom_fields"] = json.loads(row["custom_fields"])
        except Exception:
            row["custom_fields"] = {}
    return {k: (str(v) if hasattr(v, "isoformat") else v) for k, v in row.items()}


def _is_consented(uid: str, phone: str) -> bool:
    phone = _normalize_phone(phone)
    if not phone:
        return False
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT consent_status FROM customer_profiles WHERE user_id=%s AND phone=%s LIMIT 1",
                    (uid, phone),
                )
                row = cur.fetchone()
        return (row or {}).get("consent_status") == "consented"
    except Exception as e:
        logger.warning("[Consent] status check failed uid=%s phone=%s error=%s", uid, phone, e)
        return False


def can_process_call(uid: str, call_id: str):
    if not CONSENT_ENFORCEMENT:
        return True, ""
    ensure_schema()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT caller_number FROM calls WHERE id=%s AND user_id=%s LIMIT 1",
                (call_id, uid),
            )
            call = cur.fetchone()
    if not call:
        return False, "통화를 찾을 수 없습니다"
    phone = _normalize_phone(call.get("caller_number"))
    if not phone:
        return False, "고객 전화번호가 없어 동의 여부를 확인할 수 없습니다"
    if not _is_consented(uid, phone):
        return False, "고객 동의 완료 후 AI 분석을 진행할 수 있습니다"
    return True, ""


def _photo_url(s3_key: str, expires=3600) -> str:
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET_NAME, "Key": s3_key},
        ExpiresIn=expires,
    )


def _handle_consent_link(event: dict, uid: str, phone: str) -> dict:
    body = _json_body(event)
    phone = _normalize_phone(phone)
    if not phone:
        return _response(400, {"error": "phone 필수"}, event)

    name = (body.get("name") or body.get("customer_name") or "").strip() or None
    store_id = (body.get("store_id") or "").strip() or None

    _upsert_profile(uid, phone, name=name, consent_status="pending")

    token = secrets.token_urlsafe(32)
    token_hash = _token_hash(token)
    expires_at = datetime.utcnow() + timedelta(days=CONSENT_TOKEN_TTL_DAYS)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE consent_links
                SET status='revoked'
                WHERE user_id=%s AND phone=%s AND status='active'
            """, (uid, phone))
            cur.execute("""
                INSERT INTO consent_links
                    (id, user_id, store_id, phone, customer_name, token_hash, status, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'active', %s)
            """, (str(uuid.uuid4()), uid, store_id, phone, name, token_hash, expires_at))
        conn.commit()

    return _response(201, {
        "phone": phone,
        "customer_name": name or "",
        "consent_status": "pending",
        "token": token,
        "consent_url": _consent_url(event, token),
        "expires_at": expires_at.isoformat() + "Z",
    }, event)


def _get_consent_link(token: str) -> dict | None:
    h = _token_hash(token)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, user_id, store_id, phone, customer_name, status, expires_at, used_at, created_at
                FROM consent_links
                WHERE token_hash=%s
                LIMIT 1
            """, (h,))
            row = cur.fetchone()
    return row


def _handle_consent_get(event: dict, token: str) -> dict:
    ensure_schema()
    link = _get_consent_link(token)
    if not link:
        return _response(404, {"error": "동의 링크를 찾을 수 없습니다"}, event)

    now = datetime.utcnow()
    expires_at = link.get("expires_at")
    if link.get("status") != "active":
        return _response(410, {"error": "이미 사용되었거나 비활성화된 동의 링크입니다"}, event)
    if expires_at and expires_at < now:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE consent_links SET status='expired' WHERE id=%s", (link["id"],))
            conn.commit()
        return _response(410, {"error": "만료된 동의 링크입니다"}, event)

    return _response(200, {
        "phone": link["phone"],
        "customer_name": link.get("customer_name") or "",
        "consent_version": CONSENT_VERSION,
        "expires_at": str(link.get("expires_at") or ""),
        "title": "개인정보 수집·이용 및 AI 분석 동의",
        "description": "통화 내용과 메모를 고객관리 목적으로 정리하기 위한 동의입니다.",
    }, event)


def _handle_consent_submit(event: dict, token: str) -> dict:
    ensure_schema()
    body = _json_body(event)
    if "agreed" not in body:
        return _response(400, {"error": "agreed 필수"}, event)

    raw_agreed = body.get("agreed")
    agreed = raw_agreed is True or str(raw_agreed).lower() in ("true", "1", "yes", "y", "동의")

    link = _get_consent_link(token)
    if not link:
        return _response(404, {"error": "동의 링크를 찾을 수 없습니다"}, event)
    if link.get("status") != "active":
        return _response(410, {"error": "이미 사용되었거나 비활성화된 동의 링크입니다"}, event)
    if link.get("expires_at") and link["expires_at"] < datetime.utcnow():
        return _response(410, {"error": "만료된 동의 링크입니다"}, event)

    status = "consented" if agreed else "declined"
    phone = _normalize_phone(link["phone"])
    name = (body.get("customer_name") or link.get("customer_name") or "").strip() or None

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO customer_profiles
                    (id, user_id, phone, name, consent_status, consented_at, consent_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    name = COALESCE(VALUES(name), name),
                    consent_status = VALUES(consent_status),
                    consented_at = VALUES(consented_at),
                    consent_version = VALUES(consent_version),
                    updated_at = CURRENT_TIMESTAMP
            """, (
                str(uuid.uuid4()),
                link["user_id"],
                phone,
                name,
                status,
                datetime.utcnow() if agreed else None,
                CONSENT_VERSION,
            ))
            cur.execute("""
                INSERT INTO consent_records
                    (id, user_id, phone, customer_name, agreed, consent_version,
                     consent_scope, ip_address, user_agent)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                str(uuid.uuid4()),
                link["user_id"],
                phone,
                name,
                1 if agreed else 0,
                CONSENT_VERSION,
                json.dumps({
                    "call_recording": agreed,
                    "stt": agreed,
                    "ai_summary": agreed,
                    "customer_analysis": agreed,
                    "manual_memo": True,
                }, ensure_ascii=False),
                _client_ip(event),
                _header(event, "user-agent")[:500],
            ))
            cur.execute("""
                UPDATE consent_links
                SET status='used', used_at=CURRENT_TIMESTAMP
                WHERE id=%s
            """, (link["id"],))
        conn.commit()

    return _response(200, {
        "message": "동의 결과가 저장되었습니다",
        "agreed": agreed,
        "consent_status": status,
    }, event)


def _handle_customer_get(event: dict, uid: str, phone: str) -> dict:
    phone = _normalize_phone(phone)
    if not phone:
        return _response(400, {"error": "phone 필수"}, event)
    _upsert_profile(uid, phone)
    profile = _get_profile(uid, phone)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT analysis, call_count, generated_at
                FROM customer_analysis
                WHERE user_id=%s AND phone=%s
                LIMIT 1
            """, (uid, phone))
            analysis = cur.fetchone() or {}

    analysis = {k: (str(v) if hasattr(v, "isoformat") else v) for k, v in analysis.items()}
    if CONSENT_ENFORCEMENT and profile.get("consent_status") != "consented":
        analysis = {
            "analysis": "",
            "locked": True,
            "message": "동의 완료 후 AI 고객 분석을 사용할 수 있습니다.",
        }

    return _response(200, {
        "profile": profile,
        "analysis": analysis,
    }, event)


def _handle_customer_patch(event: dict, uid: str, phone: str) -> dict:
    phone = _normalize_phone(phone)
    if not phone:
        return _response(400, {"error": "phone 필수"}, event)

    body = _json_body(event)
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip()
    tendency = (body.get("tendency") or "").strip()
    medical = (body.get("medical") or "").strip()
    special_notes = (body.get("special_notes") or "").strip()
    custom_fields = body.get("custom_fields")
    custom_json = json.dumps(custom_fields, ensure_ascii=False) if custom_fields is not None else None

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO customer_profiles
                    (id, user_id, phone, name, email, tendency, medical, special_notes, custom_fields)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    name=VALUES(name),
                    email=VALUES(email),
                    tendency=VALUES(tendency),
                    medical=VALUES(medical),
                    special_notes=VALUES(special_notes),
                    custom_fields=VALUES(custom_fields),
                    updated_at=CURRENT_TIMESTAMP
            """, (
                str(uuid.uuid4()),
                uid,
                phone,
                name or None,
                email or None,
                tendency or None,
                medical or None,
                special_notes or None,
                custom_json,
            ))
        conn.commit()

    return _response(200, {"message": "고객 주요 정보 저장 완료"}, event)


def _handle_memo_create(event: dict, uid: str, phone: str) -> dict:
    phone = _normalize_phone(phone)
    if not phone:
        return _response(400, {"error": "phone 필수"}, event)
    body = _json_body(event)
    memo = (body.get("memo") or "").strip()
    if not memo:
        return _response(400, {"error": "memo 필수"}, event)

    is_anonymized = bool(body.get("is_anonymized", False))
    _upsert_profile(uid, phone)

    memo_id = str(uuid.uuid4())
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO customer_memos
                    (id, user_id, phone, memo, source, is_anonymized)
                VALUES (%s, %s, %s, %s, 'manual', %s)
            """, (memo_id, uid, phone, memo, 1 if is_anonymized else 0))
        conn.commit()

    return _response(201, {
        "id": memo_id,
        "phone": phone,
        "memo": memo,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }, event)


def _verify_memo(uid: str, phone: str, memo_id: str) -> bool:
    phone = _normalize_phone(phone)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM customer_memos
                WHERE id=%s AND user_id=%s AND phone=%s
                LIMIT 1
            """, (memo_id, uid, phone))
            return cur.fetchone() is not None


def _photo_content_type(file_name: str) -> tuple[str, str]:
    ext = os.path.splitext(file_name or "")[-1].lower() or ".jpg"
    return ext, _PHOTO_CONTENT_TYPE.get(ext, "image/jpeg")


def _handle_memo_photo_upload_url(event: dict, uid: str, phone: str, memo_id: str) -> dict:
    phone = _normalize_phone(phone)
    if not _verify_memo(uid, phone, memo_id):
        return _response(404, {"error": "메모를 찾을 수 없습니다"}, event)

    body = _json_body(event)
    file_name = (body.get("file_name") or "memo-photo.jpg").strip()
    ext, content_type = _photo_content_type(file_name)

    photo_id = str(uuid.uuid4())
    s3_key = f"customer-memos/{uid}/{phone}/{memo_id}/{photo_id}{ext}"

    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET_NAME, "Key": s3_key, "ContentType": content_type},
        ExpiresIn=600,
    )

    return _response(200, {
        "photo_id": photo_id,
        "memo_id": memo_id,
        "s3_key": s3_key,
        "upload_url": upload_url,
        "upload_headers": {"Content-Type": content_type},
    }, event)


def _handle_memo_photo_save(event: dict, uid: str, phone: str, memo_id: str) -> dict:
    phone = _normalize_phone(phone)
    if not _verify_memo(uid, phone, memo_id):
        return _response(404, {"error": "메모를 찾을 수 없습니다"}, event)

    body = _json_body(event)
    photo_id = (body.get("photo_id") or "").strip() or str(uuid.uuid4())
    s3_key = (body.get("s3_key") or "").strip()
    caption = (body.get("caption") or "").strip() or None
    if not s3_key:
        return _response(400, {"error": "s3_key 필수"}, event)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO customer_memo_photos
                    (id, memo_id, user_id, phone, s3_key, caption)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    s3_key=VALUES(s3_key),
                    caption=VALUES(caption)
            """, (photo_id, memo_id, uid, phone, s3_key, caption))
        conn.commit()

    return _response(201, {
        "id": photo_id,
        "memo_id": memo_id,
        "url": _photo_url(s3_key),
        "caption": caption or "",
    }, event)


def _handle_history(event: dict, uid: str, phone: str) -> dict:
    phone = _normalize_phone(phone)
    entries = []

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.id AS call_id, c.created_at, c.status, c.caller_number,
                       s.summary, s.category, n.memo AS call_memo
                FROM calls c
                LEFT JOIN summaries s ON s.call_id = c.id
                LEFT JOIN call_notes n ON n.call_id = c.id
                WHERE c.user_id=%s AND c.caller_number=%s
                ORDER BY c.created_at DESC
                LIMIT 50
            """, (uid, phone))
            calls = cur.fetchall() or []

            cur.execute("""
                SELECT m.id, m.memo, m.created_at, m.is_anonymized,
                       p.id AS photo_id, p.s3_key, p.caption, p.created_at AS photo_created_at
                FROM customer_memos m
                LEFT JOIN customer_memo_photos p ON p.memo_id = m.id
                WHERE m.user_id=%s AND m.phone=%s
                ORDER BY m.created_at DESC, p.created_at ASC
                LIMIT 200
            """, (uid, phone))
            memo_rows = cur.fetchall() or []

            cur.execute("""
                SELECT agreed, consent_version, created_at
                FROM consent_records
                WHERE user_id=%s AND phone=%s
                ORDER BY created_at DESC
                LIMIT 20
            """, (uid, phone))
            consent_rows = cur.fetchall() or []

    for c in calls:
        entries.append({
            "type": "call",
            "id": c.get("call_id"),
            "created_at": str(c.get("created_at") or ""),
            "title": "통화 기록",
            "summary": c.get("summary") or "",
            "category": c.get("category") or "",
            "memo": c.get("call_memo") or "",
            "status": c.get("status") or "",
            "photos": [],
        })

    memo_map = {}
    for r in memo_rows:
        memo_id = r["id"]
        if memo_id not in memo_map:
            memo_map[memo_id] = {
                "type": "manual_memo",
                "id": memo_id,
                "created_at": str(r.get("created_at") or ""),
                "title": "수동 메모",
                "memo": r.get("memo") or "",
                "is_anonymized": bool(r.get("is_anonymized")),
                "photos": [],
            }
        if r.get("photo_id") and r.get("s3_key"):
            memo_map[memo_id]["photos"].append({
                "id": r.get("photo_id"),
                "url": _photo_url(r.get("s3_key")),
                "caption": r.get("caption") or "",
                "created_at": str(r.get("photo_created_at") or ""),
            })

    entries.extend(memo_map.values())

    for r in consent_rows:
        agreed = bool(r.get("agreed"))
        entries.append({
            "type": "consent",
            "id": f"consent-{r.get('created_at')}",
            "created_at": str(r.get("created_at") or ""),
            "title": "동의 완료" if agreed else "동의 거절",
            "summary": f"동의 버전: {r.get('consent_version') or CONSENT_VERSION}",
            "photos": [],
        })

    entries.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    return _response(200, {
        "phone": phone,
        "items": entries,
        "count": len(entries),
    }, event)


def fetch_customer_analysis_items(uid: str, phone: str) -> list[str]:
    ensure_schema()
    phone = _normalize_phone(phone)
    if CONSENT_ENFORCEMENT and not _is_consented(uid, phone):
        return []

    items = []
    profile = _get_profile(uid, phone)
    profile_lines = []
    for label, key in [
        ("고객명", "name"),
        ("성향", "tendency"),
        ("주의사항", "medical"),
        ("특이사항", "special_notes"),
    ]:
        value = (profile.get(key) or "").strip() if isinstance(profile.get(key), str) else profile.get(key)
        if value:
            profile_lines.append(f"{label}: {value}")
    if profile_lines:
        items.append("[고객 주요 정보] " + " / ".join(profile_lines))

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.summary, s.category, n.memo AS call_memo, c.created_at
                FROM calls c
                LEFT JOIN summaries s ON s.call_id = c.id
                LEFT JOIN call_notes n ON n.call_id = c.id
                WHERE c.user_id=%s AND c.caller_number=%s
                ORDER BY c.created_at DESC
                LIMIT 20
            """, (uid, phone))
            calls = cur.fetchall() or []

            cur.execute("""
                SELECT m.memo, m.created_at, GROUP_CONCAT(p.caption SEPARATOR ' / ') AS photo_captions
                FROM customer_memos m
                LEFT JOIN customer_memo_photos p ON p.memo_id = m.id
                WHERE m.user_id=%s AND m.phone=%s
                GROUP BY m.id, m.memo, m.created_at
                ORDER BY m.created_at DESC
                LIMIT 30
            """, (uid, phone))
            memos = cur.fetchall() or []

    for r in calls:
        summary = (r.get("summary") or "").strip()
        call_memo = (r.get("call_memo") or "").strip()
        category = (r.get("category") or "").strip()
        parts = []
        if summary:
            parts.append(f"요약: {summary}")
        if call_memo:
            parts.append(f"통화 메모: {call_memo}")
        if parts:
            items.append(f"[통화/{category or '기타'}] " + " / ".join(parts))

    for r in memos:
        memo = (r.get("memo") or "").strip()
        captions = (r.get("photo_captions") or "").strip()
        parts = []
        if memo:
            parts.append(f"메모: {memo}")
        if captions:
            parts.append(f"이미지 설명: {captions}")
        if parts:
            items.append("[수동 히스토리] " + " / ".join(parts))

    return items[:40]


def lambda_handler(event, context):
    ensure_schema()

    method = _method(event)
    path = _normalize_path(event)

    if method == "OPTIONS":
        return _response(200, {"message": "OK"}, event)

    parts = [p for p in path.strip("/").split("/") if p]

    if len(parts) == 2 and parts[0] == "consent":
        token = parts[1]
        if method == "GET":
            return _handle_consent_get(event, token)
        if method == "POST":
            return _handle_consent_submit(event, token)
        return _response(405, {"error": "Method not allowed"}, event)

    uid = _get_current_user_id(event)
    if not uid:
        return _response(401, {"error": "인증 필요"}, event)

    if not parts or parts[0] != "customers":
        return _response(404, {"error": "Not found", "path": path}, event)

    if len(parts) < 2:
        return _response(400, {"error": "phone 필수"}, event)

    phone = unquote(parts[1])

    if len(parts) == 2:
        if method == "GET":
            return _handle_customer_get(event, uid, phone)
        if method == "PATCH":
            return _handle_customer_patch(event, uid, phone)

    if len(parts) == 3 and parts[2] == "consent-link" and method == "POST":
        return _handle_consent_link(event, uid, phone)

    if len(parts) == 3 and parts[2] == "history" and method == "GET":
        return _handle_history(event, uid, phone)

    if len(parts) == 3 and parts[2] == "memos" and method == "POST":
        return _handle_memo_create(event, uid, phone)

    if len(parts) == 6 and parts[2] == "memos" and parts[4] == "photos" and parts[5] == "upload-url" and method == "POST":
        return _handle_memo_photo_upload_url(event, uid, phone, parts[3])

    if len(parts) == 5 and parts[2] == "memos" and parts[4] == "photos" and method == "POST":
        return _handle_memo_photo_save(event, uid, phone, parts[3])

    return _response(404, {"error": "Not found", "path": path}, event)
