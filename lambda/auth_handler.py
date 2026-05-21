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

import boto3 as _boto3

def _get_db_password() -> str:
    secret_name = os.environ.get("DB_SECRET_NAME", "")
    if secret_name:
        try:
            sm = _boto3.client("secretsmanager", region_name="ap-northeast-2")
            secret = sm.get_secret_value(SecretId=secret_name)
            data = json.loads(secret["SecretString"])
            return data.get("password") or data.get("db_password", "")
        except Exception as e:
            logger.error(f"[DB] Secrets Manager 조회 실패: {e}")
    return os.environ.get("DB_PASSWORD", "")

def get_db():
    try:
        conn = pymysql.connect(
            host=os.environ.get("DB_HOST", "call-recorder-db.czem0u8m8xfi.ap-northeast-2.rds.amazonaws.com"),
            user=os.environ.get("DB_USER", "admin"),
            password=_get_db_password(),
            db=os.environ.get("DB_NAME", "call_recorder"),
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
        )
        return conn
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
        body     = json.loads(event.get("body") or "{}")
        kakao_id = str(body.get("kakao_id", "")).strip()
        email    = body.get("email", "")
        nickname = body.get("nickname", "")

        if not kakao_id:
            return _response(400, {"error": "kakao_id 필수"})

        firebase_uid = f"kakao:{kakao_id}"
        custom_token = firebase_auth.create_custom_token(firebase_uid)
        custom_token_str = custom_token.decode("utf-8") if isinstance(custom_token, bytes) else custom_token

        db = get_db()
        user_uuid = None
        if db:
            try:
                with db.cursor() as cursor:
                    # 기존 유저 조회
                    cursor.execute(
                        "SELECT id FROM users WHERE kakao_id = %s LIMIT 1",
                        (kakao_id,)
                    )
                    user = cursor.fetchone()
                    if user:
                        user_uuid = user["id"]
                        # firebase_uid 업데이트
                        cursor.execute(
                            "UPDATE users SET firebase_uid = %s WHERE id = %s",
                            (firebase_uid, user_uuid)
                        )
                    else:
                        # 신규 유저 생성
                        user_uuid = str(__import__("uuid").uuid4())
                        cursor.execute("""
                            INSERT INTO users (id, firebase_uid, kakao_id, name, role)
                            VALUES (%s, %s, %s, %s, 'OWNER')
                        """, (user_uuid, firebase_uid, kakao_id, nickname))
                db.commit()
            except Exception as e:
                logger.error(f"[Auth] DB upsert 실패: {e}")

        invalidate_user_cache(firebase_uid)
        return _response(200, {
            "custom_token": custom_token_str,
            "uid": firebase_uid,
            "user_uuid": user_uuid,
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