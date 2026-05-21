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

if not firebase_admin._apps:
    import base64
    sa_b64 = os.environ.get("FIREBASE_SERVICE_ACCOUNT_BASE64", "")
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
    if sa_b64:
        sa_json = base64.b64decode(sa_b64).decode("utf-8")
    cred = credentials.Certificate(json.loads(sa_json))
    firebase_admin.initialize_app(cred)

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "call-recorder-db.czem0u8m8xfi.ap-northeast-2.rds.amazonaws.com"),
    "user": os.environ.get("DB_USER", ""),
    "password": os.environ.get("DB_PASSWORD", ""),
    "db": os.environ.get("DB_NAME", "call_recorder"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
    "connect_timeout": 5,
}

_db_conn = None


def get_db():
    global _db_conn
    try:
        if _db_conn is None or not _db_conn.open:
            _db_conn = pymysql.connect(**DB_CONFIG)
        return _db_conn
    except Exception as e:
        logger.error(f"[Auth] DB connection failed: {e}")
        return None


def _token_cache_key(token: str) -> str:
    return f"auth:token:{hashlib.sha256(token.encode()).hexdigest()}"


def verify_firebase_token(id_token: str):
    cache_key = _token_cache_key(id_token)
    cached = cache_get(cache_key)
    if cached is not None:
        logger.info(f"[Auth] token cache hit uid={cached.get('uid')}")
        return cached
    try:
        decoded = firebase_auth.verify_id_token(id_token, check_revoked=True)
        payload = {
            "uid": decoded["uid"],
            "email": decoded.get("email", ""),
            "exp": decoded.get("exp", 0),
        }
        cache_set(cache_key, payload, TTL_FIREBASE_TOKEN)
        logger.info(f"[Auth] Firebase verified uid={payload['uid']}")
        return payload
    except firebase_auth.RevokedIdTokenError:
        logger.warning("[Auth] revoked token")
        return None
    except firebase_auth.InvalidIdTokenError as e:
        logger.warning(f"[Auth] invalid token: {e}")
        return None
    except Exception as e:
        logger.error(f"[Auth] Firebase error: {e}")
        return None


def invalidate_token_cache(id_token: str):
    cache_delete(_token_cache_key(id_token))
    logger.info("[Auth] token cache deleted")


def get_user_info(uid: str):
    cache_key = f"auth:user:{uid}"
    cached = cache_get(cache_key)
    if cached is not None:
        logger.info(f"[Auth] user cache hit uid={uid}")
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
            user_data = {k: str(v) if hasattr(v, "isoformat") else v for k, v in user.items()}
            cache_set(cache_key, user_data, TTL_USER_INFO)
            logger.info(f"[Auth] user loaded uid={uid}")
            return user_data
        logger.warning(f"[Auth] user not found uid={uid}")
        return None
    except Exception as e:
        logger.error(f"[Auth] DB error: {e}")
        return None


def invalidate_user_cache(uid: str):
    cache_delete(f"auth:user:{uid}")


def lambda_handler(event: dict, context) -> dict:
    path = event.get("path", "")
    method = event.get("httpMethod", "POST")
    if method == "OPTIONS":
        return _response(200, {})
    if path == "/auth/kakao" and method == "POST":
        return _handle_kakao(event)
    if path == "/auth/logout" and method == "POST":
        return _handle_logout(event)
    if path == "/auth/verify" and method == "POST":
        return _handle_verify(event)
    return _response(404, {"error": "Not found"})

def _handle_kakao(event: dict) -> dict:
    try:
        body = json.loads(event.get("body") or "{}")
        kakao_access_token = body.get("access_token", "").strip()
        if not kakao_access_token:
            return _response(400, {"error": "access_token 필수"})

        import requests as req
        kakao_resp = req.get(
            "https://kapi.kakao.com/v2/user/me",
            headers={"Authorization": f"Bearer {kakao_access_token}"},
            timeout=5,
        )
        if kakao_resp.status_code != 200:
            return _response(401, {"error": "카카오 토큰 검증 실패"})

        kakao_user    = kakao_resp.json()
        kakao_id      = str(kakao_user["id"])
        kakao_account = kakao_user.get("kakao_account", {})
        email         = kakao_account.get("email", f"{kakao_id}@kakao.com")
        nickname      = kakao_account.get("profile", {}).get("nickname", "")
        uid           = f"kakao:{kakao_id}"

        custom_token = firebase_auth.create_custom_token(uid)
        custom_token_str = custom_token.decode("utf-8") if isinstance(custom_token, bytes) else custom_token

        db = get_db()
        if db:
            try:
                with db.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO users (id, uid, email, name, plan)
                        VALUES (UUID(), %s, %s, %s, 'free')
                        ON DUPLICATE KEY UPDATE
                            email = VALUES(email),
                            name  = VALUES(name)
                    """, (uid, email, nickname))
                db.commit()
            except Exception as e:
                logger.error(f"[Auth] DB upsert 실패: {e}")

        invalidate_user_cache(uid)
        return _response(200, {
            "custom_token": custom_token_str,
            "uid": uid,
            "email": email,
            "name": nickname,
        })
    except Exception as e:
        logger.exception(f"[Auth] kakao 처리 오류: {e}")
        return _response(500, {"error": "내부 오류"})


def _handle_verify(event: dict) -> dict:
    headers = event.get("headers", {}) or {}
    auth_header = headers.get("Authorization", headers.get("authorization", ""))
    if not auth_header.startswith("Bearer "):
        return _response(401, {"error": "No Authorization header"})
    id_token = auth_header[7:]
    token_payload = verify_firebase_token(id_token)
    if not token_payload:
        return _response(401, {"error": "Invalid token"})
    user = get_user_info(token_payload["uid"])
    if not user:
        return _response(404, {"error": "User not found"})
    return _response(200, {"user": user})


def _handle_logout(event: dict) -> dict:
    headers = event.get("headers", {}) or {}
    auth_header = headers.get("Authorization", headers.get("authorization", ""))
    if auth_header.startswith("Bearer "):
        invalidate_token_cache(auth_header[7:])
    body = json.loads(event.get("body") or "{}")
    uid = body.get("uid", "")
    if uid:
        invalidate_user_cache(uid)
    return _response(200, {"message": "logged out"})


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "https://dk1k75g0ji3vw.cloudfront.net",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
        },
        "body": json.dumps(body, ensure_ascii=False),
    }