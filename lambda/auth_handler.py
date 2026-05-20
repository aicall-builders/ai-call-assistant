"""
auth_handler.py ??Firebase ?좏겙 寃利?寃곌낵 + ?좎? DB 議고쉶 Redis 罹먯떛
蹂寃쎌젏:
  - Firebase Admin SDK 寃利?寃곌낵瑜?Redis??55遺?罹먯떛
  - RDS ?좎? ?뺣낫 議고쉶 寃곌낵??5遺?罹먯떛
  - ?좏겙 臾댄슚??濡쒓렇?꾩썐) ??罹먯떆 ??젣
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

# ?? Firebase Admin SDK 珥덇린??(Lambda 而⑦뀒?대꼫 ?ъ궗?? ????????????????????????
if not firebase_admin._apps:
    import base64
    sa_b64 = os.environ.get("FIREBASE_SERVICE_ACCOUNT_BASE64", "")
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
    if sa_b64:
        sa_json = base64.b64decode(sa_b64).decode("utf-8")
    cred = credentials.Certificate(json.loads(sa_json))
    firebase_admin.initialize_app(cred)

# ?? DB ?ㅼ젙 ???????????????????????????????????????????????????????????????????
DB_CONFIG = {
    "host":     os.environ.get("DB_HOST", "call-recorder-db.czem0u8m8xfi.ap-northeast-2.rds.amazonaws.com"),
    "user":     os.environ.get("DB_USER", ""),
    "password": os.environ.get("DB_PASSWORD", ""),
    "db":       os.environ.get("DB_NAME", "call_recorder"),
    "charset":  "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
    "connect_timeout": 5,
}

_db_conn = None  # DB ?곌껐 ?깃???

def get_db():
    global _db_conn
    try:
        if _db_conn is None or not _db_conn.open:
            _db_conn = pymysql.connect(**DB_CONFIG)
        return _db_conn
    except Exception as e:
        logger.error(f"[Auth] DB ?곌껐 ?ㅽ뙣: {e}")
        return None


# ?? Firebase ?좏겙 寃利?????????????????????????????????????????????????????????

def _token_cache_key(token: str) -> str:
    """?좏겙 ?꾩껜瑜??ㅻ줈 ?곕㈃ ?덈Т 湲몄뼱??SHA256 ?댁떆 ?ъ슜"""
    return f"auth:token:{hashlib.sha256(token.encode()).hexdigest()}"


def verify_firebase_token(id_token: str) -> dict | None:
    """
    Firebase ID ?좏겙 寃利?
    Redis 罹먯떆 hit ??利됱떆 諛섑솚 (Firebase SDK ?몄텧 ?놁쓬)
    Redis 罹먯떆 miss ??Firebase 寃利???罹먯떛
    """
    cache_key = _token_cache_key(id_token)

    # 罹먯떆 議고쉶
    cached = cache_get(cache_key)
    if cached is not None:
        logger.info(f"[Auth] ?좏겙 罹먯떆 hit uid={cached.get('uid')}")
        return cached

    # Firebase 寃利?    try:
        decoded = firebase_auth.verify_id_token(id_token, check_revoked=True)
        payload = {
            "uid":   decoded["uid"],
            "email": decoded.get("email", ""),
            "exp":   decoded.get("exp", 0),
        }
        cache_set(cache_key, payload, TTL_FIREBASE_TOKEN)
        logger.info(f"[Auth] Firebase 寃利??깃났, 罹먯떆 ???uid={payload['uid']}")
        return payload

    except firebase_auth.RevokedIdTokenError:
        logger.warning("[Auth] ?먭린???좏겙")
        return None
    except firebase_auth.InvalidIdTokenError as e:
        logger.warning(f"[Auth] ?좏슚?섏? ?딆? ?좏겙: {e}")
        return None
    except Exception as e:
        logger.error(f"[Auth] Firebase 寃利??ㅻ쪟: {e}")
        return None


def invalidate_token_cache(id_token: str):
    """濡쒓렇?꾩썐 ???대떦 ?좏겙 罹먯떆 ??젣"""
    cache_delete(_token_cache_key(id_token))
    logger.info("[Auth] ?좏겙 罹먯떆 ??젣 ?꾨즺")


# ?? ?좎? ?뺣낫 議고쉶 ????????????????????????????????????????????????????????????

def get_user_info(uid: str) -> dict | None:
    """
    Firebase UID濡?RDS ?좎? ?뺣낫 議고쉶.
    Redis 罹먯떆 5遺???DB 議고쉶 ?쒖꽌.
    """
    cache_key = f"auth:user:{uid}"

    cached = cache_get(cache_key)
    if cached is not None:
        logger.info(f"[Auth] ?좎? 罹먯떆 hit uid={uid}")
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
            # datetime ??str 蹂????罹먯떛
            user_data = {k: str(v) if hasattr(v, 'isoformat') else v for k, v in user.items()}
            cache_set(cache_key, user_data, TTL_USER_INFO)
            logger.info(f"[Auth] DB ?좎? 議고쉶 ?꾨즺, 罹먯떆 ???uid={uid}")
            return user_data
        else:
            logger.warning(f"[Auth] ?좎? ?놁쓬 uid={uid}")
            return None

    except Exception as e:
        logger.error(f"[Auth] DB 議고쉶 ?ㅻ쪟: {e}")
        return None


def invalidate_user_cache(uid: str):
    """?좎? ?뺣낫 蹂寃???罹먯떆 臾댄슚??""
    cache_delete(f"auth:user:{uid}")


# ?? Lambda ?몃뱾????????????????????????????????????????????????????????????????

def lambda_handler(event: dict, context) -> dict:
    path   = event.get("path", "")
    method = event.get("httpMethod", "POST")

    if path == "/auth/logout" and method == "POST":
        return _handle_logout(event)

    if path == "/auth/verify" and method == "POST":
        return _handle_verify(event)

    return _response(404, {"error": "Not found"})


def _handle_verify(event: dict) -> dict:
    """POST /auth/verify ???좏겙 寃利?+ ?좎? ?뺣낫 諛섑솚"""
    headers = event.get("headers", {}) or {}
    auth_header = headers.get("Authorization", headers.get("authorization", ""))

    if not auth_header.startswith("Bearer "):
        return _response(401, {"error": "Authorization ?ㅻ뜑 ?놁쓬"})

    id_token = auth_header[7:]  # "Bearer " ?쒓굅

    token_payload = verify_firebase_token(id_token)
    if not token_payload:
        return _response(401, {"error": "?좏슚?섏? ?딆? ?좏겙"})

    user = get_user_info(token_payload["uid"])
    if not user:
        return _response(404, {"error": "?좎? ?놁쓬"})

    return _response(200, {"user": user})


def _handle_logout(event: dict) -> dict:
    """POST /auth/logout ??罹먯떆 ??젣"""
    headers = event.get("headers", {}) or {}
    auth_header = headers.get("Authorization", headers.get("authorization", ""))

    if auth_header.startswith("Bearer "):
        id_token = auth_header[7:]
        invalidate_token_cache(id_token)

    body = json.loads(event.get("body") or "{}")
    uid = body.get("uid", "")
    if uid:
        invalidate_user_cache(uid)

    return _response(200, {"message": "濡쒓렇?꾩썐 ?꾨즺"})


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }

