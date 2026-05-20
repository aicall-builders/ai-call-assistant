"""
auth_handler.py — Firebase 토큰 검증 결과 + 유저 DB 조회 Redis 캐싱
변경점:
  - Firebase Admin SDK 검증 결과를 Redis에 55분 캐싱
  - RDS 유저 정보 조회 결과도 5분 캐싱
  - 토큰 무효화(로그아웃) 시 캐시 삭제
"""
import os
import json
import hashlib
import logging
import pymysql
import firebase_admin
from firebase_admin import auth as firebase_auth, credentials

from redis_client import (
    cache_get, cache_set, cache_delete,
    TTL_FIREBASE_TOKEN, TTL_USER_INFO,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ── Firebase Admin SDK 초기화 (Lambda 컨테이너 재사용) ────────────────────────
if not firebase_admin._apps:
    cred = credentials.Certificate(
        json.loads(os.environ.get("FIREBASE_SERVICE_ACCOUNT", "{}"))
    )
    firebase_admin.initialize_app(cred)

# ── DB 설정 ───────────────────────────────────────────────────────────────────
DB_CONFIG = {
    "host":     os.environ.get("DB_HOST", "call-recorder-db.czem0u8m8xfi.ap-northeast-2.rds.amazonaws.com"),
    "user":     os.environ.get("DB_USER", ""),
    "password": os.environ.get("DB_PASSWORD", ""),
    "db":       os.environ.get("DB_NAME", "call_recorder"),
    "charset":  "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
    "connect_timeout": 5,
}

_db_conn = None  # DB 연결 싱글턴


def get_db():
    global _db_conn
    try:
        if _db_conn is None or not _db_conn.open:
            _db_conn = pymysql.connect(**DB_CONFIG)
        return _db_conn
    except Exception as e:
        logger.error(f"[Auth] DB 연결 실패: {e}")
        return None


# ── Firebase 토큰 검증 ────────────────────────────────────────────────────────

def _token_cache_key(token: str) -> str:
    """토큰 전체를 키로 쓰면 너무 길어서 SHA256 해시 사용"""
    return f"auth:token:{hashlib.sha256(token.encode()).hexdigest()}"


def verify_firebase_token(id_token: str) -> dict | None:
    """
    Firebase ID 토큰 검증.
    Redis 캐시 hit → 즉시 반환 (Firebase SDK 호출 없음)
    Redis 캐시 miss → Firebase 검증 후 캐싱
    """
    cache_key = _token_cache_key(id_token)

    # 캐시 조회
    cached = cache_get(cache_key)
    if cached is not None:
        logger.info(f"[Auth] 토큰 캐시 hit uid={cached.get('uid')}")
        return cached

    # Firebase 검증
    try:
        decoded = firebase_auth.verify_id_token(id_token, check_revoked=True)
        payload = {
            "uid":   decoded["uid"],
            "email": decoded.get("email", ""),
            "exp":   decoded.get("exp", 0),
        }
        cache_set(cache_key, payload, TTL_FIREBASE_TOKEN)
        logger.info(f"[Auth] Firebase 검증 성공, 캐시 저장 uid={payload['uid']}")
        return payload

    except firebase_auth.RevokedIdTokenError:
        logger.warning("[Auth] 폐기된 토큰")
        return None
    except firebase_auth.InvalidIdTokenError as e:
        logger.warning(f"[Auth] 유효하지 않은 토큰: {e}")
        return None
    except Exception as e:
        logger.error(f"[Auth] Firebase 검증 오류: {e}")
        return None


def invalidate_token_cache(id_token: str):
    """로그아웃 시 해당 토큰 캐시 삭제"""
    cache_delete(_token_cache_key(id_token))
    logger.info("[Auth] 토큰 캐시 삭제 완료")


# ── 유저 정보 조회 ────────────────────────────────────────────────────────────

def get_user_info(uid: str) -> dict | None:
    """
    Firebase UID로 RDS 유저 정보 조회.
    Redis 캐시 5분 → DB 조회 순서.
    """
    cache_key = f"auth:user:{uid}"

    cached = cache_get(cache_key)
    if cached is not None:
        logger.info(f"[Auth] 유저 캐시 hit uid={uid}")
        return cached

    db = get_db()
    if db is None:
        return None

    try:
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT id, uid, email, name, plan, created_at FROM users WHERE uid = %s LIMIT 1",
                (uid,)
            )
            user = cursor.fetchone()

        if user:
            # datetime → str 변환 후 캐싱
            user_data = {k: str(v) if hasattr(v, 'isoformat') else v for k, v in user.items()}
            cache_set(cache_key, user_data, TTL_USER_INFO)
            logger.info(f"[Auth] DB 유저 조회 완료, 캐시 저장 uid={uid}")
            return user_data
        else:
            logger.warning(f"[Auth] 유저 없음 uid={uid}")
            return None

    except Exception as e:
        logger.error(f"[Auth] DB 조회 오류: {e}")
        return None


def invalidate_user_cache(uid: str):
    """유저 정보 변경 시 캐시 무효화"""
    cache_delete(f"auth:user:{uid}")


# ── Lambda 핸들러 ──────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    path   = event.get("path", "")
    method = event.get("httpMethod", "POST")

    if path == "/auth/logout" and method == "POST":
        return _handle_logout(event)

    if path == "/auth/verify" and method == "POST":
        return _handle_verify(event)

    return _response(404, {"error": "Not found"})


def _handle_verify(event: dict) -> dict:
    """POST /auth/verify — 토큰 검증 + 유저 정보 반환"""
    headers = event.get("headers", {}) or {}
    auth_header = headers.get("Authorization", headers.get("authorization", ""))

    if not auth_header.startswith("Bearer "):
        return _response(401, {"error": "Authorization 헤더 없음"})

    id_token = auth_header[7:]  # "Bearer " 제거

    token_payload = verify_firebase_token(id_token)
    if not token_payload:
        return _response(401, {"error": "유효하지 않은 토큰"})

    user = get_user_info(token_payload["uid"])
    if not user:
        return _response(404, {"error": "유저 없음"})

    return _response(200, {"user": user})


def _handle_logout(event: dict) -> dict:
    """POST /auth/logout — 캐시 삭제"""
    headers = event.get("headers", {}) or {}
    auth_header = headers.get("Authorization", headers.get("authorization", ""))

    if auth_header.startswith("Bearer "):
        id_token = auth_header[7:]
        invalidate_token_cache(id_token)

    body = json.loads(event.get("body") or "{}")
    uid = body.get("uid", "")
    if uid:
        invalidate_user_cache(uid)

    return _response(200, {"message": "로그아웃 완료"})


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }
